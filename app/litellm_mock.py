"""Stand-in for the LiteLLM proxy (guardrail endpoint + router). Mock only, nothing leaves the process.

The governance layer owns projects; LiteLLM owns money. Each project is a LiteLLM team with
max_budget; its virtual key belongs to that team. LiteLLM prices every call from the usage the
provider returns (Bedrock reports input, output, cache-read and cache-write tokens) times the
per-token prices in its model config, adds it to the team spend, and rejects calls once
spend >= max_budget.

The functions below are named after the LiteLLM admin endpoints the governance layer will call
over HTTP with the master key in production (/team/new, /team/update, /team/info,
/key/generate, /key/delete, /model/info).
"""
import json
import secrets
import threading
import uuid

from fastapi import APIRouter, Body, Header
from fastapi.responses import JSONResponse

M = 1_000_000


def _model(name, platform, upstream, inp, out, cache_read=None, cache_write=None):
    """Prices in USD per 1M tokens -> LiteLLM model_list entry (per-token costs)."""
    info = {"platform": platform, "input_cost_per_token": inp / M, "output_cost_per_token": out / M}
    if cache_read is not None:
        info |= {"cache_read_input_token_cost": cache_read / M, "cache_creation_input_token_cost": cache_write / M}
    return {"model_name": name, "litellm_params": {"model": upstream}, "model_info": info}


# Indicative prices. In production they come from the AWS Price List API (Bedrock, per region)
# and from internal GPU chargeback (on-prem), loaded into LiteLLM's model config.
MODEL_LIST = [
    _model("claude-opus-5-5", "Bedrock", "bedrock/eu.anthropic.claude-opus-5-5", 4.0, 20.0, 0.4, 5.0),
    _model("claude-sonnet-5-5", "Bedrock", "bedrock/eu.anthropic.claude-sonnet-5-5", 2.0, 10.0, 0.2, 2.5),
    _model("claude-haiku-5-5", "Bedrock", "bedrock/eu.anthropic.claude-haiku-5-5", 0.1, 0.5, 0.01, 0.125),
    _model("mistral-small-onprem", "On-prem", "hosted_vllm/mistral-small", 0.05, 0.15),
    _model("llama-70b-onprem", "On-prem", "hosted_vllm/llama-3.3-70b", 0.2, 0.6),
]
MODELS = {m["model_name"]: m for m in MODEL_LIST}

TEAMS: dict[str, dict] = {}  # team_id -> {team_alias, max_budget, spend}
KEYS: dict[str, str] = {}    # virtual key -> team_id
LOCK = threading.Lock()


# ---------- admin API (called by the governance layer) ----------

def model_info() -> list[dict]:
    return MODEL_LIST


def team_new(team_alias: str, max_budget: float) -> dict:
    team_id = str(uuid.uuid4())
    TEAMS[team_id] = {"team_id": team_id, "team_alias": team_alias, "max_budget": max_budget, "spend": 0.0}
    return TEAMS[team_id]


def team_update(team_id: str, max_budget: float) -> dict:
    with LOCK:
        TEAMS[team_id]["max_budget"] = max_budget
    return TEAMS[team_id]


def team_info(team_id: str) -> dict:
    return TEAMS[team_id]


def key_generate(team_id: str) -> dict:
    key = "sk-" + secrets.token_urlsafe(24)
    KEYS[key] = team_id
    return {"key": key, "team_id": team_id}


def key_delete(keys: list[str]) -> dict:
    for k in keys:
        KEYS.pop(k, None)
    return {"deleted_keys": keys}


# ---------- LLM API (called by apps with their virtual key) ----------

router = APIRouter()
MOCK_OUTPUT_TOKENS = 500


def _error(status: int, message: str, type_: str) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": type_, "param": None, "code": str(status)}},
                        status_code=status)


def _provider_usage(payload) -> dict:
    # shortcut: fake provider. Real Bedrock returns these counts in its usage block; nothing is counted here.
    return {"input": max(1, len(json.dumps(payload)) // 4), "output": MOCK_OUTPUT_TOKENS, "cache_read": 0, "cache_write": 0}


def _cost(info: dict, u: dict) -> float:
    return (u["input"] * info["input_cost_per_token"] + u["output"] * info["output_cost_per_token"]
            + u["cache_read"] * info.get("cache_read_input_token_cost", 0)
            + u["cache_write"] * info.get("cache_creation_input_token_cost", 0))


def _call(authorization: str, model_name, payload):
    """Auth, pre-call budget check, fake provider call, post-call spend. Returns (model, usage, cost) or an error."""
    team_id = KEYS.get(authorization.removeprefix("Bearer ").strip())
    if not team_id:
        return _error(401, "Authentication Error, invalid or revoked API key.", "invalid_api_key")
    model = MODELS.get(model_name)
    if not model:
        return _error(400, f"Invalid model name passed in model={model_name}.", "invalid_request_error")
    team = TEAMS[team_id]
    # Like LiteLLM: checked before the call, charged after it, so the last call can overshoot the budget.
    if team["spend"] >= team["max_budget"]:
        return _error(400, f"Budget has been exceeded! Team={team['team_alias']} "
                           f"Current cost: {team['spend']:.6f}, Max budget: {team['max_budget']}", "budget_exceeded")
    usage = _provider_usage(payload)
    cost = _cost(model["model_info"], usage)
    with LOCK:
        team["spend"] += cost
    return model, usage, cost


def _ok(body: dict, cost: float) -> JSONResponse:
    return JSONResponse(body, headers={"x-litellm-response-cost": f"{cost:.8f}"})


@router.post("/v1/chat/completions")
def chat_completions(body: dict = Body(...), authorization: str = Header("")):
    r = _call(authorization, body.get("model"), body.get("messages", []))
    if isinstance(r, JSONResponse):
        return r
    model, u, cost = r
    return _ok({
        "id": "chatcmpl-mock-" + secrets.token_hex(6),
        "object": "chat.completion",
        "model": model["model_name"],
        "choices": [{"index": 0, "finish_reason": "stop", "message": {
            "role": "assistant", "content": f"[mock] Response from {model['model_name']} ({model['model_info']['platform']})."}}],
        "usage": {"prompt_tokens": u["input"] + u["cache_read"] + u["cache_write"], "completion_tokens": u["output"],
                  "total_tokens": u["input"] + u["cache_read"] + u["cache_write"] + u["output"],
                  "prompt_tokens_details": {"cached_tokens": u["cache_read"]},
                  "cache_read_input_tokens": u["cache_read"], "cache_creation_input_tokens": u["cache_write"]},
    }, cost)


@router.post("/v1/responses")
def responses(body: dict = Body(...), authorization: str = Header("")):
    r = _call(authorization, body.get("model"), [body.get("instructions", ""), body.get("input", "")])
    if isinstance(r, JSONResponse):
        return r
    model, u, cost = r
    text = f"[mock] Response from {model['model_name']} ({model['model_info']['platform']})."
    return _ok({
        "id": "resp_mock_" + secrets.token_hex(6),
        "object": "response",
        "status": "completed",
        "model": model["model_name"],
        "output": [{"type": "message", "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": text, "annotations": []}]}],
        "output_text": text,
        "usage": {"input_tokens": u["input"] + u["cache_read"] + u["cache_write"], "output_tokens": u["output"],
                  "total_tokens": u["input"] + u["cache_read"] + u["cache_write"] + u["output"],
                  "input_tokens_details": {"cached_tokens": u["cache_read"]}},
    }, cost)
