"""STORAGE=postgres end to end: run the app, stop it, start it again, check nothing was lost.

Needs a PostgreSQL database: TEST_DATABASE_URL=postgresql://user:password@localhost:5432/postgres uv run pytest -q
Each run works in a throwaway schema that is dropped afterwards, so existing tables are never touched.
"""
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="set TEST_DATABASE_URL to run the PostgreSQL tests")
ROOT = Path(__file__).parent.parent


@pytest.fixture
def db_url():
    import psycopg

    schema = "gateway_test_" + uuid.uuid4().hex[:8]
    with psycopg.connect(URL, autocommit=True) as c:
        c.execute(f"CREATE SCHEMA {schema}")
    yield f"{URL}{'&' if '?' in URL else '?'}options=-csearch_path%3D{schema}"
    with psycopg.connect(URL, autocommit=True) as c:
        c.execute(f"DROP SCHEMA {schema} CASCADE")


FIRST_START = '''
from fastapi.testclient import TestClient
from app import main
main.startup()
assert set(main.USERS) == {"admin"} and not main.PROJECTS  # no demo data with a database
c = TestClient(main.app)
assert c.post("/api/login", json={"role": "admin", "username": "admin", "password": "first-password-long"}).json()["must_change_password"]
assert c.post("/api/me/password", json={"current": "first-password-long", "new": "a-long-admin-password"}).status_code == 200
assert c.post("/api/users", json={"username": "dave"}).status_code == 201
p = c.post("/api/projects", json={"name": "Persisted", "owner": "dave", "budget": 3}).json()
assert c.put("/api/projects/persisted/models", json={"models": ["claude-haiku-5-5"]}).status_code == 200
r = c.post("/v1/chat/completions", headers={"Authorization": "Bearer " + p["api_key"]},
           json={"model": "claude-haiku-5-5", "messages": [{"role": "user", "content": "hi"}]})
assert r.status_code == 200
assert c.post("/api/feedback", json={"message": "kept"}).status_code == 201
print(c.cookies.get("session"), p["api_key"])
'''

SECOND_START = '''
import sys
from fastapi.testclient import TestClient
from app import main
main.startup()
token, key = sys.argv[1:]
assert set(main.USERS) == {"admin", "dave"} and list(main.PROJECTS) == ["persisted"]
assert main.check_password("a-long-admin-password", main.USERS["admin"]["password"])
c = TestClient(main.app, cookies={"session": token})
assert c.get("/api/me").json()["username"] == "admin"  # the session survived the restart
p = c.get("/api/projects").json()[0]
assert p["budget"] == 3 and p["spent"] > 0 and p["models"] == ["claude-haiku-5-5"] and p["api_key"] == key
assert c.get("/api/usage?days=1").json()["rows"]
assert [f["message"] for f in c.get("/api/feedback").json()] == ["kept"]
r = c.post("/v1/chat/completions", headers={"Authorization": "Bearer " + key},
           json={"model": "claude-opus-5-5", "messages": []})
assert r.status_code == 401  # the model list survived too
'''


def start(code: str, url: str, *args: str) -> str:
    env = {**os.environ, "STORAGE": "postgres", "DATABASE_URL": url, "ADMIN_PASSWORD": "first-password-long"}
    r = subprocess.run([sys.executable, "-c", code, *args], env=env, cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def test_state_survives_restart(db_url):
    token, key = start(FIRST_START, db_url).split()
    start(SECOND_START, db_url, token, key)
