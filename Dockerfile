# GenAI gateway governance layer (FastAPI). Build: docker build -t genai-gateway .
# Runs STORAGE=memory unless told otherwise; for deployments pass STORAGE=postgres and DATABASE_URL (see compose.yaml).

# ---- build: resolve dependencies from uv.lock into /app/.venv ----
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.10.3 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PYTHON=/usr/local/bin/python3.12
WORKDIR /app
# dependencies first, so code changes reuse this layer
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY app ./app

# ---- runtime: no uv, no build tools, non-root ----
FROM python:3.12-slim
RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin gateway
WORKDIR /app
COPY --from=build --chown=root:root /app /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
USER gateway
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/', timeout=4)"]
# one process on purpose: the app keeps its data in memory and writes through to PostgreSQL (see app/store.py)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
