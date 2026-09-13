"""MOCK ONLY pure-function examples; no research data, network or ledger IO."""
from dataclasses import replace

import numpy as np
import pandas as pd
import pandas_market_calendars as mcal
import pytest

from src.ben_b1.events import (EarningsRevision, earnings_gate, earnings_window,
                              first_executable_rth, session_clock, completed_sessions_since_exit)
from src.ben_b1.rules import (PressureState, Quote, StopLeg, capped_quote_fill,
    completed_minutes_asof, constrained_entry_limit, daily_features,
    deviation_reduction, entry_signal, initial_stop_plan, nearest_overhead,
    net_reward_risk, pressure_failure, provisional_ema, reentry_eligible,
    seeded_ema, split_units, stop_trigger_orders, tighten_stop, wilder_atr)


def ts(value):
    return pd.Timestamp(value, tz="America/New_York")


@pytest.fixture(scope="module")
def schedule():
    return mcal.get_calendar("NYSE").schedule("2025-01-01", "2026-12-31")


def test_ema_is_seeded_sma_then_exponential_and_never_future():
    prices = pd.Series([1., 2., 3., 8., 9.])
    actual = seeded_ema(prices, 3)
    assert actual.iloc[:2].isna().all()
    assert actual.iloc[2:].tolist() == [2., 5., 7.]
    assert actual.iloc[3] != prices.rolling(3).mean().iloc[3]
    changed = prices.copy(); changed.iloc[4] = 1000.
    pd.testing.assert_series_equal(actual.iloc[:4], seeded_ema(changed, 3).iloc[:4])


def test_ema_missing_does_not_fabricate_price_or_warmup():
    output = seeded_ema(pd.Series([1., np.nan, 0., 2., 3.]), 3)
    assert output.iloc[:4].isna().all()
    assert output.iloc[4] == 2


def test_wilder_atr_seed_and_gap():
    high = pd.Series([11., 14., 16., 17.])
    low = pd.Series([9., 12., 14., 15.])
    close = pd.Series([10., 13., 15., 16.])
    # TR 2,4,3,2 => ATR3 seed=3 then (3*2+2)/3.
    atr = wilder_atr(high, low, close, 3)
    assert atr.iloc[:2].isna().all()
    assert atr.iloc[2] == 3
    assert atr.iloc[3] == pytest.approx(8 / 3)


def test_daily_screen_never_certifies_unverified_daily_rth():
    close = [100.] * 100 + [110.]
    frame = pd.DataFrame({"close": close, "high": np.array(close) + 1,
                          "low": np.array(close) - 1})
    coarse = daily_features(frame, regular_session_verified=False)
    verified = daily_features(frame, regular_session_verified=True)
    assert not coarse.strict_indicator_eligible.any()
    assert coarse.daily_screen_candidate.iloc[-1]
    assert verified.first_entry_signal.iloc[-1]
    assert not verified.strict_indicator_eligible.iloc[:99].any()
    assert verified.prior_high30.iloc[-1] == 101  # Today's high excluded.
    changed = frame.copy(); changed.loc[100, "high"] = 9999
    assert daily_features(changed, True).prior_high30.iloc[-1] == 101


def test_daily_rejects_mixed_identities_and_session_order():
    frame = pd.DataFrame({"symbol": ["A", "B"], "high": [2., 2.], "low": [1., 1.], "close": [1.5, 1.5]})
    with pytest.raises(ValueError, match="SINGLE_SECURITY"):
        daily_features(frame)
    with pytest.raises(ValueError, match="ORDERED_UNIQUE"):
        daily_features(frame.iloc[::-1])


def test_intraday_1550_excludes_1600_and_late_availability():
    frame = pd.DataFrame({"bar_end": [ts("2026-09-11 15:49"), ts("2026-09-11 15:50"), ts("2026-09-11 16:00")],
                          "close": [100., 101., 200.],
                          "available_at": [None, ts("2026-09-11 15:51"), None],
                          "source_received_at": ["UNKNOWN"] * 3})
    selected = completed_minutes_asof(frame, ts("2026-09-11 15:50"))
    assert selected.close.tolist() == [100.]
    assert selected.source_received_at.tolist() == ["UNKNOWN"]
    assert provisional_ema(90, selected.close.iloc[-1], 5) == pytest.approx(93.33333333)
    with pytest.raises(ValueError, match="TIMEZONE_AWARE"):
        completed_minutes_asof(frame, "2026-09-11 15:50")


