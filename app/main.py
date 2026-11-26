"""Governance layer of the gateway VM (mock).

Owns projects (name, owners, members) and the web UI. Money and keys live in LiteLLM:
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
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Body, Cookie, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import litellm_mock, litellm_proxy, store

# LITELLM_URL set: the real LiteLLM proxy (admin API with the master key). Unset: the in-process stand-in.
litellm = litellm_proxy if os.environ.get("LITELLM_URL") else litellm_mock

LITELLM_BASE_URL = os.environ.get("LITELLM_BASE_URL")  # LLM endpoint shown to users; defaults to the stand-in on this server
DEFAULT_PROJECT_BUDGET = float(os.environ.get("DEFAULT_PROJECT_BUDGET", "100"))  # USD, new projects and project requests
SESSION_HOURS = float(os.environ.get("SESSION_HOURS", "12"))  # a sign-in expires after this
# Catalog page: the role LiteLLM assumes for Bedrock (not secret; LiteLLM refuses os.environ/ references in test calls)
BEDROCK_ROLE_ARN = os.environ.get("BEDROCK_ROLE_ARN", "")
ONPREM_API_BASE = os.environ.get("ONPREM_API_BASE", "http://onprem-llm.internal:8000/v1")  # default endpoint of new on-prem models
COOKIE_SECURE = os.environ.get("COOKIE_SECURE") == "1"  # set to 1 when the app is served over HTTPS
STATIC = Path(__file__).parent / "static"
DEFAULT_ADMIN_PASSWORDS = {"admin", "change-me-at-first-sign-in"}  # public in this repo, refused with a database

# Working set: filled by startup() from the store; every change goes to the store first (see app/store.py)
USERS: dict[str, dict] = {}     # username -> {username, role, password (scrypt hash), must_change, created}
PROJECTS: dict[str, dict] = {}  # id -> {id, name, owners, members, team_id, api_key}
REQUESTS: list[dict] = []       # project, budget and model requests from users
FEEDBACK: list[dict] = []
SESSIONS: dict[str, dict] = {}  # sha256(cookie token) -> {username, role, created}
_request_ids = _feedback_ids = itertools.count(1)
DUMMY_HASH = ""


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def catalog_models(models: list[str] | None) -> list[str]:
    """A project's whitelist, checked against the platform catalog (LiteLLM's model list), in catalog order.
    Empty = no model. None = the whole catalog (demo projects only)."""
    catalog = [m["model_name"] for m in litellm.model_info()]
    if models is None:
        return catalog
    if not set(models) <= set(catalog):
        raise HTTPException(422, "Unknown model: " + ", ".join(sorted(set(models) - set(catalog))))
    return [m for m in catalog if m in models]


def add_project(name: str, owner: str, members: list[str], budget: float, models: list[str] | None = None) -> dict:
    pid = slug(name)
    team = litellm.team_new(team_alias=pid, max_budget=budget, models=catalog_models(models))
    project = {"id": pid, "name": name, "owners": [owner], "members": sorted(set(members) - {owner}),
               "team_id": team["team_id"], "api_key": litellm.key_generate(team["team_id"])["key"]}
    store.STORE.save("projects", pid, project)
    PROJECTS[pid] = project
    return project


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    return salt.hex() + ":" + hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1).hex()


def check_password(password: str, stored: str) -> bool:
    salt, digest = stored.split(":")
    return hmac.compare_digest(hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1).hex(), digest)


def add_user(username: str, role: str, password: str, must_change: bool) -> dict:
    account = {"username": username, "role": role, "password": hash_password(password),
               "must_change": must_change, "created": now()}
    store.STORE.save("users", username, account)
    USERS[username] = account
    return account


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def expired(session: dict) -> bool:
    created = datetime.fromisoformat(session.get("created", "1970-01-01T00:00:00+00:00"))
    return datetime.now(timezone.utc) - created > timedelta(hours=SESSION_HOURS)


def startup():
    """Load the state from the store and create what a first start needs. Runs once, when the app starts."""
    global _request_ids, _feedback_ids, DUMMY_HASH
    store.init()
    if litellm is litellm_proxy and not store.STORE.persistent:  # projects would vanish, their LiteLLM teams would not
        raise RuntimeError("The real LiteLLM (LITELLM_URL) needs STORAGE=postgres")
    litellm.startup()
    db = store.STORE
    for live, table in ((USERS, "users"), (PROJECTS, "projects"), (SESSIONS, "sessions")):
        live.clear()
        live.update(db.load(table))
    for p in PROJECTS.values():  # rows saved before projects had several owners
        if "owner" in p:
            p["owners"] = [p.pop("owner")]
    for key, s in list(SESSIONS.items()):
        if expired(s):
            db.delete("sessions", key)
            SESSIONS.pop(key)
    REQUESTS[:] = sorted(db.load("requests").values(), key=lambda r: r["id"])
    FEEDBACK[:] = sorted(db.load("feedback").values(), key=lambda f: f["id"])
    _request_ids = itertools.count(max((r["id"] for r in REQUESTS), default=0) + 1)
    _feedback_ids = itertools.count(max((f["id"] for f in FEEDBACK), default=0) + 1)
    DUMMY_HASH = hash_password(secrets.token_urlsafe())  # unknown usernames take as long as wrong passwords

    if not USERS:  # first start: the bootstrap admin, whose first password must be changed when there is a database
        admin_password = os.environ.get("ADMIN_PASSWORD", "admin")
        if db.persistent and (admin_password in DEFAULT_ADMIN_PASSWORDS or len(admin_password) < 12):
            raise RuntimeError("First start with a database: set ADMIN_PASSWORD to a password of 12+ characters "
                               "that is not a default, or anyone reaching the app first could take the admin account")
        add_user("admin", "admin", admin_password, must_change=db.persistent)
    if db.persistent:
        return
    for u in os.environ.get("GATEWAY_USERS", "alice,bob,carol").split(","):  # demo data, memory mode only
        if u.strip() and u.strip().lower() not in USERS:
            add_user(u.strip().lower(), "user", os.environ.get("USER_PASSWORD", "user"), must_change=False)
    for name, owner, members, budget, spend in [
        ("Code assistant", "alice", ["bob"], 50.0, 12.4),
        ("Demo sandbox", "alice", [], 0.02, 0.0),
        ("Contract review", "bob", ["alice", "carol"], 20.0, 3.1),
        ("HR chatbot", "carol", [], 100.0, 41.75),
    ]:
        litellm.seed_history(add_project(name, owner, members, budget)["team_id"], spend, seed=len(PROJECTS))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    startup()
    yield


app = FastAPI(title="GenAI gateway - governance (mock)", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.include_router(litellm.router)


# ---------- auth ----------

def sid(token: str | None) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()  # only the hash is kept, so a leaked table opens nothing


def drop_session(key: str):
    if key in SESSIONS:
        store.STORE.delete("sessions", key)
        SESSIONS.pop(key, None)


def session_user(session: str | None = Cookie(None)) -> dict:
    s = SESSIONS.get(sid(session))
    account = USERS.get(s["username"]) if s else None
    # the role is read on every request, so a demoted admin loses admin access at once
    if not account or expired(s) or (s["role"] == "admin" and account["role"] != "admin"):
        drop_session(sid(session))
        raise HTTPException(401, "Not signed in")
    return {"username": s["username"], "role": s["role"], "must_change_password": account["must_change"]}


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
    session = {"username": account["username"], "role": role, "created": now()}
    store.STORE.save("sessions", sid(token), session)
    SESSIONS[sid(token)] = session
    response.set_cookie("session", token, httponly=True, samesite="strict", secure=COOKIE_SECURE,
                        max_age=int(SESSION_HOURS * 3600))
    return {"username": account["username"], "role": role, "must_change_password": account["must_change"]}


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
    store.update("users", account["username"], account, password=hash_password(new), must_change=False)
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
    for key in [k for k, s in list(SESSIONS.items()) if s["username"] == username and (keep is None or k != sid(keep))]:
        drop_session(key)


def new_password() -> str:
    return secrets.token_urlsafe(12)  # 16 characters, shown once to the admin, changed at first sign-in


@app.get("/api/users")
def list_users(user: dict = Depends(admin_only)):
    return [public(u) for _, u in sorted(list(USERS.items()))]


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
    if username == user["username"]:  # it would sign you out before you could read the new password
        raise HTTPException(409, "Use Change password for your own account")
    account, password = get_user(username), new_password()
    store.update("users", username, account, password=hash_password(password), must_change=True)
    end_sessions(username)
    return {**public(account), "password": password}


@app.patch("/api/users/{username}")
def set_role(username: str, role: str = Body(..., embed=True), user: dict = Depends(admin_only)):
    account = get_user(username)
    if role not in ROLES:
        raise HTTPException(422, "Role must be user or admin")
    if role == "user" and account["role"] == "admin" and sum(u["role"] == "admin" for u in list(USERS.values())) == 1:
        raise HTTPException(409, "Keep at least one admin")
    return public(store.update("users", username, account, role=role))


# ---------- projects ----------

def visible(p: dict, user: dict) -> bool:
    return user["role"] == "admin" or user["username"] in p["owners"] + p["members"]


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
    return [{**with_budget(p), "endpoint": endpoint} for p in list(PROJECTS.values()) if visible(p, user)]


@app.get("/api/config")
def config(user: dict = Depends(current_user)):
    return {"default_budget": DEFAULT_PROJECT_BUDGET, "bedrock_region": litellm.pricing.BEDROCK_REGION,
            "onprem_api_base": ONPREM_API_BASE}


@app.get("/api/models")
def list_models(user: dict = Depends(current_user)):
    # USD per 1M tokens for display, from LiteLLM's model config (the prices it bills with). On-prem is 0.
    # added: put in the catalog from the Catalog page (LiteLLM database), so it can be removed there too
    return [{"id": m["model_name"], "platform": i["platform"], "input": i["input_cost_per_token"] * 1e6,
             "model": i.get("bedrock_model") or m["litellm_params"]["model"], "endpoint": i.get("endpoint"),
             "aws_model": i.get("aws_model"), "added": bool(i.get("db_model")),
             "output": i["output_cost_per_token"] * 1e6,
             "cached": i["cache_read_input_token_cost"] * 1e6 if i.get("cache_read_input_token_cost") is not None else None}
            for m in litellm.model_info() for i in [m["model_info"]]]


@app.get("/api/usage")
def usage(days: int = Query(30, ge=1, le=366), user: dict = Depends(current_user)):
    """Daily spend and tokens per project and model, for the projects this user can see."""
    today = datetime.now(timezone.utc).date()
    dates = [str(today - timedelta(days=d)) for d in range(days - 1, -1, -1)]
    teams = {p["team_id"]: p["id"] for p in list(PROJECTS.values()) if visible(p, user)}
    agg = defaultdict(lambda: [0.0, 0])
    # shortcut: sums raw per-call rows in Python; at volume, query LiteLLM's daily spend tables instead
    for r in litellm.spend_logs(dates[0], list(teams)):
        if r["team_id"] in teams:
            a = agg[(r["startTime"][:10], teams[r["team_id"]], r["model_group"])]
            a[0] += r["spend"]
            a[1] += r["total_tokens"]
    return {"days": dates, "rows": [{"date": d, "project": p, "model": m, "spend": s, "tokens": t}
                                    for (d, p, m), (s, t) in sorted(agg.items())]}


@app.post("/api/projects", status_code=201)
def create_project(name: str = Body(...), owner: str = Body(...), members: list[str] = Body([]),
                   budget: float = Body(DEFAULT_PROJECT_BUDGET), models: list[str] = Body([]),
                   user: dict = Depends(admin_only)):
    """models: the project's whitelist, from the platform catalog. None by default: the admin adds them."""
    name = new_project_name(name)
    if owner not in USERS or not set(members) <= set(USERS):
        raise HTTPException(422, "Unknown owner or member")
    valid_budget(budget)
    catalog_models(models)  # reject a bad list before anything is created in LiteLLM
    return with_budget(add_project(name, owner, members, budget, models))


