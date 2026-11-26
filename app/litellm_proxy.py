"""Client for the real LiteLLM proxy admin API, used when LITELLM_URL is set (see litellm/ and compose.yaml).

Same functions as app/litellm_mock.py, so the governance layer does not care which one it talks to.
Each project is a LiteLLM team (max_budget, models); its API key is a virtual key of that team.
LiteLLM prices every call with the prices in litellm/config.yaml and keeps the spend.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException

from app import pricing  # noqa: F401  main reads litellm.pricing.BEDROCK_REGION

URL = os.environ.get("LITELLM_URL", "").rstrip("/")  # how this app reaches LiteLLM, e.g. http://litellm:4000
MASTER_KEY = os.environ.get("LITELLM_MASTER_KEY", "")
router = APIRouter()  # the LLM endpoints are LiteLLM's own: nothing to serve here
NO_MODELS = "no-default-models"  # an empty team model list means every model in LiteLLM: "none" is this name alone


def _team_models(models: list[str]) -> list[str]:
    return list(models) or [NO_MODELS]


def _call(method: str, path: str, body: dict | None = None, **params) -> dict:
    query = "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}) if params else ""
    req = urllib.request.Request(URL + path + query, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": f"Bearer {MASTER_KEY}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise HTTPException(502, f"LiteLLM {path} returned {e.code}: {e.read().decode(errors='replace')[:300]}")
    except OSError as e:  # connection refused, DNS, timeout
        raise HTTPException(502, f"LiteLLM unreachable at {URL}: {e}")


def startup():
    if not URL or not MASTER_KEY:
        raise RuntimeError("LITELLM_URL and LITELLM_MASTER_KEY are both needed to use the LiteLLM proxy")


def model_info() -> list[dict]:
    return _call("GET", "/model/info")["data"]


def model_add(model_name: str, litellm_params: dict, model_info: dict) -> dict:
    """Needs STORE_MODEL_IN_DB=True on LiteLLM: the model is kept in its database, next to the config ones."""
    return _call("POST", "/model/new", {"model_name": model_name, "litellm_params": litellm_params, "model_info": model_info})


def model_delete(model_id: str):
    _call("POST", "/model/delete", {"id": model_id})


def test_connection(litellm_params: dict) -> dict:
    """One short chat request with these settings, without adding the model (costs a fraction of a cent)."""
    r = _call("POST", "/health/test_connection", {"litellm_params": litellm_params, "mode": "chat"})
    if r.get("status") == "success":
        return {"ok": True, "message": "The model answered."}
    error = r.get("result", {}).get("error") or r
    return {"ok": False, "message": str(error)[:500]}


def team_new(team_alias: str, max_budget: float, models: list[str]) -> dict:
    return _call("POST", "/team/new", {"team_alias": team_alias, "max_budget": max_budget, "models": _team_models(models)})


def team_update(team_id: str, max_budget: float | None = None, models: list[str] | None = None) -> dict:
    changes = {"max_budget": max_budget} if max_budget is not None else {}
    if models is not None:
        changes["models"] = _team_models(models)
    return _call("POST", "/team/update", {"team_id": team_id, **changes})


def team_info(team_id: str) -> dict:
    # shortcut: one HTTP call per project on every project list; use /team/list if there are hundreds of projects
    team = _call("GET", "/team/info", team_id=team_id)["team_info"]
    return {**team, "models": [m for m in team["models"] if m != NO_MODELS]}


def key_generate(team_id: str) -> dict:
    return _call("POST", "/key/generate", {"team_id": team_id})


def key_delete(keys: list[str]):
    _call("POST", "/key/delete", {"keys": list(keys)})


def spend_logs(start_date: str, team_ids: list[str]) -> list[dict]:
    """Daily rows per team and model since start_date, from LiteLLM's daily team spend table.
    That table is filled in batches, so the latest calls show up after about a minute."""
    rows = []
    for team_id in team_ids:  # one team at a time: the breakdown is per model within the filtered team
        page = 1
        while True:
            r = _call("GET", "/team/daily/activity", team_ids=team_id, start_date=start_date,
                      end_date=str(datetime.now(timezone.utc).date()), page=page, page_size=100)
            for day in r["results"]:
                for model, m in day["breakdown"]["model_groups"].items():
                    rows.append({"startTime": str(day["date"]), "team_id": team_id, "model_group": model,
                                 "spend": m["metrics"]["spend"], "total_tokens": m["metrics"]["total_tokens"]})
            if not r["metadata"].get("has_more"):
                break
            page += 1
    return rows