def test_first_cross_does_not_require_ordered_emas_but_requires_20():
    emas = {5: 98., 10: 99., 20: 97., 50: 95., 100: 96.}
    previous = {5: 97., 10: 99., 20: 98.}
    assert entry_signal(100, 98, emas, previous, 100)["allowed"]
    assert not entry_signal(96, 95, emas, previous, 100)["allowed"]
    assert not entry_signal(100, 98, emas, previous, 99)["allowed"]


def test_support_clustering_not_transitive_two_legs_odd_share():
    # 97,97.4 belong together; 97.8 must not transitively enter their .5 range.
    plan = initial_stop_plan(100, 100, {10: 97., 20: 97.4, 50: 97.8, 100: 98.2}, 5)
    assert plan.status == "VALID"
    assert [(leg.quantity, leg.support, leg.stop) for leg in plan.legs] == [(3, 97.8, 96.82), (2, 97., 96.03)]
    one = initial_stop_plan(100, 100, {10: 97., 20: 97.4, 50: 97.8, 100: 98.2}, 1)
    assert len(one.legs) == 1 and one.legs[0].quantity == 1


def test_far_long_ema_does_not_drag_support_down():
    plan = initial_stop_plan(100, 100, {10: 99., 20: 98., 50: 80., 100: 60.}, 10)
    assert plan.status == "VALID"
    assert {leg.support for leg in plan.legs} == {98., 99.}
    invalid = initial_stop_plan(100, 100, {10: 90., 20: 88., 50: 89., 100: 88.5}, 10)
    assert invalid.reason == "NO_STRUCTURAL_STOP_WITHIN_7_PERCENT"


def test_stop_7_percent_after_tick_rounding_and_never_clipped():
    boundary = 93.01 / .99
    valid = initial_stop_plan(100, 100, {10: boundary, 20: boundary, 50: 50., 100: 40.}, 2)
    assert valid.status == "VALID" and valid.legs[0].stop == 93.01
    # Unrounded stop 92.9999 fails, not silently replaced by exactly 93.
    invalid = initial_stop_plan(100, 100, {10: 92.9999/.99, 20: 92.9999/.99, 50: 50., 100: 40.}, 2)
    assert invalid.status == "REJECTED"
    assert initial_stop_plan(100, 100, {10: 98, 20: 97, 50: 99, 100: np.nan}, 5).reason == "SUPPORT_INPUT_UNKNOWN"


def test_trailing_tightens_and_split_is_economic_unit_change():
    assert tighten_stop(97, 95) == 97
    assert tighten_stop(97, 100) == 99
    split = split_units(5, 100, 95, 4)
    assert split == {"quantity": 20, "price": 25., "stop": 23.75, "gross_value": 500}
    assert split["quantity"] * split["stop"] == 5 * 95


def test_gap_triggers_both_legs_at_observable_price_and_two_order_fees():
    legs = (StopLeg(3, 99, 98.01), StopLeg(2, 97, 96.03))
    assert not stop_trigger_orders(legs, 90, False)
    assert not stop_trigger_orders(legs, None, True)
    orders = stop_trigger_orders(legs, 90, True)
    assert [order["quantity"] for order in orders] == [3, 2]
    assert sum(order["commission"] for order in orders) == 2
    assert all(order["price"] == pytest.approx(89.91) for order in orders)
    assert sum(order["quantity"] * (100 - order["price"]) for order in orders) > 500 * .07


def test_overhead_missing_not_no_known_and_nearest_cannot_be_skipped():
    assert nearest_overhead(100, {20: 99, 50: 102, 100: 120}, 130)["price"] == 102
    assert nearest_overhead(100, {20: 99, 50: 98, 100: 97}, 95)["status"] == "NO_KNOWN_OVERHEAD_IN_DEFINED_SET"
    assert nearest_overhead(100, {20: 99, 50: 98, 100: np.nan}, 95)["status"] == "OVERHEAD_INPUT_UNKNOWN"


