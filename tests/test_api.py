from fastapi.testclient import TestClient

from app import litellm_mock, main, pricing, store


def login(username, password, role="user"):
    c = TestClient(main.app)
    assert c.post("/api/login", json={"role": role, "username": username, "password": password}).status_code == 200
    return c


def admin():
    return login("admin", "admin", role="admin")


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
    assert a.put("/api/projects/contract-review/models", json={"models": []}).status_code == 422
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
