"""Explicit MOCK fixtures for transport, quality, storage, and orchestration."""
from datetime import date, datetime, timezone
import io
import json
import zipfile
from unittest.mock import Mock

import pandas as pd
import pytest
import requests
from filelock import Timeout

from src.data.alpaca_calendar import finalized_day, request_bounds, sessions
from src.data.alpaca_client import AlpacaMarketData, DataAccessError, PacedStockClient, normalize_bars
from src.data.alpaca_config import DataConfig, FUTURE_INITIAL_CAPITAL_USD, REFERENCES, SYMBOLS, TRADE_UNIVERSE, load_config
from src.data.alpaca_quality import inspect_quality
from src.data.alpaca_store import ParquetStore
from src.data.alpaca_sync import DataSync


def mock_bar(day="2024-03-08", price=100, volume=1000):
    start, _ = request_bounds(date.fromisoformat(day), date.fromisoformat(day))
    return {"t": start.isoformat(), "o": price, "h": price + 2, "l": price - 2,
            "c": price + 1, "v": volume, "n": 50, "vw": price + .5}


def response(payload, status=200):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(payload).encode()
    result.url = "https://data.alpaca.markets/v2/stocks/bars"
    return result


def test_config_and_fixed_scope(monkeypatch):
    assert len(TRADE_UNIVERSE) == 15 and len(REFERENCES) == 3
    assert not set(TRADE_UNIVERSE) & set(REFERENCES)
    assert FUTURE_INITIAL_CAPITAL_USD == 5500
    monkeypatch.setenv("ALPACA_API_KEY", "mock-env-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "mock-secret")
    config = load_config({"ALPACA_API_KEY": "mock-secrets-key"})
    assert config.api_key == "mock-secrets-key" and config.has_credentials
    assert "mock-secret" not in repr(config)


def test_missing_credentials_fail_closed():
    with pytest.raises(DataAccessError, match="MISSING_CREDENTIALS"):
        AlpacaMarketData(DataConfig())


@pytest.mark.parametrize("instant,expected", [
    ("2024-03-12T09:59:59+00:00", "2024-03-08"),
    ("2024-03-12T10:00:00+00:00", "2024-03-11"),
    ("2024-07-05T16:00:00+00:00", "2024-07-03"),
    ("2024-11-30T11:00:00+00:00", "2024-11-29"),
    ("2024-11-29T23:00:00+00:00", "2024-11-27"),
])
def test_cutoff_weekend_holiday_early_close_dst(instant, expected):
    assert str(finalized_day(datetime.fromisoformat(instant))) == expected


def test_timezone_and_original_timestamp():
    frame = normalize_bars({"NVDA": [mock_bar("2024-03-08"), mock_bar("2024-03-11")]}, ("NVDA",))
    assert frame.timestamp.dt.hour.tolist() == [5, 4]
    assert frame.trade_date.tolist() == ["2024-03-08", "2024-03-11"]
    assert frame.timestamp_original.iloc[0].endswith("-05:00")
    madrid = frame.timestamp.dt.tz_convert("Europe/Madrid")
    assert madrid.dt.hour.tolist() == [6, 5]
    with pytest.raises(ValueError):
        finalized_day(datetime(2024, 3, 1))


def test_sdk_paginates_across_symbols_and_pages(monkeypatch):
    sdk = PacedStockClient(DataConfig("mock", "mock"))
    monkeypatch.setattr("src.data.alpaca_client.time.sleep", lambda _: None)
    calls = []
    pages = [response({"bars": {"NVDA": [mock_bar()]}, "next_page_token": "page2"}),
             response({"bars": {"AMD": [mock_bar()]}, "next_page_token": "page3"}),
             response({"bars": {"MSFT": [mock_bar()]}, "next_page_token": None})]

    def request(method, url, **kwargs):
        calls.append({**kwargs["params"]})
        assert kwargs["timeout"] == (10, 45)
        assert method == "GET" and url.startswith("https://data.alpaca.markets/")
        return pages.pop(0)
    monkeypatch.setattr(sdk._session, "request", request)
    frame = AlpacaMarketData(DataConfig(), sdk).bars(("NVDA", "AMD", "MSFT"), date(2024, 3, 1), date(2024, 3, 8), "all")
    assert set(frame.symbol) == {"NVDA", "AMD", "MSFT"}
    assert [c["page_token"] for c in calls] == [None, "page2", "page3"]
    assert all(c["feed"] == "sip" and c["adjustment"] == "all" for c in calls)


@pytest.mark.parametrize("status,code", [(401, "AUTH_OR_PERMISSION_DENIED"),
                                        (403, "AUTH_OR_PERMISSION_DENIED"),
                                        (422, "SIP_PERMISSION_DENIED")])
def test_permissions_no_retry_no_iex_no_secret(monkeypatch, status, code):
    sdk = PacedStockClient(DataConfig("mock-sensitive-key", "mock-sensitive-secret"))
    request = Mock(return_value=response({"message": "subscription denied mock-sensitive-secret"}, status))
    monkeypatch.setattr(sdk._session, "request", request)
    result = AlpacaMarketData(DataConfig(), sdk).probe("latest", date(2024, 3, 8))
    assert result["status"] == code
    assert request.call_count == 1
    assert "mock-sensitive" not in str(result)
    assert request.call_args.kwargs["params"]["feed"] == "sip"


def test_retry_rate_limit_server_error_and_timeout(monkeypatch):
    sdk = PacedStockClient(DataConfig("mock", "mock"))
    waits = []
    monkeypatch.setattr("src.data.alpaca_client.time.sleep", waits.append)
    request = Mock(side_effect=[response({}, 429), requests.Timeout(), response({}, 503),
                                response({"bars": {"NVDA": [mock_bar()]}, "next_page_token": None})])
    monkeypatch.setattr(sdk._session, "request", request)
    frame = AlpacaMarketData(DataConfig(), sdk).bars(("NVDA",), date(2024, 3, 1), date(2024, 3, 8), "raw")
    assert len(frame) == 1 and request.call_count == 4
    assert 1 in waits and 2 in waits and 4 in waits


def test_retries_are_bounded(monkeypatch):
    sdk = PacedStockClient(DataConfig("mock", "mock"))
    monkeypatch.setattr("src.data.alpaca_client.time.sleep", lambda _: None)
    request = Mock(return_value=response({}, 429))
    monkeypatch.setattr(sdk._session, "request", request)
    result = AlpacaMarketData(DataConfig(), sdk).probe("historical", date(2024, 3, 8))
    assert result["status"] == "RATE_LIMITED" and request.call_count == 4


def test_history_and_latest_are_independent():
    sdk = Mock()
    sdk.get_stock_bars.return_value = {s: [mock_bar()] for s in ("NVDA", "AMD", "MSFT")}
    sdk.get_stock_latest_trade.side_effect = DataAccessError("SIP_PERMISSION_DENIED")
    client = AlpacaMarketData(DataConfig(), sdk)
    assert client.probe("historical", date(2024, 3, 8))["status"] == "ACCESS_OK"
    assert client.probe("latest", date(2024, 3, 8))["status"] == "SIP_PERMISSION_DENIED"
    sdk.get_stock_bars.return_value = {"NVDA": [mock_bar()]}
    assert client.probe("historical", date(2024, 3, 8))["status"] == "INCOMPLETE_RESPONSE"


def test_unfinished_and_unexpected_symbols_rejected():
    sdk = Mock()
    sdk.get_stock_bars.return_value = {"NVDA": [mock_bar("2024-03-11")]}
    client = AlpacaMarketData(DataConfig(), sdk)
    with pytest.raises(DataAccessError, match="OUT_OF_RANGE"):
        client.bars(("NVDA",), date(2024, 3, 1), date(2024, 3, 8), "raw")
    with pytest.raises(DataAccessError, match="UNEXPECTED_RESPONSE"):
        normalize_bars({"OTHER": []}, ("NVDA",))


def test_quality_missing_volume_not_imputed_and_unknown_gaps():
    bad = mock_bar("2024-03-06", volume=None)
    bad["h"] = 50
    frame = normalize_bars({"NVDA": [bad, bad, mock_bar("2024-03-08", price=-1)]}, ("NVDA",))
    result = inspect_quality(frame, ("NVDA",), date(2024, 3, 4), date(2024, 3, 10))
    codes = {i["code"] for i in result["issues"]}
    assert {"DUPLICATE", "OHLC_RELATION", "MISSING_VALUE", "INVALID_VOLUME", "INVALID_PRICE",
            "UNKNOWN_PREHISTORY", "UNKNOWN_MISSING_SESSION"} <= codes
    assert frame.volume.isna().sum() == 2
    assert result["closed_calendar_days"] == 2
    assert result["halt_verification"] == "UNKNOWN"
    assert not result["passed_numeric_checks"]


def test_abnormal_volume():
    days = sessions(date(2024, 3, 1), date(2024, 3, 15))
    bars = [mock_bar(str(d), volume=1000 if i < len(days) - 1 else 1000000) for i, d in enumerate(days)]
    frame = normalize_bars({"NVDA": bars}, ("NVDA",))
    result = inspect_quality(frame, ("NVDA",), days[0], days[-1])
    assert "ABNORMAL_VOLUME_REVIEW" in {i["code"] for i in result["issues"]}


class MockMarketData:
    """Synthetic transport fixture; only ever used with pytest tmp_path."""
    def __init__(self):
        self.calls = []
        self.fail_month = None
        self.fail_adjustment = "raw"
        self.adjusted_shift = 0
        self.invalid = False

    def probe(self, kind, end):
        return {"status": "ACCESS_OK", "check": kind, "mock": True}

    def bars(self, symbols, start, end, adjustment):
        self.calls.append((adjustment, start, end, symbols))
        if start.month == self.fail_month and adjustment == self.fail_adjustment:
            raise DataAccessError("NETWORK_ERROR")
        price = 100 + (self.adjusted_shift if adjustment == "all" else 0)
        return normalize_bars({s: [mock_bar(str(d), price, None if self.invalid else 1000)
                                   for d in sessions(start, end)] for s in symbols}, symbols)


@pytest.fixture
def sync_setup(monkeypatch, tmp_path):
    monkeypatch.setattr("src.data.alpaca_sync.HISTORY_START", date(2024, 1, 1))
    store = ParquetStore(tmp_path / "mock-only")
    config = DataConfig(data_dir=store.root)
    client = MockMarketData()
    return DataSync(client, store, config), client, store


NOW = datetime(2024, 3, 16, 12, tzinfo=timezone.utc)


def test_first_and_incremental_download_parquet_duckdb_export(sync_setup):
    sync, client, store = sync_setup
    result = sync.update(now=NOW)
    assert len(client.calls) == 6
    assert all(set(call[3]) == set(SYMBOLS) for call in client.calls)
    assert result["raw"]["rows"] > 0 and result["all"]["rows"] > 0
    first_rows = store.read("raw")
    client.calls.clear()
    sync.update(now=NOW)
    assert len(client.calls) == 2  # latest month only, not all history
    assert len(store.read("raw")) == len(first_rows)
    assert not store.read("raw").duplicated(["symbol", "trade_date"]).any()
    assert set(store.read("raw", symbols=("NVDA",)).symbol) == {"NVDA"}
    archive = zipfile.ZipFile(io.BytesIO(store.export_zip()))
    assert "raw/manifest.json" in archive.namelist() and "all/manifest.json" in archive.namelist()
    assert sum(name.endswith(".parquet") for name in archive.namelist()) == 6
    manifest = json.loads(archive.read("all/manifest.json"))
    assert all(m["listing_date"] == "UNKNOWN" for m in manifest["mapping"])
    assert manifest["chunks"]["2024-01"]["request"]["feed"] == "sip"
    assert "sip" in store.inventory(SYMBOLS).source.iloc[0].lower()


def test_failure_resumes_completed_months_and_keeps_active(sync_setup):
    sync, client, store = sync_setup
    client.fail_month = 2
    with pytest.raises(DataAccessError):
        sync.update(now=NOW)
    assert not store.active("raw")
    assert len(store.read_json("raw/pending.json")["completed"]) == 1
    assert store.read_json("raw/last_attempt.json")["status"] == "NETWORK_ERROR"
    client.fail_month = None
    client.calls.clear()
    sync.update(now=NOW)
    assert ("raw", date(2024, 1, 1)) not in [(c[0], c[1]) for c in client.calls]
    published = store.active("raw")["version"]
    client.fail_month = 3
    with pytest.raises(DataAccessError):
        sync.update(now=NOW)
    assert store.active("raw")["version"] == published


def test_adjusted_revision_rebuilds_entire_history(sync_setup):
    sync, client, store = sync_setup
    sync.update(now=NOW)
    client.adjusted_shift = 5
    client.calls.clear()
    result = sync.update(now=NOW)
    assert result["all"]["full_refresh"] is True
    assert len([c for c in client.calls if c[0] == "all"]) == 3
    assert (store.read("all").open == 105).all()
    assert (store.read("raw").open == 100).all()


def test_adjusted_rebuild_failure_does_not_mix_bases(sync_setup):
    sync, client, store = sync_setup
    sync.update(now=NOW)
    previous = store.active("all")["version"]
    client.adjusted_shift = 5
    client.fail_month, client.fail_adjustment = 2, "all"
    with pytest.raises(DataAccessError):
        sync.update(now=NOW)
    assert store.active("all")["version"] == previous
    assert (store.read("all").open == 100).all()
    client.fail_month = None
    sync.update(now=NOW)
    assert (store.read("all").open == 105).all()


def test_invalid_prices_quarantined(sync_setup):
    sync, client, store = sync_setup
    client.invalid = True
    with pytest.raises(DataAccessError, match="QUALITY_REJECTED"):
        sync.update(now=NOW)
    assert not store.active("raw")
    assert store.read_json("raw/quarantine.json")["rows"] > 0


def test_concurrent_writer_rejected(sync_setup):
    sync, _, store = sync_setup
    with store.lock():
        with pytest.raises(Timeout):
            sync.update(now=NOW)


def test_download_short_probe_failure_writes_no_prices(sync_setup):
    sync, client, store = sync_setup
    client.probe = lambda *args: {"status": "SIP_PERMISSION_DENIED"}
    with pytest.raises(DataAccessError, match="SIP_PERMISSION_DENIED"):
        sync.update(now=NOW)
    assert not list(store.root.rglob("*.parquet")) and not client.calls


def test_later_available_history_not_fabricated(sync_setup):
    sync, client, store = sync_setup
    original = client.bars

    def limited(symbols, start, end, adjustment):
        frame = original(symbols, start, end, adjustment)
        return frame[(frame.symbol != "ALAB") | (frame.trade_date >= "2024-03-01")].copy()
    client.bars = limited
    sync.update(now=NOW)
    frame = store.read("raw", symbols=("ALAB",))
    assert frame.trade_date.min() == "2024-03-01"
    manifest = store.active("raw")
    assert next(m for m in manifest["mapping"] if m["requested_symbol"] == "ALAB")["listing_date"] == "UNKNOWN"
    assert any(i["symbol"] == "ALAB" and i["code"] == "UNKNOWN_PREHISTORY"
               for i in manifest["quality"]["issues"])


def test_empty_months_are_preserved_without_fake_bars(sync_setup):
    sync, client, store = sync_setup
    original = client.bars
    client.bars = lambda symbols, start, end, adjustment: (
        normalize_bars({}, symbols) if start.month == 1 else original(symbols, start, end, adjustment))
    sync.update(now=NOW)
    assert store.active("raw")["chunks"]["2024-01"]["rows"] == 0
    assert store.read("raw").trade_date.min() == "2024-02-01"


def test_incremental_extends_partial_month(sync_setup):
    sync, client, store = sync_setup
    sync.update(now=NOW)
    rows = len(store.read("raw"))
    client.calls.clear()
    sync.update(now=datetime(2024, 3, 19, 12, tzinfo=timezone.utc))
    assert len(client.calls) == 2
    assert store.read("raw").trade_date.max() == "2024-03-18"
    assert len(store.read("raw")) == rows + len(SYMBOLS)


def test_data_dir_does_not_overwrite_unrelated_files(tmp_path):
    (tmp_path / ".gitignore").write_text("keep-me\n")
    store = ParquetStore(tmp_path)
    with pytest.raises(DataAccessError, match="DATA_DIR_NOT_DEDICATED"):
        store.initialize()
    assert (tmp_path / ".gitignore").read_text() == "keep-me\n"


def test_export_restore_then_incremental(sync_setup, tmp_path):
    sync, client, store = sync_setup
    sync.update(now=NOW)
    restored = tmp_path / "restored-mock-only"
    with zipfile.ZipFile(io.BytesIO(store.export_zip())) as archive:
        archive.extractall(restored)
    new_store = ParquetStore(restored)
    assert len(new_store.read("all")) == len(store.read("all"))
    client.calls.clear()
    DataSync(client, new_store, DataConfig(data_dir=restored)).update(now=NOW)
    assert len(client.calls) == 2
