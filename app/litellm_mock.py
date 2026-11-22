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
import logging
import os
import random
import secrets
import threading
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Body, Header
from fastapi.responses import JSONResponse

from app import pricing, store

M = 1_000_000
ONPREM_API_BASE = os.environ.get("ONPREM_API_BASE", "http://onprem-llm.internal:8000/v1")


def _model(name, platform, params, inp=0.0, out=0.0, cache_read=None, cache_write=None, aws_model=None):
    """Prices in USD per 1M tokens -> LiteLLM model_list entry (per-token costs)."""
    info = {"platform": platform, "input_cost_per_token": inp / M, "output_cost_per_token": out / M}
    if cache_read is not None:
        info |= {"cache_read_input_token_cost": cache_read / M, "cache_creation_input_token_cost": cache_write / M}
    if aws_model:
        info["aws_model"] = aws_model  # model name in the AWS price file
    return {"model_name": name, "litellm_params": params, "model_info": info}


def _bedrock(name, model_id, aws_model, *prices):
    # regional (in-region) model IDs, not eu.* cross-region inference profiles
    return _model(name, "Bedrock", {"model": f"bedrock/{model_id}", "aws_region_name": pricing.BEDROCK_REGION},
                  *prices, aws_model=aws_model)


# Bedrock: regional on-demand prices for eu-west-3 from the AWS price file of 2026-11-11, replaced at
# startup when SYNC_PRICES=1. On-prem models are free: zero prices, so calls never touch the budget.
MODEL_LIST = [
    _bedrock("claude-opus-5-5", "anthropic.claude-opus-5-5", "Claude Opus 5.5", 4.4, 22.0, 0.22, 5.5),
    _bedrock("claude-sonnet-5-5", "anthropic.claude-sonnet-5-5", "Claude Sonnet 5.5", 2.2, 11.0, 0.11, 2.75),
    _bedrock("claude-haiku-5-5", "anthropic.claude-haiku-5-5", "Claude Haiku 5.5", 0.11, 0.55, 0.011, 0.1375),
    _model("mistral-small-onprem", "On-prem", {"model": "hosted_vllm/mistral-small", "api_base": ONPREM_API_BASE}),
    _model("llama-70b-onprem", "On-prem", {"model": "hosted_vllm/llama-3.3-70b", "api_base": ONPREM_API_BASE}),
]


def sync_prices(prices: dict[str, dict]) -> list[str]:
    """Load AWS prices (USD per 1M tokens, from app.pricing) into the model config. Returns the models updated.
    Production: the same job calls LiteLLM's /model/update instead."""
    fields = {"input": "input_cost_per_token", "output": "output_cost_per_token",
              "cache_read": "cache_read_input_token_cost", "cache_write": "cache_creation_input_token_cost"}
    updated = []
    for m in MODEL_LIST:
        p = prices.get(m["model_info"].get("aws_model"), {})
        if {"input", "output"} <= p.keys():  # skip models missing from the price file, keep their old prices
            m["model_info"] |= {fields[k]: v / M for k, v in p.items() if k in fields}
            updated.append(m["model_name"])
    return updated


MODELS = {m["model_name"]: m for m in MODEL_LIST}

TEAMS: dict[str, dict] = {}  # team_id -> {team_alias, max_budget, spend, models}
KEYS: dict[str, str] = {}    # virtual key -> team_id
SPEND_LOGS: list[dict] = []  # one row per call, like LiteLLM_SpendLogs. Memory mode only: with a database, queried by date
LOCK = threading.Lock()


def startup():
    """Load state from the store; called by the app at startup (main.startup)."""
    if os.environ.get("SYNC_PRICES") == "1":
        try:
            logging.getLogger(__name__).warning("Bedrock prices synced for %s", sync_prices(pricing.fetch()))
        except (OSError, ValueError) as e:  # keep serving with the built-in prices
            logging.getLogger(__name__).error("Price sync failed, using built-in prices: %s", e)
    TEAMS.clear()
    TEAMS.update(store.STORE.load("litellm_mock_teams"))
    KEYS.clear()
    KEYS.update({k: v["team_id"] for k, v in store.STORE.load("litellm_mock_keys").items()})
    SPEND_LOGS.clear()


# ---------- admin API (called by the governance layer) ----------

def model_info() -> list[dict]:
    return MODEL_LIST


