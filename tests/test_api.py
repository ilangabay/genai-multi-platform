from fastapi.testclient import TestClient

from app import litellm_mock, main


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
    expected = (100 * 2 + 10 * 10 + 1000 * 0.2 + 200 * 2.5) / 1e6
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
