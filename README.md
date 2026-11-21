# genai-multi-platform

Software layer of the gateway VM between the air-gapped environment and cloud LLM APIs (Amazon Bedrock) / on-prem models.

```
apps (air-gap) ──► guardrail LiteLLM endpoint ──► LiteLLM router ──► on-prem models (GPU)
                                                               └──► Amazon Bedrock (EU, HTTPS 443 only)
                         ▲
              governance layer (this repo): projects, owners, budgets, API keys, web UI
```

This repo currently holds the **governance layer** and its web UI.
Everything is mocked: no LLM is called, data lives in memory and resets on restart.
Guardrails are built by another team; the mock endpoint stands in for both LiteLLM endpoints.

## Run

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run uvicorn app.main:app --port 8000
```

Open http://localhost:8000 and pick **Sign in as user** or **Sign in as admin**.

| Role  | Username                  | Password (env var)         |
|-------|---------------------------|----------------------------|
| Admin | none                      | `admin` (`ADMIN_PASSWORD`) |
| User  | `alice`, `bob` or `carol` | `user` (`USER_PASSWORD`)   |

| Env var            | Default             | Purpose                                                 |
|--------------------|---------------------|---------------------------------------------------------|
| `ADMIN_PASSWORD`   | `admin`             | Admin password                                          |
| `USER_PASSWORD`    | `user`              | Shared user password (to be replaced by SSO)            |
| `LITELLM_BASE_URL` | `<this server>/v1`  | Guardrail LiteLLM endpoint shown to users               |

## Features

**Users** (see only projects they own or are a member of)
- Budget used and left (read-only).
- Endpoint and API key (show / copy).
- Model catalog (Bedrock and on-prem) with prices, and a sample request per model (curl or Python OpenAI SDK) plus a "Send mock request" button.
- Documentation tab.

**Admins**
- See all projects and create new ones (name, owner, members, budget; a key is generated).
- Change budgets, revoke and regenerate API keys.
- Documentation tab, with an admin section.

**Mock LLM endpoint**: `POST /v1/chat/completions` (OpenAI format, like LiteLLM).
Each call charges the project budget by tokens; once the budget is spent it returns `402 budget_exceeded`.
A revoked key returns `401 invalid_api_key`, an unknown model `404 model_not_found`.

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $GATEWAY_API_KEY" -H "Content-Type: application/json" \
  -d '{"model": "claude-sonnet-5-5", "messages": [{"role": "user", "content": "Hello"}]}'
```

The `Demo sandbox` project (owner `alice`) has a $0.02 budget so you hit the limit after two calls.

## Layout

| Path                    | What                                          |
|-------------------------|-----------------------------------------------|
| `app/main.py`           | FastAPI: auth, projects, admin API, mock LLM  |
| `app/static/index.html` | Whole UI (plain HTML/JS, no build step)       |
| `tests/test_api.py`     | API tests                                     |

## Tests

```bash
uv run pytest -q
```

## Not done yet

- Persistence (PostgreSQL), SSO login, wiring keys and budgets to LiteLLM's own key/team API.
- Anthropic-format `/v1/messages` in the mock (LiteLLM supports it in production).
- Model prices are indicative, not real Bedrock EU rates.
