# genai-multi-platform

Software layer of the gateway VM between the air-gapped environment and cloud LLM APIs (Amazon Bedrock) / on-prem models.
This repo currently holds the **governance layer** (projects, owners, budgets, API keys) with its web UI.
Everything is mocked: no LLM is called, data lives in memory and resets on restart. Guardrails are out of scope (another team).

## Run

```bash
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

Open http://localhost:8000.

| Role  | Username                     | Password (env var)            |
|-------|------------------------------|-------------------------------|
| Admin | any                          | `admin` (`ADMIN_PASSWORD`)    |
| User  | `alice`, `bob` or `carol`    | `user` (`USER_PASSWORD`)      |

Set `GATEWAY_URL` to change the endpoint shown to users (defaults to `<this server>/v1`).

## What it does

- **Users** see only projects they own or are a member of: budget usage, endpoint, API key, model catalog (Bedrock + on-prem), and a "test call" button.
- **Admins** see all projects, change budgets, revoke and regenerate API keys.
- **Mock LLM endpoint** `POST /v1/chat/completions` (OpenAI shape). Each call charges the project budget; once the budget is spent it returns `402 budget_exceeded`.

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer <project api key>" -H "Content-Type: application/json" \
  -d '{"model": "claude-sonnet-5-5", "messages": [{"role": "user", "content": "Hello"}]}'
```

The `Demo sandbox` project (owner `alice`) has a $0.02 budget so you can hit the limit after a few calls.

## Tests

```bash
python -m pytest -q
```
