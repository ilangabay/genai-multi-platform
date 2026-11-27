import pytest
from fastapi.testclient import TestClient

from app import litellm_mock, main, pricing, store


def login(username, password, role="user"):
    c = TestClient(main.app)
    assert c.post("/api/login", json={"role": role, "username": username, "password": password}).status_code == 200
    return c


def admin():
    return login("admin", "admin", role="admin")


def team(pid):
    return litellm_mock.TEAMS[main.PROJECTS[pid]["team_id"]]  # the live record, so tests can change budgets


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
    # no model until an admin whitelists one: LiteLLM gets a non-empty list, since an empty one would mean every model
    assert p["models"] == [] and team("search-poc")["models"] == [litellm_mock.NO_MODELS]
    r = call(a, p["api_key"])
    assert r.status_code == 401 and r.json()["error"]["type"] == "team_model_access_denied"
    a.put("/api/projects/search-poc/models", json={"models": ["claude-sonnet-5-5"]})
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
        "d": row("EUW3-MP:EUW3_input_tokens_global_standard-Units", "1.0"),     # global: under "(global)", no output: dropped
        "e": row("EUW3-MP:EUW3_input_tokens_long_ctx_standard-Units", "9.0"),   # long context, ignored
        "f": row("EUW3-MP:EUW3_output_tokens_long_ctx_standard-Units", "99"),   # long context, ignored
        "g": row("EUW3-MP:EUW3_cache_write_tokens_1h_standard-Units", "9.9"),   # 1-hour cache, ignored
    }
    for r in rows.values():  # AWS tags long-context and 1h rows like the standard ones: they must not override them
        r["attributes"]["inferenceType"] = "Output tokens" if "output" in r["attributes"]["usagetype"] else "Input tokens"
    offer = {"products": {k: {"attributes": v["attributes"]} for k, v in rows.items()},
             "terms": {"OnDemand": {k: v["terms"] for k, v in rows.items()}}}
    prices = pricing.parse(offer)
    assert prices == {"Claude Sonnet 5.5": {"input": 3.0, "output": 15.0, "cache_read": 0.3}}



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
    assert main.PROJECTS["bob-lab"]["owners"] == ["bob"] and team("bob-lab")["max_budget"] == main.DEFAULT_PROJECT_BUDGET
    assert a.post(f"/api/requests/{model['id']}/reject", json={"note": "not on Bedrock EU"}).json()["status"] == "rejected"


def test_feedback():
    carol = login("carol", "user")
    assert carol.post("/api/feedback", json={"message": "  "}).status_code == 422
    assert carol.post("/api/feedback", json={"message": "Great"}).status_code == 201
    assert [f["message"] for f in carol.get("/api/feedback").json()] == ["Great"]
    assert login("alice", "user").get("/api/feedback").json() == []
    assert "Great" in [f["message"] for f in admin().get("/api/feedback").json()]


def test_usage_is_scoped_and_matches_spend():
    rows = admin().get("/api/usage?days=90").json()["rows"]
    seeded = sum(r["spend"] for r in rows if r["project"] == "hr-chatbot")
    assert abs(seeded - team("hr-chatbot")["spend"]) < 1e-6  # history adds up to the team spend
    assert all(r["spend"] == 0 for r in rows if r["model"].endswith("-onprem"))
    assert any(r["tokens"] for r in rows if r["model"].endswith("-onprem"))

    carol = login("carol", "user")
    mine = {p["id"] for p in carol.get("/api/projects").json()}
    assert {r["project"] for r in carol.get("/api/usage?days=90").json()["rows"]} <= mine and "code-assistant" not in mine
    data = carol.get("/api/usage?days=7").json()
    assert len(data["days"]) == 7 and all(r["date"] >= data["days"][0] for r in data["rows"])
    assert carol.get("/api/usage?days=0").status_code == 422


def test_calls_show_up_in_usage_today():
    a = admin()
    def today_haiku():
        return [r for r in a.get("/api/usage?days=1").json()["rows"]
                if r["project"] == "contract-review" and r["model"] == "claude-haiku-5-5"]
    before = sum(r["spend"] for r in today_haiku())
    cost = float(call(a, main.PROJECTS["contract-review"]["api_key"], "claude-haiku-5-5").headers["x-litellm-response-cost"])
    assert abs(sum(r["spend"] for r in today_haiku()) - before - cost) < 1e-9


