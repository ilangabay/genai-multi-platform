"""Governance layer of the gateway VM (mock).

Owns projects (name, owner, members) and the web UI. Money and keys live in LiteLLM:
each project is a LiteLLM team with a max_budget, and its API key is a LiteLLM virtual key
of that team. Spend is read back from LiteLLM, never computed here.
In production, apps call the guardrail LiteLLM endpoint, which forwards to the LiteLLM router
serving Bedrock and on-prem models; app/litellm_mock.py stands in for both.
"""
import hashlib
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
from app.store import STORE

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")  # first password of the bootstrap account "admin"
USER_PASSWORD = os.environ.get("USER_PASSWORD", "user")     # first password of the GATEWAY_USERS demo accounts
LITELLM_BASE_URL = os.environ.get("LITELLM_BASE_URL")  # guardrail LiteLLM endpoint shown to users; defaults to this mock
DEFAULT_PROJECT_BUDGET = float(os.environ.get("DEFAULT_PROJECT_BUDGET", "100"))  # USD, new projects and project requests
STATIC = Path(__file__).parent / "static"

# Working set, loaded from the store (empty in memory mode) and written through on every change: see app/store.py
USERS: dict[str, dict] = STORE.load("users")  # username -> {username, role, password (scrypt hash), must_change, created}
PROJECTS: dict[str, dict] = STORE.load("projects")  # id -> {id, name, owner, members, team_id, api_key}
REQUESTS: list[dict] = sorted(STORE.load("requests").values(), key=lambda r: r["id"])  # project, budget, model requests
FEEDBACK: list[dict] = sorted(STORE.load("feedback").values(), key=lambda f: f["id"])
SESSIONS: dict[str, dict] = STORE.load("sessions")  # sha256(cookie token) -> {username, role}


def add_project(name: str, owner: str, members: list[str], budget: float) -> dict:
    pid = slug(name)
    # explicit list, so a model added to LiteLLM later is not opened to every project
    team = litellm.team_new(team_alias=pid, max_budget=budget, models=[m["model_name"] for m in litellm.model_info()])
    PROJECTS[pid] = {"id": pid, "name": name, "owner": owner, "members": sorted(set(members) - {owner}),
                     "team_id": team["team_id"], "api_key": litellm.key_generate(team["team_id"])["key"]}
    STORE.save("projects", pid, PROJECTS[pid])
    return PROJECTS[pid]


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    return salt.hex() + ":" + hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1).hex()


def check_password(password: str, stored: str) -> bool:
    salt, digest = stored.split(":")
    return hmac.compare_digest(hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1).hex(), digest)