@app.patch("/api/projects/{pid}/budget")
def set_budget(pid: str, budget: float = Body(..., embed=True), user: dict = Depends(admin_only)):
    valid_budget(budget)
    p = get_project(pid)
    litellm.team_update(p["team_id"], max_budget=budget)
    return with_budget(p)


@app.put("/api/projects/{pid}/models")
def set_models(pid: str, models: list[str] = Body(..., embed=True), user: dict = Depends(admin_only)):
    litellm.team_update(get_project(pid)["team_id"], models=catalog_models(models))
    return with_budget(get_project(pid))


@app.post("/api/projects/{pid}/key/revoke")
def revoke_key(pid: str, user: dict = Depends(admin_only)):
    p = get_project(pid)
    if p["api_key"]:
        litellm.key_delete([p["api_key"]])
    return with_budget(store.update("projects", pid, p, api_key=None))


@app.post("/api/projects/{pid}/key/regenerate")
def regenerate_key(pid: str, user: dict = Depends(admin_only)):
    p = get_project(pid)
    if p["api_key"]:
        litellm.key_delete([p["api_key"]])
    # the spend stays on the team, so a new key does not reset the budget
    return with_budget(store.update("projects", pid, p, api_key=litellm.key_generate(p["team_id"])["key"]))


# ---------- project members: owners and admins manage them ----------

