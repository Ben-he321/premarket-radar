"""Explicit synthetic accounting fixtures only; no market requests or research I/O."""
from datetime import date
import json
import math

import pytest

from src.ben_b1.ledger import FinanceConfig, Ledger, money


def funded_margin(**overrides):
    account = Ledger(FinanceConfig(mode="P200_MARGIN_RESEARCH", **overrides))
    quantity = account.plan_entry("X", 100, stop_leg_count=2)["quantity"]
    account.buy("entry", "X", quantity, 100, "2026-09-11", leg_quantities=[(quantity+1)//2, quantity//2])
    return account


@pytest.mark.parametrize("mode,expected,slots", [("P50_PRIMARY", 27, 2), ("P100_CONCENTRATED", 54, 1), ("P200_MARGIN_RESEARCH", 109, 1)])
def test_target_positions_are_half_full_and_two_times_not_legacy_risk_budget(mode, expected, slots):
    ledger = Ledger(FinanceConfig(mode=mode))
    plan = ledger.plan_entry("X", 100, stop_leg_count=2)
    assert plan["quantity"] == expected
    ledger.buy("entry", "X", expected, 100, "2026-09-11", leg_quantities=[(expected+1)//2, expected//2])
    state = ledger.snapshot({"X": 100})
    assert state["initial_equity"] == 5500
    assert state["cash"] >= 0
    assert state["reserved_exit_fees"] == 2
    assert state["net_equity"] == pytest.approx(5500 - expected*.1 - 1)
    if slots == 1:
        assert ledger.plan_entry("Y", 100, {"X": 100})["status"] == "POSITION_SLOTS_FULL"
    if mode != "P200_MARGIN_RESEARCH":
        assert state["debt"] == 0
        assert state["cash"] >= state["reserved_exit_fees"]
    else:
        assert state["debt"] > 5000 and state["gross_exposure"] <= 2


def test_p50_two_positions_use_actual_remaining_cash_and_exit_fee_reserve():
    ledger = Ledger()
    ledger.buy("x", "X", 27, 100, "2026-09-11", leg_quantities=[14, 13])
    assert ledger.plan_entry("Y", 100, {"X": 100}, stop_leg_count=2)["quantity"] == 27
    ledger.buy("y", "Y", 27, 100, "2026-09-11", {"X": 100}, leg_quantities=[14, 13])
    assert ledger.cash == 92.60
    assert ledger.reserved_exit_fees == 4
    assert ledger.plan_entry("Z", 1, {"X": 100, "Y": 100})["status"] == "POSITION_SLOTS_FULL"


def test_integer_affordability_excludes_both_buy_and_exit_fees():
    ledger = Ledger(FinanceConfig(mode="P100_CONCENTRATED", initial_equity=202, friction_bps=0))
    assert ledger.plan_entry("X", 100)["quantity"] == 2
    ledger.buy("entry", "X", 2, 100, "2026-09-11")
    assert ledger.cash == ledger.reserved_exit_fees == 1
    with pytest.raises(ValueError):
        Ledger(FinanceConfig(mode="P100_CONCENTRATED", initial_equity=201, friction_bps=0)).buy("too_big", "X", 2, 100, "2026-09-11")


def test_actual_quote_friction_cannot_cross_market_limit():
    ledger = Ledger(FinanceConfig(friction_bps=25))
    assert ledger.plan_entry("X", 100.9, limit=101)["status"] == "FRICTION_WOULD_EXCEED_LIMIT"
    with pytest.raises(ValueError, match="EXCEED_LIMIT"):
        ledger.buy("x", "X", 10, 100.9, "2026-09-11", limit=101)
    fill = ledger.buy("y", "Y", 10, 99, "2026-09-11", limit=101)
    assert fill["execution_price"] == pytest.approx(99*1.0025)
    assert fill["execution_price"] < 101
    assert ledger.cash == money(5500-10*99*1.0025-1)


def test_quote_size_is_a_share_cap_and_bad_or_zero_prices_are_not_fills():
    ledger = Ledger()
    assert ledger.plan_entry("X", 10, displayed_size=3)["quantity"] == 3
    assert ledger.plan_entry("X", 10, displayed_size=0)["quantity"] == 0
    for bad_price in [0, -1, float("nan")]:
        with pytest.raises(ValueError):
            ledger.buy("x", "X", 1, bad_price, "2026-09-11")
    with pytest.raises(ValueError, match="DISPLAYED_SHARES"):
        ledger.buy("x", "X", 4, 10, "2026-09-11", displayed_size=3)
    with pytest.raises(ValueError, match="INTEGER"):
        ledger.buy("x", "X", 1.5, 10, "2026-09-11")


def test_partial_order_commission_once_and_restart_deduplication():
    ledger = Ledger()
    a = ledger.buy("entry", "X", 5, 100, "2026-09-11", fill_id="one", order_quantity=20, displayed_size=5)
    restored = Ledger.from_dict(json.loads(json.dumps(ledger.to_dict())))
    assert restored.buy("entry", "X", 5, 100, "2026-09-11", fill_id="one", order_quantity=20, displayed_size=5) == a
    restored.buy("entry", "X", 3, 100, "2026-09-11", {"X": 100}, fill_id="two", displayed_size=3)
    assert restored.positions["X"]["quantity"] == 8
    assert restored.cash == pytest.approx(5500 - 800.8 - 1)
    assert restored.campaign_summary()["actual_orders"] == 1
    assert restored.campaign_summary()["actual_fills"] == 2
    assert restored.campaigns["entry"]["commission"] == 1
    restored.cancel_order("entry", "2026-09-11T16:15:00-04:00")
    with pytest.raises(ValueError, match="CANCELLED"):
        restored.buy("entry", "X", 1, 100, "2026-09-11", {"X": 100}, fill_id="three")
    with pytest.raises(ValueError, match="FILL_ID_CONFLICT"):
        restored.buy("entry", "X", 6, 100, "2026-09-11", fill_id="one")


def test_no_averaging_down_and_no_refilling_after_reduction():
    ledger = Ledger()
    ledger.buy("entry", "X", 10, 100, "2026-09-11", order_quantity=20, fill_id="first")
    with pytest.raises(ValueError, match="NO_ADDING"):
        ledger.buy("second", "X", 1, 90, "2026-09-11", {"X": 90})
    ledger.sell("stop", "X", 2, 90, "2026-09-14", "2026-09-15")
    with pytest.raises(ValueError, match="NO_REFILL"):
        ledger.buy("entry", "X", 1, 90, "2026-09-14", {"X": 90}, fill_id="later")


def test_partial_fill_keeps_original_limit_after_restore_without_caller_repeating_it():
    ledger = Ledger()
    ledger.buy("entry", "X", 1, 100, "2026-09-11", limit=101, fill_id="first", order_quantity=10)
    restored = Ledger.from_dict(ledger.to_dict())
    with pytest.raises(ValueError, match="EXCEED_LIMIT"):
        restored.buy("entry", "X", 1, 101, "2026-09-11", {"X": 101}, fill_id="second")
    with pytest.raises(ValueError, match="CANNOT_RAISE"):
        restored.buy("entry", "X", 1, 100.5, "2026-09-11", {"X": 100.5}, fill_id="second", limit=102)
    fill = restored.buy("entry", "X", 1, 100.5, "2026-09-11", {"X": 100.5}, fill_id="second")
    assert fill["execution_price"] == pytest.approx(100.5*1.001)
    assert fill["limit"] == 101


def test_stop_legs_at_same_gap_charge_two_actual_orders_one_campaign():
    ledger = Ledger()
    ledger.buy("entry", "X", 21, 100, "2026-09-11", leg_quantities=[11, 10])
    # Caller supplies the true available gap bid; no loss clipping to the 7% plan.
    ledger.sell("near-stop", "X", 11, 85, "2026-09-14", "2026-09-15", leg_index=0, reason="GAP_STOP")
    assert ledger.reserved_exit_fees == 1
    ledger.sell("far-stop", "X", 10, 85, "2026-09-14", "2026-09-15", leg_index=1, reason="GAP_STOP")
    summary = ledger.campaign_summary()
    assert summary["closed_campaigns"] == 1
    assert summary["losing_campaigns"] == 1
    assert summary["actual_orders"] == 3
    assert summary["campaigns"][0]["commission"] == 3
    assert summary["campaigns"][0]["net_profit"] < -315
    assert ledger.reserved_exit_fees == 0


def test_partial_exit_order_fee_once_then_only_next_session_cash_is_usable():
    ledger = Ledger(FinanceConfig(mode="P100_CONCENTRATED", initial_equity=1002, friction_bps=0))
    ledger.buy("entry", "X", 10, 100, "2026-09-11")
    first = ledger.sell("exit", "X", 2, 110, "2026-09-14", "2026-09-15", fill_id="sell1", order_quantity=10)
    ledger.sell("exit", "X", 8, 110, "2026-09-14", "2026-09-15", fill_id="sell2")
    assert ledger.cash == 1
    assert ledger.unsettled_cash == 1099
    assert ledger.plan_entry("Y", 100)["quantity"] == 0
    assert ledger.snapshot({})["net_equity"] == 1100
    restored = Ledger.from_dict(ledger.to_dict())
    assert restored.sell("exit", "X", 2, 110, "2026-09-14", "2026-09-15", fill_id="sell1", order_quantity=10) == first
    restored.advance_day("2026-09-15")
    assert restored.cash == 1100 and restored.unsettled_cash == 0
    assert restored.campaign_summary()["campaigns"][0]["commission"] == 2


def test_fully_merged_exit_is_one_fee_even_when_original_campaign_had_two_legs():
    ledger = Ledger()
    ledger.buy("entry", "X", 10, 100, "2026-09-11", leg_quantities=[5, 5])
    ledger.sell("earnings", "X", 10, 105, "2026-09-14", "2026-09-15", reason="EARNINGS_EXIT")
    assert ledger.campaign_summary()["campaigns"][0]["commission"] == 2


@pytest.mark.parametrize("rate", [.08, .12])
def test_debt_calendar_weekend_interest_and_equity_identity(rate):
    ledger = funded_margin(annual_interest_rate=rate)
    debt = ledger.debt
    before = ledger.snapshot({"X": 100})["net_equity"]
    ledger.advance_day("2026-09-14")  # Friday close -> Monday: Fri, Sat, Sun.
    assert ledger.accrued_interest == pytest.approx(debt*rate/360*3)
    state = ledger.snapshot({"X": 100})
    assert state["net_equity"] == pytest.approx(before-debt*rate/360*3)
    assert state["net_equity"] == pytest.approx(state["cash"]+state["unsettled_cash"]+state["market_value"]-state["debt"]-state["accrued_interest"])
    assert len([x for x in ledger.events if x.get("type") == "INTEREST_ACCRUAL"]) == 3
    ledger.advance_day("2026-09-14")
    assert ledger.accrued_interest == pytest.approx(debt*rate/360*3)


def test_monthly_posting_does_not_double_count_interest_and_restart_preserves_accrual():
    ledger = Ledger(FinanceConfig(mode="P200_MARGIN_RESEARCH"))
    ledger.buy("entry", "X", 100, 100, "2026-09-29")
    initial_debt = ledger.debt
    initial_equity = ledger.snapshot({"X": 100})["net_equity"]
    ledger.advance_day("2026-10-01")
    expected = initial_debt*.08/360*2
    assert ledger.interest_total == pytest.approx(expected)
    assert ledger.interest_posted == money(expected)
    assert ledger.debt == money(initial_debt+money(expected))
    assert ledger.snapshot({"X": 100})["net_equity"] == pytest.approx(initial_equity-expected)
    restored = Ledger.from_dict(json.loads(json.dumps(ledger.to_dict())))
    restored.advance_day("2026-10-02")
    assert restored.interest_total == pytest.approx(expected + ledger.debt*.08/360)
    assert len([x for x in restored.events if x.get("type") == "MONTHLY_INTEREST_POST"]) == 1


def test_sale_settlement_repays_debt_before_positive_cash_and_keeps_lag_interest():
    ledger = funded_margin()
    quantity = ledger.positions["X"]["quantity"]
    debt = ledger.debt
    ledger.sell("exit", "X", quantity, 100, "2026-09-11", "2026-09-14")
    assert ledger.debt == debt and ledger.cash == 0
    ledger.advance_day("2026-09-14")
    assert ledger.debt == 0 and ledger.cash > 5000
    assert ledger.interest_total == pytest.approx(debt*.08/360*3)
    assert ledger.campaigns["entry"]["interest"] == pytest.approx(ledger.interest_total)


def test_more_restrictive_initial_margin_cannot_borrow_two_times():
    ledger = Ledger(FinanceConfig(mode="P200_MARGIN_RESEARCH", initial_margin=.75))
    quantity = ledger.plan_entry("X", 100)["quantity"]
    assert quantity == 73
    ledger.buy("entry", "X", quantity, 100, "2026-09-11")
    state = ledger.snapshot({"X": 100})
    assert state["market_value"] <= (state["net_equity"]-state["reserved_exit_fees"])/.75 + 1e-6


def test_maintenance_raise_to_fifty_requires_actual_minimal_reduction():
    ledger = funded_margin()
    assert ledger.margin_check({"X": 98}, .30)["margin_status"] == "MARGIN_OK"
    assert ledger.margin_check({"X": 98}, .50)["margin_status"] == "MARGIN_CALL"
    result = ledger.liquidate_margin("call", {"X": 98}, {"X": 97.9}, "2026-09-11", "2026-09-14", .50)
    assert len(result["fills"]) == 1
    assert 0 < result["fills"][0]["quantity"] < 109
    assert result["fills"][0]["execution_price"] == pytest.approx(97.9*.999)
    assert result["check"]["margin_status"] == "MARGIN_OK"
    assert ledger.unsettled_cash > 0 and ledger.cash == 0


def test_missing_price_margin_breach_remains_unresolved_without_synthetic_sale():
    ledger = funded_margin()
    assert ledger.margin_check({})["margin_status"] == "MARGIN_UNDETERMINED_MISSING_PRICE"
    result = ledger.liquidate_margin("call", {"X": 60}, {}, "2026-09-11", "2026-09-14")
    assert result["status"] == "MARGIN_CALL_PENDING_EXECUTABLE_PRICE"
    assert not result["fills"] and ledger.positions["X"]["quantity"] == 109
    assert len(ledger.orders) == 1


def test_equity_below_zero_stops_new_entry_and_records_unrepaid_deficit():
    ledger = funded_margin()
    state = ledger.margin_check({"X": 40})
    assert state["net_equity"] < 0
    assert state["margin_status"] == "INSOLVENT_STOP_NEW_ENTRIES"
    result = ledger.liquidate_margin("bankrupt", {"X": 40}, {"X": 39.9}, "2026-09-11", "2026-09-14")
    assert not ledger.positions and len(result["fills"]) == 1
    ledger.advance_day("2026-09-14")
    assert ledger.cash == 0 and ledger.debt > 0
    assert ledger.plan_entry("Y", 1)["quantity"] == 0
    with pytest.raises(ValueError, match="NONPOSITIVE"):
        ledger.buy("no", "Y", 1, 1, "2026-09-14")


def test_deviation_counts_campaign_original_size_and_all_previous_reductions():
    ledger = Ledger()
    ledger.buy("entry", "X", 21, 100, "2026-09-11", leg_quantities=[11, 10])
    assert ledger.deviation_quantity("X") == 10
    ledger.sell("reduce", "X", 6, 105, "2026-09-14", "2026-09-15")
    assert ledger.deviation_quantity("X") == 4
    ledger.mark_deviation_done("X")
    assert ledger.deviation_quantity("X") == 0
    ledger.sell("reduce-again", "X", 4, 105, "2026-09-14", "2026-09-15")
    assert ledger.deviation_quantity("X") == 0


def test_zero_trade_and_no_loser_metrics_remain_defined_without_fake_zero_evidence():
    ledger = Ledger()
    assert ledger.campaign_summary()["win_rate"] is None
    ledger.buy("entry", "X", 1, 100, "2026-09-11")
    assert ledger.campaign_summary()["closed_campaigns"] == 0
    ledger.sell("exit", "X", 1, 110, "2026-09-14", "2026-09-15")
    summary = ledger.campaign_summary()
    assert summary["win_rate"] == 1
    assert summary["mean_win_loss_ratio"] is None
    assert summary["actual_orders"] == 2


def test_invalid_settlement_or_backwards_clock_is_rejected():
    ledger = Ledger()
    ledger.buy("entry", "X", 1, 100, "2026-09-11")
    with pytest.raises(ValueError, match="SETTLEMENT"):
        ledger.sell("exit", "X", 1, 110, "2026-09-14", "2026-09-14")
    with pytest.raises(ValueError, match="NON_MONOTONIC"):
        ledger.advance_day("2026-09-10")


@pytest.mark.parametrize("kwargs", [{"initial_equity": 0}, {"commission": -1}, {"friction_bps": 10000}, {"initial_margin": 0}, {"annual_interest_rate": -0.1}])
def test_invalid_finance_assumptions_are_not_silently_accepted(kwargs):
    with pytest.raises(ValueError):
        FinanceConfig(**kwargs)
