"""Governance layer of the gateway VM (mock).

Serves the web UI, the project/admin API and a mock OpenAI-compatible
/v1/chat/completions that charges the project budget. No real LLM is called.
In production, apps call the guardrail LiteLLM endpoint, which forwards to the
LiteLLM router serving Bedrock and on-prem models; this mock stands in for both.
"""
import hmac
import json
import os
import re
import secrets
import threading
from pathlib import Path

from fastapi import Body, Cookie, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")
USER_PASSWORD = os.environ.get("USER_PASSWORD", "user")
LITELLM_BASE_URL = os.environ.get("LITELLM_BASE_URL")  # guardrail LiteLLM endpoint shown to users; defaults to this mock
STATIC = Path(__file__).parent / "static"

# shortcut: in-memory mock store, reset on restart; move to PostgreSQL (NUMERIC for money) for real use.
USERS = {"alice", "bob", "carol"}

# Prices are USD per 1M tokens (input, output). Bedrock rates are indicative only.
MODELS = [
    {"id": "claude-opus-5-5", "platform": "Bedrock", "upstream": "eu.anthropic.claude-opus-5-5", "input": 4.0, "output": 20.0},
    {"id": "claude-sonnet-5-5", "platform": "Bedrock", "upstream": "eu.anthropic.claude-sonnet-5-5", "input": 2.0, "output": 10.0},
    {"id": "claude-haiku-5-5", "platform": "Bedrock", "upstream": "eu.anthropic.claude-haiku-5-5", "input": 0.1, "output": 0.5},
    {"id": "mistral-small-onprem", "platform": "On-prem", "upstream": "vllm/mistral-small", "input": 0.05, "output": 0.15},
    {"id": "llama-70b-onprem", "platform": "On-prem", "upstream": "vllm/llama-3.3-70b", "input": 0.2, "output": 0.6},
]
MODELS_BY_ID = {m["id"]: m for m in MODELS}
MOCK_COMPLETION_TOKENS = 500


def new_key() -> str:
    return "sk-gw-" + secrets.token_urlsafe(24)


PROJECTS = {
    p["id"]: p
    for p in [
        {"id": "code-assistant", "name": "Code assistant", "owner": "alice", "members": ["bob"], "budget": 50.0, "spent": 12.4},
        {"id": "demo-sandbox", "name": "Demo sandbox", "owner": "alice", "members": [], "budget": 0.02, "spent": 0.0},
        {"id": "contract-review", "name": "Contract review", "owner": "bob", "members": ["alice", "carol"], "budget": 20.0, "spent": 3.1},
        {"id": "hr-chatbot", "name": "HR chatbot", "owner": "carol", "members": [], "budget": 100.0, "spent": 41.75},
    ]
}
for _p in PROJECTS.values():
    _p["api_key"] = new_key()

SESSIONS: dict[str, dict] = {}
LOCK = threading.Lock()  # budget check + debit must be atomic across worker threads

app = FastAPI(title="GenAI gateway - governance (mock)")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


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


@app.get("/api/projects")
def list_projects(request: Request, user: dict = Depends(current_user)):
    endpoint = LITELLM_BASE_URL or str(request.base_url).rstrip("/") + "/v1"
    return [{**p, "endpoint": endpoint} for p in PROJECTS.values() if visible(p, user)]


@app.get("/api/models")
def list_models(user: dict = Depends(current_user)):
    return MODELS


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
    if not budget >= 0:
        raise HTTPException(422, "Budget must be a positive number")
    PROJECTS[pid] = {"id": pid, "name": name, "owner": owner, "members": sorted(set(members) - {owner}),
                     "budget": budget, "spent": 0.0, "api_key": new_key()}
    return PROJECTS[pid]


@app.patch("/api/projects/{pid}/budget")
def set_budget(pid: str, budget: float = Body(..., embed=True), user: dict = Depends(admin_only)):
    if not budget >= 0:  # also rejects NaN
        raise HTTPException(422, "Budget must be a positive number")
    p = get_project(pid)
    with LOCK:
        p["budget"] = budget
    return p


@app.post("/api/projects/{pid}/key/revoke")
def revoke_key(pid: str, user: dict = Depends(admin_only)):
    p = get_project(pid)
    p["api_key"] = None
    return p


@app.post("/api/projects/{pid}/key/regenerate")
def regenerate_key(pid: str, user: dict = Depends(admin_only)):
    p = get_project(pid)
    p["api_key"] = new_key()
    return p


# ---------- mock LLM endpoint (OpenAI-compatible shape) ----------

def oai_error(status: int, message: str, code: str) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": code, "code": code}}, status_code=status)


@app.post("/v1/chat/completions")
def chat_completions(body: dict = Body(...), authorization: str = Header("")):
    key = authorization.removeprefix("Bearer ").strip()
    project = next((p for p in PROJECTS.values() if p["api_key"] and hmac.compare_digest(p["api_key"], key)), None)
    if not project:
        return oai_error(401, "Invalid or revoked API key.", "invalid_api_key")
    model = MODELS_BY_ID.get(body.get("model"))
    if not model:
        return oai_error(404, f"Model '{body.get('model')}' is not available.", "model_not_found")

    prompt_tokens = max(1, len(json.dumps(body.get("messages", []))) // 4)  # rough mock tokenizer
    cost = (prompt_tokens * model["input"] + MOCK_COMPLETION_TOKENS * model["output"]) / 1_000_000
    with LOCK:
        remaining = project["budget"] - project["spent"]
        if cost > remaining:
            # 402 rather than 429 so OpenAI SDKs do not auto-retry
            return oai_error(
                402,
                f"Budget exceeded for project '{project['name']}': ${max(remaining, 0):.6f} left, "
                f"this call needs ${cost:.6f}. Ask an admin to raise the budget.",
                "budget_exceeded",
            )
        project["spent"] += cost

    return {
        "id": "chatcmpl-mock-" + secrets.token_hex(6),
        "object": "chat.completion",
        "model": model["id"],
        "choices": [{"index": 0, "finish_reason": "stop",
                     "message": {"role": "assistant", "content": f"[mock] Response from {model['id']} ({model['platform']})."}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": MOCK_COMPLETION_TOKENS,
                  "total_tokens": prompt_tokens + MOCK_COMPLETION_TOKENS},
        "x_cost_usd": round(cost, 6),
        "x_budget_remaining_usd": round(project["budget"] - project["spent"], 6),
    }


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
