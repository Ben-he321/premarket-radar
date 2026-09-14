"""Explicit offline fixtures: isolated cash/dividend math, no market requests."""
import pandas as pd
import pytest

from src.ben_b1_2.benchmarks import exact_rth_open, simulate_hold


def test_ex_date_entitlement_payable_date_reinvest_once_and_no_negative_cash():
    days = ["2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"]
    close = dict.fromkeys(days, 100.)
    result = simulate_hold("MOCK", days, close, close, dict.fromkeys(days, 100.), [
        {"symbol": "MOCK", "id": "already_ex", "ex_date": "2025-12-19", "payable_date": "2026-01-05", "rate": 99},
        {"symbol": "MOCK", "id": "ordinary", "ex_date": "2026-01-05", "payable_date": "2026-01-06", "rate": 2},
    ], capital=550.)
    assert [x["date"] for x in result["fills"]] == ["2026-01-02"]
    assert result["fills"][0]["shares"] == 5
    assert result["summary"]["end_cash_usd"] == 58.50
    assert result["summary"]["commissions_usd"] == 1
    assert result["summary"]["dividend_cash_paid_usd"] == 10
    assert result["daily"][1]["dividend_receivable_usd"] == 10
    assert result["daily"][2]["dividend_receivable_usd"] == 0
    assert result["order_decisions"][1]["date"] == "2026-01-07"
    assert result["order_decisions"][1]["status"] == "NO_AFFORDABLE_WHOLE_SHARE_NO_COMMISSION"
    assert len(result["order_decisions"]) == 2
    assert all(x["cash_usd"] >= 0 for x in result["daily"])


def test_weekend_payment_reinvests_next_session_whole_shares_and_pays_one_fee():
    days = ["2026-01-02", "2026-01-05", "2026-01-06"]
    prices = dict.fromkeys(days, 10.)
    result = simulate_hold("MOCK", days, prices, prices, prices, [
        {"symbol": "MOCK", "id": "same_ex", "ex_date": "2026-01-02", "payable_date": "2026-01-03", "rate": 100},
    ], capital=100.)
    assert result["dividends"][0]["entitled_shares"] == 0
    assert result["summary"]["dividend_cash_paid_usd"] == 0
    days = ["2026-01-02", "2026-01-05", "2026-01-09", "2026-01-12"]
    prices = dict.fromkeys(days, 10.)
    result = simulate_hold("MOCK", days, prices, prices, prices, [
        {"symbol": "MOCK", "id": "weekend", "ex_date": "2026-01-05", "payable_date": "2026-01-10", "rate": 2},
    ], capital=100.)
    assert result["fills"][1]["date"] == "2026-01-12"
    assert result["fills"][1]["shares"] == 2
    assert result["summary"]["end_shares"] == 11
    assert result["summary"]["commissions_usd"] == 2
    assert result["cashflows"][0]["date"] == "2026-01-10"


def test_unpaid_dividend_remains_receivable_and_all_adjusted_is_separate():
    days = ["2026-03-19", "2026-03-20", "2026-03-31"]
    raw = dict.fromkeys(days, 100.)
    adjusted = {days[0]: 98., days[1]: 100., days[2]: 110.}
    result = simulate_hold("MOCK", days, raw, adjusted, {days[0]: 100.}, [
        {"symbol": "MOCK", "id": "future_pay", "ex_date": "2026-03-20", "payable_date": "2026-04-30", "rate": 1.796999},
    ], capital=550.)
    summary = result["summary"]
    assert summary["end_dividend_receivable_usd"] == 8.98
    assert summary["end_cash_usd"] == 48.50
    assert summary["end_equity_usd"] == 557.48
    assert summary["all_adjusted_reference_end_usd"] == pytest.approx(550*110/98)
    assert summary["dividend_cash_paid_usd"] == 0
    assert summary["sell_order_count"] == 0
    assert summary["terminal_sell_cost_included"] is False


def test_duplicate_dividend_id_is_deduplicated_but_conflict_is_rejected():
    days = ["2026-01-02", "2026-01-05"]
    prices = dict.fromkeys(days, 10.)
    action = {"symbol": "MOCK", "id": "same", "ex_date": "2026-01-05", "payable_date": "2026-01-06", "rate": 1}
    result = simulate_hold("MOCK", days, prices, prices, prices, [action, action], capital=100.)
    assert result["summary"]["end_dividend_receivable_usd"] == 9
    with pytest.raises(ValueError, match="CONFLICTING_DIVIDEND_ID"):
        simulate_hold("MOCK", days, prices, prices, prices, [action, {**action, "rate": 2}])


def test_missing_initial_or_reinvestment_open_never_backfills_later():
    days = ["2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08"]
    prices = dict.fromkeys(days, 10.)
    result = simulate_hold("MOCK", days, prices, prices, {days[-1]: 10.}, [])
    assert result["summary"]["status"] == "NOT_RUN_DATA_GATED"
    assert result["summary"]["end_equity_usd"] is None
    assert result["fills"] == []
    result = simulate_hold("MOCK", days, prices, prices, {days[0]: 10., days[-1]: 10.}, [
        {"symbol": "MOCK", "id": "pay", "ex_date": "2026-01-05", "payable_date": "2026-01-06", "rate": 2}
    ], capital=100.)
    assert result["summary"]["status"] == "PARTIAL_INPUTS"
    assert len(result["fills"]) == 1
    assert result["order_decisions"][1]["status"] == "OPEN_UNKNOWN_NO_ORDER_NO_BACKFILL"


def test_exact_open_rejects_later_minute_and_duplicate_open():
    stamp = pd.Timestamp("2026-01-02T14:30:00Z")
    frame = pd.DataFrame({"timestamp": [stamp + pd.Timedelta(minutes=1)], "open": [100.]})
    assert exact_rth_open(frame, stamp) is None
    frame.loc[0, "timestamp"] = stamp
    assert exact_rth_open(frame, stamp) == 100
    assert exact_rth_open(pd.concat([frame, frame]), stamp) is None


def test_missing_end_mark_keeps_result_unknown_and_no_fictitious_liquidation():
    days = ["2026-01-02", "2026-01-05"]
    result = simulate_hold("MOCK", days, {days[0]: 10.}, {days[0]: 10.}, {days[0]: 10.}, [])
    assert result["summary"]["end_equity_usd"] is None
    assert result["summary"]["maximum_drawdown"] is None
    assert result["summary"]["end_shares"] > 0
    assert result["summary"]["sell_order_count"] == 0


def test_unknown_dividend_payment_date_does_not_become_zero_or_exdate_cash():
    days = ["2026-01-02", "2026-01-05"]
    prices = dict.fromkeys(days, 10.)
    with pytest.raises(ValueError, match="DIVIDEND_DATE_OR_RATE_UNKNOWN"):
        simulate_hold("MOCK", days, prices, prices, prices, [
            {"symbol": "MOCK", "id": "unknown_pay", "ex_date": "2026-01-05", "rate": 1}
        ])
