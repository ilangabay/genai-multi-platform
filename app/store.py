"""Where the governance layer and the LiteLLM mock keep their state.

STORAGE=memory (default): nothing is persisted and demo data is seeded at every start. For local runs and tests.
STORAGE=postgres: every change is written to PostgreSQL (DATABASE_URL) and loaded back at startup.

Both modes keep the working set in process memory. Every change goes to the store first and to memory only
once the store accepted it (see update()), so a failed database write leaves memory as it was.
shortcut: one process only (uvicorn --workers 1); several workers would each hold their own copy,
move reads to SQL before scaling out.
Each table is (id text primary key, data jsonb, updated_at): a row holds the dict the app already uses.
Spend logs are the exception to "everything in memory": with a database they are queried by date (since()).
"""
import os

TABLES = ("users", "sessions", "projects", "requests", "feedback",
          "litellm_mock_teams", "litellm_mock_keys", "litellm_mock_spend_logs", "litellm_mock_models")  # mock_ tables go once LiteLLM is wired


class MemoryStore:
    persistent = False

    def load(self, table: str) -> dict[str, dict]:
        return {}

    def save(self, table: str, key: str, doc: dict):
        pass

    def delete(self, table: str, key: str):
        pass

    def since(self, table: str, field: str, value: str) -> list[dict]:
        return []


class PostgresStore:
    persistent = True

    def __init__(self, url: str):
        from psycopg_pool import ConnectionPool

        self.pool = ConnectionPool(url, min_size=1, max_size=4, kwargs={"autocommit": True}, open=True)
        self.pool.wait(timeout=15)  # fail at startup, not at the first request, if the database is unreachable
        with self.pool.connection() as c:
            for t in TABLES:
                c.execute(f"CREATE TABLE IF NOT EXISTS {t} "
                          "(id text PRIMARY KEY, data jsonb NOT NULL, updated_at timestamptz NOT NULL DEFAULT now())")
            c.execute("CREATE INDEX IF NOT EXISTS litellm_mock_spend_logs_time "
                      "ON litellm_mock_spend_logs ((data->>'startTime'))")

    def load(self, table: str) -> dict[str, dict]:
        with self.pool.connection() as c:
            return dict(c.execute(f"SELECT id, data FROM {_table(table)}").fetchall())

    def save(self, table: str, key: str, doc: dict):
        from psycopg.types.json import Jsonb

        with self.pool.connection() as c:
            c.execute(f"INSERT INTO {_table(table)} (id, data) VALUES (%s, %s) "
                      "ON CONFLICT (id) DO UPDATE SET data = EXCLUDED.data, updated_at = now()", (str(key), Jsonb(doc)))

    def delete(self, table: str, key: str):
        with self.pool.connection() as c:
            c.execute(f"DELETE FROM {_table(table)} WHERE id = %s", (str(key),))

    def since(self, table: str, field: str, value: str) -> list[dict]:
        with self.pool.connection() as c:
            rows = c.execute(f"SELECT data FROM {_table(table)} WHERE data->>%s >= %s ORDER BY data->>%s",
                             (field, value, field)).fetchall()
        return [r[0] for r in rows]


def _table(name: str) -> str:
    assert name in TABLES, name  # table names are never user input; this keeps it that way
    return name


def make_store(storage: str | None = None, url: str | None = None):
    storage = storage or os.environ.get("STORAGE", "memory")
    if storage == "memory":
        return MemoryStore()
    if storage == "postgres":
        url = url or os.environ.get("DATABASE_URL")
        if not url:
            raise RuntimeError("STORAGE=postgres needs DATABASE_URL, e.g. postgresql://user:password@host:5432/gateway")
        return PostgresStore(url)
    raise RuntimeError(f"STORAGE must be 'memory' or 'postgres', not {storage!r}")


STORE = MemoryStore()  # replaced by init() at app startup; read it as store.STORE, never import the name


def init():
    global STORE
    STORE = make_store()


def update(table: str, key, doc: dict, **changes) -> dict:
    """Store first, then memory: if the write fails, the in-memory doc is left as it was."""
    STORE.save(table, key, {**doc, **changes})
    doc.update(changes)
    return doc