def test_net_rr_charges_whole_campaign_planned_legs_and_not_infinite():
    legs = (StopLeg(3, 99, 98), StopLeg(2, 97, 96))
    rr = net_reward_risk(100, legs, 102)
    assert not rr["allowed"]
    expected_risk = 3 * (100 - 98 * .999) + 2 * (100 - 96 * .999) + 3
    assert rr["risk_net"] == pytest.approx(expected_risk)
    no_target = net_reward_risk(100, legs, None)
    assert no_target["allowed"] and no_target["rr"] is None
    assert not no_target["identified_2r_target"]
    assert not net_reward_risk(100, legs, None, overhead_inputs_complete=False)["allowed"]


def test_limit_recomputed_from_stop_and_net_rr():
    legs = (StopLeg(30, 98, 97),)
    limit = constrained_entry_limit(100, legs, 110)
    assert limit <= 101
    assert net_reward_risk(limit, legs, 110)["allowed"]
    closer = constrained_entry_limit(100, legs, 103)
    assert closer < limit
    assert net_reward_risk(closer, legs, 103)["allowed"]


def fill(quote=None, **changes):
    now = ts("2026-09-11 16:05:00")
    quote = quote or Quote(100, 100.10, 20, now - pd.Timedelta(seconds=1), "shares")
    args = dict(quote=quote, now=now, order_created_at=now, expires_at=ts("2026-09-11 16:15"),
                limit=101., target_quantity=10, budget=1100., short_ema_top=99, highest_stop=97)
    args.update(changes)
    return capped_quote_fill(**args)


def test_quote_uses_lower_ask_not_limit_and_preserves_unknown_receipt():
    result = fill()
    assert result["status"] == "FILLED"
    assert result["price"] == pytest.approx(100.2001)
    assert result["price"] < 101
    assert result["source_received_at"] == "UNKNOWN"


@pytest.mark.parametrize("bid,ask,quantity,unit,expected", [
    (100.9, 101., 10, "shares", "FRICTION_INCLUSIVE_PRICE_EXCEEDS_LIMIT"),
    (100, 100.5, 10, "shares", "SPREAD_TOO_WIDE"),
    (0, 100, 10, "shares", "INVALID_OR_ZERO_QUOTE"),
    (101, 100, 10, "shares", "CROSSED_QUOTE"),
    (100, 100.1, 0, "shares", "NO_DISPLAYED_ASK_QUANTITY"),
    (100, 100.1, 10, "UNKNOWN", "QUOTE_SIZE_UNIT_UNKNOWN"),
    (98., 98.1, 10, "shares", "ENTRY_STRUCTURE_INVALIDATED"),
])
def test_quote_rejects_unexecutable_cases(bid, ask, quantity, unit, expected):
    quote = Quote(bid, ask, quantity, ts("2026-09-11 16:05"), unit)
    assert fill(quote)["reason"] == expected


def test_quote_partial_budget_expiry_age_availability_and_cost_stress():
    quote = Quote(100, 100.1, 3, ts("2026-09-11 16:05"), "shares")
    assert fill(quote)["quantity"] == 3
    assert fill(quote)["status"] == "PARTIAL_FILL"
    assert fill(quote, budget=202)["quantity"] == 1  # Buy+exit fee reserves.
    assert fill(quote, now=ts("2026-09-11 16:15"))["reason"] == "ORDER_EXPIRED"
    assert fill(replace(quote, event_time=ts("2026-09-11 16:04:54")))["reason"] == "STALE_OR_FUTURE_QUOTE"
    assert fill(replace(quote, event_time=ts("2026-09-11 16:05:01")))["reason"] == "STALE_OR_FUTURE_QUOTE"
    assert fill(replace(quote, available_at=ts("2026-09-11 16:06")))["reason"] == "QUOTE_NOT_YET_AVAILABLE"
    tighter = replace(quote, bid=100.7, ask=100.8)
    assert fill(tighter)["quantity"] == 3
    assert fill(tighter, friction=.0025)["reason"] == "FRICTION_INCLUSIVE_PRICE_EXCEEDS_LIMIT"


def test_deviation_requires_both_scales_profit_and_only_original_half_once():
    base = dict(price=130, provisional5=110, provisional10=105, atr_previous=2,
                original_quantity=10, current_quantity=10, entry_cost_per_share=100)
    assert deviation_reduction(**base)["quantity"] == 5
    assert deviation_reduction(**{**base, "current_quantity": 7})["quantity"] == 2
    assert deviation_reduction(**{**base, "current_quantity": 5})["quantity"] == 0
    assert deviation_reduction(**{**base, "already_used": True})["quantity"] == 0
    assert deviation_reduction(**{**base, "atr_previous": 20})["quantity"] == 0
    assert deviation_reduction(**{**base, "entry_cost_per_share": 135})["quantity"] == 0


