from fastapi.testclient import TestClient

from app import litellm_mock, main, pricing


def login(username, password, role="user"):
    c = TestClient(main.app)
    assert c.post("/api/login", json={"role": role, "username": username, "password": password}).status_code == 200
    return c


def admin():
    return login("", "admin", role="admin")


def team(pid):
    return litellm_mock.team_info(main.PROJECTS[pid]["team_id"])


def call(c, key, model="claude-sonnet-5-5"):
    return c.post("/v1/chat/completions", headers={"Authorization": f"Bearer {key}"},
                  json={"model": model, "messages": [{"role": "user", "content": "hi"}]})


def test_user_sees_only_own_projects():
    ids = {p["id"] for p in login("carol", "user").get("/api/projects").json()}
    assert ids == {"contract-review", "hr-chatbot"}
    assert len(admin().get("/api/projects").json()) == len(main.PROJECTS)


def test_bad_login_and_unknown_user():
    c = TestClient(main.app)
    assert c.post("/api/login", json={"role": "user", "username": "alice", "password": "nope"}).status_code == 401
    assert c.post("/api/login", json={"role": "user", "username": "mallory", "password": "user"}).status_code == 401
    # the user password must not open the admin role, nor the admin password the user role
    assert c.post("/api/login", json={"role": "admin", "password": "user"}).status_code == 401
    assert c.post("/api/login", json={"role": "user", "username": "alice", "password": "admin"}).status_code == 401


def test_user_cannot_manage():
    c = login("alice", "user")
    assert c.patch("/api/projects/demo-sandbox/budget", json={"budget": 999}).status_code == 403
    assert c.post("/api/projects/demo-sandbox/key/revoke").status_code == 403
    assert c.post("/api/projects", json={"name": "x", "owner": "alice", "budget": 1}).status_code == 403


def test_spend_comes_from_litellm_and_blocks_when_exhausted():
    p = main.PROJECTS["demo-sandbox"]
    t = team("demo-sandbox")
    t["max_budget"], t["spend"] = 0.02, 0.0
    c = TestClient(main.app)
    costs = []
    while (r := call(c, p["api_key"])).status_code == 200:
        costs.append(float(r.headers["x-litellm-response-cost"]))
    assert costs and abs(t["spend"] - sum(costs)) < 1e-9
    assert r.status_code == 400 and r.json()["error"]["type"] == "budget_exceeded"
    assert t["spend"] < t["max_budget"] + max(costs)  # at most one call of overshoot, like LiteLLM

    listed = {x["id"]: x for x in login("alice", "user").get("/api/projects").json()}["demo-sandbox"]
    assert listed["spent"] == t["spend"] and listed["budget"] == 0.02

    assert admin().patch("/api/projects/demo-sandbox/budget", json={"budget": 1}).status_code == 200
    assert t["max_budget"] == 1 and call(c, p["api_key"]).status_code == 200


def test_cost_uses_cache_prices():
    info = litellm_mock.MODELS["claude-sonnet-5-5"]["model_info"]
    u = {"input": 100, "output": 10, "cache_read": 1000, "cache_write": 200}
    expected = (100 * 2.2 + 10 * 11 + 1000 * 0.11 + 200 * 2.75) / 1e6
    assert abs(litellm_mock._cost(info, u) - expected) < 1e-12


def test_revoke_and_regenerate_key_keeps_spend():
    a = admin()
    old = main.PROJECTS["hr-chatbot"]["api_key"]
    spend = team("hr-chatbot")["spend"]
    a.post("/api/projects/hr-chatbot/key/revoke")
    assert call(a, old).status_code == 401
    new = a.post("/api/projects/hr-chatbot/key/regenerate").json()["api_key"]
    assert new != old and call(a, new).status_code == 200
    assert team("hr-chatbot")["spend"] > spend


def test_unknown_model():
    r = call(TestClient(main.app), main.PROJECTS["code-assistant"]["api_key"], "gpt-x")
    assert r.status_code == 400 and r.json()["error"]["type"] == "invalid_request_error"


def test_admin_creates_project():
    a = admin()
    r = a.post("/api/projects", json={"name": "Search POC", "owner": "bob", "members": ["carol", "bob"], "budget": 5})
    assert r.status_code == 201
    p = r.json()
    assert p["id"] == "search-poc" and p["members"] == ["carol"] and p["api_key"] and p["budget"] == 5
    assert a.post("/api/projects", json={"name": "search poc", "owner": "bob", "budget": 5}).status_code == 409
    assert a.post("/api/projects", json={"name": "Other", "owner": "mallory", "budget": 5}).status_code == 422
    assert "search-poc" in {p["id"] for p in login("carol", "user").get("/api/projects").json()}
    assert call(a, p["api_key"]).status_code == 200