def test_admin_sets_project_models():
    a, key = admin(), main.PROJECTS["contract-review"]["api_key"]
    assert login("bob", "user").put("/api/projects/contract-review/models", json={"models": ["claude-haiku-5-5"]}).status_code == 403
    assert a.put("/api/projects/contract-review/models", json={"models": []}).json()["models"] == []
    assert call(a, key, "claude-haiku-5-5").status_code == 401  # empty whitelist: nothing, not everything
    assert a.put("/api/projects/contract-review/models", json={"models": ["gpt-x"]}).status_code == 422
    try:
        p = a.put("/api/projects/contract-review/models", json={"models": ["llama-70b-onprem", "claude-haiku-5-5"]}).json()
        assert p["models"] == ["claude-haiku-5-5", "llama-70b-onprem"]  # catalog order
        assert call(a, key, "claude-haiku-5-5").status_code == 200
        r = call(a, key, "claude-opus-5-5")
        assert r.status_code == 401 and r.json()["error"]["type"] == "team_model_access_denied"
        listed = {x["id"]: x for x in login("bob", "user").get("/api/projects").json()}["contract-review"]
        assert listed["models"] == p["models"]
    finally:
        a.put("/api/projects/contract-review/models", json={"models": list(litellm_mock.MODELS)})


def test_user_management():
    a = admin()
    assert login("bob", "user").post("/api/users", json={"username": "dave"}).status_code == 403
    assert a.post("/api/users", json={"username": "A b"}).status_code == 422
    assert a.post("/api/users", json={"username": "bob"}).status_code == 409
    created = a.post("/api/users", json={"username": "dave"}).json()
    assert created["role"] == "user" and created["must_change"] and len(created["password"]) >= 16
    assert "password" not in str(a.get("/api/users").json())  # hashes and passwords never listed

    # first sign-in: only the password change works
    dave = login("dave", created["password"])
    assert dave.get("/api/projects").status_code == 403
    assert dave.post("/api/me/password", json={"current": created["password"], "new": "short"}).status_code == 422
    assert dave.post("/api/me/password", json={"current": "wrong", "new": "a-long-new-password"}).status_code == 403
    assert dave.post("/api/me/password", json={"current": created["password"], "new": "a-long-new-password"}).status_code == 200
    assert dave.get("/api/projects").json() == []

    # promote: dave can now sign in as admin; demote: his admin session ends at once
    c = TestClient(main.app)
    assert c.post("/api/login", json={"role": "admin", "username": "dave", "password": "a-long-new-password"}).status_code == 401
    assert a.patch("/api/users/dave", json={"role": "admin"}).json()["role"] == "admin"
    dave_admin = login("dave", "a-long-new-password", role="admin")
    assert dave_admin.get("/api/users").status_code == 200
    assert a.patch("/api/users/dave", json={"role": "user"}).status_code == 200
    assert dave_admin.get("/api/users").status_code == 401

    # reset: new temporary password, old one and old sessions stop working
    reset = a.post("/api/users/dave/reset-password").json()
    assert dave.get("/api/me").status_code == 401
    assert c.post("/api/login", json={"role": "user", "username": "dave", "password": "a-long-new-password"}).status_code == 401
    assert login("dave", reset["password"]).get("/api/me").json()["must_change_password"]


def test_last_admin_cannot_be_demoted():
    a = admin()
    assert a.patch("/api/users/admin", json={"role": "user"}).status_code == 409
    assert a.patch("/api/users/admin", json={"role": "owner"}).status_code == 422


def test_storage_config_fails_fast(monkeypatch):
    from app import store
    monkeypatch.delenv("DATABASE_URL", raising=False)
    for storage in ("postgres", "sqlite"):
        try:
            store.make_store(storage)
            raise AssertionError(f"{storage} should have been refused")
        except RuntimeError as e:
            assert "DATABASE_URL" in str(e) or "memory" in str(e)
    assert not store.make_store("memory").persistent


class FlakyDB(store.MemoryStore):
    """Pretends to be a database whose writes fail."""
    persistent = True

    def save(self, table, key, doc):
        raise OSError("database unreachable")


def test_failed_write_leaves_memory_unchanged(monkeypatch):
    a = admin()
    monkeypatch.setattr(store, "STORE", FlakyDB())
    try:
        a.patch("/api/users/bob", json={"role": "admin"})
    except OSError:
        pass
    assert main.USERS["bob"]["role"] == "user"
    spend = team("hr-chatbot")["spend"]
    try:
        call(TestClient(main.app), main.PROJECTS["hr-chatbot"]["api_key"])
    except OSError:
        pass
    assert team("hr-chatbot")["spend"] == spend


