"""Governance layer of the gateway VM (mock).

Owns projects (name, owner, members) and the web UI. Money and keys live in LiteLLM:
each project is a LiteLLM team with a max_budget, and its API key is a LiteLLM virtual key
of that team. Spend is read back from LiteLLM, never computed here.
In production, apps call the guardrail LiteLLM endpoint, which forwards to the LiteLLM router
serving Bedrock and on-prem models; app/litellm_mock.py stands in for both.
"""
import hmac
import os
import re
import secrets
from pathlib import Path

from fastapi import Body, Cookie, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import litellm_mock as litellm  # production: HTTP client for the LiteLLM admin API (master key)

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")
USER_PASSWORD = os.environ.get("USER_PASSWORD", "user")
LITELLM_BASE_URL = os.environ.get("LITELLM_BASE_URL")  # guardrail LiteLLM endpoint shown to users; defaults to this mock
STATIC = Path(__file__).parent / "static"

# shortcut: in-memory mock store, reset on restart; move to PostgreSQL for real use.
USERS = {"alice", "bob", "carol"}
PROJECTS: dict[str, dict] = {}  # id -> {id, name, owner, members, team_id, api_key}


def add_project(name: str, owner: str, members: list[str], budget: float) -> dict:
    pid = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    team = litellm.team_new(team_alias=pid, max_budget=budget)
    PROJECTS[pid] = {"id": pid, "name": name, "owner": owner, "members": sorted(set(members) - {owner}),
                     "team_id": team["team_id"], "api_key": litellm.key_generate(team["team_id"])["key"]}
    return PROJECTS[pid]


for _name, _owner, _members, _budget, _spend in [
    ("Code assistant", "alice", ["bob"], 50.0, 12.4),
    ("Demo sandbox", "alice", [], 0.02, 0.0),
    ("Contract review", "bob", ["alice", "carol"], 20.0, 3.1),
    ("HR chatbot", "carol", [], 100.0, 41.75),
]:
    litellm.TEAMS[add_project(_name, _owner, _members, _budget)["team_id"]]["spend"] = _spend  # mock seed only

SESSIONS: dict[str, dict] = {}

app = FastAPI(title="GenAI gateway - governance (mock)")
app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.include_router(litellm.router)


# ---------- auth ----------

def current_user(session: str | None = Cookie(None)) -> dict:
    user = SESSIONS.get(session or "")
    if not user:
        raise HTTPException(401, "Not signed in")
    return user


def admin_only(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(403, "Admin only")
    return user


@app.post("/api/login")
def login(response: Response, role: str = Body(...), password: str = Body(...), username: str = Body("")):
    username = "admin" if role == "admin" else username.strip().lower()
    ok = {"admin": ADMIN_PASSWORD, "user": USER_PASSWORD}.get(role)
    if not ok or not hmac.compare_digest(password, ok) or (role == "user" and username not in USERS):
        raise HTTPException(401, "Invalid credentials")
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = {"username": username, "role": role}
    response.set_cookie("session", token, httponly=True, samesite="strict")
    return SESSIONS[token]


@app.post("/api/logout")
def logout(response: Response, session: str | None = Cookie(None)):
    SESSIONS.pop(session or "", None)
    response.delete_cookie("session")
    return {"ok": True}


@app.get("/api/me")
def me(user: dict = Depends(current_user)):
    return user


# ---------- projects ----------

def visible(p: dict, user: dict) -> bool:
    return user["role"] == "admin" or user["username"] == p["owner"] or user["username"] in p["members"]


def get_project(pid: str) -> dict:
    if pid not in PROJECTS:
        raise HTTPException(404, "Project not found")
    return PROJECTS[pid]


def with_budget(p: dict) -> dict:
    team = litellm.team_info(p["team_id"])
    return {**p, "budget": team["max_budget"], "spent": team["spend"]}


def valid_budget(budget: float):
    if not budget >= 0:  # also rejects NaN
        raise HTTPException(422, "Budget must be a positive number")


@app.get("/api/projects")
def list_projects(request: Request, user: dict = Depends(current_user)):
    endpoint = LITELLM_BASE_URL or str(request.base_url).rstrip("/") + "/v1"
    return [{**with_budget(p), "endpoint": endpoint} for p in PROJECTS.values() if visible(p, user)]


@app.get("/api/models")
def list_models(user: dict = Depends(current_user)):
    # USD per 1M tokens for display, from LiteLLM's model config (the prices it bills with)
    return [{"id": m["model_name"], "platform": i["platform"], "input": i["input_cost_per_token"] * 1e6,
             "output": i["output_cost_per_token"] * 1e6,
             "cached": i["cache_read_input_token_cost"] * 1e6 if "cache_read_input_token_cost" in i else None}
            for m in litellm.model_info() for i in [m["model_info"]]]


@app.get("/api/users")
def list_users(user: dict = Depends(admin_only)):
    return sorted(USERS)


@app.post("/api/projects", status_code=201)
def create_project(name: str = Body(...), owner: str = Body(...), members: list[str] = Body([]),
                   budget: float = Body(...), user: dict = Depends(admin_only)):
    name = name.strip()
    pid = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    if not pid:
        raise HTTPException(422, "Project name is required")
    if pid in PROJECTS:
        raise HTTPException(409, "A project with this name already exists")
    if owner not in USERS or not set(members) <= USERS:
        raise HTTPException(422, "Unknown owner or member")
    valid_budget(budget)
    return with_budget(add_project(name, owner, members, budget))


@app.patch("/api/projects/{pid}/budget")
def set_budget(pid: str, budget: float = Body(..., embed=True), user: dict = Depends(admin_only)):
    valid_budget(budget)
    p = get_project(pid)
    litellm.team_update(p["team_id"], max_budget=budget)
    return with_budget(p)


@app.post("/api/projects/{pid}/key/revoke")
def revoke_key(pid: str, user: dict = Depends(admin_only)):
    p = get_project(pid)
    if p["api_key"]:
        litellm.key_delete([p["api_key"]])
    p["api_key"] = None
    return with_budget(p)


@app.post("/api/projects/{pid}/key/regenerate")
def regenerate_key(pid: str, user: dict = Depends(admin_only)):
    p = get_project(pid)
    if p["api_key"]:
        litellm.key_delete([p["api_key"]])
    # the spend stays on the team, so a new key does not reset the budget
    p["api_key"] = litellm.key_generate(p["team_id"])["key"]
    return with_budget(p)


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
