from fastapi.testclient import TestClient

from app import main


def login(username, password):
    c = TestClient(main.app)
    assert c.post("/api/login", json={"username": username, "password": password}).status_code == 200
    return c


def call(c, key, model="claude-sonnet-5-5"):
    return c.post("/v1/chat/completions", headers={"Authorization": f"Bearer {key}"},
                  json={"model": model, "messages": [{"role": "user", "content": "hi"}]})


def test_user_sees_only_own_projects():
    ids = {p["id"] for p in login("carol", "user").get("/api/projects").json()}
    assert ids == {"contract-review", "hr-chatbot"}
    assert len(login("x", "admin").get("/api/projects").json()) == len(main.PROJECTS)


def test_bad_login_and_unknown_user():
    c = TestClient(main.app)
    assert c.post("/api/login", json={"username": "alice", "password": "nope"}).status_code == 401
    assert c.post("/api/login", json={"username": "mallory", "password": "user"}).status_code == 401


def test_user_cannot_manage():
    c = login("alice", "user")
    assert c.patch("/api/projects/demo-sandbox/budget", json={"budget": 999}).status_code == 403
    assert c.post("/api/projects/demo-sandbox/key/revoke").status_code == 403


def test_call_debits_budget_then_fails_when_exhausted():
    p = main.PROJECTS["demo-sandbox"]
    p["budget"], p["spent"] = 0.02, 0.0
    c = TestClient(main.app)
    ok = 0
    while (r := call(c, p["api_key"])).status_code == 200:
        ok += 1
        assert r.json()["x_budget_remaining_usd"] >= 0
    assert ok >= 1
    assert r.status_code == 402 and r.json()["error"]["code"] == "budget_exceeded"
    assert p["spent"] <= p["budget"]

    admin = login("admin", "admin")
    assert admin.patch("/api/projects/demo-sandbox/budget", json={"budget": 1}).status_code == 200
    assert call(c, p["api_key"]).status_code == 200


def test_revoke_and_regenerate_key():
    admin = login("admin", "admin")
    old = main.PROJECTS["hr-chatbot"]["api_key"]
    admin.post("/api/projects/hr-chatbot/key/revoke")
    assert call(admin, old).status_code == 401
    new = admin.post("/api/projects/hr-chatbot/key/regenerate").json()["api_key"]
    assert new != old and call(admin, new).status_code == 200


def test_unknown_model():
    assert call(TestClient(main.app), main.PROJECTS["code-assistant"]["api_key"], "gpt-x").status_code == 404