def managed_project(pid: str, user: dict) -> dict:
    p = get_project(pid)
    if user["role"] != "admin" and user["username"] not in p["owners"]:
        raise HTTPException(403, "Only the project owners and admins can manage members")
    return p


def set_project_role(pid: str, p: dict, username: str, role: str | None) -> dict:
    """role None removes the user from the project."""
    owners = [u for u in p["owners"] if u != username] + ([username] if role == "owner" else [])
    members = [u for u in p["members"] if u != username] + ([username] if role == "member" else [])
    if not owners:
        raise HTTPException(409, "Keep at least one owner")
    return with_budget(store.update("projects", pid, p, owners=sorted(owners), members=sorted(members)))


@app.put("/api/projects/{pid}/members/{username}")
def set_member(pid: str, username: str, role: str = Body(..., embed=True), user: dict = Depends(current_user)):
    p = managed_project(pid, user)
    if username not in USERS:
        raise HTTPException(404, "Unknown user")
    if role not in {"owner", "member"}:
        raise HTTPException(422, "Role must be owner or member")
    return set_project_role(pid, p, username, role)


@app.delete("/api/projects/{pid}/members/{username}")
def remove_member(pid: str, username: str, user: dict = Depends(current_user)):
    p = managed_project(pid, user)
    if username not in p["owners"] + p["members"]:
        raise HTTPException(404, "Not a member of this project")
    return set_project_role(pid, p, username, None)


