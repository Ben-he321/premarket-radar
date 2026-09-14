"""Minimal continuous-account integration with synthetic fixtures in tmp_path.

These tests exercise the actual quarterly adapter and inherited execution
kernel. All provider methods are replaced by deterministic temporary fixtures.
"""
from __future__ import annotations

from pathlib import Path
import hashlib
import json

import pandas as pd
import pytest

from test_ben_b1_replay import schedule, warmup, daily, quote, earnings, ev, ny
from src.ben_b1_2 import portfolio


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class FixtureContinuousInputs:
    def __init__(self, schedule, root, mode="Q1", fail_symbol=None, missing_held=False):
        self.schedule = schedule
        self.clocks = {str(day.date()): row for day, row in schedule.iterrows()}
        self.universe = {symbol: {"scope": "KEEP", "security_id": "MOCK_ONLY_" + symbol,
                        "identity_sort_hash": str(i)} for i, symbol in enumerate(["X", "Y", "UNKNOWN_INPUT"])}
        self.universe.update({f"EXCLUDED_{i:02d}": {"scope": "EXCLUDE_BUSINESS", "security_id": f"MOCK_EXCLUDED_{i}"}
                              for i in range(63)})
        self.daily = {}
        self._bars = {}
        for symbol in ("X", "Y"):
            entries = warmup(schedule, symbol)
            if not missing_held:
                entries += [daily("2026-05-04", 80., symbol), daily("2026-05-05", 80., symbol)]
            self._bars[symbol] = entries
            self.daily[symbol] = pd.DataFrame([{**event["payload"], "trade_date": event["payload"]["session_date"]}
                                   for event in entries]).set_index("trade_date", drop=False)
        self.root, self.mode, self.fail_symbol, self.missing_held = root, mode, fail_symbol, missing_held
        root.mkdir(exist_ok=True, parents=True)
        static = root / "STATIC_FIXTURE_INPUT.json"
        if not static.exists():
            static.write_text('{"classification":"SYNTHETIC_TEST_INPUT_ONLY"}', encoding="utf-8")
        self.pinned = {str(static): digest(static)}
        self.coverage = []
        self.calls = []
    def daily_event(self, symbol, row):
        event = next(item for item in self._bars[symbol] if item["payload"]["session_date"] == row["trade_date"])
        return {**event, "at": ny(row["trade_date"] + " 16:01:00")}
    def corporate_events(self):
        return []
    def earnings_events(self, tier):
        return [earnings(symbol) for symbol in ("X", "Y")]
    def minute_events(self, symbol, day):
        return []
    def quotes(self, symbol, day, holding):
        self.calls.append((symbol, day, holding))
        if self.fail_symbol == symbol and not holding:
            raise RuntimeError("MOCK_TRANSIENT_PROVIDER_REQUEST_FAILED")
        source = self.root / f"DYNAMIC_{symbol}_{day}_{'holding' if holding else 'entry'}.json"
        if not source.exists():
            source.write_text(json.dumps({"classification": "MOCK_QUOTE_RECEIPT_ONLY", "symbol": symbol, "day": day}), encoding="utf-8")
        self.pinned[str(source)] = digest(source)
        if holding:
            events = [] if self.missing_held else [quote(day + " 09:30:00", symbol, bid=80., ask=80.05)]
        else:
            at = day + (" 16:05:00" if self.mode == "Q0" else " 16:05:00.500000")
            events = [quote(at, symbol, bid=100.99 if symbol == "X" else 100.95)]
        receipt = {"complete": True, "status": "MOCK_HTTP_COMPLETE", "params": {"symbols": symbol}}
        self.coverage.append({"symbol": symbol, "day": day, "purpose": "ACTUAL_HOLDING_RTH" if holding else "CAUSAL_SIGNAL_ENTRY_WINDOW",
                              "rows": len(events), "complete": True, "path": str(source)})
        return events, receipt