def test_first_start_with_database_refuses_default_admin_password(monkeypatch):
    monkeypatch.setattr(store, "make_store", lambda: type("EmptyDB", (store.MemoryStore,), {"persistent": True})())
    for weak in ("admin", "change-me-at-first-sign-in", "short"):
        monkeypatch.setenv("ADMIN_PASSWORD", weak)
        try:
            main.startup()
            raise AssertionError(f"{weak!r} should have been refused")
        except RuntimeError as e:
            assert "ADMIN_PASSWORD" in str(e)
    monkeypatch.setenv("ADMIN_PASSWORD", "a-long-first-password")
    main.startup()
    assert list(main.USERS) == ["admin"] and main.USERS["admin"]["must_change"] and not main.PROJECTS


def test_admin_cannot_reset_own_password():
    a = admin()
    assert a.post("/api/users/admin/reset-password").status_code == 409
    assert a.get("/api/me").status_code == 200  # still signed in


def test_sessions_expire(monkeypatch):
    c = login("bob", "user")
    assert c.get("/api/me").status_code == 200
    monkeypatch.setattr(main, "SESSION_HOURS", 0)
    assert c.get("/api/me").status_code == 401
    assert not [s for s in main.SESSIONS.values() if s["username"] == "bob"]  # dropped, not just refused


def test_feedback_ids_continue_after_loaded_ones(monkeypatch):
    loaded = {"7": {"id": 7, "by": "carol", "created": "", "message": "old"}}
    monkeypatch.setattr(store, "make_store", lambda: type("DB", (store.MemoryStore,), {
        "load": lambda self, table: dict(loaded) if table == "feedback" else {}})())
    main.startup()
    carol = login("carol", "user")
    assert [carol.post("/api/feedback", json={"message": "new"}).json()["id"] for _ in range(3)] == [8, 9, 10]


def test_project_members():
    alice, bob = login("alice", "user"), login("bob", "user")
    url = "/api/projects/code-assistant/members/"
    assert bob.put(url + "carol", json={"role": "member"}).status_code == 403  # bob is a member, not an owner
    p = alice.put(url + "carol", json={"role": "member"}).json()
    assert p["owners"] == ["alice"] and p["members"] == ["bob", "carol"]
    assert alice.put(url + "bob", json={"role": "owner"}).json()["owners"] == ["alice", "bob"]
    assert bob.delete(url + "carol").json()["members"] == []  # bob is an owner now
    assert "code-assistant" not in [p["id"] for p in login("carol", "user").get("/api/projects").json()]
    assert alice.put(url + "mallory", json={"role": "member"}).status_code == 404
    assert alice.put(url + "carol", json={"role": "admin"}).status_code == 422
    assert alice.delete(url + "alice").status_code == 200
    assert bob.delete(url + "bob").status_code == 409  # last owner
    assert bob.put(url + "bob", json={"role": "member"}).status_code == 409
    assert admin().put(url + "carol", json={"role": "owner"}).status_code == 200  # admins manage every project


def test_real_litellm_daily_usage_is_read_per_team_and_model(monkeypatch):
    from app import litellm_proxy
    day = lambda d, models: {"date": d, "metrics": {}, "breakdown": {"model_groups": {
        m: {"metrics": {"spend": s, "total_tokens": t}} for m, (s, t) in models.items()}}}
    pages = {("t1", 1): {"results": [day("2026-11-20", {"claude-haiku-5-5": (0.5, 100)})], "metadata": {"has_more": True}},
             ("t1", 2): {"results": [day("2026-11-21", {"claude-opus-5-5": (2.0, 50)})], "metadata": {"has_more": False}},
             ("t2", 1): {"results": [], "metadata": {}}}
    monkeypatch.setattr(litellm_proxy, "_call", lambda method, path, body=None, **q: pages[(q["team_ids"], q["page"])])
    assert litellm_proxy.spend_logs("2026-11-01", ["t1", "t2"]) == [
        {"startTime": "2026-11-20", "team_id": "t1", "model_group": "claude-haiku-5-5", "spend": 0.5, "total_tokens": 100},
        {"startTime": "2026-11-21", "team_id": "t1", "model_group": "claude-opus-5-5", "spend": 2.0, "total_tokens": 50}]


def test_real_litellm_needs_postgres(monkeypatch):
    from app import litellm_proxy
    monkeypatch.setattr(main, "litellm", litellm_proxy)
    with pytest.raises(RuntimeError, match="STORAGE=postgres"):
        main.startup()