# ---------- platform catalog: admins add models to LiteLLM (/model/new) ----------

# a Bedrock model ID (anthropic.claude-sonnet-5-5, eu.anthropic.claude-sonnet-5-5) or ARN (foundation model,
# inference profile, application inference profile, provisioned throughput)
BEDROCK_MODEL = re.compile(r"arn:aws:bedrock:([a-z]{2}(?:-gov)?-[a-z]+-\d):\d{0,12}:[a-z-]+/[\w.:/-]+|[a-z0-9-]+(?:\.[\w:-]+)+")
SERVED_MODEL = re.compile(r"[\w.:/-]{1,200}")  # model name as the on-prem server knows it (vLLM --served-model-name)
CATALOG_NAME = re.compile(r"[a-z0-9][a-z0-9.-]{0,62}")
HOSTINGS = {"bedrock": "Bedrock", "onprem": "On-prem"}
ONPREM_SERVERS = {"vllm": "hosted_vllm", "litellm": "litellm_proxy"}  # on-prem server -> LiteLLM provider


def bedrock_model(model: str) -> tuple[str, str]:
    """Model ID or ARN as pasted by the admin -> (ID or ARN for Bedrock calls, region to call)."""
    model = model.strip()
    m = BEDROCK_MODEL.fullmatch(model)
    if not m:
        raise HTTPException(422, "Not a Bedrock model ID or ARN")
    if ":foundation-model/" in model:  # a foundation model ARN is its ID in that region
        return model.rsplit("/", 1)[1], m.group(1)
    return model, m.group(1) or litellm.pricing.BEDROCK_REGION


def aws_prices(region: str) -> dict[str, dict]:
    try:
        return litellm.pricing.fetch(region)
    except (OSError, ValueError) as e:
        raise HTTPException(502, f"AWS price list unreachable ({litellm.pricing.PRICING_URL}): {e}")