def setup(monkeypatch, tmp_path, schedule, mode="Q1", **kwargs):
    instances = []
    def factory(earnings_path):
        value = FixtureContinuousInputs(schedule, tmp_path / "FIXTURE_INPUTS", mode, **kwargs)
        instances.append(value)
        return value
    monkeypatch.setattr(portfolio, "ContinuousInputs", factory)
    monkeypatch.setattr(portfolio, "ROOT", tmp_path / "ONLY_TEST_OUTPUT")
    monkeypatch.setattr(portfolio, "START", "2026-05-01")
    monkeypatch.setattr(portfolio, "END", "2026-05-01")
    monkeypatch.setattr(portfolio, "TAIL_END", "2026-05-05")
    monkeypatch.setattr(portfolio, "deadline", lambda: None)
    monkeypatch.setattr(portfolio, "status", lambda *args, **kwargs: None)
    return instances


@pytest.mark.parametrize("mode", ["Q0", "Q1"])
def test_continuous_adapter_has_all66_one_5500_cash_pool_and_real_event_cycles(monkeypatch, tmp_path, schedule, mode):
    instances = setup(monkeypatch, tmp_path, schedule, mode)
    result = portfolio.run_account(mode, "A", tmp_path / "MOCK_EARNINGS.json", "SYNTHETIC_INTEGRATION")
    assert result["error_count"] == 0
    assert result["buy_fills"] == 2 and result["sell_fills"] == 4
    assert result["latest_equity"]["initial_equity"] == result["common_capital"] == 5500
    assert result["latest_equity"]["net_equity"] < 5500
    assert result["latest_equity"]["cash"] >= 0 and not result["open_positions"]
    assert result["portfolio_not_stitched"] and result["coverage_status"] == "COVERAGE_LIMITED"
    out = portfolio.ROOT / "portfolio/SYNTHETIC_INTEGRATION" / f"B12_{mode}_P50_A_SYNTHETIC_INTEGRATION"
    quarter = json.loads((out / "QUARTER_END_ACCOUNT.json").read_text(encoding="utf-8"))
    assert len(quarter["open_positions"]) == 2
    quarter_ledger = json.loads((out / "QUARTER_END_LEDGER.json").read_text(encoding="utf-8"))
    close_value = json.loads((out / "QUARTER_END_CLOSE_VALUATION.json").read_text(encoding="utf-8"))
    assert quarter_ledger["state"]["positions"]["X"]["last_mark"] == 100.99
    assert quarter_ledger["state"]["positions"]["Y"]["last_mark"] == 100.95
    assert close_value["net_equity"] > quarter["latest_equity"]["net_equity"]
    assert close_value["execution_marks_unchanged"] is True
    decisions = pd.read_csv(out / "coverage_funnel.csv")
    assert decisions.symbol.nunique() == 66
    assert set(instances[0].calls) == {(s, "2026-05-01", False) for s in ["X", "Y"]} | {(s, "2026-05-04", True) for s in ["X", "Y"]}
    clocks = json.loads((out / "COMMON_CLOCK.json").read_text(encoding="utf-8"))
    first_day_sources = next(row for row in clocks if row["day"] == "2026-05-01")["quote_sources"]
    assert {source["symbol"] for source in first_day_sources} == {"X", "Y"}
    assert all(source["purpose"] == "CAUSAL_SIGNAL_ENTRY_WINDOW" for source in first_day_sources)
    recovery = json.loads((out / "RECOVERY_IDEMPOTENCY.json").read_text(encoding="utf-8"))
    assert recovery["status"] == "PASS"


def test_single_symbol_quote_request_failure_does_not_cancel_other_candidate_execution(monkeypatch, tmp_path, schedule):
    setup(monkeypatch, tmp_path, schedule, fail_symbol="X")
    result = portfolio.run_account("Q1", "A", tmp_path / "MOCK_EARNINGS.json", "SYNTHETIC_REQUEST_FAILURE")
    assert result["error_count"] == 0 and result["buy_fills"] == 1
    out = portfolio.ROOT / "portfolio/SYNTHETIC_REQUEST_FAILURE/B12_Q1_P50_A_SYNTHETIC_REQUEST_FAILURE"
    filled = pd.read_csv(out / "fills.csv")
    assert set(filled.symbol) == {"Y"}
    assert result["coverage_status"] == "COVERAGE_LIMITED"
    assert "REQUEST" in json.dumps(result) or "QUOTE" in json.dumps(result)


