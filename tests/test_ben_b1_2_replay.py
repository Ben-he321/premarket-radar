"""B1.2 offline engineering acceptance; synthetic inputs stay in pytest tmp_path.

The natural EMA fixtures are imported unchanged from B1.1. These checks never
create test orders directly, never call a data API, and are not research results.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import os

import pandas as pd
import pytest

from test_ben_b1_replay import (schedule, ny, ev, quote, daily, earnings, warmup,
                               full_input, fills, textual_trace)
from src.ben_b1.replay import ReplayEngine as OriginalEngine, ReplayConfig as OriginalConfig
from src.ben_b1.events import EarningsRevision, earnings_gate
from src.ben_b1_2.replay import ReplayEngine, ReplayConfig


EVIDENCE = {}


def record(name, engine, **facts):
    assert not engine.state["errors"]
    EVIDENCE[name] = {"assertions_completed": True, "buy_intents": len([o for o in engine.orders.values() if o["side"] == "BUY"]),
                      "buy_fills": len(fills(engine, "BUY")), "sell_fills": len(fills(engine, "SELL")),
                      "initial_equity": engine.ledger.config.initial_equity,
                      "cash": engine.ledger.cash, **facts}


@pytest.fixture(scope="module", autouse=True)
def save_b12_evidence():
    yield
    target = os.getenv("BEN_B1_2_ENGINE_EVIDENCE_DIR")
    if target:
        root = Path(__file__).resolve().parents[1]
        hashes = {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                  for name in ["src/ben_b1_2/replay.py", "src/ben_b1/replay.py", "src/ben_b1/ledger.py",
                               "tests/test_ben_b1_replay.py", "tests/test_ben_b1_2_replay.py"]}
        out = Path(target)
        out.mkdir(parents=True, exist_ok=True)
        (out / "B12_ENGINE_INTEGRATION.json").write_text(json.dumps({
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "classification": "OFFLINE_SYNTHETIC_ENGINEERING_NOT_REAL_MARKET_PERFORMANCE",
            "synthetic_storage": "pytest temporary directories only",
            "engine_invocation": "ReplayEngine.run/process; natural EMA signals; no manually created intents",
            "source_hashes": hashes, "scenarios": EVIDENCE,
        }, ensure_ascii=False, indent=2), encoding="utf-8")


def make(schedule, tmp_path, symbols=("X",), quote_mode="Q1", **kwargs):
    return ReplayEngine(schedule, ReplayConfig(quote_mode=quote_mode, synthetic_test=True,
                        checkpoint_every=0, **kwargs),
                        {s: {"security_id": "SYNTHETIC_ONLY_" + s, "scope": "KEEP"} for s in symbols},
                        checkpoint_path=tmp_path / "checkpoint.json")


def prefix(events, timestamp):
    return [event for event in events if pd.Timestamp(event["at"]) <= pd.Timestamp(ny(timestamp))]


def buy_orders(engine):
    return [order for order in engine.orders.values() if order["side"] == "BUY"]


@pytest.mark.parametrize("symbol,delay", [("BE", ".486314"), ("RIVN", ".790726")])
def test_delayed_first_quote_q0_rejects_q1_waits_for_arrival(schedule, tmp_path, symbol, delay):
    at = "2026-05-01 16:05:00" + delay
    events = full_input(schedule, symbols=(symbol,), quotes=False) + [quote(at, symbol)]
    q0 = make(schedule, tmp_path / "Q0", symbols=(symbol,), quote_mode="Q0")
    q1 = make(schedule, tmp_path / "Q1", symbols=(symbol,))
    q0.run(events)
    q1.run(prefix(events, "2026-05-01 16:05:00"))
    assert not buy_orders(q0) and not fills(q0, "BUY")
    assert not buy_orders(q1) and not fills(q1, "BUY")
    q1.run([event for event in events if pd.Timestamp(event["at"]) > pd.Timestamp(ny("2026-05-01 16:05:00"))])
    assert len(buy_orders(q1)) == len(fills(q1, "BUY")) == 1
    assert pd.Timestamp(buy_orders(q1)[0]["created_at"]) == pd.Timestamp(ny(at))
    assert pd.Timestamp(fills(q1, "BUY")[0]["at"]) >= pd.Timestamp(ny(at))
    record("delayed_arrival_" + symbol, q1, q0_orders=0, actual_first_arrival=ny(at),
           fixture_prices_are_synthetic=True, historical_timestamp_shape_only=True)


@pytest.mark.parametrize("bad,kwargs", [
    ("chase_over_one_percent", {"bid": 102.4, "ask": 102.5}),
    ("structure_broken", {"bid": 94.95, "ask": 95.}),
    ("spread_too_wide", {"bid": 100.0, "ask": 101.}),
    ("no_displayed_quantity", {"ask_size": 0}),
])
def test_first_bad_quote_never_backfills_later_recovery(schedule, tmp_path, bad, kwargs):
    engine = make(schedule, tmp_path)
    first = "2026-05-01 16:05:01"
    good = "2026-05-01 16:05:03"
    events = full_input(schedule, quotes=False) + [quote(first, **kwargs)]
    engine.run(prefix(events, first))
    assert not buy_orders(engine) and not fills(engine, "BUY")
    engine.run([quote(good)])
    assert len(buy_orders(engine)) == 1 and fills(engine, "BUY")
    assert pd.Timestamp(buy_orders(engine)[0]["created_at"]) == pd.Timestamp(ny(good))
    assert all(pd.Timestamp(fill["at"]) >= pd.Timestamp(ny(good)) for fill in fills(engine, "BUY"))
    record("bad_then_recovery_" + bad, engine, first_bad_quote=ny(first), first_intent_at=ny(good))


def test_first_display_quantity_must_itself_pass_net_two_r_before_intent(schedule, tmp_path):
    engine = make(schedule, tmp_path)
    events = full_input(schedule, quotes=False)
    bars = [event for event in events if event["kind"] == "DAILY_BAR"]
    bars[-20]["payload"]["high"] = 114.
    first, good = "2026-05-01 16:05:01", "2026-05-01 16:05:03"
    engine.run(prefix(events, first) + [quote(first, ask_size=1)])
    assert not buy_orders(engine) and not fills(engine, "BUY")
    assert engine.ledger.cash == 5500
    assert sum(row["ask_consumed"] for row in engine.state["quote_inventory"].values()) == 0
    engine.run([quote(good, ask_size=1000)])
    assert len(buy_orders(engine)) == 1
    assert pd.Timestamp(buy_orders(engine)[0]["created_at"]) == pd.Timestamp(ny(good))
    assert sum(fill["quantity"] for fill in fills(engine, "BUY")) == 27
    record("insufficient_initial_quantity_net_two_r", engine, rejected_one_share_creates_no_intent=True)


@pytest.mark.parametrize("at", ["2026-05-01 16:15:00", "2026-05-01 16:15:00.000001", "2026-05-01 16:16:00"])
def test_entry_window_right_endpoint_is_exclusive(schedule, tmp_path, at):
    engine = make(schedule, tmp_path)
    engine.run(full_input(schedule, quotes=False) + [quote(at)])
    assert not buy_orders(engine) and not fills(engine, "BUY")
    record("exclusive_expiry_" + at, engine, window="[close+5min,close+15min)")


def test_stale_pre_window_quote_is_not_first_new_arrival(schedule, tmp_path):
    engine = make(schedule, tmp_path)
    old = quote("2026-05-01 16:04:59", quote_id="BEFORE_WINDOW")
    poll = {**old, "event_id": "POLL_NO_NEW_DISPLAY", "at": ny("2026-05-01 16:05:01")}
    engine.run(full_input(schedule, quotes=False) + [old, poll])
    assert not buy_orders(engine) and not fills(engine, "BUY")
    record("poll_is_not_new_arrival", engine, before_window_quote_never_relabelled_as_new=True)


def test_duplicate_poll_checkpoint_recovery_and_later_fill_do_not_expand_order(schedule, tmp_path):
    engine = make(schedule, tmp_path)
    original = quote("2026-05-01 16:05:01", ask_size=10, quote_id="ONE_DISPLAY")
    first = prefix(full_input(schedule, quotes=False), "2026-05-01 16:05:01") + [original]
    engine.run(first)
    planned = buy_orders(engine)[0]["quantity"]
    assert sum(fill["quantity"] for fill in fills(engine, "BUY")) == 10
    engine.save()
    resumed = ReplayEngine.restore(tmp_path / "checkpoint.json", schedule)
    repeated = {**original, "event_id": "SAME_DISPLAY_OTHER_POLL", "at": ny("2026-05-01 16:05:02")}
    resumed.run([original, repeated])
    assert sum(fill["quantity"] for fill in fills(resumed, "BUY")) == 10
    assert sum(fill["commission"] for fill in fills(resumed, "BUY")) == 1
    resumed.run([quote("2026-05-01 16:05:03", ask_size=1000)])
    assert len(buy_orders(resumed)) == 1
    assert buy_orders(resumed)[0]["quantity"] == planned
    assert sum(fill["quantity"] for fill in fills(resumed, "BUY")) == planned
    assert sum(fill["commission"] for fill in fills(resumed, "BUY")) == 1
    record("duplicate_recovery_fixed_quantity", resumed, original_intent_quantity=planned,
           original_display_consumed_only_once=True, total_entry_commission=1.)


def test_unfinished_same_timestamp_group_survives_restart_before_any_intent(schedule, tmp_path):
    symbols = ("EARLY", "BETTER", "BEST")
    engine = make(schedule, tmp_path, symbols=symbols)
    engine.run(prefix(full_input(schedule, symbols=symbols, quotes=False), "2026-05-01 16:05:00"))
    timestamp = "2026-05-01 16:05:01"
    first = quote(timestamp, "EARLY", bid=100.75)
    engine.process(first)
    assert not buy_orders(engine)
    engine.save()
    resumed = ReplayEngine.restore(tmp_path / "checkpoint.json", schedule)
    resumed.process(first)  # Retrying the first event may not close the group.
    resumed.process(quote(timestamp, "BETTER", bid=100.9))
    resumed.process(quote(timestamp, "BEST", bid=100.99))
    assert not buy_orders(resumed)
    resumed.flush_quote_batch()
    assert [fill["symbol"] for fill in fills(resumed, "BUY")] == ["BEST", "BETTER"]
    assert len(buy_orders(resumed)) == 2 and resumed.ledger.cash >= 0
    snapshot = resumed.to_dict()
    resumed.flush_quote_batch()
    assert resumed.to_dict() == snapshot
    record("unfinished_group_restart_then_rank", resumed, no_order_before_complete_group=True,
           repeated_flush_added_orders=0, winners=["BEST", "BETTER"])


def test_partial_orders_reserve_one_common_cash_pool(schedule, tmp_path):
    symbols = ("ONE", "TWO", "THREE")
    engine = make(schedule, tmp_path, symbols=symbols)
    events = prefix(full_input(schedule, symbols=symbols, quotes=False), "2026-05-01 16:05:00")
    events += [quote("2026-05-01 16:05:01", "ONE", ask_size=10, bid=100.99),
               quote("2026-05-01 16:05:01", "TWO", ask_size=10, bid=100.95),
               quote("2026-05-01 16:05:01", "THREE", ask_size=10, bid=100.9)]
    engine.run(events)
    assert len(buy_orders(engine)) == 2
    assert all(order["filled_quantity"] == 10 and order["quantity"] > 10 for order in buy_orders(engine))
    reserved_unfilled = sum((order["quantity"]-order["filled_quantity"])*order["limit"] + 1
                            for order in buy_orders(engine))
    assert reserved_unfilled + engine.ledger.reserved_exit_fees <= engine.ledger.cash + .01
    assert engine.ledger.cash >= 0 and "THREE" not in engine.ledger.positions
    record("shared_unfilled_cash_reservations", engine, reserved_unfilled=reserved_unfilled,
           remaining_cash=engine.ledger.cash, only_one_5500_account=True)


def test_partial_entry_remainder_remains_reserved_when_held_quote_mark_rises(schedule, tmp_path):
    """A mark change cannot spend cash already promised to an earlier intent.

    Fixed natural signal fixtures; the large synthetic quote move only stresses
    accounting. It is not a new signal threshold or a historical research input.
    """
    from src.ben_b1_2.shared import SharedReplayEngine
    symbols = ("FIRST", "SECOND")
    engine = SharedReplayEngine(schedule, ReplayConfig(quote_mode="Q1", synthetic_test=True, checkpoint_every=0),
        {s: {"security_id": "SYNTHETIC_ONLY_"+s, "scope": "KEEP"} for s in symbols},
        checkpoint_path=tmp_path / "checkpoint.json")
    base = prefix(full_input(schedule, symbols=symbols, quotes=False), "2026-05-01 16:05:00")
    engine.run(base + [quote("2026-05-01 16:05:01", "FIRST", ask_size=10)])
    first = buy_orders(engine)[0]
    frozen_quantity, frozen_limit = first["quantity"], first["limit"]
    assert first["status"] == "OPEN" and first["filled_quantity"] == 10
    engine.run([quote("2026-05-01 16:05:02", "FIRST", bid=130., ask=130.05),
                quote("2026-05-01 16:05:03", "SECOND", ask_size=10)])
    assert first["quantity"] == frozen_quantity and first["limit"] == frozen_limit
    assert first["filled_quantity"] == 10
    open_entries = [o for o in buy_orders(engine) if o["status"] == "OPEN"]
    remaining_notional = sum((o["quantity"]-o["filled_quantity"])*o["limit"] for o in open_entries)
    unpaid_entry_fees = sum(engine.config.commission for o in open_entries if o["order_id"] not in engine.ledger.orders)
    commitments = remaining_notional + unpaid_entry_fees + engine.ledger.reserved_exit_fees
    second_decision = next(d for d in engine.state["decisions"] if d.get("symbol") == "SECOND" and d.get("status") == "INTENT_CREATED")
    assert second_decision["net_equity_before_candidate"] == pytest.approx(5787.99)
    assert second_decision["cash_before_candidate"] == pytest.approx(4487.99)
    assert second_decision["displayed_ask_remaining_before_candidate"] == 10
    assert second_decision["equity_valuation_status_before_candidate"] == "CURRENT_MARKS"
    assert second_decision["intended_execution_friction_bps"] == engine.config.friction_bps
    details = {"cash": engine.ledger.cash, "commitments": commitments,
               "remaining_entry_notional": remaining_notional, "reserved_exit_fees": engine.ledger.reserved_exit_fees,
               "orders": [{k: o[k] for k in ["symbol", "quantity", "filled_quantity", "limit", "status"]} for o in buy_orders(engine)]}
    assert commitments <= engine.ledger.cash + .01, details
    engine.close()


def test_quote_availability_and_final_bar_availability_are_not_backdated(schedule, tmp_path):
    engine = make(schedule, tmp_path)
    events = full_input(schedule, quotes=False)
    last_bar = next(event for event in events if event["kind"] == "DAILY_BAR"
                    and event["payload"]["session_date"] == "2026-05-01")
    last_bar["at"] = ny("2026-05-01 16:06:00")
    last_bar["payload"]["available_at"] = last_bar["at"]
    early = quote("2026-05-01 16:05:01")
    engine.run(prefix(events, "2026-05-01 16:05:01") + [early])
    assert not buy_orders(engine)
    engine.run([last_bar])
    assert not buy_orders(engine)  # A bar arriving later cannot reuse the old quote arrival.
    delayed = quote("2026-05-01 16:06:01", available_at=ny("2026-05-01 16:06:02"))
    engine.run([delayed])
    assert not buy_orders(engine)
    engine.run([quote("2026-05-01 16:06:03")])
    assert pd.Timestamp(buy_orders(engine)[0]["created_at"]) == pd.Timestamp(ny("2026-05-01 16:06:03"))
    record("data_availability_never_backdated", engine,
           daily_bar_delivered_at=ny("2026-05-01 16:06:00"),
           assumption_not_free_live_feed_verification=True)


@pytest.mark.parametrize("sequential", [False, True])
def test_common_capital_two_slots_and_causal_ranking(schedule, tmp_path, sequential):
    symbols = ("EARLY", "SECOND", "FUTURE")
    engine = make(schedule, tmp_path, symbols=symbols)
    events = full_input(schedule, symbols=symbols, quotes=False)
    if sequential:
        events += [quote("2026-05-01 16:05:01", "EARLY", bid=100.75),
                   quote("2026-05-01 16:05:02", "SECOND", bid=100.90),
                   quote("2026-05-01 16:05:03", "FUTURE", bid=100.99)]
        expected = ["EARLY", "SECOND"]
    else:
        events += [quote("2026-05-01 16:05:01", "EARLY", bid=100.75),
                   quote("2026-05-01 16:05:01", "SECOND", bid=100.90),
                   quote("2026-05-01 16:05:01", "FUTURE", bid=100.99)]
        expected = ["FUTURE", "SECOND"]
    engine.run(list(reversed(events)))  # Caller order cannot select a winner.
    assert [fill["symbol"] for fill in fills(engine, "BUY")] == expected
    assert len(engine.ledger.positions) == 2
    assert engine.ledger.config.initial_equity == 5500
    assert engine.ledger.cash >= engine.ledger.reserved_exit_fees >= 0
    assert sum(fill["quantity"] * fill["execution_price"] + fill["commission"]
               for fill in fills(engine, "BUY")) + engine.ledger.cash == pytest.approx(5500, abs=.02)
    record("common_capital_" + ("successive" if sequential else "simultaneous"), engine,
           winners=expected, accounts=1, shared_initial_cash=5500, maximum_positions=2)


def test_future_quotes_cannot_change_existing_intent_or_quantity(schedule, tmp_path):
    before = []
    for bid, label in [(100.99, "tight"), (100.71, "wide")]:
        engine = make(schedule, tmp_path / label, symbols=("FIRST", "LATER"))
        events = full_input(schedule, symbols=("FIRST", "LATER"), quotes=False)
        events += [quote("2026-05-01 16:05:01", "FIRST", bid=100.8),
                   quote("2026-05-01 16:05:05", "LATER", bid=bid)]
        engine.run(events)
        before.append([row for row in buy_orders(engine) if row["symbol"] == "FIRST"])
    assert before[0] == before[1] and len(before[0]) == 1
    record("future_window_quotes_do_not_reorder_past", engine, identical_first_intent=True)


@pytest.mark.parametrize("tier", ["A", "B"])
def test_missing_earnings_is_unknown_in_both_layers(schedule, tmp_path, tier):
    engine = make(schedule, tmp_path, earnings_tier=tier)
    events = [event for event in full_input(schedule, quotes=False) if event["kind"] != "EARNINGS_REVISION"]
    engine.run(events + [quote("2026-05-01 16:05:01")])
    assert not buy_orders(engine) and not fills(engine, "BUY")
    assert "UNKNOWN" in textual_trace(engine)
    record("missing_earnings_layer_" + tier, engine, no_quote_can_override_earnings_unknown=True)


@pytest.mark.parametrize("tier,expected", [("A", 0), ("B", 1)])
def test_actual_release_only_never_becomes_pit_plan(schedule, tmp_path, tier, expected):
    engine = make(schedule, tmp_path, earnings_tier=tier)
    events = [event for event in full_input(schedule, quotes=False) if event["kind"] != "EARNINGS_REVISION"]
    release = ev("EARNINGS_REVISION", "2026-01-01 09:00:00", revision={
        "event_id": "MOCK_ACTUAL_DATE_ONLY", "actual_release_date": "2026-07-30",
        "actual_time_precision": "DATE", "source": "SYNTHETIC_TEST_ONLY",
        "known_at": None, "planned_date": None, "source_received_at": None,
    }, coverage_complete=True, coverage_start="2026-01-01", coverage_end="2026-07-30")
    engine.run(events + [release, quote("2026-05-01 16:05:01")])
    assert len(buy_orders(engine)) == expected
    assert all(decision.get("earnings", {}).get("strict_eligible") is not True for decision in engine.state["decisions"])
    assert "UNKNOWN" in textual_trace(engine)
    record("actual_only_layer_" + tier, engine, expected_intents=expected,
           historical_receipt_not_fabricated=True, actual_day_not_promoted_to_prior_plan=True)


@pytest.mark.parametrize("tier", ["A", "B"])
def test_release_date_precision_and_first_complete_recovery_session_remain_distinct(schedule, tmp_path, tier):
    retrospective = tier == "B"
    earlier = EarningsRevision(event_id="MOCK_Q1", planned_date="2026-04-30",
        known_at=ny("2026-04-01 10:00:00"), actual_release_date="2026-04-30",
        actual_time_precision="DATE", source="SYNTHETIC_TEST_ONLY", source_received_at=None)
    later = EarningsRevision(event_id="MOCK_Q2", planned_date="2026-07-30",
        known_at=ny("2026-04-01 10:00:00"), actual_release_date="2026-07-30",
        actual_time_precision="DATE", source="SYNTHETIC_TEST_ONLY", source_received_at=None)
    before = earnings_gate(ny("2026-05-01 16:04:59"), schedule, [earlier, later], retrospective=retrospective)
    after = earnings_gate(ny("2026-05-01 16:05:00"), schedule, [earlier, later], retrospective=retrospective)
    assert not before["new_entry_allowed"] and after["new_entry_allowed"]
    assert after["strict_eligible"] == (tier == "A")
    assert all(item["source_received_at"] == "UNKNOWN" for item in after["evidence"])
    assert all(item["actual_release_at"] == "UNKNOWN" for item in after["evidence"])
    assert all(item["actual_time_precision"] == "DATE" for item in after["evidence"])
    EVIDENCE["release_recovery_date_only_" + tier] = {"assertions_completed": True,
        "engine_method": "Inherited earnings_gate called by ReplayEngine", "tier": tier,
        "before_recovery_allowed": False, "after_recovery_allowed": True,
        "date_precision_retained": True, "received_at_kept_unknown": True}


def test_newly_received_plan_reschedule_cancels_unfilled_remainder_without_backdating(schedule, tmp_path):
    engine = make(schedule, tmp_path)
    events = prefix(full_input(schedule, quotes=False), "2026-05-01 16:05:01")
    events += [quote("2026-05-01 16:05:01", ask_size=10)]
    engine.run(events)
    assert sum(fill["quantity"] for fill in fills(engine, "BUY")) == 10
    arrived = "2026-05-01 16:05:02"
    engine.run([earnings(planned="2026-05-05", known=arrived, actual="2026-05-05 16:05:00"),
                quote("2026-05-01 16:05:03", ask_size=1000)])
    assert sum(fill["quantity"] for fill in fills(engine, "BUY")) == 10
    assert all(pd.Timestamp(order["created_at"]) >= pd.Timestamp(ny(arrived))
               for order in engine.orders.values() if order["side"] == "SELL")
    assert not fills(engine, "SELL")  # After-hours risk awareness is not a fabricated RTH fill.
    record("late_reschedule_cancels_without_backdating", engine, revision_arrived_at=ny(arrived),
           entry_shares_before_and_after_revision=10, no_retroactive_exit=True)


def test_one_missing_security_does_not_hide_healthy_execution_or_unknown_portfolio_nav(schedule, tmp_path):
    engine = make(schedule, tmp_path, symbols=("BAD", "GOOD"))
    events = full_input(schedule, symbols=("BAD", "GOOD"), quotes=False)
    events = [event for event in events if not (event["kind"] == "DAILY_BAR" and event.get("symbol") == "BAD")]
    events += [ev("DATA_GAP", "2026-05-01 16:01:00", "BAD", reason="MOCK_SYMBOL_COVERAGE_MISSING"),
               quote("2026-05-01 16:05:01", "BAD"), quote("2026-05-01 16:05:01", "GOOD")]
    engine.run(events)
    assert {fill["symbol"] for fill in fills(engine, "BUY")} == {"GOOD"}
    summary = engine.summary()
    assert "COVERAGE_LIMITED" in json.dumps(summary)
    engine.run([ev("DATA_GAP", "2026-05-04 16:01:00", "GOOD", reason="MOCK_HELD_MARK_MISSING"),
                ev("ENTRY_DECISION", "2026-05-04 16:05:00", "", session_date="2026-05-04"),
                ev("ENTRY_EXPIRY", "2026-05-04 16:15:00", "", session_date="2026-05-04")])
    final = engine.summary()
    assert final["latest_equity"]["net_equity"] is None
    assert "GOOD" in final["latest_equity"]["missing_current_marks"]
    assert "COVERAGE_LIMITED" in json.dumps(final)
    record("coverage_limited_and_held_nav_unknown", engine, healthy_symbol_traded=True,
           missing_candidate_disclosed=True, stale_entry_price_not_used_for_current_nav=True)