def failure(state, session=11, observed="2026-09-11 15:50", **changes):
    args = dict(state=state, anchor_id="EMA50_AT_FIRST_FAILURE", session_index=session,
                price=99, observed_high=100, atr_previous=1, observation_time=ts(observed),
                active_decision_time=ts("2026-09-11 15:50"), session_close=ts("2026-09-11 16:00"))
    args.update(changes)
    return pressure_failure(**args)


def test_pressure_first_requires_close_same_day_once_different_anchor_separate():
    state = PressureState("EMA50_AT_FIRST_FAILURE", 100)
    assert failure(state)["reason"] == "FIRST_FAILURE_AWAITS_CLOSE"
    confirmed = failure(state, observed="2026-09-11 16:00")["state"]
    assert confirmed.first_failure_session == 11
    assert failure(confirmed)["reason"] == "SAME_SESSION_COUNTS_ONCE"
    assert failure(confirmed, session=12, anchor_id="PRIOR30_AT_FIRST_FAILURE")["reason"] == "DIFFERENT_ANCHOR_NOT_COMBINED"


def test_pressure_second_fixed_clock_not_backdated_expiry_and_reset():
    state = PressureState("EMA50_AT_FIRST_FAILURE", 100, 10)
    assert failure(state)["action"] == "EXIT_NOW"
    assert failure(state, observed="2026-09-11 15:55")["action"] == "NONE"
    assert failure(state, observed="2026-09-11 16:00")["action"] == "EXIT_NEXT_RTH"
    assert failure(state, session=16)["reason"] == "FIRST_FAILURE_AWAITS_CLOSE"
    reset = failure(state, observed="2026-09-11 16:00", price=102, observed_high=102)
    assert reset["state"].first_failure_session is None


def test_reentry_wait_and_repair_are_prior_observations():
    days = [{"low": 98, "high": 101, "close": 100, "ema5": 100, "ema10": 99, "atr14_prev": 2,
             "session_close": ts("2026-09-09 16:00")}]
    args = dict(repaired_days=days, close=104, previous_high=103, emas={5: 101, 10: 100, 20: 99},
                decision_time=ts("2026-09-11 16:05"))
    assert not reentry_eligible(1, **args)["allowed"]
    assert reentry_eligible(2, **args)["allowed"]
    assert not reentry_eligible(2, **{**args, "repaired_days": []})["allowed"]
    assert not reentry_eligible(2, **{**args, "close": 102})["allowed"]
    for repair_time in ("2026-09-11 16:00", "2026-09-14 16:00"):
        future = [{**days[0], "session_close": ts(repair_time)}]
        assert not reentry_eligible(2, **{**args, "repaired_days": future})["allowed"]
    late = [{**days[0], "available_at": ts("2026-09-12 10:00")}]
    assert not reentry_eligible(2, **{**args, "repaired_days": late})["allowed"]
    assert not reentry_eligible(2, **{**args, "decision_time": None})["allowed"]


def test_reentry_session_count_uses_complete_calendar_sessions(schedule):
    exit_time = ts("2026-09-04 15:50")  # Friday, followed by Labor Day holiday.
    assert completed_sessions_since_exit(schedule, exit_time, ts("2026-09-08 16:05")) == 1
    assert completed_sessions_since_exit(schedule, exit_time, ts("2026-09-09 16:05")) == 2


def release(event_id="q1", planned="2026-05-07", known="2026-04-01 09:00", actual="2026-05-07 16:05", **changes):
    args = dict(event_id=event_id, planned_date=planned, known_at=ts(known),
                actual_release_at=ts(actual), release_known_at=ts(actual), source="MOCK_IR", time_class="AMC")
    args.update(changes)
    return EarningsRevision(**args)