def deployment(hosting: str, model: str, api_base: str = "", api_key: str = "", server: str = "vllm") -> tuple[dict, dict]:
    """What LiteLLM needs to call the model (litellm_params, without prices) and what the catalog shows (model_info)."""
    if hosting == "bedrock":
        model_id, region = bedrock_model(model)
        if litellm is litellm_proxy and not BEDROCK_ROLE_ARN:
            raise HTTPException(503, "Set BEDROCK_ROLE_ARN for the governance app to add Bedrock models")
        params = {"model": f"bedrock/converse/{model_id}",  # Converse: one request format for every Bedrock model
                  "aws_region_name": region, "aws_session_name": "genai-gateway"}
        if BEDROCK_ROLE_ARN:
            params["aws_role_name"] = BEDROCK_ROLE_ARN
        return params, {"platform": "Bedrock", "bedrock_model": model_id, "region": region}
    if hosting == "onprem":
        model, api_base = model.strip(), (api_base.strip() or ONPREM_API_BASE).rstrip("/")
        if server not in ONPREM_SERVERS:
            raise HTTPException(422, "Server must be vllm or litellm")
        if not SERVED_MODEL.fullmatch(model):
            raise HTTPException(422, "Model: the name the on-prem server serves it under")
        if not re.fullmatch(r"https?://[^\s/]+(/[^\s]*)?", api_base):
            raise HTTPException(422, "Endpoint: an http(s) URL of an OpenAI-compatible server, e.g. http://vllm.internal:8000/v1")
        params = {"model": f"{ONPREM_SERVERS[server]}/{model}", "api_base": api_base}
        if api_key.strip():
            params["api_key"] = api_key.strip()  # kept encrypted by LiteLLM (LITELLM_SALT_KEY), never by this app
        return params, {"platform": "On-prem", "endpoint": api_base, "server": server}
    raise HTTPException(422, "Hosting must be " + " or ".join(HOSTINGS))


@app.get("/api/catalog/lookup")
def catalog_lookup(model: str, user: dict = Depends(admin_only)):
    """Bedrock model -> suggested catalog name, its entry in the AWS price list and the entries to pick from.
    Prices are USD per 1M tokens, straight from AWS for the region the model is called in."""
    model_id, region = bedrock_model(model)
    prices = aws_prices(region)
    is_global = model_id.rsplit("/", 1)[-1].startswith("global.")
    entries = {n: p for n, p in sorted(prices.items()) if n.endswith(litellm.pricing.GLOBAL) == is_global}
    aws_model = litellm.pricing.match(model_id, entries)
    # suggested name: the AWS model name when matched (claude-haiku-4-5), else the ID without vendor and profile
    name = aws_model or model_id.rsplit("/", 1)[-1].split(".", 2)[-1]
    return {"name": re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-"), "model": model_id, "region": region,
            "aws_model": aws_model, "prices": entries}


@app.post("/api/catalog/test")
def catalog_test(hosting: str = Body(...), model: str = Body(...), api_base: str = Body(""), api_key: str = Body(""),
                 server: str = Body("vllm"), user: dict = Depends(admin_only)):
    """Sends one short prompt with these settings through LiteLLM, before the model is added."""
    return litellm.test_connection(deployment(hosting, model, api_base, api_key, server)[0])


@app.post("/api/catalog", status_code=201)
def catalog_add(hosting: str = Body(...), name: str = Body(...), model: str = Body(...), aws_model: str = Body(""),
                api_base: str = Body(""), api_key: str = Body(""), server: str = Body("vllm"),
                user: dict = Depends(admin_only)):
    """Adds a model to the catalog. Bedrock prices come from the AWS price list (aws_model: the entry to bill with,
    matched from the model ID when empty); on-prem models are free. LiteLLM bills calls with these prices."""
    name = name.strip().lower()
    if not CATALOG_NAME.fullmatch(name):
        raise HTTPException(422, "Name: lowercase letters, digits, dots and dashes")
    if name in {m["model_name"] for m in litellm.model_info()}:
        raise HTTPException(409, "The catalog already has a model with this name")
    params, info = deployment(hosting, model, api_base, api_key, server)
    if hosting == "bedrock":
        prices = aws_prices(info["region"])
        aws_model = aws_model or litellm.pricing.match(info["bedrock_model"], prices)
        if aws_model not in prices:
            raise HTTPException(422, "No AWS price found for this model: pick its entry in the AWS price list")
        p = prices[aws_model]
        info["aws_model"] = aws_model
    else:
        p = {"input": 0.0, "output": 0.0}  # on-prem: no budget used
    fields = {"input": "input_cost_per_token", "output": "output_cost_per_token",
              "cache_read": "cache_read_input_token_cost", "cache_write": "cache_creation_input_token_cost"}
    litellm.model_add(name, params | {fields[k]: v / 1e6 for k, v in p.items() if k in fields}, info)
    return list_models(user)


