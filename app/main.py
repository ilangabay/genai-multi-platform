"""Governance layer of the gateway VM (mock).

Owns projects (name, owner, members) and the web UI. Money and keys live in LiteLLM:
each project is a LiteLLM team with a max_budget, and its API key is a LiteLLM virtual key
of that team. Spend is read back from LiteLLM, never computed here.
In production, apps call the guardrail LiteLLM endpoint, which forwards to the LiteLLM router
serving Bedrock and on-prem models; app/litellm_mock.py stands in for both.
"""
import hmac
import itertools
import os
import re
import secrets
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Body, Cookie, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import litellm_mock as litellm  # production: HTTP client for the LiteLLM admin API (master key)

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")
USER_PASSWORD = os.environ.get("USER_PASSWORD", "user")
LITELLM_BASE_URL = os.environ.get("LITELLM_BASE_URL")  # guardrail LiteLLM endpoint shown to users; defaults to this mock
DEFAULT_PROJECT_BUDGET = float(os.environ.get("DEFAULT_PROJECT_BUDGET", "100"))  # USD, new projects and project requests
STATIC = Path(__file__).parent / "static"

# shortcut: in-memory mock store, reset on restart; move to PostgreSQL for real use.
USERS = {u.strip().lower() for u in os.environ.get("GATEWAY_USERS", "alice,bob,carol").split(",") if u.strip()}
PROJECTS: dict[str, dict] = {}  # id -> {id, name, owner, members, team_id, api_key}
REQUESTS: list[dict] = []  # project, budget and model requests from users
FEEDBACK: list[dict] = []


def add_project(name: str, owner: str, members: list[str], budget: float) -> dict:
    pid = slug(name)
    team = litellm.team_new(team_alias=pid, max_budget=budget)
    PROJECTS[pid] = {"id": pid, "name": name, "owner": owner, "members": sorted(set(members) - {owner}),
                     "team_id": team["team_id"], "api_key": litellm.key_generate(team["team_id"])["key"]}
    return PROJECTS[pid]


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


for _name, _owner, _members, _budget, _spend in [
    ("Code assistant", "alice", ["bob"], 50.0, 12.4),
    ("Demo sandbox", "alice", [], 0.02, 0.0),
    ("Contract review", "bob", ["alice", "carol"], 20.0, 3.1),
    ("HR chatbot", "carol", [], 100.0, 41.75),
]:
    litellm.seed_history(add_project(_name, _owner, _members, _budget)["team_id"], _spend, seed=len(PROJECTS))  # mock only

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


def new_project_name(name: str) -> str:
    name = name.strip()
    if not slug(name):
        raise HTTPException(422, "Project name is required")
    if slug(name) in PROJECTS:
        raise HTTPException(409, "A project with this name already exists")
    return name


def valid_budget(budget: float):
    if not budget >= 0:  # also rejects NaN
        raise HTTPException(422, "Budget must be a positive number")


@app.get("/api/projects")
def list_projects(request: Request, user: dict = Depends(current_user)):
    endpoint = LITELLM_BASE_URL or str(request.base_url).rstrip("/") + "/v1"
    return [{**with_budget(p), "endpoint": endpoint} for p in PROJECTS.values() if visible(p, user)]


@app.get("/api/config")
def config(user: dict = Depends(current_user)):
    return {"default_budget": DEFAULT_PROJECT_BUDGET, "bedrock_region": litellm.pricing.BEDROCK_REGION}


@app.get("/api/models")
def list_models(user: dict = Depends(current_user)):
    # USD per 1M tokens for display, from LiteLLM's model config (the prices it bills with). On-prem is 0.
    return [{"id": m["model_name"], "platform": i["platform"], "input": i["input_cost_per_token"] * 1e6,
             "output": i["output_cost_per_token"] * 1e6,
             "cached": i["cache_read_input_token_cost"] * 1e6 if "cache_read_input_token_cost" in i else None}
            for m in litellm.model_info() for i in [m["model_info"]]]


@app.get("/api/usage")
def usage(days: int = Query(30, ge=1, le=366), user: dict = Depends(current_user)):
    """Daily spend and tokens per project and model, for the projects this user can see."""
    today = datetime.now(timezone.utc).date()
    dates = [str(today - timedelta(days=d)) for d in range(days - 1, -1, -1)]
    teams = {p["team_id"]: p["id"] for p in PROJECTS.values() if visible(p, user)}
    agg = defaultdict(lambda: [0.0, 0])
    # shortcut: sums raw per-call rows in Python; at volume, query LiteLLM's daily spend tables instead
    for r in litellm.spend_logs(dates[0]):
        if r["team_id"] in teams:
            a = agg[(r["startTime"][:10], teams[r["team_id"]], r["model_group"])]
            a[0] += r["spend"]
            a[1] += r["total_tokens"]
    return {"days": dates, "rows": [{"date": d, "project": p, "model": m, "spend": s, "tokens": t}
                                    for (d, p, m), (s, t) in sorted(agg.items())]}