def add_user(username: str, role: str, password: str, must_change: bool) -> dict:
    USERS[username] = {"username": username, "role": role, "password": hash_password(password),
                       "must_change": must_change, "created": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    return save_user(USERS[username])


def save_user(account: dict) -> dict:
    STORE.save("users", account["username"], account)
    return account


if not USERS:  # first start: bootstrap admin; with a database its first password must be changed at sign-in
    add_user("admin", "admin", ADMIN_PASSWORD, must_change=STORE.persistent)
if not STORE.persistent:  # demo accounts, memory mode only
    for _u in os.environ.get("GATEWAY_USERS", "alice,bob,carol").split(","):
        if _u.strip() and _u.strip().lower() not in USERS:
            add_user(_u.strip().lower(), "user", USER_PASSWORD, must_change=False)
DUMMY_HASH = hash_password(secrets.token_urlsafe())  # unknown usernames take as long as wrong passwords


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


for _name, _owner, _members, _budget, _spend in [] if STORE.persistent else [  # demo projects, memory mode only
    ("Code assistant", "alice", ["bob"], 50.0, 12.4),
    ("Demo sandbox", "alice", [], 0.02, 0.0),
    ("Contract review", "bob", ["alice", "carol"], 20.0, 3.1),
    ("HR chatbot", "carol", [], 100.0, 41.75),
]:
    litellm.seed_history(add_project(_name, _owner, _members, _budget)["team_id"], _spend, seed=len(PROJECTS))

app = FastAPI(title="GenAI gateway - governance (mock)")
app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.include_router(litellm.router)


# ---------- auth ----------

def sid(token: str | None) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()  # only the hash is kept, so a leaked table opens nothing


def drop_session(key: str):
    if SESSIONS.pop(key, None):
        STORE.delete("sessions", key)


def session_user(session: str | None = Cookie(None)) -> dict:
    s = SESSIONS.get(sid(session))
    account = USERS.get(s["username"]) if s else None
    # the role is read on every request, so a demoted admin loses admin access at once
    if not account or (s["role"] == "admin" and account["role"] != "admin"):
        drop_session(sid(session))
        raise HTTPException(401, "Not signed in")
    return {**s, "must_change_password": account["must_change"]}


def current_user(user: dict = Depends(session_user)) -> dict:
    if user["must_change_password"]:
        raise HTTPException(403, "Change your password first")
    return user


def admin_only(user: dict = Depends(current_user)) -> dict:
    if user["role"] != "admin":
        raise HTTPException(403, "Admin only")
    return user


@app.post("/api/login")
def login(response: Response, role: str = Body(...), password: str = Body(...), username: str = Body("")):
    """role is the chosen view: "admin" needs an admin account; an admin may also sign in as a user."""
    account = USERS.get(username.strip().lower())
    ok = check_password(password, account["password"] if account else DUMMY_HASH)
    if not ok or not account or role not in ROLES or (role == "admin" and account["role"] != "admin"):
        raise HTTPException(401, "Invalid credentials")
    token = secrets.token_urlsafe(32)
    SESSIONS[sid(token)] = {"username": account["username"], "role": role}
    STORE.save("sessions", sid(token), SESSIONS[sid(token)])
    response.set_cookie("session", token, httponly=True, samesite="strict")
    return {**SESSIONS[sid(token)], "must_change_password": account["must_change"]}


@app.post("/api/logout")
def logout(response: Response, session: str | None = Cookie(None)):
    drop_session(sid(session))
    response.delete_cookie("session")
    return {"ok": True}


@app.get("/api/me")
def me(user: dict = Depends(session_user)):
    return user


@app.post("/api/me/password")
def change_password(current: str = Body(...), new: str = Body(...), user: dict = Depends(session_user),
                    session: str | None = Cookie(None)):
    account = USERS[user["username"]]
    if not check_password(current, account["password"]):
        raise HTTPException(403, "Current password is wrong")
    if len(new) < 12:
        raise HTTPException(422, "Use at least 12 characters")
    if new == current:
        raise HTTPException(422, "Choose a password different from the current one")
    account |= {"password": hash_password(new), "must_change": False}
    save_user(account)
    end_sessions(account["username"], keep=session)
    return {"ok": True}


# ---------- user management (admin) ----------

ROLES = {"user", "admin"}
USERNAME = re.compile(r"[a-z0-9][a-z0-9._-]{1,31}")


def public(u: dict) -> dict:
    return {k: u[k] for k in ("username", "role", "must_change", "created")}


def get_user(username: str) -> dict:
    if username not in USERS:
        raise HTTPException(404, "User not found")
    return USERS[username]


def end_sessions(username: str, keep: str | None = None):
    for key in [k for k, s in SESSIONS.items() if s["username"] == username and (keep is None or k != sid(keep))]:
        drop_session(key)


def new_password() -> str:
    return secrets.token_urlsafe(12)  # 16 characters, shown once to the admin, changed at first sign-in


@app.get("/api/users")
def list_users(user: dict = Depends(admin_only)):
    return [public(u) for _, u in sorted(USERS.items())]


@app.post("/api/users", status_code=201)
def create_user(username: str = Body(...), role: str = Body("user"), user: dict = Depends(admin_only)):
    username = username.strip().lower()
    if not USERNAME.fullmatch(username):
        raise HTTPException(422, "Username: 2 to 32 characters, lowercase letters, digits, dot, dash or underscore")
    if role not in ROLES:
        raise HTTPException(422, "Role must be user or admin")
    if username in USERS:
        raise HTTPException(409, "This username already exists")
    password = new_password()
    return {**public(add_user(username, role, password, must_change=True)), "password": password}


@app.post("/api/users/{username}/reset-password")
def reset_password(username: str, user: dict = Depends(admin_only)):
    account, password = get_user(username), new_password()
    account |= {"password": hash_password(password), "must_change": True}
    save_user(account)
    end_sessions(username)
    return {**public(account), "password": password}


@app.patch("/api/users/{username}")
def set_role(username: str, role: str = Body(..., embed=True), user: dict = Depends(admin_only)):
    account = get_user(username)
    if role not in ROLES:
        raise HTTPException(422, "Role must be user or admin")
    if role == "user" and account["role"] == "admin" and sum(u["role"] == "admin" for u in USERS.values()) == 1:
        raise HTTPException(409, "Keep at least one admin")
    account["role"] = role
    return public(save_user(account))


# ---------- projects ----------

def visible(p: dict, user: dict) -> bool:
    return user["role"] == "admin" or user["username"] == p["owner"] or user["username"] in p["members"]


def get_project(pid: str) -> dict:
    if pid not in PROJECTS:
        raise HTTPException(404, "Project not found")
    return PROJECTS[pid]


def with_budget(p: dict) -> dict:
    team = litellm.team_info(p["team_id"])
    return {**p, "budget": team["max_budget"], "spent": team["spend"], "models": team["models"]}


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


@app.post("/api/projects", status_code=201)
def create_project(name: str = Body(...), owner: str = Body(...), members: list[str] = Body([]),
                   budget: float = Body(DEFAULT_PROJECT_BUDGET), user: dict = Depends(admin_only)):
    name = new_project_name(name)
    if owner not in USERS or not set(members) <= set(USERS):
        raise HTTPException(422, "Unknown owner or member")
    valid_budget(budget)
    return with_budget(add_project(name, owner, members, budget))


@app.patch("/api/projects/{pid}/budget")
def set_budget(pid: str, budget: float = Body(..., embed=True), user: dict = Depends(admin_only)):
    valid_budget(budget)
    p = get_project(pid)
    litellm.team_update(p["team_id"], max_budget=budget)
    return with_budget(p)


@app.put("/api/projects/{pid}/models")
def set_models(pid: str, models: list[str] = Body(..., embed=True), user: dict = Depends(admin_only)):
    catalog = [m["model_name"] for m in litellm.model_info()]
    if not models:  # an empty list would mean "every model" in LiteLLM
        raise HTTPException(422, "A project needs at least one model")
    if not set(models) <= set(catalog):
        raise HTTPException(422, "Unknown model")
    litellm.team_update(get_project(pid)["team_id"], models=[m for m in catalog if m in models])
    return with_budget(get_project(pid))


@app.post("/api/projects/{pid}/key/revoke")
def revoke_key(pid: str, user: dict = Depends(admin_only)):
    p = get_project(pid)
    if p["api_key"]:
        litellm.key_delete([p["api_key"]])
    p["api_key"] = None
    STORE.save("projects", pid, p)
    return with_budget(p)


@app.post("/api/projects/{pid}/key/regenerate")
def regenerate_key(pid: str, user: dict = Depends(admin_only)):
    p = get_project(pid)
    if p["api_key"]:
        litellm.key_delete([p["api_key"]])
    # the spend stays on the team, so a new key does not reset the budget
    p["api_key"] = litellm.key_generate(p["team_id"])["key"]
    STORE.save("projects", pid, p)
    return with_budget(p)


# ---------- requests and feedback ----------

_ids = itertools.count(max((r["id"] for r in REQUESTS), default=0) + 1)
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
    STORE.save("requests", req["id"], req)
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
    STORE.save("requests", req["id"], req)
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
    STORE.save("feedback", FEEDBACK[-1]["id"], FEEDBACK[-1])
    return FEEDBACK[-1]


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
