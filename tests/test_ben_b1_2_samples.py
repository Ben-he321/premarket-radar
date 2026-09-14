"""Original20 adapter checks with explicitly synthetic pytest temporary inputs."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import json

import pandas as pd
import pytest

from test_ben_b1_replay import schedule, warmup, daily, quote, earnings, ny, ev
from src.ben_b1_2 import samples


def test_overlapping_quote_receipts_deduplicate_display_quantity_and_keep_first_receipt():
    first = quote("2026-05-01 16:05:00.486314", "BE", ask_size=10)
    first["payload"]["source_received_at"] = "MOCK_FIRST_CURRENT_DOWNLOAD"
    second = {**first, "event_id": "SECOND_OVERLAPPING_QUERY", "payload": {**first["payload"],
              "source_received_at": "MOCK_LATER_CURRENT_DOWNLOAD"}}
    result = samples.deduplicate_events([first, second])
    assert len(result) == 1 and result[0]["payload"]["ask_size"] == 10
    assert result[0]["payload"]["source_received_at"] == "MOCK_FIRST_CURRENT_DOWNLOAD"
    assert first["payload"]["source_received_at"] == "MOCK_FIRST_CURRENT_DOWNLOAD"


def test_same_nonquote_id_with_different_content_is_not_silently_deduplicated():
    first = daily("2026-05-01", 101.)
    changed = {**first, "payload": {**first["payload"], "close": 999.}}
    with pytest.raises(ValueError, match="ADAPTER_EVENT_ID_CONTENT_CONFLICT"):
        samples.deduplicate_events([first, changed])


def test_vendor_quote_conversion_keeps_actual_download_separate_from_unknown_historical_receipt():
    when = "2026-05-01T20:05:00.486314Z"
    frame = pd.DataFrame([{"t": when, "bp": 100.95, "ap": 101., "bs": 20, "as": 10,
                          "bx": "Q", "ax": "Q", "c": [], "size_unit": "shares",
                          "source_received_at": "2026-09-14T07:00:00Z"}])
    events = samples.quote_events("BE", frame, {})
    assert len(events) == 1
    value = events[0]
    assert pd.Timestamp(value["at"]) == pd.Timestamp(when)
    assert value["payload"]["historical_network_received_at"] == "UNKNOWN"
    assert value["payload"]["source_received_at"] == "2026-09-14T07:00:00Z"
    assert value["payload"]["availability_basis"] == "HISTORICAL_EVENT_CLOCK_NOT_LIVE_BASIC_RECEIPT"


@pytest.mark.parametrize("symbol,delay", [("BE", ".486314"), ("RIVN", ".790726")])
def test_fixed_quote_coverage_and_window_quote_coverage_are_separate(symbol, delay):
    inputs = SimpleNamespace(clocks={"2026-05-01": SimpleNamespace(market_close=pd.Timestamp("2026-05-01T20:00:00Z"))})
    result = samples._sample_quote_coverage(inputs, {"symbol": symbol, "trade_date": "2026-05-01"},
        {"2026-05-01": [quote("2026-05-01 16:05:00"+delay, symbol), quote("2026-05-01 16:15:00", symbol)]})
    assert result["fixed_point_quote_records_age_le5s"] == 0
    assert result["window_new_quote_records"] == 1
    assert result["classification"] == "FIXED_POINT_NO_QUOTE_WINDOW_HAS_QUOTE"
    assert result["quote_coverage_is_not_trade_qualification"] is True


class SyntheticInputs:
    """Injected adapter fixture. It contains no real prices or provider setup."""
    def __init__(self, schedule, signal=True):
        self.schedule = schedule
        self.clocks = {str(day.date()): row for day, row in schedule.iterrows()}
        self.pinned = {}
        self.first = [{"symbol": "MOCK", "trade_date": "2026-05-01", "local_security_id": "SYNTHETIC_ONLY_MOCK",
                       "scope_policy": "KEEP", "identity_sort_hash": "MOCK_FIXED_RANK"}]
        self.earnings = {"classification": "SYNTHETIC_FIXTURE_ORIGINAL_EVIDENCE"}
        self.bars = warmup(schedule, "MOCK", signal=signal) + [daily("2026-05-04", 80., "MOCK"),
                    daily("2026-05-05", 80., "MOCK"), daily("2026-05-06", 80., "MOCK")]
        self.daily = pd.DataFrame([{**item["payload"], "trade_date": item["payload"]["session_date"]} for item in self.bars])
        self.quotes = {"2026-05-01": [quote("2026-05-01 16:05:01", "MOCK")]}
    def symbol_data(self, symbol):
        return self.daily, {}, self.quotes
    def daily_event(self, symbol, row):
        return next(item for item in self.bars if item["payload"]["session_date"] == row["trade_date"])
    def earnings_events(self, symbol, tier, first):
        return [earnings("MOCK")]
    def corporate_events(self, symbol, first, last):
        return []
    def day_events(self, symbol, day, daily_frame, minutes, quotes):
        return [item for item in self.bars if item["payload"]["session_date"] == day] + self.quotes.get(day, [])


@pytest.mark.parametrize("signal", [False, True])
def test_holding_quotes_requested_only_after_natural_position_and_not_from_future_outcomes(monkeypatch, tmp_path, schedule, signal):
    monkeypatch.setattr(samples, "deadline", lambda: None)
    monkeypatch.setattr(samples, "TAIL_END", "2026-05-06")
    monkeypatch.setattr(samples, "B11", tmp_path / "NO_REAL_LEGACY_DATA")
    inputs = SyntheticInputs(schedule, signal=signal)
    requested = []
    def provider(symbol, day):
        requested.append((symbol, day))
        return {"events": [quote(day+" 09:30:00", symbol, bid=80., ask=80.05)],
                "complete": True, "source_files": {}, "receipt": {"classification": "SYNTHETIC_FIXTURE_ONLY"}}
    result = samples.run_sample(inputs, 1, "A", version="OFFLINE_SYNTHETIC_TEST_ONLY",
                                day_quote_provider=provider, output_root=tmp_path / "ONLY_TEST_OUTPUT")
    if signal:
        assert result["buy_fills"] == 1 and result["sell_fills"] == 2
        assert requested == [("MOCK", "2026-05-04")]
        assert result["stop_reason"] == "NATURAL_CAMPAIGN_SETTLED"
    else:
        assert result["buy_fills"] == 0 and requested == []
    assert result["error_count"] == 0 and result["full_pool_account"] is False
    assert result["original_earnings_version"] == samples.digest(inputs.earnings)
    spec = json.loads((Path(result["output"]) / "RUN_SPEC.json").read_text(encoding="utf-8"))
    assert spec["original_earnings_no_b12_enrichment"] and spec["config"]["entry_dates"] == ["2026-05-01"]
    assert spec["config"]["initial_equity"] == 5500
    assert "NOT_SHARED_PORTFOLIO" in spec["config"]["research_scope"]
    recovery = json.loads((Path(result["output"]) / "RECOVERY_IDEMPOTENCY.json").read_text(encoding="utf-8"))
    assert recovery["status"] == "PASS" and recovery["same_state"]


@pytest.mark.parametrize("rank,tier", [(0, "A"), (21, "B"), (1, "C")])
def test_sample_adapter_cannot_expand_fixed_twenty_or_mix_unknown_tier(monkeypatch, tmp_path, rank, tier):
    monkeypatch.setattr(samples, "deadline", lambda: None)
    with pytest.raises(ValueError, match="ORIGINAL_TWENTY_AND_ORIGINAL_A_B_ONLY"):
        samples.run_sample(object(), rank, tier, output_root=tmp_path)
    assert not list(tmp_path.iterdir())
