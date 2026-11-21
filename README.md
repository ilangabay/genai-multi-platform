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

Configuration is by environment variables. Copy `.env.example` to `.env` (gitignored), edit it, then
`uv run --env-file .env uvicorn app.main:app --port 8000`.

| Env var                  | Default                                   | Purpose                                                        |
|--------------------------|-------------------------------------------|----------------------------------------------------------------|
| `ADMIN_PASSWORD`         | `admin`                                   | Admin password                                                 |
| `USER_PASSWORD`          | `user`                                    | Shared user password (to be replaced by SSO)                   |
| `GATEWAY_USERS`          | `alice,bob,carol`                         | Usernames allowed to sign in                                   |
| `LITELLM_BASE_URL`       | `<this server>/v1`                        | Guardrail LiteLLM endpoint shown to users                      |
| `DEFAULT_PROJECT_BUDGET` | `100`                                     | USD budget for new projects and project requests               |
| `BEDROCK_REGION`         | `eu-west-3`                               | Region of the in-region Bedrock models and of their prices     |
| `PRICING_URL`            | `https://pricing.us-east-1.amazonaws.com` | AWS Price List Bulk API (public, needs a proxy allowlist entry) |
| `SYNC_PRICES`            | `0`                                       | `1` loads Bedrock prices from `PRICING_URL` at startup         |
| `ONPREM_API_BASE`        | `http://onprem-llm.internal:8000/v1`      | On-prem upstream in LiteLLM's model config                     |

## Features

**Users** (see only projects they own or are a member of)
- Home page: their projects and the list of available models with prices (on-prem models are free).
- Budget used and left (read-only).
- Endpoint and API key (show / copy).
- Per project, a chart of daily usage per model (spend or tokens, 7 / 30 / 90 days).
- Per project, a sample request per model (Chat Completions or Responses, curl or Python OpenAI SDK) plus a "Send mock request" button.
- Requests: ask for a new project, a budget increase or a new model, and follow their status.
- Feedback, and a Documentation tab.

**Admins**
- See all projects and create new ones (name, owner, members, budget; a key is generated).
- Change budgets, revoke and regenerate API keys.
- Usage page: totals and daily charts overall, per model and per project (spend or tokens, 7 / 30 / 90 days).
  Charts are plain inline SVG, no library or CDN, so they work in the air gap.
- Requests list, pending first, with a count in the sidebar. Approving a project request creates it (owner = requester);
  approving a budget request adds the amount. A model request is only recorded: the admin adds the model to LiteLLM.
- All user feedback. Documentation tab, with an admin section.

**Mock LLM endpoints**: `POST /v1/chat/completions` and `POST /v1/responses` (OpenAI formats, both exposed by LiteLLM).
They live in `app/litellm_mock.py`, a stand-in for the LiteLLM proxy that behaves like it:

- Each project is a LiteLLM **team** with `max_budget`; its API key is a virtual key of that team.
  The governance layer only stores owner/members and reads budget and spend back from LiteLLM.
- Cost = provider-reported usage (input, output, cache-read, cache-write tokens) × per-token prices
  from LiteLLM's model config. It is returned in the `x-litellm-response-cost` header and added to the team spend.
  On-prem models have zero prices, so they never use budget.
- Budget is checked before each call, so the last call can overshoot by one call.
  Then all calls of the team get `400 budget_exceeded`, on-prem included (LiteLLM checks the team budget before routing).
  A revoked key gets `401`.

- Every call writes a spend log row (time, team, model, spend, tokens), like LiteLLM's `LiteLLM_SpendLogs`.
  `GET /api/usage?days=N` sums them per day, project and model, limited to the projects the caller can see.
  The seeded projects get 90 days of fake history that adds up to their spend.

In production, `app/litellm_mock.py` is replaced by HTTP calls to the LiteLLM admin API
(`/team/new`, `/team/update`, `/team/info`, `/key/generate`, `/key/delete`, `/model/info`, `/spend/logs`) with the master key.
At volume, usage should come from LiteLLM's daily spend tables rather than summing per-call rows.

## Bedrock prices

`app/pricing.py` reads in-region on-demand token prices (input, output, cache read, cache write) from the
public AWS Price List Bulk API: `AmazonBedrockFoundationModels/current/<BEDROCK_REGION>/index.json`.
No AWS credentials or IAM permission are needed; the proxy must allow the host in `PRICING_URL`.
The built-in prices are the eu-west-3 rates of the 2026-11-11 price file.

```bash
uv run python -m app.pricing          # print current prices for BEDROCK_REGION
SYNC_PRICES=1 uv run uvicorn app.main:app --port 8000   # load them at startup
```

Each Bedrock model maps to its price-file name through `model_info.aws_model` (for example `Claude Sonnet 5.5`).
Only `*_standard` rows are used: `*_global_standard` is global cross-region inference, `*_long_ctx_*` and `*_1h_*`
are long-context and 1-hour-cache rates. These are list prices; Savings Plans and private discounts are not included,
so the AWS bill stays the reference.

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $GATEWAY_API_KEY" -H "Content-Type: application/json" \
  -d '{"model": "claude-sonnet-5-5", "messages": [{"role": "user", "content": "Hello"}]}'
```

The `Demo sandbox` project (owner `alice`) has a $0.02 budget so you hit the limit after two calls.

## Layout

| Path                    | What                                          |
|-------------------------|-----------------------------------------------|
| `app/main.py`           | FastAPI: auth, projects, admin API            |
| `app/litellm_mock.py`   | LiteLLM stand-in: teams, keys, budgets, LLM endpoints |
| `app/pricing.py`        | Bedrock prices from the AWS Price List Bulk API |
| `app/static/index.html` | Whole UI (plain HTML/JS, no build step)       |
| `tests/test_api.py`     | API tests                                     |

## Tests

```bash
uv run pytest -q
```

## Not done yet

- Persistence (PostgreSQL), SSO login, wiring keys and budgets to LiteLLM's own key/team API.
- Notifications for requests (email or Teams); editing project members after creation.
- Anthropic-format `/v1/messages` in the mock (LiteLLM supports it in production).
- A daily price sync job pushing prices to LiteLLM's `/model/update` (today: at startup, into the mock).
- Bedrock model IDs (`anthropic.claude-*-5-5`) are placeholders: check the exact in-region IDs in the Bedrock console.
