"""OFFLINE SYNTHETIC end-to-end replay acceptance, never research returns.

Every order below must originate from the real replay's computed EMA signal.
No tests pre-fund positions, manufacture intents, or monkeypatch strategy rules.
Only the immutable synthetic event stream and the exchange calendar are inputs.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

import pandas as pd
import pandas_market_calendars as mcal
import pytest

from src.ben_b1.replay import ReplayConfig, ReplayEngine, calendar_events


NY = "America/New_York"
EVIDENCE: dict = {}


def ny(value):
    return pd.Timestamp(value, tz=NY).isoformat()


def ev(kind, at, symbol="X", event_id=None, **payload):
    return {"kind": kind, "at": ny(at), "symbol": symbol,
            "event_id": event_id or f"MOCK:{symbol}:{kind}:{at}", "payload": payload}


@pytest.fixture(scope="module")
def schedule():
    return mcal.get_calendar("NYSE").schedule("2025-01-01", "2026-12-31")


@pytest.fixture(scope="module", autouse=True)
def integration_evidence():
    """Persist assertion evidence only; synthetic prices/ledgers stay in tmp_path."""
    yield
    output = os.getenv("BEN_B1_ENGINE_EVIDENCE_DIR")
    if output:
        destination = Path(output)
        destination.mkdir(parents=True, exist_ok=True)
        source = Path(__file__).resolve().parents[1] / "src" / "ben_b1" / "replay.py"
        document = {
            "classification": "OFFLINE_SYNTHETIC_ENGINEERING_NOT_STRATEGY_PERFORMANCE",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "engine_source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "ledger_source_sha256": hashlib.sha256((source.parent / "ledger.py").read_bytes()).hexdigest(),
            "real_adapter_source_sha256": hashlib.sha256((source.parent / "b11_research.py").read_bytes()).hexdigest(),
            "test_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "engine_invocation": "ReplayEngine.run/process on natural EMA inputs, no manual intents",
            "required_groups": list("ABCDEFGH"),
            "asserted_scenarios": EVIDENCE,
            "groups_observed": sorted({item["group"] for item in EVIDENCE.values()}),
            "synthetic_data_storage": "pytest temporary directories only",
            "real_market_validation": False,
        }
        (destination / "ENGINE_INTEGRATION.json").write_text(
            json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")


def evidence(name, group, engine, **facts):
    EVIDENCE[name] = {"group": group, "assertions_completed": True,
        "orders": len(engine.ledger.orders), "fills": len(engine.ledger.fills),
        "open_positions": len(engine.ledger.positions),
        "closed_campaigns": engine.ledger.campaign_summary()["closed_campaigns"], **facts}


def make_engine(schedule, tmp_path, symbols=("X",), **config):
    return ReplayEngine(schedule, ReplayConfig(synthetic_test=True, **config),
                        {s: {"security_id": f"MOCK_ONLY_{s}", "scope": "KEEP"} for s in symbols},
                        checkpoint_path=tmp_path / "replay_checkpoint.json")


def daily(day, close, symbol="X", high=None, low=None, **extra):
    return ev("DAILY_BAR", f"{day} 16:00:00", symbol,
              session_date=day, open=close, high=close+.2 if high is None else high,
              low=close-.2 if low is None else low, close=close, volume=100_000,
              regular_session_verified=True, action_units_verified=True,
              source_received_at="UNKNOWN", **extra)


def warmup(schedule, symbol="X", signal=True):
    sessions = schedule.loc[:"2026-05-01"].tail(111)
    closes = [100.]*100 + [94.]*10 + [101. if signal else 94.]
    return [daily(str(day.date()), close, symbol) for day, close in zip(sessions.index, closes)]


def earnings(symbol="X", planned="2026-07-30", known="2026-01-01 09:00:00",
             actual="2026-07-30 16:05:00", revision_id="MOCK_Q2", **extra):
    return ev("EARNINGS_REVISION", known, symbol, event_id=f"{symbol}:{revision_id}:{known}",
              revision={"event_id": revision_id, "planned_date": planned, "known_at": ny(known),
                        "actual_release_at": ny(actual), "release_known_at": ny(actual),
                        "source": "SYNTHETIC_TEST_ONLY", "time_class": "AMC"},
              coverage_complete=True, **extra)


def quote(at, symbol="X", bid=100.95, ask=101., bid_size=1000, ask_size=1000,
          quote_id=None, **extra):
    identifier = quote_id or f"QUOTE:{symbol}:{at}"
    return ev("QUOTE", at, symbol, event_id=f"EVENT:{identifier}:{at}", bid=bid, ask=ask,
              bid_size=bid_size, ask_size=ask_size, timestamp=ny(at), size_unit="shares",
              quote_id=identifier, source_received_at="UNKNOWN", **extra)


def full_input(schedule, end="2026-05-01", signal=True, symbols=("X",), quotes=True):
    result = list(calendar_events(schedule, "2026-05-01", end))
    for symbol in symbols:
        result += warmup(schedule, symbol, signal) + [earnings(symbol)]
        if quotes:
            result.append(quote("2026-05-01 16:05:00", symbol, ask_size=10))
    return result


def fills(engine, side):
    return [f for f in engine.ledger.fills.values() if f["side"] == side]


def textual_trace(engine):
    return json.dumps(engine.to_dict(), default=str, sort_keys=True)


def test_a_two_partial_buys_near_stop_far_trail_then_calendar_settlement(schedule, tmp_path):
    engine = make_engine(schedule, tmp_path)
    events = full_input(schedule, "2026-05-06") + [
        quote("2026-05-01 16:05:02", ask_size=100),
        quote("2026-05-04 09:30:00", bid=95.50, ask=95.55),
        daily("2026-05-04", 102.),
        quote("2026-05-05 09:30:00", bid=95.90, ask=95.95),
        daily("2026-05-05", 96.), daily("2026-05-06", 96.),
    ]
    engine.run(events)
    bought, sold = fills(engine, "BUY"), fills(engine, "SELL")
    assert len(bought) == 2
    assert [f["quantity"] for f in bought] == [10, 17]
    assert len({f["order_id"] for f in bought}) == 1
    assert sum(f["commission"] for f in bought) == 1
    assert [(f["quantity"], f["observed_price"]) for f in sold] == [(14, 95.50), (13, 95.90)]
    assert not engine.ledger.positions and not engine.ledger.settlements
    assert engine.ledger.campaign_summary()["closed_campaigns"] == 1
    assert sum(e.get("type") == "SALE_SETTLEMENT" for e in engine.ledger.events) == 2
    assert engine.ledger.cash < 5500  # The gap/stop losses are kept, not clipped.
    evidence("a_complete_two_leg_cycle", "A", engine,
             partial_buy_quantities=[10, 17], near_leg_exit_quantity=14,
             far_leg_exit_quantity=13, settled_sales=2, final_cash_positive=engine.ledger.cash > 0)


def test_b_same_quote_different_poll_and_restart_cannot_reuse_size_or_commission(schedule, tmp_path):
    engine = make_engine(schedule, tmp_path)
    events = full_input(schedule)
    original = quote("2026-05-01 16:05:00", ask_size=10, quote_id="ONE_DISPLAY")
    events = [event for event in events if event["kind"] != "QUOTE"] + [original]
    engine.run([event for event in events if pd.Timestamp(event["at"]) <= pd.Timestamp(ny("2026-05-01 16:05:00"))])
    assert sum(f["quantity"] for f in fills(engine, "BUY")) == 10
    engine.save()
    resumed = ReplayEngine.restore(tmp_path / "replay_checkpoint.json", schedule)
    resumed.process(original)
    duplicate = {**original, "event_id": "DIFFERENT_POLL_SAME_QUOTE", "at": ny("2026-05-01 16:05:02")}
    resumed.process(duplicate)
    assert sum(f["quantity"] for f in fills(resumed, "BUY")) == 10
    assert sum(f["commission"] for f in fills(resumed, "BUY")) == 1
    resumed.process(quote("2026-05-01 16:05:03", ask_size=1, quote_id="NEW_DISPLAY"))
    assert sum(f["quantity"] for f in fills(resumed, "BUY")) == 11
    assert sum(f["commission"] for f in fills(resumed, "BUY")) == 1
    evidence("b_display_inventory_survives_restart", "B", resumed,
             duplicate_poll_added_shares=0, new_display_added_shares=1, entry_commissions=1)


def test_b_partial_rr_rejection_is_atomic_then_larger_new_quote_can_fill(schedule, tmp_path):
    engine = make_engine(schedule, tmp_path)
    events = full_input(schedule)
    bars = [event for event in events if event["kind"] == "DAILY_BAR"]
    bars[-20]["payload"]["high"] = 114.  # Fixed known pressure; planned27 pass, actual1 fails net2R.
    for event in events:
        if event["kind"] == "QUOTE":
            event["payload"]["ask_size"] = 1
    cut = pd.Timestamp(ny("2026-05-01 16:05:00"))
    engine.run([event for event in events if pd.Timestamp(event["at"]) <= cut])
    assert len(engine.orders) == 1 and not engine.ledger.fills
    assert engine.ledger.cash == 5500
    assert "PARTIAL_QUANTITY_NET_RR_BELOW_2" in textual_trace(engine)
    assert sum(q["ask_consumed"] for q in engine.state["quote_inventory"].values()) == 0
    engine.process(quote("2026-05-01 16:05:02", ask_size=1000))
    assert sum(f["quantity"] for f in fills(engine, "BUY")) == 27
    assert sum(f["commission"] for f in fills(engine, "BUY")) == 1
    evidence("b_partial_fill_quantity_rr_atomicity", "B", engine,
             rejected_one_share_used_cash=0, rejected_one_share_used_inventory=0,
             later_new_display_qualified_quantity=27)


def test_b_near_and_far_exit_orders_share_one_bid_inventory(schedule, tmp_path):
    engine = make_engine(schedule, tmp_path)
    engine.run(full_input(schedule))
    original = quote("2026-05-04 09:30:00", bid=80., ask=80.05, bid_size=6, quote_id="SINGLE_GAP_DISPLAY")
    engine.process(original)
    assert [f["quantity"] for f in fills(engine, "SELL")] == [5, 1]
    engine.save()
    resumed = ReplayEngine.restore(tmp_path / "replay_checkpoint.json", schedule)
    resumed.process({**original, "event_id": "REPEATED_GAP_POLL", "at": ny("2026-05-04 09:30:02")})
    assert sum(f["quantity"] for f in fills(resumed, "SELL")) == 6
    resumed.process(quote("2026-05-04 09:30:03", bid=80., ask=80.05, bid_size=4))
    assert not resumed.ledger.positions
    assert sum(f["quantity"] for f in fills(resumed, "SELL")) == 10
    assert sum(f["commission"] for f in fills(resumed, "SELL")) == 2
    evidence("b_two_stop_orders_share_bid_inventory", "B", resumed,
             first_display_limit=6, first_display_total_sold=6,
             repeated_display_extra_sold=0, exit_order_commissions=2)


def test_c_symbol_data_failure_does_not_block_healthy_symbol(schedule, tmp_path):
    engine = make_engine(schedule, tmp_path, symbols=("BAD", "GOOD"))
    events = full_input(schedule, symbols=("BAD", "GOOD"))
    for event in events:
        if (event["kind"] == "DAILY_BAR" and event.get("symbol") == "BAD"
                and event["payload"]["session_date"] == "2026-05-01"):
            event["payload"]["high"] = 0.  # Truly invalid input, not a signal override.
    events += [ev("DATA_GAP", "2026-05-01 16:01:00", "BAD", reason="MOCK_EXACT_DATE_INPUT_FAILURE")]
    engine.run(events)
    assert fills(engine, "BUY")
    assert {f["symbol"] for f in fills(engine, "BUY")} == {"GOOD"}
    assert "MOCK_EXACT_DATE_INPUT_FAILURE" in textual_trace(engine)
    assert any(error["symbol"] == "BAD" for error in engine.state["errors"])
    evidence("c_symbol_failure_isolated", "C", engine, failed_symbol="BAD", filled_symbol="GOOD")


@pytest.mark.parametrize("symbols,expected_buys", [(("X",), 1), (("X", "Y"), 0)])
def test_c_missing_secondary_volume_only_blocks_when_tie_needs_it(schedule, tmp_path, symbols, expected_buys):
    engine = make_engine(schedule, tmp_path, symbols=symbols)
    events = full_input(schedule, symbols=symbols)
    x_bars = [e for e in events if e["kind"] == "DAILY_BAR" and e.get("symbol") == "X"]
    for event in x_bars[-21:-1]:
        event["payload"]["volume"] = None
    engine.run(events)
    assert len(fills(engine, "BUY")) == expected_buys
    if len(symbols) == 1:
        assert engine.state["decisions"][0]["ranking_secondary_status"] == "RANKING_SECONDARY_NOT_NEEDED"
    else:
        assert all(row["reason"] == "TIED_SPREAD_SECONDARY_DOLLAR_VOLUME_UNKNOWN"
                   for row in engine.state["decisions"])
    evidence("c_secondary_volume_tie_" + str(len(symbols)), "C", engine,
             same_primary_spread_candidates=len(symbols), actual_buy_fills=expected_buys,
             missing_volume_never_zero_filled=True)


def test_d_late_earnings_revision_never_backdates_exit(schedule, tmp_path):
    engine = make_engine(schedule, tmp_path)
    events = full_input(schedule, "2026-05-06") + [
        daily("2026-05-04", 101.),
        earnings(planned="2026-05-07", known="2026-05-05 12:00:00", actual="2026-05-07 16:05:00"),
        quote("2026-05-05 12:00:00", bid=100.95, ask=101.),
        daily("2026-05-05", 101.), daily("2026-05-06", 101.),
    ]
    engine.run(events)
    assert fills(engine, "BUY") and fills(engine, "SELL")
    assert all(pd.Timestamp(f["at"]) >= pd.Timestamp(ny("2026-05-05 12:00:00")) for f in fills(engine, "SELL"))
    assert all("EARNINGS" in f["reason"] for f in fills(engine, "SELL"))
    assert "UNKNOWN" in textual_trace(engine)
    evidence("d_late_revision_causal_exit", "D", engine,
             earliest_exit_not_before_revision=True, unknown_source_receipt_not_fabricated=True)


def test_e_1550_decision_cannot_consume_final_1600_bar(schedule, tmp_path):
    before = []
    for final_close, label in [(101., "normal"), (500., "future_extreme")]:
        engine = make_engine(schedule, tmp_path / label)
        events = full_input(schedule, "2026-05-04") + [
            ev("MINUTE_BAR", "2026-05-04 15:50:00", bar_end=ny("2026-05-04 15:50:00"),
               open=101., high=101.1, low=100.9, close=101., volume=1000),
            quote("2026-05-04 15:50:00"), daily("2026-05-04", final_close),
        ]
        engine.run(events)
        actual = [row for row in engine.trace if row["kind"] == "ACTIVE_CAUSAL_EVALUATION"]
        assert len(actual) == 1
        assert actual[0]["price"] == 101.
        assert actual[0]["daily_input_cutoff"] == "2026-05-01"
        assert actual[0]["last_bar_end"] == ny("2026-05-04 15:50:00")
        assert not actual[0]["deviation"]["triggered"]
        before.append(actual)
    assert before[0] == before[1]
    evidence("e_intraday_future_bar_excluded", "E", engine,
             identical_account_through_1550=True, future_final_bars_different=True)


def test_e_actual_deviation_reduces_half_once_and_preserves_other_leg(schedule, tmp_path):
    engine = make_engine(schedule, tmp_path)
    events = full_input(schedule, "2026-05-05") + [
        ev("MINUTE_BAR", "2026-05-04 15:50:00", bar_end=ny("2026-05-04 15:50:00"),
           open=120., high=120.1, low=119.9, close=120., volume=1000),
        quote("2026-05-04 15:50:00", bid=119.95, ask=120.), daily("2026-05-04", 120.),
        ev("MINUTE_BAR", "2026-05-05 15:50:00", bar_end=ny("2026-05-05 15:50:00"),
           open=150., high=150.1, low=149.9, close=150., volume=1000),
        quote("2026-05-05 15:50:00", bid=149.95, ask=150.), daily("2026-05-05", 150.),
    ]
    engine.run(events)
    sold = fills(engine, "SELL")
    assert len(sold) == 1 and sold[0]["quantity"] == 5
    assert sold[0]["reason"] == "DEVIATION_REDUCTION"
    assert engine.ledger.positions["X"]["quantity"] == 5
    assert not engine.state["errors"]
    evidence("e_positive_deviation_once_per_campaign", "E", engine,
             original_quantity=10, reduced_quantity=5, remaining_quantity=5)


@pytest.mark.parametrize("daily_delay_minutes", [0, 1])
def test_e_two_pressure_failures_and_simultaneous_deviation_do_not_over_sell(schedule, tmp_path, daily_delay_minutes):
    engine = make_engine(schedule, tmp_path)
    events = full_input(schedule, "2026-05-06")
    bars = [event for event in events if event["kind"] == "DAILY_BAR"]
    bars[-20]["payload"]["high"] = 114.
    for event in events:
        if event["kind"] == "QUOTE":
            event["payload"]["ask_size"] = 1000
    events += [daily("2026-05-04", 113., high=114., low=112.8),
        ev("MINUTE_BAR", "2026-05-05 15:50:00", bar_end=ny("2026-05-05 15:50:00"),
           open=113., high=114., low=112.8, close=113., volume=1000),
        quote("2026-05-05 15:50:00", bid=112.95, ask=113.),
        daily("2026-05-05", 113., high=114., low=112.8), daily("2026-05-06", 113.)]
    if daily_delay_minutes:
        for event in events:
            if event["kind"] == "DAILY_BAR" and event["payload"]["session_date"] >= "2026-05-01":
                event["at"] = (pd.Timestamp(event["at"]) + pd.Timedelta(minutes=daily_delay_minutes)).isoformat()
                event["payload"]["available_at"] = event["at"]
    engine.run(events)
    assert not engine.state["errors"]
    assert sum(f["quantity"] for f in fills(engine, "BUY")) == 27
    assert sum(f["quantity"] for f in fills(engine, "SELL")) == 27
    assert not engine.ledger.positions
    pressure = [row for row in engine.trace if row["kind"] == "PRESSURE_EVALUATION"]
    assert any(row["reason"] == "FIRST_CLOSE_FAILURE" for row in pressure)
    assert any(row["action"] == "EXIT_NOW" for row in pressure)
    assert any(f["reason"] == "TWO_PRESSURE_FAILURES" for f in fills(engine, "SELL"))
    assert any(pd.Timestamp(row["session_close"]).date().isoformat() == "2026-05-04"
               for row in engine.state["repairs"]["X"])
    if daily_delay_minutes:
        first = next(row for row in pressure if row["reason"] == "FIRST_CLOSE_FAILURE")
        assert pd.Timestamp(first["at"]).tz_convert(NY) == pd.Timestamp(ny("2026-05-04 16:01:00"))
    evidence(f"e_two_failures_simultaneous_exit_delay_{daily_delay_minutes}", "E", engine,
             original_quantity=27, total_exits=27, no_oversell_or_event_rollback=True,
             late_daily_delivery_minutes=daily_delay_minutes)


def test_e_stale_quote_cannot_become_fresh_valuation_on_later_poll(schedule, tmp_path):
    engine = make_engine(schedule, tmp_path)
    engine.run(full_input(schedule))
    stale = quote("2026-05-04 09:30:00", bid=200., ask=200.05)
    stale["payload"]["timestamp"] = ny("2026-05-01 16:05:00")
    engine.run(list(calendar_events(schedule, "2026-05-04", "2026-05-04")) + [stale])
    assert not fills(engine, "SELL")
    assert engine.summary()["latest_equity"]["net_equity"] is None
    evidence("e_old_quote_poll_does_not_refresh_valuation", "E", engine,
             prior_session_quote_remains_stale=True, published_unknown_equity=None)


@pytest.mark.parametrize("failure", ["ZERO_SIGNAL", "QUOTE_MISSING", "REJECTED_QUOTE"])
def test_f_nontrades_have_distinct_reasons_not_strategy_performance(schedule, tmp_path, failure):
    engine = make_engine(schedule, tmp_path)
    events = full_input(schedule, signal=failure != "ZERO_SIGNAL", quotes=failure != "QUOTE_MISSING")
    if failure == "REJECTED_QUOTE":
        for event in events:
            if event["kind"] == "QUOTE":
                event["payload"].update(bid=99., ask=103.)
    engine.run(events)
    assert not engine.ledger.fills
    rendered = textual_trace(engine)
    if failure == "ZERO_SIGNAL":
        assert "NO_FRESH_CROSS" in rendered or "CLOSE_NOT_ABOVE_SHORT_EMAS" in rendered
    elif failure == "QUOTE_MISSING":
        assert "QUOTE" in rendered and ("MISSING" in rendered or "UNKNOWN" in rendered)
    else:
        assert any(reason in rendered for reason in ("SPREAD_TOO_WIDE", "LIMIT", "QUOTE_REJECTED"))
    evidence("f_" + failure.lower(), "F", engine, expected_status=failure,
             no_natural_fills=True, strategy_effectiveness_not_inferred=True)


def test_f_held_position_missing_exit_price_remains_open_and_unvalued(schedule, tmp_path):
    engine = make_engine(schedule, tmp_path)
    events = full_input(schedule, "2026-05-05") + [
        ev("DATA_GAP", "2026-05-04 09:30:00", reason="HELD_SESSION_PRICE_UNAVAILABLE"),
        earnings(planned="2026-05-07", known="2026-05-04 09:31:00", actual="2026-05-07 16:05:00"),
    ]
    engine.run(events)
    assert fills(engine, "BUY") and not fills(engine, "SELL")
    assert "X" in engine.ledger.positions
    assert engine.ledger.campaign_summary()["campaigns"][0]["net_profit"] is None
    assert engine.summary()["latest_equity"]["net_equity"] is None
    assert any(word in textual_trace(engine) for word in ("PENDING", "UNAVAILABLE", "MISSING"))
    evidence("f_held_missing_exit_retained", "F", engine,
             entry_not_deleted=True, unclosed_campaign_profit=None)


def test_g_gap_loss_not_clipped_to_seven_percent(schedule, tmp_path):
    engine = make_engine(schedule, tmp_path)
    events = full_input(schedule, "2026-05-05") + [
        quote("2026-05-04 09:30:00", bid=80., ask=80.05),
        daily("2026-05-04", 80.), daily("2026-05-05", 80.),
    ]
    engine.run(events)
    assert fills(engine, "SELL")
    assert all(f["observed_price"] == 80 for f in fills(engine, "SELL"))
    summary = engine.ledger.campaign_summary()["campaigns"][0]
    assert summary["net_profit"] < -.07 * summary["entry_outlay"]
    evidence("g_actual_gap_exceeds_initial_stop_distance", "G", engine,
             realized_loss_exceeds_7_percent_of_entry_outlay=True)


def test_g_split_and_dividend_are_processed_once_without_false_stop(schedule, tmp_path):
    engine = make_engine(schedule, tmp_path)
    entry = full_input(schedule)
    engine.run(entry)
    assert engine.ledger.positions["X"]["quantity"] == 10
    cash_after_entry = engine.ledger.cash
    ex = ev("DIVIDEND_EX", "2026-05-04 09:30:00", action_id="MOCK_DIVIDEND",
            amount_per_share=.5, pay_date="2026-05-05")
    later = list(calendar_events(schedule, "2026-05-04", "2026-05-04")) + [
        ev("SPLIT", "2026-05-04 09:29:00", ratio=2., action_id="MOCK_SPLIT"),
        ex, quote("2026-05-04 09:30:00", bid=50.45, ask=50.5), daily("2026-05-04", 50.5),
    ]
    engine.run(later)
    assert engine.ledger.positions["X"]["quantity"] == 20
    assert not fills(engine, "SELL")
    assert engine.ledger.cash == cash_after_entry
    engine.save()
    resumed = ReplayEngine.restore(tmp_path / "replay_checkpoint.json", schedule)
    pay = ev("DIVIDEND_PAY", "2026-05-05 09:30:00", action_id="MOCK_DIVIDEND")
    resumed.process(pay)
    resumed.process(pay)
    resumed.process({**pay, "event_id": "REPEATED_PROVIDER_DIVIDEND_PAY", "at": ny("2026-05-05 09:30:01")})
    assert resumed.ledger.cash == pytest.approx(cash_after_entry + 10.)
    assert resumed.ledger.positions["X"]["quantity"] == 20
    assert not fills(resumed, "SELL")
    evidence("g_split_dividend_and_restart", "G", resumed,
             split_quantity_before=10, split_quantity_after=20,
             dividend_cash_paid_once=10., false_split_stops=0)


def test_g_reverse_split_keeps_fractional_entitlement_until_confirmed_cash(schedule, tmp_path):
    engine = make_engine(schedule, tmp_path)
    events = full_input(schedule)
    for event in events:
        if event["kind"] == "QUOTE":
            event["payload"]["ask_size"] = 1000
    engine.run(events)
    assert engine.ledger.positions["X"]["quantity"] == 27
    cash_before = engine.ledger.cash
    engine.process(ev("SPLIT", "2026-05-04 09:29:00", ratio=.1, action_id="MOCK_REVERSE_SPLIT"))
    assert engine.ledger.positions["X"]["quantity"] == pytest.approx(2.7)
    assert engine.ledger.cash == cash_before
    confirm = ev("CASH_IN_LIEU", "2026-05-04 09:30:00", action_id="MOCK_CONFIRMED_CIL", confirmed_amount=700.)
    engine.process(confirm)
    engine.process(confirm)
    assert not engine.state["errors"]
    assert engine.ledger.positions["X"]["quantity"] == 2
    assert engine.ledger.cash == pytest.approx(cash_before + 700.)
    assert sum(leg["quantity"] for leg in engine.ledger.positions["X"]["legs"]) == 2
    evidence("g_reverse_fractional_only_confirmed_cash", "G", engine,
             original_shares=27, entitlement_after_reverse=2.7,
             confirmed_cash_paid_once=700., residual_whole_shares=2,
             no_fabricated_cash_price=True)


def test_g_financing_uses_debt_weekend_interest_and_display_limited_margin_exit(schedule, tmp_path):
    engine = make_engine(schedule, tmp_path, mode="P200_MARGIN_RESEARCH")
    initial = full_input(schedule)
    for event in initial:
        if event["kind"] == "QUOTE":
            event["payload"]["ask_size"] = 1000
    engine.run(initial)
    original_debt = engine.ledger.debt
    assert original_debt > 5000 and engine.ledger.config.initial_equity == 5500
    assert engine.ledger.cash >= 0
    engine.save()
    resumed = ReplayEngine.restore(tmp_path / "replay_checkpoint.json", schedule)
    resumed.run(list(calendar_events(schedule, "2026-05-04", "2026-05-04"))[:1])
    assert resumed.ledger.interest_total == pytest.approx(original_debt * .08 / 360 * 3)
    crash_quote = quote("2026-05-04 09:30:00", bid=60., ask=60.05, bid_size=5)
    resumed.process(crash_quote)
    sold = fills(resumed, "SELL")
    assert sum(fill["quantity"] for fill in sold) <= 5
    assert resumed.ledger.cash >= 0 and resumed.ledger.debt > 0
    assert "MARGIN" in textual_trace(resumed)
    evidence("g_financed_weekend_margin_and_restart", "G", resumed,
             actual_initial_capital=5500, debt_created=True, elapsed_interest_days=3,
             total_sold_on_five_displayed_bid_shares=sum(fill["quantity"] for fill in sold),
             no_negative_cash=True)


def test_h_strategy_failure_never_becomes_test_order(schedule, tmp_path):
    engine = make_engine(schedule, tmp_path)
    engine.run(full_input(schedule, signal=False))
    assert not engine.ledger.orders and not engine.ledger.fills and not engine.ledger.campaigns
    evidence("h_no_forced_orders_when_rule_fails", "H", engine, forced_orders=0)


def test_h_later_quote_does_not_retroactively_create_fixed_1605_intent(schedule, tmp_path):
    engine = make_engine(schedule, tmp_path)
    engine.run(full_input(schedule, quotes=False) + [quote("2026-05-01 16:05:02")])
    assert not engine.ledger.orders and not engine.ledger.fills
    evidence("h_future_quote_cannot_repair_fixed_decision", "H", engine,
             first_quote_seconds_after_fixed_decision=2, retroactive_intents=0)


def test_h_unknown_earnings_never_passes_even_when_structure_and_quote_pass(schedule, tmp_path):
    engine = make_engine(schedule, tmp_path)
    events = [event for event in full_input(schedule) if event["kind"] != "EARNINGS_REVISION"]
    engine.run(events)
    assert not engine.ledger.orders and not engine.ledger.fills
    assert "EARNINGS" in textual_trace(engine)
    assert "UNKNOWN" in textual_trace(engine)
    evidence("h_missing_earnings_remains_fail_closed", "H", engine,
             structural_cross_present=True, displayed_quote_present=True,
             unknown_earnings_cannot_generate_order=True)


def test_a_reentry_waits_two_full_sessions_and_creates_new_campaign(schedule, tmp_path):
    engine = make_engine(schedule, tmp_path)
    events = full_input(schedule, "2026-05-06") + [
        quote("2026-05-04 10:00:00", bid=80., ask=80.05),
        daily("2026-05-04", 96.5, high=100., low=80.),
        daily("2026-05-05", 96.5),
        quote("2026-05-05 16:05:00", bid=96.45, ask=96.5),
        daily("2026-05-06", 102.),
        quote("2026-05-06 16:05:00", bid=101.95, ask=102.),
    ]
    engine.run(events)
    buys = fills(engine, "BUY")
    assert len(buys) == 2
    assert buys[0]["campaign_id"] != buys[1]["campaign_id"]
    assert pd.Timestamp(buys[1]["at"]).tz_convert(NY).date().isoformat() == "2026-05-06"
    may5 = [d for d in engine.state["decisions"] if d["decision_time"].startswith("2026-05-05")]
    assert may5[0]["reason"] == "WAIT_TWO_COMPLETE_RTH_SESSIONS"
    assert engine.ledger.campaign_summary()["closed_campaigns"] == 1
    assert engine.ledger.campaign_summary()["campaigns"][0]["net_profit"] < 0
    evidence("a_reentry_new_campaign_preserves_prior_loss", "A", engine,
             required_post_exit_full_sessions=2, new_campaign_after_wait=True,
             prior_closed_loss_preserved=True)


def test_h_repair_before_actual_exit_cannot_qualify_reentry(schedule, tmp_path):
    engine = make_engine(schedule, tmp_path)
    events = full_input(schedule, "2026-05-07") + [
        daily("2026-05-04", 96.5, high=97., low=94.),  # Repair before actual exit.
        quote("2026-05-05 10:00:00", bid=80., ask=80.05),
        daily("2026-05-05", 104., low=80.),
        daily("2026-05-06", 110.), daily("2026-05-07", 111.),
        quote("2026-05-07 16:05:00", bid=110.95, ask=111.),
    ]
    engine.run(events)
    may7 = [d for d in engine.state["decisions"] if d["decision_time"].startswith("2026-05-07")]
    assert may7[0]["reason"] == "NO_PRIOR_REPAIR_EVIDENCE"
    assert len(fills(engine, "BUY")) == 1
    evidence("h_pre_exit_repair_excluded", "H", engine,
             pre_exit_repair_present=True, valid_post_exit_repair=False,
             subsequent_new_orders=0)