def test_projects_pick_models_from_the_catalog():
    a = admin()
    p = a.post("/api/projects", json={"name": "Picked", "owner": "bob", "models": ["claude-haiku-5-5", "llama-70b-onprem"]}).json()
    assert p["models"] == ["claude-haiku-5-5", "llama-70b-onprem"]
    assert a.post("/api/projects", json={"name": "Empty", "owner": "bob"}).json()["models"] == []  # default: none
    assert a.post("/api/projects", json={"name": "Bad", "owner": "bob", "models": ["gpt-x"]}).status_code == 422
    assert "bad" not in main.PROJECTS  # nothing half-created

    bob = login("bob", "user")
    rid = bob.post("/api/requests", json={"kind": "project", "name": "Bob tools", "models": ["claude-sonnet-5-5"]}).json()["id"]
    a.post(f"/api/requests/{rid}/approve", json={})
    assert team("bob-tools")["models"] == ["claude-sonnet-5-5"]

    # a catalog model asked for a project is enabled when approved; an unknown one is only recorded
    assert bob.post("/api/requests", json={"kind": "model", "name": "claude-sonnet-5-5", "project": "bob-tools"}).status_code == 409
    rid = bob.post("/api/requests", json={"kind": "model", "name": "claude-opus-5-5", "project": "bob-tools"}).json()["id"]
    other = bob.post("/api/requests", json={"kind": "model", "name": "Some new model", "project": "bob-tools"}).json()["id"]
    a.post(f"/api/requests/{rid}/approve", json={})
    assert a.post(f"/api/requests/{other}/approve", json={}).status_code == 200
    assert team("bob-tools")["models"] == ["claude-opus-5-5", "claude-sonnet-5-5"]


AWS_PRICES = {"Claude Sonnet 5.5": {"input": 2.2, "output": 11.0, "cache_read": 0.11, "cache_write": 2.75},
              "Claude Sonnet 5.5 (global)": {"input": 2.0, "output": 10.0}, "Nova Pro": {"input": 1.18, "output": 4.72}}


def test_admin_adds_bedrock_and_onprem_models_to_the_catalog(monkeypatch):
    monkeypatch.setattr(pricing, "fetch", lambda region: AWS_PRICES)
    a = admin()
    assert login("bob", "user").get("/api/catalog/lookup?model=anthropic.claude-sonnet-5-5").status_code == 403
    found = a.get("/api/catalog/lookup?model=arn:aws:bedrock:eu-west-3::foundation-model/anthropic.claude-sonnet-5-5").json()
    assert found["aws_model"] == "Claude Sonnet 5.5" and found["name"] == "claude-sonnet-5-5"
    assert "Claude Sonnet 5.5 (global)" not in found["prices"]  # in-region: regional rates only
    assert a.get("/api/catalog/lookup?model=global.anthropic.claude-sonnet-5-5").json()["aws_model"] == "Claude Sonnet 5.5 (global)"
    assert a.get("/api/catalog/lookup?model=meta.llama3-2-1b-instruct-v1:0").json()["aws_model"] is None
    assert a.get("/api/catalog/lookup?model=curl http://x").status_code == 422

    assert a.post("/api/catalog/test", json={"hosting": "bedrock", "model": "eu.amazon.nova-pro-v1:0"}).json()["ok"]
    assert a.post("/api/catalog/test", json={"hosting": "onprem", "model": "x", "api_base": "ftp://x"}).status_code == 422

    # Bedrock: prices from AWS, nothing typed by the admin
    nova = {"hosting": "bedrock", "name": "nova-pro", "model": "eu.amazon.nova-pro-v1:0"}
    assert a.post("/api/catalog", json={**nova, "name": "claude-opus-5-5"}).status_code == 409
    assert a.post("/api/catalog", json={**nova, "name": "llama", "model": "meta.llama3-2-1b-instruct-v1:0"}).status_code == 422
    listed = {m["id"]: m for m in a.post("/api/catalog", json=nova).json()}
    assert listed["nova-pro"]["added"] and listed["nova-pro"]["input"] == 1.18 and listed["nova-pro"]["aws_model"] == "Nova Pro"
    params = litellm_mock.MODELS["nova-pro"]["litellm_params"]
    assert params["model"] == "bedrock/converse/eu.amazon.nova-pro-v1:0" and params["aws_region_name"] == "eu-west-3"

    # On-prem: free, with its own endpoint
    listed = {m["id"]: m for m in a.post("/api/catalog", json={"hosting": "onprem", "name": "qwen-onprem", "model": "qwen3-32b",
                                                               "api_base": "http://gpu01.internal:8000/v1/"}).json()}
    assert listed["qwen-onprem"]["platform"] == "On-prem" and listed["qwen-onprem"]["input"] == 0
    assert litellm_mock.MODELS["qwen-onprem"]["litellm_params"] == {"model": "hosted_vllm/qwen3-32b", "api_base": "http://gpu01.internal:8000/v1",
                                                                     "input_cost_per_token": 0.0, "output_cost_per_token": 0.0}

    # projects can whitelist them, and calls are billed with the AWS prices
    key = main.PROJECTS["contract-review"]["api_key"]
    a.put("/api/projects/contract-review/models", json={"models": ["nova-pro", "qwen-onprem"]})
    assert float(call(a, key, "nova-pro").headers["x-litellm-response-cost"]) > 0
    assert float(call(a, key, "qwen-onprem").headers["x-litellm-response-cost"]) == 0
    behind_litellm = {"hosting": "onprem", "server": "litellm", "name": "llama-onprem", "model": "llama-3.3-70b", "api_key": "sk-onprem"}
    a.post("/api/catalog", json=behind_litellm)
    assert litellm_mock.MODELS["llama-onprem"]["litellm_params"]["model"] == "litellm_proxy/llama-3.3-70b"
    assert a.post("/api/catalog/test", json={**behind_litellm, "server": "tgi"}).status_code == 422

    assert a.delete("/api/catalog/claude-opus-5-5").status_code == 409  # from litellm/config.yaml
    assert "nova-pro" not in {m["id"] for m in a.delete("/api/catalog/nova-pro").json()}
    assert team("contract-review")["models"] == ["qwen-onprem"]  # taken off the project too
    a.delete("/api/catalog/llama-onprem")
    main.startup()  # memory mode keeps nothing: the catalog is back to the config models, with no leftover
    assert "qwen-onprem" not in litellm_mock.MODELS and len(litellm_mock.MODEL_LIST) == 5