@app.get("/api/users")
def list_users(user: dict = Depends(admin_only)):
    return sorted(USERS)


@app.post("/api/projects", status_code=201)
def create_project(name: str = Body(...), owner: str = Body(...), members: list[str] = Body([]),
                   budget: float = Body(DEFAULT_PROJECT_BUDGET), user: dict = Depends(admin_only)):
    name = new_project_name(name)
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


# ---------- requests and feedback ----------

_ids = itertools.count(1)
KINDS = {"project", "budget", "model"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def text(value: str, field: str, required: bool = True) -> str:
    value = value.strip()
    if required and not value:
        raise HTTPException(422, f"{field} is required")
    if len(value) > 2000:
        raise HTTPException(422, f"{field} is too long (2000 characters max)")
    return value


def mine(items: list[dict], user: dict) -> list[dict]:
    """Newest first; admins see everyone's."""
    return [x for x in reversed(items) if user["role"] == "admin" or x["by"] == user["username"]]


@app.get("/api/requests")
def list_requests(user: dict = Depends(current_user)):
    return mine(REQUESTS, user)


@app.post("/api/requests", status_code=201)
def create_request(kind: str = Body(...), name: str = Body(""), project: str = Body(""), amount: float = Body(0),
                   reason: str = Body(""), user: dict = Depends(current_user)):
    """kind=project: name, amount (budget). kind=budget: project, amount (USD to add). kind=model: name, project optional."""
    if user["role"] != "user":
        raise HTTPException(403, "Requests are made by users")
    if kind not in KINDS:
        raise HTTPException(422, "Unknown request kind")
    req = {"id": next(_ids), "kind": kind, "by": user["username"], "created": now(), "status": "pending",
           "reason": text(reason, "Reason", required=False), "name": "", "project": "", "amount": 0.0, "note": ""}
    if kind == "project":
        valid_budget(amount)
        req |= {"name": new_project_name(text(name, "Project name")), "amount": amount or DEFAULT_PROJECT_BUDGET}
    elif kind == "budget":
        if not amount > 0:
            raise HTTPException(422, "Amount must be more than 0")
        if project not in PROJECTS or not visible(PROJECTS[project], user):
            raise HTTPException(404, "Project not found")
        req |= {"project": project, "amount": amount}
    else:
        if project and (project not in PROJECTS or not visible(PROJECTS[project], user)):
            raise HTTPException(404, "Project not found")
        req |= {"name": text(name, "Model name"), "project": project}
    REQUESTS.append(req)
    return req


def get_request(rid: int) -> dict:
    req = next((r for r in REQUESTS if r["id"] == rid), None)
    if not req:
        raise HTTPException(404, "Request not found")
    if req["status"] != "pending":
        raise HTTPException(409, "Request already decided")
    return req


def decide(req: dict, status: str, note: str, user: dict) -> dict:
    req |= {"status": status, "note": text(note, "Note", required=False), "decided_by": user["username"], "decided": now()}
    return req


@app.post("/api/requests/{rid}/approve")
def approve_request(rid: int, note: str = Body("", embed=True), user: dict = Depends(admin_only)):
    req = get_request(rid)
    if req["kind"] == "project":
        add_project(new_project_name(req["name"]), req["by"], [], req["amount"])
    elif req["kind"] == "budget":
        team = litellm.team_info(get_project(req["project"])["team_id"])
        litellm.team_update(team["team_id"], max_budget=team["max_budget"] + req["amount"])
    # kind=model: the admin adds the model to LiteLLM's config; approving records the decision
    return decide(req, "approved", note, user)


@app.post("/api/requests/{rid}/reject")
def reject_request(rid: int, note: str = Body("", embed=True), user: dict = Depends(admin_only)):
    return decide(get_request(rid), "rejected", note, user)


@app.get("/api/feedback")
def list_feedback(user: dict = Depends(current_user)):
    return mine(FEEDBACK, user)


@app.post("/api/feedback", status_code=201)
def send_feedback(message: str = Body(..., embed=True), user: dict = Depends(current_user)):
    FEEDBACK.append({"id": len(FEEDBACK) + 1, "by": user["username"], "created": now(), "message": text(message, "Message")})
    return FEEDBACK[-1]


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
