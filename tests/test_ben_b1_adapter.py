"""SYNTHETIC adapter integration in temporary directories, no live providers."""
from pathlib import Path
import json

import pandas as pd
import pandas_market_calendars as mcal
import pytest

from src.ben_b1 import b11_research as adapter
from src.ben_b1.replay import ReplayConfig, ReplayEngine
from test_ben_b1_replay import daily, quote, warmup


class SyntheticInputs(adapter.Inputs):
    def __init__(self, tmp_path, with_dividend=False):
        self.schedule = mcal.get_calendar("NYSE").schedule("2025-01-01", "2026-12-31")
        self.clocks = {str(d.date()): r for d, r in self.schedule.iterrows()}
        self.first = [{"symbol": "X", "trade_date": "2026-05-01", "scope_policy": "KEEP",
                       "local_security_id": "MOCK_ONLY_X", "identity_sort_hash": "MOCK_X"}]
        self.earnings = {"created_at": "UNKNOWN", "facts": [{"symbol": "X", "coverage_complete": True,
            "previous_release_date_ny": "2026-01-30", "previous_source": "MOCK_IR_PREVIOUS",
            "next_release_date_ny": "2026-07-30", "next_source": "MOCK_IR_NEXT",
            "coverage_start": "2026-01-30", "coverage_end": "2026-07-30",
            "completeness_basis": "SYNTHETIC_TEST_ONLY"}], "plans": [{"symbol": "X",
            "planned_release_date": "2026-07-30", "conservative_known_at_ny": "2026-04-01 09:00:00",
            "source": "MOCK_IR_PRIOR_PUBLIC_PLAN", "availability_basis": "SYNTHETIC_TEST_ONLY"}]}
        dividend = {"symbol": "X", "ex_date": "2026-05-04", "payable_date": "2026-05-08",
                    "rate": .5, "id": "MOCK_DIVIDEND"}
        self.actions = {"corporate_actions": {"cash_dividends": [dividend] if with_dividend else [],
                                             "forward_splits": [], "reverse_splits": []}}
        self.histories = {"X": {"minutes": {"receipt": {"last_source_received_at": "UNKNOWN"}}}}
        self.input_file = tmp_path / "synthetic_fixture_identity.json"
        self.input_file.write_text(json.dumps({"synthetic": True, "version": 1}), encoding="utf-8")
        self.pinned = {str(self.input_file): adapter.sha(self.input_file)}
        records = [event["payload"] for event in warmup(self.schedule)]
        records += [daily(day, 80.)["payload"] for day in ["2026-05-04", "2026-05-05", "2026-05-06", "2026-05-07", "2026-05-08"]]
        for row in records:
            row["trade_date"] = row["session_date"]
        self.daily = pd.DataFrame(records).set_index("trade_date", drop=False)
        self.minute = pd.DataFrame(columns=["timestamp", "time", "ny_day", "open", "high", "low", "close", "volume"])
        self.quotes = {"2026-05-01": [quote("2026-05-01 16:05:00", ask_size=10)],
                       "2026-05-04": [quote("2026-05-04 10:00:00", bid=80., ask=80.05)]}

    def symbol_data(self, symbol):
        assert symbol == "X"
        return self.daily, self.minute, self.quotes


@pytest.fixture
def isolated_adapter(tmp_path, monkeypatch):
    monkeypatch.setattr(adapter, "ROOT", tmp_path / "mock_adapter_output")
    original = ReplayConfig
    monkeypatch.setattr(adapter, "ReplayConfig", lambda **kwargs: original(synthetic_test=True, **kwargs))
    return tmp_path


def test_prior_public_earnings_plan_is_delivered_before_first_sample_day(isolated_adapter):
    inputs = SyntheticInputs(isolated_adapter)
    result = adapter.run_sample(inputs, 1, "A", "synthetic_prior_plan")
    assert result["buy_fills"] == 1 and result["sell_fills"] == 2
    assert result["error_count"] == 0
    assert result["stop_reason"] == "NATURAL_CAMPAIGN_SETTLED"
    restored = ReplayEngine.restore(Path(result["output"]) / "checkpoint.json", inputs.schedule)
    received = [row for row in restored.trace if row["kind"] == "EARNINGS_VERSION_RECEIVED"]
    assert len(received) == 1
    assert pd.Timestamp(received[0]["at"]).date().isoformat() == "2026-04-01"
    assert restored.state["decisions"][0]["earnings"]["strict_eligible"]
    assert result["synthetic_engineering_only"]