def test_price_files_are_matched_to_model_ids():
    names = ["Claude Sonnet 5.5", "Claude Sonnet 5.5 (global)", "Claude 3.5 Sonnet v2", "Claude Haiku 4.5", "Nova Pro", "Mistral Large"]
    assert pricing.match("eu.anthropic.claude-sonnet-5-5", names) == "Claude Sonnet 5.5"
    assert pricing.match("global.anthropic.claude-sonnet-5-5", names) == "Claude Sonnet 5.5 (global)"
    assert pricing.match("anthropic.claude-3-5-sonnet-20241022-v2:0", names) == "Claude 3.5 Sonnet v2"
    assert pricing.match("arn:aws:bedrock:eu-west-3:1:inference-profile/eu.anthropic.claude-haiku-4-5-20251001-v1:0", names) == "Claude Haiku 4.5"
    assert pricing.match("amazon.nova-pro-v1:0", names) == "Nova Pro"
    assert pricing.match("mistral.mistral-large-2402-v1:0", names) == "Mistral Large"
    assert pricing.match("meta.llama3-2-1b-instruct-v1:0", names) is None


def test_prices_follow_the_aws_price_list(monkeypatch):
    aws = {"Claude Sonnet 5.5": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75},
           "Claude Opus 5.5": {"input": 4.4, "output": 22.0, "cache_read": 0.22, "cache_write": 5.5},
           "Claude Haiku 5.5": {"input": 0.11, "output": 0.55, "cache_read": 0.011, "cache_write": 0.1375}}
    monkeypatch.setattr(pricing, "fetch", lambda region, max_age=0: aws)
    key, a = main.PROJECTS["contract-review"]["api_key"], admin()
    before = float(call(a, key, "claude-sonnet-5-5").headers["x-litellm-response-cost"])
    assert login("bob", "user").post("/api/catalog/sync").status_code == 403
    status = a.post("/api/catalog/sync").json()
    assert status["updated"] == ["claude-sonnet-5-5"] and status["at"] and not status["errors"]  # Opus and Haiku unchanged
    info = litellm_mock.MODELS["claude-sonnet-5-5"]["model_info"]
    assert abs(info["input_cost_per_token"] - 3e-6) < 1e-15 and abs(info["cache_creation_input_token_cost"] - 3.75e-6) < 1e-15
    after = float(call(a, key, "claude-sonnet-5-5").headers["x-litellm-response-cost"])
    assert after > before  # the next call is billed at the new rate
    assert a.post("/api/catalog/sync").json()["updated"] == []  # nothing changed since

    del aws["Claude Haiku 5.5"]  # gone from the AWS list: old prices kept, the admin sees why
    status = a.get("/api/catalog/sync").json() | a.post("/api/catalog/sync").json()
    assert status["errors"] and "claude-haiku-5-5" in status["errors"][0]
    assert litellm_mock.MODELS["claude-haiku-5-5"]["model_info"]["input_cost_per_token"] == 0.11e-6