def team_new(team_alias: str, max_budget: float, models: list[str]) -> dict:
    """models: the team's allowed model names. Like LiteLLM, an empty list means every model."""
    team_id = str(uuid.uuid4())
    team = {"team_id": team_id, "team_alias": team_alias, "max_budget": max_budget, "spend": 0.0, "models": list(models)}
    store.STORE.save("litellm_mock_teams", team_id, team)
    TEAMS[team_id] = team
    return team


def team_update(team_id: str, max_budget: float | None = None, models: list[str] | None = None) -> dict:
    changes = {k: v for k, v in (("max_budget", max_budget), ("models", models and list(models))) if v is not None}
    with LOCK:
        return store.update("litellm_mock_teams", team_id, TEAMS[team_id], **changes)


def team_info(team_id: str) -> dict:
    return TEAMS[team_id]


def key_generate(team_id: str) -> dict:
    key = "sk-" + secrets.token_urlsafe(24)
    store.STORE.save("litellm_mock_keys", key, {"team_id": team_id})
    KEYS[key] = team_id
    return {"key": key, "team_id": team_id}


def spend_logs(start_date: str) -> list[dict]:
    """Rows since start_date (YYYY-MM-DD, UTC). Production: LiteLLM's /spend/logs, or a query on its spend tables."""
    if store.STORE.persistent:
        return store.STORE.since("litellm_mock_spend_logs", "startTime", start_date)
    return [r for r in SPEND_LOGS if r["startTime"][:10] >= start_date]


def seed_history(team_id: str, total_spend: float, days: int = 90, seed: int = 0):
    """Mock only: daily spend rows over the last `days` days, Bedrock rows adding up to total_spend."""
    rng = random.Random(seed)
    today = datetime.now(timezone.utc).date()
    weights = [rng.choice([0, 0.3, 1, 2]) for _ in MODEL_LIST]  # each team favors a few models
    rows = []
    for d in range(days):
        day = today - timedelta(days=days - 1 - d)
        load = (0.4 + d / days) * (0.25 if day.weekday() >= 5 else 1) * rng.uniform(0.6, 1.4)
        for m, w in zip(MODEL_LIST, weights):
            tokens = int(load * w * rng.uniform(0.5, 1.5) * 200_000)
            if tokens:
                i = m["model_info"]
                cost = tokens * (0.8 * i["input_cost_per_token"] + 0.2 * i["output_cost_per_token"])
                rows.append({"id": uuid.uuid4().hex, "startTime": f"{day}T12:00:00+00:00", "team_id": team_id, "model_group": m["model_name"],
                             "spend": cost, "total_tokens": tokens})
    raw = sum(r["spend"] for r in rows)
    for r in rows:  # scale paid rows so the history matches the seeded spend
        if raw and r["spend"]:
            r["spend"] *= total_spend / raw
            r["total_tokens"] = int(r["total_tokens"] * total_spend / raw)
    for r in rows:
        store.STORE.save("litellm_mock_spend_logs", r["id"], r)
    if not store.STORE.persistent:
        SPEND_LOGS.extend(rows)
    store.update("litellm_mock_teams", team_id, TEAMS[team_id], spend=sum(r["spend"] for r in rows) if raw else total_spend)


def key_delete(keys: list[str]) -> dict:
    for k in keys:
        store.STORE.delete("litellm_mock_keys", k)
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
    if team["models"] and model_name not in team["models"]:
        return _error(401, f"Team={team['team_alias']} not allowed to access model={model_name}. "
                           f"Allowed team models = {team['models']}", "team_model_access_denied")
    # Like LiteLLM: checked before the call, charged after it, so the last call can overshoot the budget.
    if team["spend"] >= team["max_budget"]:
        return _error(400, f"Budget has been exceeded! Team={team['team_alias']} "
                           f"Current cost: {team['spend']:.6f}, Max budget: {team['max_budget']}", "budget_exceeded")
    usage = _provider_usage(payload)
    cost = _cost(model["model_info"], usage)
    log = {"id": uuid.uuid4().hex, "startTime": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "team_id": team_id, "model_group": model_name, "spend": cost, "total_tokens": sum(usage.values())}
    with LOCK:  # spend first: if the log write fails, the budget is still right and only the chart misses a call
        store.update("litellm_mock_teams", team_id, team, spend=team["spend"] + cost)
        store.STORE.save("litellm_mock_spend_logs", log["id"], log)
        if not store.STORE.persistent:
            SPEND_LOGS.append(log)
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