def test_natural_completion_waits_for_dividend_cash_and_cache_reuse_is_read_only(isolated_adapter):
    inputs = SyntheticInputs(isolated_adapter, with_dividend=True)
    result = adapter.run_sample(inputs, 1, "A", "synthetic_dividend")
    assert result["processed_through"] == "2026-05-08"
    assert result["stop_reason"] == "NATURAL_CAMPAIGN_SETTLED"
    path = Path(result["output"]) / "checkpoint.json"
    restored = ReplayEngine.restore(path, inputs.schedule)
    assert restored.ledger.dividend_receivables["MOCK_DIVIDEND"]["status"] == "PAID"
    before = adapter.sha(path)
    cached = adapter.run_sample(inputs, 1, "A", "synthetic_dividend")
    assert cached == result and adapter.sha(path) == before
    assert json.loads((path.parent / "RECOVERY_IDEMPOTENCY.json").read_text())["same_state"]


def test_changed_pinned_input_cannot_silently_reuse_completed_run(isolated_adapter):
    inputs = SyntheticInputs(isolated_adapter)
    result = adapter.run_sample(inputs, 1, "A", "synthetic_pin")
    checkpoint = Path(result["output"]) / "checkpoint.json"
    original_hash = adapter.sha(checkpoint)
    inputs.input_file.write_text(json.dumps({"synthetic": True, "version": 2}), encoding="utf-8")
    with pytest.raises(ValueError, match="IMMUTABLE_RUN_SOURCE_CHANGED"):
        adapter.run_sample(inputs, 1, "A", "synthetic_pin")
    assert adapter.sha(checkpoint) == original_hash


def test_actual_inputs_csv_optional_blanks_replay_without_nan_json_failure(isolated_adapter, monkeypatch):
    """Read real CSV/Parquet loader paths, then execute the actual sample adapter."""
    source = SyntheticInputs(isolated_adapter)
    root = adapter.ROOT
    data = root / "data"
    data.mkdir(parents=True)
    (root / "earnings").mkdir()
    monkeypatch.setattr(adapter, "DATA", data)
    pd.DataFrame([{**source.first[0], "optional_overhead_source": "", "optional_date": ""}]).to_csv(
        data / "FIRST20_FROZEN.csv", index=False)
    rth_path, minute_path = data / "mock_rth.parquet", data / "mock_minute.parquet"
    source.daily.to_parquet(rth_path, index=False)
    source.minute.to_parquet(minute_path, index=False)
    quote_path = data / "mock_quote_request"
    quote_path.mkdir()
    quotes = []
    for day in source.quotes.values():
        for value in day:
            p = value["payload"]
            quotes.append({"t": p["timestamp"], "bp": p["bid"], "ap": p["ask"],
                           "bs": p["bid_size"], "as": p["ask_size"], "c": [],
                           "size_unit": "shares", "source_received_at": "UNKNOWN"})
    pd.DataFrame(quotes).to_parquet(quote_path / "data.parquet", index=False)
    fixtures = {
        data / "FIRST20_INPUTS.json": [{"symbol": "X", "quote_windows": [{"receipt": {
            "path": str(quote_path), "complete": True, "last_source_received_at": "UNKNOWN"}}]}],
        data / "HISTORY_INPUTS.json": [{"symbol": "X", "rth": {"path": str(rth_path)},
            "minutes": {"path": str(minute_path), "receipt": {"last_source_received_at": "UNKNOWN"}}}],
        data / "corporate_actions.json": source.actions,
        root / "earnings" / "PRIMARY_EARNINGS.json": source.earnings,
        root / "B11_PROTOCOL.json": {"classification": "SYNTHETIC_TEST_ONLY"},
    }
    for path, value in fixtures.items():
        path.write_text(json.dumps(value), encoding="utf-8")
    inputs = adapter.Inputs()
    result = adapter.run_sample(inputs, 1, "A", "synthetic_actual_csv_loader")
    assert result["buy_fills"] == 1 and result["sell_fills"] == 2
    assert result["error_count"] == 0
    spec = json.loads((Path(result["output"]) / "RUN_SPEC.json").read_text())
    assert spec["sample"]["optional_overhead_source"] is None
    assert spec["sample"]["optional_date"] is None
    assert result["stop_reason"] == "NATURAL_CAMPAIGN_SETTLED"
