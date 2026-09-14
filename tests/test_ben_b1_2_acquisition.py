"""Read-only acquisition pruning cannot alter executed shared cash accounts.

All natural signal, earnings, quote and account inputs below are explicitly
synthetic fixtures in pytest temporary directories. No market API is called.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from test_ben_b1_replay import schedule, earnings, quote, full_input, ny
from test_ben_b1_2_portfolio import FixtureContinuousInputs
from src.ben_b1_2 import portfolio
from src.ben_b1_2.replay import ReplayConfig
from src.ben_b1_2.shared import SharedReplayEngine


def run_fixture(monkeypatch, schedule, root, scenario, tier="A", prune=True):
    instances = []
    class Inputs(FixtureContinuousInputs):
        def earnings_events(self, requested_tier):
            values = [earnings("Y")]
            if scenario == "unknown":
                return values
            if scenario == "clock_recovery":
                values += [earnings("X", planned="2026-04-30", actual="2026-04-30 16:05:00", revision_id="MOCK_PRIOR"), earnings("X")]
            elif scenario == "window_revision":
                values.append(earnings("X", known="2026-05-01 16:05:01"))
            elif scenario == "held_becomes_unknown":
                values += [earnings("X"), earnings("X", planned=None, known="2026-05-04 00:00:00")]
            else:
                raise AssertionError("Unknown synthetic scenario")
            return values
        def quotes(self, symbol, day, holding):
            events, receipt = super().quotes(symbol, day, holding)
            if scenario == "window_revision" and symbol == "X" and not holding:
                events.append(quote(day + " 16:05:02", symbol))
            return events, receipt
    def factory(path):
        result = Inputs(schedule, root / "FIXTURE_INPUTS")
        instances.append(result)
        return result
    with monkeypatch.context() as patch:
        patch.setattr(portfolio, "ContinuousInputs", factory)
        patch.setattr(portfolio, "ROOT", root / "ONLY_TEST_OUTPUT")
        patch.setattr(portfolio, "START", "2026-05-01")
        patch.setattr(portfolio, "END", "2026-05-01")
        patch.setattr(portfolio, "TAIL_END", "2026-05-05")
        patch.setattr(portfolio, "deadline", lambda: None)
        patch.setattr(portfolio, "status", lambda *args, **kwargs: None)
        if not prune:
            def previous_price_only(engine, symbol, day, window_events):
                eligible = portfolio.eligible_for_quote(engine, symbol, day)
                return {"allowed": eligible, "price_candidate": eligible, "reason": "MOCK_COMPARATOR_ORIGINAL_PRICE_ONLY_ACQUISITION"}
            patch.setattr(portfolio, "acquisition_eligible", previous_price_only)
        # Keep tmp-only test paths below this Windows host's path-length limit.
        version = "MOCK_ACQ_" + {"unknown": "U", "clock_recovery": "C", "window_revision": "W", "held_becomes_unknown": "H"}[scenario]
        result = portfolio.run_account("Q1", tier, root / "MOCK_EARNINGS.json", version)
        out = portfolio.ROOT / "portfolio" / version / f"B12_Q1_P50_{tier}_{version}"
    return result, out, instances[-1]


def assert_financial_equality(left, right):
    l, lo, _ = left
    r, ro, _ = right
    for key in ("buy_intents", "buy_fills", "buy_shares", "sell_fills", "sell_shares",
                "latest_equity", "campaigns", "open_positions", "pending_exit_count", "errors"):
        assert l[key] == r[key], key
    for name in ("fills.csv", "orders.csv", "campaigns.csv", "account_events.csv", "continuous_close_equity.csv"):
        pd.testing.assert_frame_equal(pd.read_csv(lo / name), pd.read_csv(ro / name))
    assert r["latest_equity"]["initial_equity"] == 5500
    decisions = pd.read_csv(ro / "coverage_funnel.csv")
    assert decisions.symbol.nunique() == 66
    assert set(decisions[decisions.symbol.eq("X")].status)  # X remains in actual decisions.
    assert r["coverage_status"] == "COVERAGE_LIMITED"


@pytest.mark.parametrize("tier", ["A", "B"])
def test_empty_revision_pruning_keeps_finances_other_candidate_and_held_inputs_identical(monkeypatch, tmp_path, schedule, tier):
    full = run_fixture(monkeypatch, schedule, tmp_path / "unpruned", "unknown", tier, prune=False)
    pruned = run_fixture(monkeypatch, schedule, tmp_path / "pruned", "unknown", tier)
    assert_financial_equality(full, pruned)
    assert ("X", "2026-05-01", False) in full[2].calls
    assert ("X", "2026-05-01", False) not in pruned[2].calls
    assert ("Y", "2026-05-01", False) in pruned[2].calls
    assert ("Y", "2026-05-04", True) in pruned[2].calls
    evidence = json.loads((pruned[1] / "QUOTE_COVERAGE.json").read_text(encoding="utf-8"))
    skipped = next(row for row in evidence if row["symbol"] == "X" and row["day"] == "2026-05-01")
    assert skipped["status"] == "NOT_REQUESTED_EARNINGS_UNVERIFIABLE_THIS_WINDOW"
    assert skipped["rows"] is None and skipped["complete"] is None
    assert skipped["quote_availability"] == "NOT_CHECKED"
    assert pruned[0]["buy_fills"] == 1


@pytest.mark.parametrize("tier", ["A", "B"])
def test_clock_only_1605_earnings_recovery_is_never_pruned(monkeypatch, tmp_path, schedule, tier):
    full = run_fixture(monkeypatch, schedule, tmp_path / "unpruned", "clock_recovery", tier, prune=False)
    pruned = run_fixture(monkeypatch, schedule, tmp_path / "pruned", "clock_recovery", tier)
    assert_financial_equality(full, pruned)
    assert full[2].calls == pruned[2].calls
    assert pruned[0]["buy_fills"] == 2


def test_future_window_revision_keeps_quotes_but_cannot_approve_earlier_arrival(monkeypatch, tmp_path, schedule):
    full = run_fixture(monkeypatch, schedule, tmp_path / "unpruned", "window_revision", prune=False)
    pruned = run_fixture(monkeypatch, schedule, tmp_path / "pruned", "window_revision")
    assert_financial_equality(full, pruned)
    assert full[2].calls == pruned[2].calls
    orders = pd.read_csv(pruned[1] / "orders.csv")
    entry = orders[orders.symbol.eq("X") & orders.side.eq("BUY")].iloc[0]
    assert pd.Timestamp(entry.created_at) == pd.Timestamp(ny("2026-05-01 16:05:02"))
    assert pruned[0]["buy_fills"] == 2


def test_already_held_security_that_becomes_unknown_still_downloads_exit_inputs(monkeypatch, tmp_path, schedule):
    full = run_fixture(monkeypatch, schedule, tmp_path / "unpruned", "held_becomes_unknown", prune=False)
    pruned = run_fixture(monkeypatch, schedule, tmp_path / "pruned", "held_becomes_unknown")
    assert_financial_equality(full, pruned)
    assert ("X", "2026-05-04", True) in pruned[2].calls
    assert not pruned[0]["open_positions"] and pruned[0]["sell_fills"] > 0


def pure_engine(schedule, tmp_path, tier="B", revisions=True):
    engine = SharedReplayEngine(schedule, ReplayConfig(quote_mode="Q1", earnings_tier=tier,
                         synthetic_test=True, checkpoint_every=0),
               {"X": {"scope": "KEEP", "security_id": "MOCK_ONLY_X"}}, tmp_path / "checkpoint.json")
    events = [e for e in full_input(schedule, quotes=False) if pd.Timestamp(e["at"]) <= pd.Timestamp(ny("2026-05-01 16:01:00"))]
    if not revisions:
        events = [e for e in events if e["kind"] != "EARNINGS_REVISION"]
    engine.run(events)
    return engine


@pytest.mark.parametrize("coverage", [
    {"complete": False},
    {"complete": True, "start": "2026-05-02", "end": "2026-07-31"},
    {"complete": True, "start": "2026-01-01", "end": "2026-04-30"},
])
def test_b_fixed_coverage_boundaries_stay_unknown_without_mutating_engine(schedule, tmp_path, coverage):
    engine = pure_engine(schedule, tmp_path)
    engine.state["earnings_coverage"]["X"] = coverage  # Explicit synthetic coverage evidence.
    before = engine.state_digest()
    result = portfolio.acquisition_eligible(engine, "X", "2026-05-01", [])
    assert result["price_candidate"] and not result["allowed"]
    assert "COVERAGE" in result["reason"] or "WINDOW" in result["reason"]
    assert engine.state_digest() == before
    assert not engine._gate("X")["new_entry_allowed"]
    update = earnings("X", known="2026-05-01 16:06:00")
    assert portfolio.acquisition_eligible(engine, "X", "2026-05-01", [update])["allowed"]
    engine.close()


@pytest.mark.parametrize("symbol,at", [("X", "2026-05-01 16:15:00"), ("Y", "2026-05-01 16:06:00")])
def test_unrelated_or_expired_revision_cannot_clear_unknown_window(schedule, tmp_path, symbol, at):
    engine = pure_engine(schedule, tmp_path, tier="A", revisions=False)
    result = portfolio.acquisition_eligible(engine, "X", "2026-05-01", [earnings(symbol, known=at)])
    assert result["price_candidate"] and not result["allowed"]
    assert not engine.orders and engine.ledger.cash == 5500
    engine.close()
