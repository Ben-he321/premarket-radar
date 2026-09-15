"""Explicit artificial cash-account engineering fixtures; no research returns."""
from decimal import Decimal

import pytest

from src.futures_f0.benchmarks import (affordable_quantity, money, performance,
                                       purchase_amounts, reconcile, simulate)


def bars(*days, price=100):
    return [dict(date=day, open=price, close=price) for day in days]


def test_dividend_is_not_spendable_before_pay_then_waits_until_next_open():
    values = bars("2022-01-03", "2022-01-04", "2022-01-05", "2022-01-06")
    result = simulate("MOCK", values, [dict(ex_date="2022-01-04", payable_date="2022-01-05", rate=20)], initial=Decimal("1000"))
    assert result["curve"][0]["shares"] == 9
    assert result["curve"][1]["dividend_receivable"] == 180
    assert result["curve"][1]["cash"] == 98.1
    assert result["curve"][2]["shares"] == 9
    assert result["curve"][2]["cash"] == 278.1
    assert result["curve"][3]["shares"] == 11
    assert result["fills"][-1]["date"] == "2022-01-06"
    assert result["reconcile"]["status"] == "PASS"


def test_weekend_payment_credits_next_close_before_later_reinvestment():
    result = simulate("MOCK", bars("2022-01-07", "2022-01-10", "2022-01-11"),
                      [dict(ex_date="2022-01-10", payable_date="2022-01-10", rate=20)], initial=Decimal("1000"))
    assert result["fills"][-1]["date"] == "2022-01-11"
    # An earlier Friday ex date is needed to receive a Saturday payment.
    result = simulate("MOCK", bars("2022-01-06", "2022-01-07", "2022-01-10", "2022-01-11"),
                      [dict(ex_date="2022-01-07", payable_date="2022-01-08", rate=20)], initial=Decimal("1000"))
    assert result["distributions"][0]["received_session"] == "2022-01-10"
    assert result["fills"][-1]["date"] == "2022-01-11"


def test_buy_on_ex_date_receives_no_distribution():
    result = simulate("MOCK", bars("2022-01-03", "2022-01-04"),
                      [dict(ex_date="2022-01-03", payable_date="2022-01-04", rate=20)])
    assert result["distributions"][0]["entitled_shares"] == 0
    assert result["curve"][-1]["paid_dividends_cumulative"] == 0
    assert len(result["fills"]) == 1


def test_future_pay_date_is_receivable_not_cash_and_no_double_count():
    result = simulate("MOCK", bars("2025-12-30", "2025-12-31"),
                      [dict(ex_date="2025-12-31", payable_date="2026-01-15", rate=2)], initial=Decimal("1000"))
    final = result["curve"][-1]
    assert final["cash"] == 98.1
    assert final["dividend_receivable"] == 18
    assert final["equity"] == 1016.1
    assert final["paid_dividends_cumulative"] == 0
    assert result["distributions"][0]["received_session"] is None


def test_terminal_sale_is_only_alternate_value_and_never_mutates_account():
    result = simulate("MOCK", bars("2025-12-31"), [], initial=Decimal("1000"))
    final = result["curve"][0]
    assert final["equity"] == 998.1
    assert final["equity_if_terminal_sale"] == 996.2
    assert final["shares"] == 9
    assert all(item["side"] == "BUY" for item in result["fills"])


def test_split_uses_raw_price_and_exact_integer_shares():
    values = [dict(date="2022-01-03", open=100, close=100),
              dict(date="2022-01-04", open=50, close=50)]
    result = simulate("MOCK", values, [], initial=Decimal("1000"),
                      splits=[dict(date="2022-01-04", ratio=2)])
    assert result["curve"][-1]["shares"] == 18
    assert result["curve"][-1]["equity"] == result["curve"][0]["equity"]
    with pytest.raises(ValueError, match="FRACTIONAL_SPLIT"):
        simulate("MOCK", values, [], initial=Decimal("1000"), splits=[dict(date="2022-01-04", ratio="0.5")])


@pytest.mark.parametrize("cash,price", [(Decimal("101.10"), 100), (Decimal("101.09"), 100),
                                      (Decimal("1.00"), 1), (Decimal("11500"), "632.17991")])
def test_affordability_never_borrows(cash, price):
    quantity = affordable_quantity(cash, price)
    if quantity:
        assert sum(purchase_amounts(price, quantity)) <= cash
    assert quantity >= 0
    assert isinstance(quantity, int)


@pytest.mark.parametrize("events,reason", [
    ([dict(ex_date="2022-01-04", rate=1)], "UNKNOWN_DIVIDEND"),
    ([dict(ex_date="2022-01-04", payable_date="2022-01-04", rate=1)] * 2, "DUPLICATE_DISTRIBUTION"),
    ([dict(ex_date="2022-01-04", payable_date="2022-01-03", rate=1)], "INVALID_OR_DUPLICATE"),
    ([dict(ex_date="2022-01-04", payable_date="2022-01-04", rate=-1)], "NEGATIVE_DIVIDEND"),
])
def test_bad_distribution_blocks_without_empty_success(events, reason):
    with pytest.raises(ValueError, match=reason):
        simulate("MOCK", bars("2022-01-03", "2022-01-04"), events)


def test_invalid_prices_and_duplicate_dates_fail():
    with pytest.raises(ValueError, match="INVALID_PRICE"):
        simulate("MOCK", bars("2022-01-03", price=0), [])
    with pytest.raises(ValueError, match="DUPLICATE_OR_UNSORTED"):
        simulate("MOCK", bars("2022-01-03", "2022-01-03"), [])
    with pytest.raises(ValueError, match="NO_REAL_PRICE_ROWS"):
        simulate("MOCK", [], [])


def test_evaluation_uses_prior_mark_no_capital_reset():
    result = simulate("MOCK", [dict(date="2023-12-29", open=100, close=200),
                                dict(date="2024-01-02", open=200, close=220)], [], initial=Decimal("1000"))
    measured = performance(result, "2024-01-01", "2024-12-31", "EVALUATION")
    assert measured["starting_equity"] == 1898.1
    assert measured["net_profit"] == 180
    assert measured["max_drawdown"] == 0


def test_reconciliation_detects_changed_equity_or_entitlement():
    result = simulate("MOCK", bars("2022-01-03", "2022-01-04"),
                      [dict(ex_date="2022-01-04", payable_date="2022-01-04", rate=1)])
    result["curve"][-1]["equity"] += 1
    with pytest.raises(AssertionError, match="RECONCILIATION"):
        reconcile(Decimal("11500"), result["curve"], result["fills"], result["distributions"], [])


def test_rounding_is_explicit_decimal_half_up():
    assert money("1.005") == Decimal("1.01")


def test_affordable_share_checks_booked_cents_at_boundary():
    assert affordable_quantity(Decimal("101.10"), Decimal("100.0001")) == 1
    assert affordable_quantity(Decimal("101.09"), Decimal("100.0001")) == 0