def test_exchange_early_close_dst_and_holiday_clocks(schedule):
    half = session_clock(schedule, "2026-11-27")
    assert "13:00:00-05:00" in half["new_york"]["market_close"]
    assert "12:50:00-05:00" in half["new_york"]["active_exit_decision"]
    # Different US/EU DST switchover: difference is five hours in this week.
    march = session_clock(schedule, "2026-03-16")
    assert "16:05:00-04:00" in march["new_york"]["entry_decision"]
    assert "21:05:00+01:00" in march["madrid"]["entry_decision"]
    with pytest.raises(ValueError, match="NOT_AN_EXCHANGE"):
        session_clock(schedule, "2026-12-25")


def test_thursday_release_blackout_starts_prior_friday_1550(schedule):
    window = earnings_window(schedule, "2026-05-07")
    assert window["forbidden_preceding_sessions"] == ["2026-05-04", "2026-05-05", "2026-05-06"]
    assert pd.Timestamp(window["required_exit_time"]).tz_convert("America/New_York") == ts("2026-05-01 15:50")
    before = earnings_gate(ts("2026-05-01 15:49"), schedule, [release()])
    assert before["new_entry_allowed"] and before["tier"] == "A_VERIFIED_PIT"
    for decision in ("2026-05-01 15:50", "2026-05-01 16:05", "2026-05-07 16:05", "2026-05-08 16:04"):
        assert not earnings_gate(ts(decision), schedule, [release()])["new_entry_allowed"]


def test_earnings_holidays_weekend_nontrading_release(schedule):
    thanksgiving = earnings_window(schedule, "2026-11-30")
    assert thanksgiving["forbidden_preceding_sessions"] == ["2026-11-24", "2026-11-25", "2026-11-27"]
    saturday = earnings_window(schedule, "2026-05-09")
    assert saturday["forbidden_preceding_sessions"] == ["2026-05-06", "2026-05-07", "2026-05-08"]
    assert saturday["resume_session"] == "2026-05-11"
    assert first_executable_rth(schedule, ts("2026-11-26 10:00")) == ts("2026-11-27 09:30")


@pytest.mark.parametrize("time_class,actual", [("AMC", "2026-05-07 16:05"), ("BMO", "2026-05-07 07:00")])
def test_bmo_amc_both_require_next_full_later_date_session(schedule, time_class, actual):
    next_event = release("q2", "2026-08-06", "2026-04-01 09:00", "2026-08-06 16:05")
    events = [release(actual=actual, time_class=time_class), next_event]
    assert not earnings_gate(ts("2026-05-08 16:04"), schedule, events)["new_entry_allowed"]
    assert earnings_gate(ts("2026-05-08 16:05"), schedule, events)["new_entry_allowed"]
    assert not earnings_gate(ts("2026-05-08 16:05"), schedule, [events[0]])["new_entry_allowed"]


def test_unknown_empty_conference_fiscal_period_cannot_stand_in_for_release(schedule):
    now = ts("2026-05-01 12:00")
    assert earnings_gate(now, schedule, [])["status"] == "EARNINGS_UNKNOWN"
    for kind in ("conference_call", "fiscal_period_end", "sec_10q"):
        assert earnings_gate(now, schedule, [release(event_kind=kind)])["status"] == "EARNINGS_UNKNOWN"
    assert earnings_gate(now, schedule, [release(planned_date=None)])["status"] == "EARNINGS_UNKNOWN"
    assert earnings_gate(now, schedule, [release(conflict=True)])["status"] == "EARNINGS_UNKNOWN"
    assert earnings_gate(now, schedule, [release(actual_release_at=None)])["status"] == "EARNINGS_UNKNOWN"


def test_actual_release_only_is_separate_b_never_strict_pit(schedule):
    event = release(known_at=None, planned_date=None)
    now = ts("2026-04-30 16:05")
    assert not earnings_gate(now, schedule, [event])["new_entry_allowed"]
    retro = earnings_gate(now, schedule, [event], retrospective=True)
    assert retro["new_entry_allowed"]
    assert retro["tier"] == "B_ACTUAL_RELEASE_ONLY" and not retro["strict_eligible"]
    assert retro["research_label"] == "RETROSPECTIVE_EARNINGS_EXCLUSION"


def test_date_revision_only_applies_after_publication_and_receipt_unknown_kept(schedule):
    old = release(planned="2026-05-14", actual="2026-05-14 16:05")
    revised = release(planned="2026-05-07", known="2026-05-01 16:00")
    previous = earnings_gate(ts("2026-05-01 15:55"), schedule, [old, revised])
    assert previous["new_entry_allowed"]
    current = earnings_gate(ts("2026-05-01 16:05"), schedule, [old, revised])
    assert not current["new_entry_allowed"]
    assert current["evidence"][0]["source_received_at"] == "UNKNOWN"
    assert current["evidence"][0]["planned_release_date"] == "2026-05-07"