def test_responses_api_charges_same_team():
    p = main.PROJECTS["contract-review"]
    before = team("contract-review")["spend"]
    r = TestClient(main.app).post("/v1/responses", headers={"Authorization": f"Bearer {p['api_key']}"},
                                  json={"model": "claude-haiku-5-5", "input": "hi"})
    assert r.status_code == 200
    assert r.json()["output"][0]["content"][0]["type"] == "output_text"
    assert team("contract-review")["spend"] > before


def test_onprem_is_free_but_callable():
    t = team("code-assistant")
    spend = t["spend"]
    r = call(TestClient(main.app), main.PROJECTS["code-assistant"]["api_key"], "llama-70b-onprem")
    assert r.status_code == 200 and float(r.headers["x-litellm-response-cost"]) == 0
    assert t["spend"] == spend


def test_price_file_parsing_and_sync():
    def row(usagetype, usd, unit="1M tokens", feature="On-demand Inference"):
        return {"attributes": {"model": "Claude Sonnet 5.5", "usagetype": usagetype, "feature": feature},
                "terms": {"T": {"priceDimensions": {"D": {"unit": unit, "pricePerUnit": {"USD": usd}}}}}}
    rows = {
        "a": row("EUW3-MP:EUW3_input_tokens_standard-Units", "3.0"),
        "b": row("EUW3-MP:EUW3_output_tokens_standard-Units", "0.015", unit="1K tokens"),
        "c": row("EUW3-MP:EUW3_cache_read_tokens_standard-Units", "0.3"),
        "d": row("EUW3-MP:EUW3_input_tokens_global_standard-Units", "1.0"),     # cross-region, ignored
        "e": row("EUW3-MP:EUW3_input_tokens_long_ctx_standard-Units", "9.0"),   # long context, ignored
    }
    offer = {"products": {k: {"attributes": v["attributes"]} for k, v in rows.items()},
             "terms": {"OnDemand": {k: v["terms"] for k, v in rows.items()}}}
    prices = pricing.parse(offer)
    assert prices == {"Claude Sonnet 5.5": {"input": 3.0, "output": 15.0, "cache_read": 0.3}}

    info = litellm_mock.MODELS["claude-sonnet-5-5"]["model_info"]
    before = dict(info)
    try:
        assert litellm_mock.sync_prices(prices) == ["claude-sonnet-5-5"]
        assert abs(info["input_cost_per_token"] - 3e-6) < 1e-15 and abs(info["output_cost_per_token"] - 15e-6) < 1e-15
        assert info["cache_creation_input_token_cost"] == before["cache_creation_input_token_cost"]  # not in file: kept
    finally:
        info.clear(); info.update(before)


def test_requests_flow():
    bob, a = login("bob", "user"), admin()
    assert a.post("/api/requests", json={"kind": "model", "name": "x"}).status_code == 403  # admins decide, users ask
    assert bob.post("/api/requests", json={"kind": "budget", "project": "hr-chatbot", "amount": 5}).status_code == 404  # not his
    assert bob.post("/api/requests", json={"kind": "budget", "project": "contract-review", "amount": 0}).status_code == 422
    budget = bob.post("/api/requests", json={"kind": "budget", "project": "contract-review", "amount": 5, "reason": "demo"}).json()
    proj = bob.post("/api/requests", json={"kind": "project", "name": "Bob lab"}).json()
    model = bob.post("/api/requests", json={"kind": "model", "name": "gpt-oss-120b"}).json()
    assert proj["amount"] == main.DEFAULT_PROJECT_BUDGET

    assert {r["id"] for r in login("carol", "user").get("/api/requests").json()}.isdisjoint({budget["id"], proj["id"]})
    assert {budget["id"], proj["id"], model["id"]} <= {r["id"] for r in a.get("/api/requests").json()}
    assert bob.post(f"/api/requests/{budget['id']}/approve").status_code == 403

    before = team("contract-review")["max_budget"]
    assert a.post(f"/api/requests/{budget['id']}/approve", json={"note": "ok"}).json()["status"] == "approved"
    assert team("contract-review")["max_budget"] == before + 5
    assert a.post(f"/api/requests/{budget['id']}/approve").status_code == 409  # applied once only

    assert a.post(f"/api/requests/{proj['id']}/approve").status_code == 200
    assert main.PROJECTS["bob-lab"]["owner"] == "bob" and team("bob-lab")["max_budget"] == main.DEFAULT_PROJECT_BUDGET
    assert a.post(f"/api/requests/{model['id']}/reject", json={"note": "not on Bedrock EU"}).json()["status"] == "rejected"


def test_feedback():
    carol = login("carol", "user")
    assert carol.post("/api/feedback", json={"message": "  "}).status_code == 422
    assert carol.post("/api/feedback", json={"message": "Great"}).status_code == 201
    assert [f["message"] for f in carol.get("/api/feedback").json()] == ["Great"]
    assert login("alice", "user").get("/api/feedback").json() == []
    assert "Great" in [f["message"] for f in admin().get("/api/feedback").json()]
