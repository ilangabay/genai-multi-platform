# genai-multi-platform

Software layer of the gateway VM between the air-gapped environment and cloud LLM APIs (Amazon Bedrock) / on-prem models.

```
apps (air-gap) ──► guardrail LiteLLM endpoint ──► LiteLLM router ──► on-prem models (GPU)
                                                               └──► Amazon Bedrock (EU, HTTPS 443 only)
                         ▲
              governance layer (this repo): projects, owners, budgets, API keys, web UI
```

This repo currently holds the **governance layer** and its web UI.
Two ways to run it:
- **Local** (`uv run`, no Docker): an in-process stand-in replaces LiteLLM, no LLM is called, demo data is created.
- **Deployment** (`docker compose`): the real LiteLLM proxy calls Amazon Bedrock in eu-west-3 through STS roles
  (IAM Roles Anywhere, no stored AWS key), state in PostgreSQL. See [Docker](#docker) and [docs/aws-setup.md](docs/aws-setup.md).

Guardrails are built by another team; one LiteLLM instance stands in for both LiteLLM endpoints for now.

## Run

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run uvicorn app.main:app --port 8000
```

Open http://localhost:8000 and pick **Sign in as user** or **Sign in as admin**.
Locally this runs with `STORAGE=memory`: demo users and projects are created at every start.

| Role  | Username                  | Password (env var)         |
|-------|---------------------------|----------------------------|
| Admin | `admin`                   | `admin` (`ADMIN_PASSWORD`) |
| User  | `alice`, `bob` or `carol` | `user` (`USER_PASSWORD`)   |

Configuration is by environment variables. Copy `.env.example` to `.env` (gitignored), edit it, then
`uv run --env-file .env uvicorn app.main:app --port 8000`.

| Env var                  | Default                                   | Purpose                                                        |
|--------------------------|-------------------------------------------|----------------------------------------------------------------|
| `STORAGE`                | `memory`                                  | `memory` (local, nothing kept) or `postgres` (deployments)     |
| `DATABASE_URL`           | none                                      | Required with `postgres`: `postgresql://user:password@host:5432/db` |
| `ADMIN_PASSWORD`         | `admin`                                   | First password of the bootstrap admin account `admin`; with `postgres`, 12+ characters and not a default |
| `SESSION_HOURS`          | `12`                                      | Sign-ins expire after this many hours                          |
| `COOKIE_SECURE`          | `0`                                       | `1` sends the session cookie over HTTPS only (behind TLS)      |
| `USER_PASSWORD`          | `user`                                    | First password of the `GATEWAY_USERS` demo accounts            |
| `GATEWAY_USERS`          | `alice,bob,carol`                         | Demo user accounts created at startup                          |
| `LITELLM_URL`            | none                                      | Set: use the real LiteLLM proxy at this address (needs `postgres`). Unset: the stand-in |
| `LITELLM_MASTER_KEY`     | none                                      | LiteLLM admin key, with `LITELLM_URL`                          |
| `LITELLM_BASE_URL`       | `<this server>/v1`                        | LLM endpoint shown to users (LiteLLM as users reach it)        |
| `BEDROCK_ROLE_ARN`       | none                                      | Role LiteLLM assumes through STS for Bedrock (LiteLLM container) |
| `DEFAULT_PROJECT_BUDGET` | `100`                                     | USD budget for new projects and project requests               |
| `BEDROCK_REGION`         | `eu-west-3`                               | Region of the in-region Bedrock models and of their prices     |
| `PRICING_URL`            | `https://pricing.us-east-1.amazonaws.com` | AWS Price List Bulk API (public, needs a proxy allowlist entry) |
| `SYNC_PRICES`            | `0`                                       | `1` loads Bedrock prices from `PRICING_URL` at startup         |
| `ONPREM_API_BASE`        | `http://onprem-llm.internal:8000/v1`      | On-prem upstream in LiteLLM's model config                     |

## Features

**Users** (see only projects they own or are a member of)
- Home page: their projects and the list of available models with prices (on-prem models are free).
- A project page has three tabs: Overview (budget, endpoint and key, usage chart), Models (samples) and Members.
- Project owners add members by username, remove them and switch them between owner and member
  (a project keeps at least one owner). Members see the list read-only. Admins manage every project.
- Budget used and left (read-only).
- Endpoint and API key (show / copy).
- Per project, a chart of daily usage per model (spend or tokens, 7 / 30 / 90 days).
- Per project, a sample request per model (Chat Completions or Responses, curl or Python OpenAI SDK) plus a "Send test request" button.
- Requests: ask for a new project (with its models, picked from the platform catalog), a budget increase or a model
  for a project, and follow their status.
- Feedback, and a Documentation tab.

**Admins**
- See all projects and create new ones (name, owner, members, budget; a key is generated).
- Change budgets, revoke and regenerate API keys.
- Catalog page: the platform's models. Add a Bedrock model by model ID or ARN (inference profiles too): **Look up**
  pre-fills a name and LiteLLM's list prices, the admin checks them and saves (LiteLLM `/model/new`). Added models can
  be removed, which also takes them off every project. Models from `litellm/config.yaml` stay until removed there.
- Each project has a whitelist of catalog models (LiteLLM team `models`): picked at creation or later in its Models tab,
  **none by default**. Any other model gets `401 team_model_access_denied`.
  Calls to a model that is not enabled get `401 team_model_access_denied`.
- Usage page: totals and daily charts overall, per model and per project (spend or tokens, 7 / 30 / 90 days).
  Charts are plain inline SVG, no library or CDN, so they work in the air gap.
- Requests list, pending first, with a count in the sidebar. Approving a project request creates it (owner = requester);
  approving a budget request adds the amount. Approving a request for a catalog model enables it on the project;
  a model that is not in the catalog is only recorded (the admin adds it to LiteLLM's config).
- Users tab: add users (user or admin) with a generated temporary password, shown once with a ready-to-send message;
  reset passwords (signs the user out); switch any account between user and admin (at least one admin stays).
  Passwords are scrypt-hashed; a temporary password must be changed at first sign-in, enforced by the API.
- All user feedback. Documentation tab, with an admin section.

**LLM endpoints**: `POST /v1/chat/completions` and `POST /v1/responses` (OpenAI formats), served by LiteLLM.
Locally they live in `app/litellm_mock.py`, a stand-in for the LiteLLM proxy that behaves like it:

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

With `LITELLM_URL` set, `app/litellm_proxy.py` makes the same calls over HTTP to the real LiteLLM admin API with the
master key: `/team/new`, `/team/update`, `/team/info`, `/key/generate`, `/key/delete`, `/model/info`, and
`/team/daily/activity` for the charts (LiteLLM's daily spend table, filled in batches: about a minute behind).

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

## Storage

| `STORAGE`  | Use                 | Behaviour |
|------------|---------------------|-----------|
| `memory`   | Local runs, tests   | Nothing is persisted. Demo users, projects and 90 days of usage are created at every start. |
| `postgres` | Deployments         | Every change is written to PostgreSQL and loaded back at startup. No demo data: only the account `admin` is created on the first start, with `ADMIN_PASSWORD` (12+ characters, defaults refused), which must be changed at first sign-in. |

`postgres` needs `DATABASE_URL` (user and password included); the app refuses to start without it, or if the
database cannot be reached within 15 seconds. Tables are created on first start, one per kind of record:
`users`, `sessions`, `projects`, `requests`, `feedback`, and `litellm_mock_teams`, `litellm_mock_keys`,
`litellm_mock_spend_logs` for the LiteLLM stand-in (these go away once the real LiteLLM, with its own database, is wired).
Each row is `(id, data jsonb, updated_at)`. Passwords are scrypt hashes and session tokens are stored as SHA-256 hashes.

Run **one process** (`uvicorn` without `--workers`): the app keeps the data in memory and writes each change
to the database first (memory changes only once the write succeeded), so several processes would each have their own copy.
Usage history is the exception: it is not kept in memory but queried by date range when the Usage charts load.
All of this happens at app startup (FastAPI lifespan), not at import.

```bash
STORAGE=postgres DATABASE_URL=postgresql://gateway:secret@db.internal:5432/gateway uv run uvicorn app.main:app --port 8000
```

## Docker

| File                 | Image                | What |
|----------------------|----------------------|------|
| `Dockerfile`         | `genai-gateway`      | The app: Python 3.12 slim, dependencies from `uv.lock` (no dev tools), non-root user, one uvicorn process on port 8000, healthcheck on `/`. Defaults to `STORAGE=memory`. |
| `postgres/Dockerfile`| `genai-gateway-db`   | PostgreSQL 17 with role `gateway`, databases `gateway` (app) and `litellm` (LiteLLM), healthcheck with `pg_isready`. The password is given at run time, never built in. |
| `litellm/Dockerfile` | `genai-gateway-litellm` | LiteLLM proxy `v1.108.7` with `litellm/config.yaml` (models, eu-west-3 prices) and `aws_signing_helper` for IAM Roles Anywhere. Port 4000. |
| `compose.yaml`       |                      | All three: the app in `postgres` mode driving LiteLLM, the database on a named volume (`pgdata`) and not published outside. |

Before the first start, set up AWS with [docs/aws-setup.md](docs/aws-setup.md): it ends with `certs/gateway.crt`,
`certs/gateway.key`, `litellm/aws-config` and these in `.env`:
`POSTGRES_PASSWORD` (letters and digits), `ADMIN_PASSWORD`, `LITELLM_MASTER_KEY`, `BEDROCK_ROLE_ARN`, `LITELLM_BASE_URL`.

```bash
docker compose up --build -d
```

Then open http://localhost:8000 and sign in as admin with `ADMIN_PASSWORD`; it must be changed at first sign-in.
Projects get real LiteLLM keys for `LITELLM_BASE_URL` (port 4000): every call goes to Bedrock and is billed to the project.
Compose refuses to start while a required value is missing. LiteLLM's own admin UI is on http://localhost:4000/ui (master key).

For the air-gapped VM, build the images on a connected machine (they pull from Docker Hub, ghcr.io and PyPI)
and push them to the internal registry, or `docker save` / `docker load` them.

## Layout

| Path                    | What                                          |
|-------------------------|-----------------------------------------------|
| `app/main.py`           | FastAPI: auth, users, projects, requests, admin API |
| `app/store.py`          | Storage: `memory` or `postgres`               |
| `app/litellm_mock.py`   | LiteLLM stand-in: teams, keys, budgets, LLM endpoints (local runs, tests) |
| `app/litellm_proxy.py`  | Client for the real LiteLLM admin API (`LITELLM_URL`) |
| `litellm/`              | LiteLLM image, config and AWS config template |
| `docs/aws-setup.md`     | AWS: Roles Anywhere, the two roles, policies, checks |
| `app/pricing.py`        | Bedrock prices from the AWS Price List Bulk API |
| `app/static/index.html` | Whole UI (plain HTML/JS, no build step)       |
| `tests/test_api.py`     | API tests (memory)                            |
| `tests/test_postgres.py`| Restart test against PostgreSQL, skipped unless `TEST_DATABASE_URL` is set |

## Tests

```bash
uv run pytest -q
TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres uv run pytest -q   # adds the PostgreSQL test
```

The PostgreSQL test works in a throwaway schema that it drops afterwards.

## Not done yet

- SSO login, login rate limiting, disabling users.
- The guardrail LiteLLM endpoint: one LiteLLM instance does everything for now.
- Several app processes against one database (today: one process, see [Storage](#storage)); schema migrations.
- Project API keys are stored in clear so the UI can show them again; real LiteLLM keeps only hashes and shows a key once.
- Notifications for requests (email or Teams).
- Anthropic-format `/v1/messages` in the mock (LiteLLM supports it in production).
- A price sync for the real LiteLLM: prices are in `litellm/config.yaml`, update them by hand (`python -m app.pricing` prints them).
- The deployment with the real LiteLLM has not been run end to end yet (Docker, AWS account).