def test_unexpected_earlier_release_does_not_invent_prior_exit(schedule):
    event = release(planned="2026-05-14", actual="2026-05-05 12:00")
    before = earnings_gate(ts("2026-05-04 16:05"), schedule, [event])
    assert before["new_entry_allowed"]
    after = earnings_gate(ts("2026-05-05 12:00"), schedule, [event])
    assert not after["new_entry_allowed"]
    assert pd.Timestamp(after["required_exit_time"]) == ts("2026-05-05 12:00")
    assert after["evidence"][0]["unexpected_date_change"]
    assert "hypothetical_prior_exit_not_knowable" in after["evidence"][0]


def test_conflicting_same_time_revisions_fail_closed(schedule):
    result = earnings_gate(ts("2026-04-15 12:00"), schedule,
                           [release(planned="2026-05-07"), release(planned="2026-05-14")])
    assert result["status"] == "EARNINGS_UNKNOWN"
    assert result["reason"] == "CONFLICTING_SIMULTANEOUS_DATE_VERSIONS"


def test_date_only_actual_release_preserves_pit_plan_without_fake_timestamp(schedule):
    event = EarningsRevision(event_id="q1", planned_date="2026-05-07", known_at=ts("2026-04-01 09:00"),
                             actual_release_date="2026-05-07", actual_time_precision="DATE",
                             source="MOCK_DATE_ONLY_IR")
    before = earnings_gate(ts("2026-04-30 16:05"), schedule, [event])
    assert before["new_entry_allowed"] and before["tier"] == "A_VERIFIED_PIT"
    evidence = before["evidence"][0]
    assert evidence["actual_release_at"] == "UNKNOWN"
    assert evidence["actual_release_date"] == "2026-05-07"
    assert evidence["actual_time_precision"] == "DATE"
    assert evidence["source_received_at"] == "UNKNOWN"
    assert not evidence["release_confirmed_asof"]
    assert not earnings_gate(ts("2026-05-07 16:05"), schedule, [event])["new_entry_allowed"]
    next_event = release("q2", "2026-08-06", "2026-04-01 09:00", "2026-08-06 16:05")
    events = [event, next_event]
    assert not earnings_gate(ts("2026-05-08 16:04"), schedule, events)["new_entry_allowed"]
    recovered = earnings_gate(ts("2026-05-08 16:05"), schedule, events)
    assert recovered["new_entry_allowed"]
    assert recovered["evidence"][0]["release_confirmed_asof"]
    assert recovered["evidence"][0]["release_completion_basis"] == "AFTER_RELEASE_NY_DATE_BOUND"
    assert not earnings_gate(ts("2026-05-08 16:05"), schedule, [event])["new_entry_allowed"]


def test_date_only_b_stays_retrospective_and_conflicting_exact_date_rejected(schedule):
    event = EarningsRevision(event_id="q1", actual_release_date="2026-05-07", actual_time_precision="DATE")
    now = ts("2026-04-30 16:05")
    assert not earnings_gate(now, schedule, [event])["new_entry_allowed"]
    retrospective = earnings_gate(now, schedule, [event], retrospective=True)
    assert retrospective["new_entry_allowed"] and not retrospective["strict_eligible"]
    assert retrospective["tier"] == "B_ACTUAL_RELEASE_ONLY"
    bad = replace(release(), actual_release_date="2026-05-08")
    assert earnings_gate(now, schedule, [bad])["reason"] == "CONFLICTING_RELEASE_DATE_AND_TIMESTAMP"


def test_date_only_early_surprise_uses_current_detection_no_backdated_clock(schedule):
    event = EarningsRevision(event_id="q1", planned_date="2026-05-14", known_at=ts("2026-04-01 09:00"),
                             actual_release_date="2026-05-05", actual_time_precision="DATE")
    now = ts("2026-05-06 10:00")
    output = earnings_gate(now, schedule, [event])
    assert not output["new_entry_allowed"]
    assert pd.Timestamp(output["required_exit_time"]) == now
    assert output["evidence"][0]["actual_release_at"] == "UNKNOWN"