def test_missing_held_quotes_and_prices_keep_position_and_unknown_nav(monkeypatch, tmp_path, schedule):
    setup(monkeypatch, tmp_path, schedule, missing_held=True)
    result = portfolio.run_account("Q1", "A", tmp_path / "MOCK_EARNINGS.json", "SYNTHETIC_MISSING_HELD_PATH")
    assert result["buy_fills"] == 2 and result["sell_fills"] == 0
    assert len(result["open_positions"]) == 2
    assert result["latest_equity"]["net_equity"] is None
    assert set(result["latest_equity"]["missing_current_marks"]) == {"X", "Y"}
    assert result["coverage_status"] == "COVERAGE_LIMITED"


def test_resume_verifies_and_retains_prior_dynamic_input_hashes(monkeypatch, tmp_path, schedule):
    instances = setup(monkeypatch, tmp_path, schedule)
    calls = 0
    def stop_after_first_day():
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("MOCK_INTERRUPTION_AFTER_DURABLE_FIRST_DAY")
    monkeypatch.setattr(portfolio, "deadline", stop_after_first_day)
    with pytest.raises(RuntimeError, match="MOCK_INTERRUPTION"):
        portfolio.run_account("Q1", "A", tmp_path / "MOCK_EARNINGS.json", "SYNTHETIC_RESUME")
    out = portfolio.ROOT / "portfolio/SYNTHETIC_RESUME/B12_Q1_P50_A_SYNTHETIC_RESUME"
    first_hashes = json.loads((out / "DYNAMIC_INPUT_HASHES.json").read_text(encoding="utf-8"))
    assert any("DYNAMIC_X_2026-05-01" in path for path in first_hashes)
    monkeypatch.setattr(portfolio, "deadline", lambda: None)
    result = portfolio.run_account("Q1", "A", tmp_path / "MOCK_EARNINGS.json", "SYNTHETIC_RESUME")
    final_hashes = json.loads((out / "DYNAMIC_INPUT_HASHES.json").read_text(encoding="utf-8"))
    assert all(final_hashes.get(path) == expected for path, expected in first_hashes.items())
    assert result["buy_fills"] == 2 and result["sell_fills"] == 4
    assert not any(day == "2026-05-01" for _, day, _ in instances[-1].calls)


def test_resume_refuses_changed_prior_quote_input(monkeypatch, tmp_path, schedule):
    setup(monkeypatch, tmp_path, schedule)
    calls = 0
    def stop_after_first_day():
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("MOCK_INTERRUPTION_AFTER_DURABLE_FIRST_DAY")
    monkeypatch.setattr(portfolio, "deadline", stop_after_first_day)
    with pytest.raises(RuntimeError, match="MOCK_INTERRUPTION"):
        portfolio.run_account("Q1", "A", tmp_path / "MOCK_EARNINGS.json", "SYNTHETIC_CHANGED_INPUT")
    target = tmp_path / "FIXTURE_INPUTS/DYNAMIC_X_2026-05-01_entry.json"
    target.write_text('{"classification":"SYNTHETIC_CHANGED_INPUT"}', encoding="utf-8")
    monkeypatch.setattr(portfolio, "deadline", lambda: None)
    with pytest.raises(ValueError, match="PINNED|HASH|CHANGED"):
        portfolio.run_account("Q1", "A", tmp_path / "MOCK_EARNINGS.json", "SYNTHETIC_CHANGED_INPUT")


@pytest.mark.parametrize("recovery_state", ["missing", "failed"])
def test_final_file_without_successful_recovery_is_not_a_completed_account(monkeypatch, tmp_path, schedule, recovery_state):
    instances = setup(monkeypatch, tmp_path, schedule)
    version = "SYNTHETIC_FINAL_RECOVERY_" + recovery_state.upper()
    original = portfolio.run_account("Q1", "A", tmp_path / "MOCK_EARNINGS.json", version)
    out = portfolio.ROOT / "portfolio" / version / f"B12_Q1_P50_A_{version}"
    final_hash = digest(out / "FINAL_ACCOUNT.json")
    fills_hash = digest(out / "fills.csv")
    recovery_path = out / "RECOVERY_IDEMPOTENCY.json"
    if recovery_state == "missing":
        recovery_path.unlink()  # Synthetic tmp-only interrupted-finalization fixture.
    else:
        recovery_path.write_text('{"status":"FAIL","classification":"MOCK_RECOVERY_FAILURE_ONLY"}', encoding="utf-8")
    try:
        resumed = portfolio.run_account("Q1", "A", tmp_path / "MOCK_EARNINGS.json", version)
    except ValueError as error:
        assert "RECOVERY" in str(error) or "INCOMPLETE" in str(error)
        assert digest(out / "FINAL_ACCOUNT.json") == final_hash
        assert digest(out / "fills.csv") == fills_hash
        return  # Explicit incomplete evidence is also an acceptable safe outcome.
    assert resumed["buy_fills"] == original["buy_fills"]
    assert resumed["sell_fills"] == original["sell_fills"]
    assert not instances[-1].calls, "Finalization must not re-download/replay completed dates"
    assert recovery_path.exists(), "FINAL_ACCOUNT alone is not evidence of recovery PASS"
    recovery = json.loads(recovery_path.read_text(encoding="utf-8"))
    assert recovery["status"] == "PASS", "Known failed recovery cannot count as a completed account"
    assert digest(out / "fills.csv") == fills_hash


