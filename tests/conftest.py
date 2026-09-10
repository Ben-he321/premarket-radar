"""All tests are OFFLINE MOCK tests. Never write to the research directory."""
import pytest
import requests


@pytest.fixture(autouse=True)
def offline_only(monkeypatch, tmp_path):
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
    monkeypatch.setenv("ALPACA_DATA_DIR", str(tmp_path / "mock-only"))
    monkeypatch.setenv("ALPACA_SECRETS_FILE", str(tmp_path / "no-secrets.toml"))
    monkeypatch.setenv("WATCHLIST_RUN_DIR", str(tmp_path / "watchlist-mock-only"))
    from src.data.alpaca_rate import SharedRateLimiter
    monkeypatch.setattr(SharedRateLimiter, 'acquire', lambda self: None)

    def forbidden(*args, **kwargs):
        raise AssertionError("Live HTTP is forbidden in offline tests")
    monkeypatch.setattr(requests.Session, "request", forbidden)
