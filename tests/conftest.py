import pytest

from app import main


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    """Every test starts from the demo data, so tests cannot depend on each other's changes."""
    monkeypatch.delenv("STORAGE", raising=False)
    main.startup()