def test_completed_day_before_progress_write_restores_reports_without_replaying_day(monkeypatch, tmp_path, schedule):
    instances = setup(monkeypatch, tmp_path, schedule)
    actual_write = portfolio.write
    intercepted = False
    def interrupt_progress(path, payload):
        nonlocal intercepted
        if Path(path).name == "progress.json" and not intercepted:
            intercepted = True
            raise RuntimeError("MOCK_DAY_FINISHED_BEFORE_PROGRESS_COMMIT")
        return actual_write(path, payload)
    monkeypatch.setattr(portfolio, "write", interrupt_progress)
    version = "SYNTHETIC_DAY_PROGRESS_INTERRUPTION"
    with pytest.raises(RuntimeError, match="MOCK_DAY_FINISHED"):
        portfolio.run_account("Q1", "A", tmp_path / "MOCK_EARNINGS.json", version)
    out = portfolio.ROOT / "portfolio" / version / f"B12_Q1_P50_A_{version}"
    prior_clock = json.loads((out / "COMMON_CLOCK.json").read_text(encoding="utf-8"))
    prior_first = next(row for row in prior_clock if row["day"] == "2026-05-01")
    assert prior_first["new_fills"] == 2 and prior_first["event_count"] > 0
    prior_close = json.loads((out / "CLOSE_VALUATIONS.json").read_text(encoding="utf-8"))[0]
    monkeypatch.setattr(portfolio, "write", actual_write)
    result = portfolio.run_account("Q1", "A", tmp_path / "MOCK_EARNINGS.json", version)
    clocks = json.loads((out / "COMMON_CLOCK.json").read_text(encoding="utf-8"))
    close_values = json.loads((out / "CLOSE_VALUATIONS.json").read_text(encoding="utf-8"))
    assert len(clocks) == len({row["day"] for row in clocks}) == 5
    assert len(close_values) == len({row["trade_date"] for row in close_values}) == 3
    assert next(row for row in clocks if row["day"] == "2026-05-01") == prior_first
    assert next(row for row in close_values if row["trade_date"] == "2026-05-01") == prior_close
    assert not any(day == "2026-05-01" for _, day, _ in instances[-1].calls)
    assert result["buy_fills"] == 2 and result["sell_fills"] == 4
    assert result["latest_equity"]["cash"] == pytest.approx(4350.22, abs=.01)


def test_corporate_split_ratio_is_new_over_old_and_acquirer_does_not_get_target_gap():
    inputs = object.__new__(portfolio.ContinuousInputs)
    inputs.universe = {"NOW": {}, "BUYER": {}, "TARGET": {}}
    inputs.actions = {"corporate_actions": {
        "forward_splits": [{"symbol": "NOW", "ex_date": "2025-12-18", "new_rate": 5, "old_rate": 1, "id": "MOCK_SPLIT"}],
        "cash_mergers": [{"acquirer_symbol": "BUYER", "acquiree_symbol": "TARGET", "effective_date": "2026-02-01", "id": "MOCK_MERGER"}]}}
    events = inputs.corporate_events()
    assert next(event for event in events if event["kind"] == "SPLIT")["payload"]["ratio"] == 5
    gaps = [event for event in events if event["kind"] == "DATA_GAP"]
    assert [event["symbol"] for event in gaps] == ["TARGET"]
