"""B1.2 market adapter safety using only pytest temporary synthetic caches.

No provider constructor, actual secret, network endpoint, or research data is
read by these tests. A complete HTTP response is not complete market coverage.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from src.ben_b1_2 import data


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def bars():
    return pd.DataFrame({"symbol": ["SATS"] * 3,
        "timestamp": pd.to_datetime(["2026-01-01T21:00:00Z", "2026-01-02T21:00:00Z", "2026-01-05T21:00:00Z"]),
        "trade_date": ["2026-01-01", "2026-01-02", "2026-01-05"],
        "open": [10.] * 3, "high": [11.] * 3, "low": [9.] * 3, "close": [10.] * 3,
        "volume": [100.] * 3, "source_received_at": ["MOCK_DOWNLOAD_TIME"] * 3})


def old_cache(tmp_path, query="SATS", empty=False):
    target = tmp_path / "OLD_READ_ONLY_CACHE"
    target.mkdir()
    if not empty:
        bars().to_parquet(target / "data.parquet", index=False)
    return {"symbol": "ECHO", "kind": "bars", "complete": True, "rows": 0 if empty else 3,
            "path": str(target), "params": {"symbols": query, "feed": "sip", "start": "2026-01-01T00:00:00Z",
                "end": "2026-01-06T23:59:59Z", "adjustment": "raw", "timeframe": "1Day"}}


def market_without_credentials(monkeypatch, previous):
    obj = object.__new__(data.B12Market)
    obj.previous = previous
    obj.state = {"cache_reuse_records": []}
    obj.used = {}
    obj.saved = 0
    def save():
        obj.saved += 1
    obj.save = save
    monkeypatch.setattr(data, "deadline", lambda: None)
    return obj


def test_matching_historical_ticker_reuses_superset_read_only_and_filters_window(monkeypatch, tmp_path):
    prior = old_cache(tmp_path)
    source = Path(prior["path"]) / "data.parquet"
    before = (digest(source), source.stat().st_mtime_ns)
    market = market_without_credentials(monkeypatch, [prior])
    def forbidden(*args, **kwargs):
        raise AssertionError("Matched immutable cache must not reach a provider request")
    monkeypatch.setattr(data.B11Market, "acquire", forbidden)
    result, receipt = market.acquire("ECHO", pd.Timestamp("2026-01-02T00:00:00Z"),
        pd.Timestamp("2026-01-02T23:59:59Z"), timeframe="1Day", query_symbol="SATS")
    assert len(result) == 1 and result.iloc[0].trade_date == "2026-01-02"
    assert receipt["old_cache_reused_read_only"] and receipt["rows_in_requested_window"] == 1
    assert result.iloc[0].symbol == "SATS"  # Historical ticker is not silently relabeled in raw input.
    assert market.used[str(source)] == before[0]
    assert before == (digest(source), source.stat().st_mtime_ns)


@pytest.mark.parametrize("cached_query", ["ECHO", "OTHER_SECURITY", None])
def test_wrong_or_unknown_query_ticker_cannot_fall_through_to_legacy_reuse(monkeypatch, tmp_path, cached_query):
    prior = old_cache(tmp_path, query=cached_query)
    old_file = Path(prior["path"]) / "data.parquet"
    before = digest(old_file)
    original_list = [prior]
    market = market_without_credentials(monkeypatch, original_list)
    new_cache = tmp_path / "NEW_CORRECT_QUERY_CACHE"
    new_cache.mkdir()
    bars().to_parquet(new_cache / "data.parquet", index=False)
    called = []
    def fake_parent(self, symbol, start, end, kind, adjustment, timeframe, scope, **kwargs):
        called.append({"symbol": symbol, "query": kwargs.get("query_symbol"), "previous": list(self.previous)})
        assert self.previous == []  # Parent B1.1's looser reuse logic is disabled.
        return bars(), {"path": str(new_cache), "complete": True}
    monkeypatch.setattr(data.B11Market, "acquire", fake_parent)
    market.acquire("ECHO", pd.Timestamp("2026-01-02T00:00:00Z"), pd.Timestamp("2026-01-02T23:59:59Z"),
                   timeframe="1Day", query_symbol="SATS")
    assert called == [{"symbol": "ECHO", "query": "SATS", "previous": []}]
    assert market.previous is original_list and market.state["cache_reuse_records"] == []
    assert digest(old_file) == before


def test_parent_request_failure_restores_cache_index_without_writing_old_file(monkeypatch, tmp_path):
    prior = old_cache(tmp_path, query="ECHO")
    old_file = Path(prior["path"]) / "data.parquet"
    before = digest(old_file)
    original = [prior]
    market = market_without_credentials(monkeypatch, original)
    def fail(self, *args, **kwargs):
        assert not self.previous
        raise RuntimeError("MOCK_NETWORK_FAILURE_NO_REAL_REQUEST")
    monkeypatch.setattr(data.B11Market, "acquire", fail)
    with pytest.raises(RuntimeError, match="MOCK_NETWORK_FAILURE"):
        market.acquire("ECHO", pd.Timestamp("2026-01-02T00:00:00Z"), pd.Timestamp("2026-01-02T23:59:59Z"),
                       timeframe="1Day", query_symbol="SATS")
    assert market.previous is original and digest(old_file) == before


@pytest.mark.parametrize("kind", ["bars", "quotes"])
@pytest.mark.parametrize("feed", ["sip", "iex", None, "MISSING"])
def test_cache_feed_must_be_explicit_sip_without_legacy_fallback(monkeypatch, tmp_path, kind, feed):
    prior = old_cache(tmp_path)
    prior["kind"] = kind
    if feed == "MISSING":
        prior["params"].pop("feed")
    else:
        prior["params"]["feed"] = feed
    source = Path(prior["path"]) / "data.parquet"
    expected = bars()
    if kind == "quotes":
        expected = pd.DataFrame([{"t": "2026-01-02T21:05:00Z", "bp": 10., "ap": 10.01,
            "bs": 100, "as": 100, "quote_id": "MOCK_IMMUTABLE_SIP_QUOTE", "source_received_at": "MOCK_DOWNLOAD_TIME"}])
        expected.to_parquet(source, index=False)
    before = (digest(source), source.stat().st_mtime_ns)
    market = market_without_credentials(monkeypatch, [prior])
    calls = []
    def fresh_sip(self, symbol, start, end, actual_kind, adjustment, timeframe, scope, **kwargs):
        assert self.previous == []  # Parent cannot accept the rejected cache through its looser matcher.
        assert feed != "sip"
        calls.append(actual_kind)
        return expected.copy(), {"path": str(tmp_path / "MOCK_NEW_SIP_ONLY"), "complete": True,
                                 "params": {"feed": "sip"}, "mock_provider_only": True}
    monkeypatch.setattr(data.B11Market, "acquire", fresh_sip)
    original_read = pd.read_parquet
    def read_only_sip(path, *args, **kwargs):
        assert feed == "sip", "Rejected source must not even be read as research input"
        return original_read(path, *args, **kwargs)
    monkeypatch.setattr(pd, "read_parquet", read_only_sip)
    result, receipt = market.acquire("ECHO", pd.Timestamp("2026-01-02T00:00:00Z"),
        pd.Timestamp("2026-01-02T23:59:59Z"), kind=kind, timeframe="1Day", query_symbol="SATS")
    assert len(result) > 0 and receipt["params"]["feed"] == "sip"
    assert before == (digest(source), source.stat().st_mtime_ns)
    if feed == "sip":
        assert calls == [] and receipt["old_cache_reused_read_only"] is True
        assert len(market.state["cache_reuse_records"]) == 1
    else:
        assert calls == [kind] and market.state["cache_reuse_records"] == []
        assert receipt["mock_provider_only"] is True


def test_restarting_completed_local_quote_cache_preserves_quote_id_bytes_and_mtime(monkeypatch, tmp_path):
    output = tmp_path / "LOCAL_SAMPLE_QUOTES"
    cached = output / "cache/MOCK_CACHE_KEY"
    cached.mkdir(parents=True)
    frame = pd.DataFrame([{"t": "2026-01-02T21:05:00.486314Z", "bp": 100.95, "ap": 101.,
        "bs": 10, "as": 10, "bx": "Q", "ax": "Q", "c": [], "size_unit": "shares",
        "quote_id": "MOCK_IMMUTABLE_ORIGINAL_QUOTE_ID", "source_received_at": "MOCK_FIRST_DOWNLOAD"}])
    source = cached / "data.parquet"
    frame.to_parquet(source, index=False)
    before = (digest(source), source.stat().st_mtime_ns)
    prior = {"symbol": "ECHO", "kind": "quotes", "complete": True, "rows": 1, "path": str(cached),
             "params": {"symbols": "SATS", "feed": "sip", "start": "2026-01-02T21:04:55Z", "end": "2026-01-02T21:15:00Z"}}
    def fake_init(self, destination):
        self.previous = []
        self.state = {"requests": [prior], "cache_reuse_records": []}
        self.state_path = Path(destination) / "PROBE_STATE.json"
        self.save = lambda: None
    def forbidden(*args, **kwargs):
        raise AssertionError("Completed local quote cache must not enter legacy quote-id regeneration")
    monkeypatch.setattr(data.B11Market, "__init__", fake_init)
    monkeypatch.setattr(data.B11Market, "acquire", forbidden)
    monkeypatch.setattr(data, "B11", tmp_path / "NO_REAL_B11")
    monkeypatch.setattr(data, "DATA", tmp_path / "NO_REAL_B12")
    monkeypatch.setattr(data, "deadline", lambda: None)
    for _ in range(2):
        market = data.B12Market(output)
        result, receipt = market.acquire("ECHO", pd.Timestamp("2026-01-02T21:04:55Z"),
            pd.Timestamp("2026-01-02T21:15:00Z"), kind="quotes", query_symbol="SATS")
        assert result.quote_id.tolist() == ["MOCK_IMMUTABLE_ORIGINAL_QUOTE_ID"]
        assert result.source_received_at.tolist() == ["MOCK_FIRST_DOWNLOAD"]
        assert receipt["old_cache_reused_read_only"] is True
        assert (digest(source), source.stat().st_mtime_ns) == before


def test_empty_completed_response_stays_empty_and_cannot_prove_market_coverage(monkeypatch, tmp_path):
    prior = old_cache(tmp_path, empty=True)
    market = market_without_credentials(monkeypatch, [prior])
    def forbidden(*args, **kwargs):
        raise AssertionError("Bounded prior empty response must not be repeatedly downloaded")
    monkeypatch.setattr(data.B11Market, "acquire", forbidden)
    result, receipt = market.acquire("ECHO", pd.Timestamp("2026-01-02T00:00:00Z"),
        pd.Timestamp("2026-01-02T23:59:59Z"), timeframe="1Day", query_symbol="SATS")
    assert result.empty and receipt["rows_in_requested_window"] == 0
    assert receipt.get("old_input_sha256") is None
    assert "coverage_verified" not in receipt and "listing_date_verified" not in receipt
    assert not list(Path(prior["path"]).iterdir())


def test_complete_empty_download_is_explicit_empty_history_not_pass_or_listing_inference(monkeypatch, tmp_path):
    old_b1, old_b11, output = (tmp_path / name for name in ["OLD_B1", "OLD_B11", "NEW_OUTPUT"])
    (old_b1 / "coarse").mkdir(parents=True)
    (old_b11 / "data").mkdir(parents=True)
    pd.DataFrame([{"symbol": "ECHO", "scope_policy": "KEEP"}, {"symbol": "AVAV", "scope_policy": "EXCLUDE_BUSINESS"}]).to_csv(old_b1 / "UNIVERSE_POLICY.csv", index=False)
    pd.DataFrame([{"symbol": "ECHO", "trade_date": "2026-01-02", "coarse_space_allowed": True,
                   "identity_sort_hash": "MOCK_STABLE_ID"}]).to_csv(old_b1 / "coarse/all_candidate_events.csv", index=False)
    (old_b11 / "data/HISTORY_INPUTS.json").write_text("[]", encoding="utf-8")
    old_before = {str(path): digest(path) for base in [old_b1, old_b11] for path in base.rglob("*") if path.is_file()}
    monkeypatch.setattr(data, "B1", old_b1)
    monkeypatch.setattr(data, "B11", old_b11)
    monkeypatch.setattr(data, "DATA", output)
    monkeypatch.setattr(data, "deadline", lambda: None)
    requests = []
    class FakeMarket:
        def __init__(self, *args):
            pass
        def corporate_actions(self, symbols):
            return {"status": "MOCK_EMPTY_ACTION_RESPONSE", "rows": []}
        def acquire(self, symbol, start, end, **kwargs):
            requests.append({"symbol": symbol, **kwargs})
            return bars().iloc[:0], {"complete": True, "path": str(output / "MOCK_CACHE"), "rows": 0}
    monkeypatch.setattr(data, "B12Market", FakeMarket)
    histories = data.prepare_histories()
    assert len(histories) == 1
    row = histories[0]
    assert row["rth"]["rows"] == row["rth"]["pre_start_valid_sessions"] == 0
    assert row["coverage_status"] == "EMPTY_RESPONSE_NOT_PROOF_OF_PRE_LISTING"
    assert all(request["query_symbol"] == "SATS" for request in requests)
    assert len(pd.read_csv(output / "ALL66_SCOPE_VERSION.csv")) == 2  # Fixture scope including excluded is preserved.
    assert all(digest(path) == value for path, value in old_before.items())


def test_pinned_old_input_change_fails_instead_of_silent_replacement(tmp_path):
    source = tmp_path / "OLD_INPUT.parquet"
    bars().to_parquet(source, index=False)
    current = digest(source)
    good = data.pin({"path": str(source), "sha256": current})
    assert good["reused_read_only"] and good["sha256"] == current
    with pytest.raises(ValueError, match="PINNED_OLD_OBJECT_CHANGED"):
        data.pin({"path": str(source), "sha256": "0" * 64})
    assert digest(source) == current


def test_missing_volume_not_zero_filled_or_reported_as_observed_zero_volume():
    frame = bars()
    frame.loc[0, "volume"] = None
    frame.loc[1, "volume"] = -1
    qc = data.frame_qc(frame)
    assert qc["missing_volume"] == qc["negative_volume"] == 1
    assert pd.isna(frame.loc[0, "volume"])


@pytest.mark.parametrize("day,expected_close", [("2026-01-02", "2026-01-02T21:00:00Z"),
                                              ("2026-03-16", "2026-03-16T20:00:00Z")])
def test_entry_and_holding_windows_derive_from_exchange_calendar(monkeypatch, day, expected_close):
    market = market_without_credentials(monkeypatch, [])
    calls = []
    def acquire(symbol, start, end, **kwargs):
        calls.append((symbol, pd.Timestamp(start), pd.Timestamp(end), kwargs))
        return pd.DataFrame(), {"rows": 0}
    market.acquire = acquire
    market.quotes("ECHO", day)
    market.holding_quotes("ECHO", day)
    close = pd.Timestamp(expected_close)
    assert calls[0][1] == close + pd.Timedelta(minutes=5, seconds=-5)
    assert calls[0][2] == close + pd.Timedelta(minutes=15)
    assert calls[1][2] == close and calls[1][1] < close
    assert all(call[3]["query_symbol"] == "SATS" for call in calls)