@app.delete("/api/catalog/{name}")
def catalog_remove(name: str, user: dict = Depends(admin_only)):
    entry = next((m for m in litellm.model_info() if m["model_name"] == name), None)
    if not entry:
        raise HTTPException(404, "Model not found")
    if not entry["model_info"].get("db_model"):
        raise HTTPException(409, "This model is defined in litellm/config.yaml: remove it there")
    for p in list(PROJECTS.values()):  # take it off every whitelist too, so no project keeps a name that is gone
        models = litellm.team_info(p["team_id"])["models"]
        if name in models:
            litellm.team_update(p["team_id"], models=[m for m in models if m != name])
    litellm.model_delete(entry["model_info"]["id"])
    return list_models(user)


# ---------- requests and feedback ----------

KINDS = {"project", "budget", "model"}


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
                   models: list[str] = Body([]), reason: str = Body(""), user: dict = Depends(current_user)):
    """kind=project: name, amount (budget), models (catalog; empty = all). kind=budget: project, amount (USD to add).
    kind=model: name, project optional. A catalog model for a project is enabled on approval; any other is only recorded."""
    if user["role"] != "user":
        raise HTTPException(403, "Requests are made by users")
    if kind not in KINDS:
        raise HTTPException(422, "Unknown request kind")
    req = {"id": next(_request_ids), "kind": kind, "by": user["username"], "created": now(), "status": "pending",
           "reason": text(reason, "Reason", required=False), "name": "", "project": "", "amount": 0.0, "note": ""}
    if kind == "project":
        valid_budget(amount)
        req |= {"name": new_project_name(text(name, "Project name")), "amount": amount or DEFAULT_PROJECT_BUDGET,
                "models": catalog_models(models)}
    elif kind == "budget":
        if not amount > 0:
            raise HTTPException(422, "Amount must be more than 0")
        if project not in PROJECTS or not visible(PROJECTS[project], user):
            raise HTTPException(404, "Project not found")
        req |= {"project": project, "amount": amount}
    else:
        if project and (project not in PROJECTS or not visible(PROJECTS[project], user)):
            raise HTTPException(404, "Project not found")
        name = text(name, "Model name")
        if project and name in litellm.team_info(PROJECTS[project]["team_id"])["models"]:
            raise HTTPException(409, "This project can already use this model")
        req |= {"name": name, "project": project}
    store.STORE.save("requests", req["id"], req)
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
    return store.update("requests", req["id"], req, status=status, note=text(note, "Note", required=False),
                        decided_by=user["username"], decided=now())


@app.post("/api/requests/{rid}/approve")
def approve_request(rid: int, note: str = Body("", embed=True), user: dict = Depends(admin_only)):
    req = get_request(rid)
    if req["kind"] == "project":
        add_project(new_project_name(req["name"]), req["by"], [], req["amount"], req.get("models", []))
    elif req["kind"] == "budget":
        team = litellm.team_info(get_project(req["project"])["team_id"])
        litellm.team_update(team["team_id"], max_budget=team["max_budget"] + req["amount"])
    elif req["project"] and req["name"] in [m["model_name"] for m in litellm.model_info()]:  # catalog model: enable it
        team = litellm.team_info(get_project(req["project"])["team_id"])
        litellm.team_update(team["team_id"], models=catalog_models(team["models"] + [req["name"]]))
    # any other model request: the admin adds the model to LiteLLM's config; approving records the decision
    return decide(req, "approved", note, user)


@app.post("/api/requests/{rid}/reject")
def reject_request(rid: int, note: str = Body("", embed=True), user: dict = Depends(admin_only)):
    return decide(get_request(rid), "rejected", note, user)


@app.get("/api/feedback")
def list_feedback(user: dict = Depends(current_user)):
    return mine(FEEDBACK, user)


@app.post("/api/feedback", status_code=201)
def send_feedback(message: str = Body(..., embed=True), user: dict = Depends(current_user)):
    feedback = {"id": next(_feedback_ids), "by": user["username"], "created": now(), "message": text(message, "Message")}
    store.STORE.save("feedback", feedback["id"], feedback)
    FEEDBACK.append(feedback)
    return feedback


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
