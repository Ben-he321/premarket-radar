"""Frozen B1 pure calculations. No downloads, orders, cache writes or defaults to data.

All timestamps must be timezone aware. ``entry`` means the expected/actual price
including execution friction, so it must not be charged a second time in RR.
Research callers remain responsible for identity, RTH and earnings qualification.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, ROUND_FLOOR
from math import floor, isfinite
from typing import Mapping, Sequence

import numpy as np
import pandas as pd


EMA_PERIODS = (5, 10, 20, 50, 100)


def _positive(value: object) -> bool:
    try:
        return isfinite(float(value)) and float(value) > 0
    except (ValueError, TypeError):
        return False


def aware(value: object) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    if pd.isna(stamp) or stamp.tzinfo is None:
        raise ValueError("TIMEZONE_AWARE_TIMESTAMP_REQUIRED")
    return stamp


def floor_to_tick(price: float, tick: float = .01) -> float:
    if not _positive(price) or not _positive(tick):
        raise ValueError("INVALID_PRICE_OR_TICK")
    units = (Decimal(str(price)) / Decimal(str(tick))).to_integral_value(rounding=ROUND_FLOOR)
    return float(units * Decimal(str(tick)))


def _support_stop(support: float) -> float:
    return floor_to_tick(Decimal(str(support)) * Decimal("0.99"))


def seeded_ema(values: pd.Series, period: int) -> pd.Series:
    """First n finite positive observations seed the EMA with their arithmetic mean.

    Missing/invalid observations produce NaN and do not create an observation or
    a price. A caller must independently flag calendar gaps in research data.
    """
    if period < 1:
        raise ValueError("INVALID_PERIOD")
    source = pd.Series(values, copy=False)
    output = pd.Series(np.nan, index=source.index, dtype=float)
    seed: list[float] = []
    previous: float | None = None
    alpha = 2.0 / (period + 1)
    for offset, value in enumerate(source):
        if not _positive(value):
            continue
        price = float(value)
        if previous is None:
            seed.append(price)
            if len(seed) == period:
                previous = sum(seed) / period
        else:
            previous = alpha * price + (1 - alpha) * previous
        if previous is not None:
            output.iloc[offset] = previous
    return output


def wilder_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """TR seed is the mean of first n valid TRs, followed by alpha=1/n.

    First observed TR is high-low. A missing predecessor close does not invent a
    gap size: that day's TR is unknown (except the first row).
    """
    if period < 1:
        raise ValueError("INVALID_PERIOD")
    frame = pd.DataFrame({"high": high, "low": low, "close": close})
    valid = frame.apply(lambda s: s.map(_positive)).all(axis=1) & (frame.high >= frame.low)
    previous_close = frame.close.shift()
    tr = pd.concat([frame.high - frame.low, (frame.high - previous_close).abs(),
                    (frame.low - previous_close).abs()], axis=1).max(axis=1)
    tr[~valid] = np.nan
    if len(frame) > 1:
        tr.iloc[1:] = tr.iloc[1:].where(previous_close.iloc[1:].map(_positive))
    result = pd.Series(np.nan, index=frame.index, dtype=float)
    seed: list[float] = []
    prior: float | None = None
    for offset, value in enumerate(tr):
        if pd.isna(value) or value < 0:
            continue
        if prior is None:
            seed.append(float(value))
            if len(seed) == period:
                prior = sum(seed) / period
        else:
            prior = (prior * (period - 1) + float(value)) / period
        if prior is not None:
            result.iloc[offset] = prior
    return result


def daily_features(frame: pd.DataFrame, regular_session_verified: bool = False) -> pd.DataFrame:
    """Features in supplied share units; daily vendor bars alone are only a screen.

    Input must be in increasing session order and contain one security. Passing
    ``regular_session_verified=False`` never produces strict eligible signals.
    """
    result = frame.copy()
    if not result.index.is_monotonic_increasing or not result.index.is_unique:
        raise ValueError("ORDERED_UNIQUE_SESSIONS_REQUIRED")
    if "symbol" in result and result.symbol.nunique() > 1:
        raise ValueError("SINGLE_SECURITY_REQUIRED")
    valid = result[["high", "low", "close"]].apply(lambda s: s.map(_positive)).all(axis=1)
    valid &= (result.high >= result.close) & (result.close >= result.low)
    prices = result.close.where(valid)
    for n in EMA_PERIODS:
        result[f"ema{n}"] = seeded_ema(prices, n)
    result["atr14"] = wilder_atr(result.high.where(valid), result.low.where(valid), prices)
    result["atr14_prev"] = result.atr14.shift()
    result["prior_high30"] = result.high.where(valid).shift().rolling(30, min_periods=30).max()
    result["valid_sessions"] = valid.cumsum()
    result["strict_indicator_eligible"] = (valid & (result.valid_sessions >= 100)
        & result[[f"ema{n}" for n in EMA_PERIODS]].notna().all(axis=1)
        & bool(regular_session_verified))
    above = result.close.gt(result[["ema5", "ema10", "ema20"]].max(axis=1))
    cross = pd.Series(False, index=result.index)
    for n in (5, 10, 20):
        cross |= (result.close.shift() <= result[f"ema{n}"].shift()) & (result.close > result[f"ema{n}"])
    result["first_entry_signal"] = result.strict_indicator_eligible & above & cross
    result["daily_screen_candidate"] = (valid & (result.valid_sessions >= 100)
        & result.ema100.notna() & above & cross)
    return result


def completed_minutes_asof(frame: pd.DataFrame, decision_time: object) -> pd.DataFrame:
    """Only explicitly ended minutes and, when known, already available records.

    Unknown source receipt time stays unknown; it is never inferred from mtime.
    The output supports an offline event-clock replay, not live Basic entitlement.
    """
    decision = aware(decision_time)
    if "bar_end" not in frame:
        raise ValueError("EXPLICIT_BAR_END_REQUIRED")
    result = frame.copy()
    ends = result.bar_end.map(aware)
    eligible = ends <= decision
    if "available_at" in result:
        for index, value in result.available_at.items():
            if value is not None and not pd.isna(value) and str(value) != "UNKNOWN":
                eligible.loc[index] &= aware(value) <= decision
    return result.loc[eligible].copy()


def provisional_ema(previous_ema: float, last_completed_price: float, period: int) -> float:
    if not _positive(previous_ema) or not _positive(last_completed_price) or period < 1:
        raise ValueError("INVALID_PROVISIONAL_EMA_INPUT")
    alpha = 2.0 / (period + 1)
    return alpha * last_completed_price + (1 - alpha) * previous_ema


def entry_signal(close: float, previous_close: float, emas: Mapping[int, float],
                 previous_emas: Mapping[int, float], valid_sessions: int) -> dict:
    if valid_sessions < 100 or any(not _positive(emas.get(n)) for n in EMA_PERIODS):
        return {"allowed": False, "reason": "INDICATOR_WARMUP_INSUFFICIENT"}
    if not _positive(close) or not _positive(previous_close):
        return {"allowed": False, "reason": "INVALID_PRICE"}
    if close <= max(emas[n] for n in (5, 10, 20)):
        return {"allowed": False, "reason": "CLOSE_NOT_ABOVE_SHORT_EMAS"}
    crossed = [n for n in (5, 10, 20) if _positive(previous_emas.get(n))
               and previous_close <= previous_emas[n] and close > emas[n]]
    return {"allowed": bool(crossed), "reason": "FRESH_CROSS" if crossed else "NO_FRESH_CROSS",
            "crossed_periods": crossed}


def nearest_overhead(close: float, emas: Mapping[int, float], prior_high30: float) -> dict:
    pressures = {f"EMA{n}": emas.get(n) for n in (20, 50, 100)}
    pressures["PRIOR_30_HIGH"] = prior_high30
    if not _positive(close) or any(not _positive(p) for p in pressures.values()):
        return {"status": "OVERHEAD_INPUT_UNKNOWN", "price": None, "source": None}
    remaining = [(float(value), name) for name, value in pressures.items() if value > close]
    if not remaining:
        return {"status": "NO_KNOWN_OVERHEAD_IN_DEFINED_SET", "price": None, "source": None}
    price, source = min(remaining)
    return {"status": "KNOWN_OVERHEAD", "price": price, "source": source}


@dataclass(frozen=True)
class StopLeg:
    quantity: int
    support: float
    stop: float


@dataclass(frozen=True)
class StopPlan:
    status: str
    legs: tuple[StopLeg, ...] = ()
    reason: str = ""


def initial_stop_plan(close: float, entry: float, emas: Mapping[int, float], quantity: int) -> StopPlan:
    if quantity < 1 or not _positive(close) or not _positive(entry):
        return StopPlan("REJECTED", reason="INVALID_QUANTITY_OR_PRICE")
    if any(not _positive(emas.get(n)) for n in (10, 20, 50, 100)):
        return StopPlan("REJECTED", reason="SUPPORT_INPUT_UNKNOWN")
    core = [float(emas[n]) for n in (10, 20)]
    candidates = [s for s in core if s < close]
    for n in (50, 100):
        level = float(emas[n])
        candidate_stop = _support_stop(level)
        if (level < close and min(core) - .01 * close <= level <= max(core) + .01 * close
                and 0 < candidate_stop < entry and (entry - candidate_stop) / entry <= .07 + 1e-12):
            candidates.append(level)
    candidates = sorted(set(candidates))
    clusters: list[list[float]] = []
    for level in candidates:
        if not clusters or level - clusters[-1][0] > .005 * close + 1e-12:
            clusters.append([level])
        else:
            clusters[-1].append(level)
    valid = []
    for cluster in clusters:
        support = min(cluster)
        stop = _support_stop(support)
        if 0 < stop < entry and (entry - stop) / entry <= .07 + 1e-12:
            valid.append((support, stop))
    if not valid:
        return StopPlan("REJECTED", reason="NO_STRUCTURAL_STOP_WITHIN_7_PERCENT")
    far, near = valid[0], valid[-1]
    distance = near[0] - far[0]
    if quantity >= 2 and far != near and .005 * entry - 1e-12 <= distance <= .03 * entry + 1e-12:
        legs = (StopLeg((quantity + 1) // 2, *near), StopLeg(quantity // 2, *far))
    else:
        legs = (StopLeg(quantity, *near),)
    return StopPlan("VALID", legs, "TWO_LEGS" if len(legs) == 2 else "ONE_LEG")


def tighten_stop(old_stop: float, confirmed_support: float) -> float:
    if not _positive(old_stop) or not _positive(confirmed_support):
        raise ValueError("UNKNOWN_CONFIRMED_SUPPORT")
    return max(old_stop, _support_stop(confirmed_support))


def split_units(quantity: float, price: float, stop: float, ratio: float) -> dict:
    """Economic equivalent unit conversion; no rounding away fractional shares.

    Caller must process broker-specific cash-in-lieu separately, if applicable.
    """
    if quantity <= 0 or any(not _positive(x) for x in (price, stop, ratio)):
        raise ValueError("INVALID_SPLIT_INPUT")
    return {"quantity": quantity * ratio, "price": price / ratio, "stop": stop / ratio,
            "gross_value": quantity * price}


def net_reward_risk(entry: float, legs: Sequence[StopLeg], overhead: float | None,
                    commission: float = 1, exit_friction: float = .001,
                    overhead_inputs_complete: bool = True) -> dict:
    if not overhead_inputs_complete:
        return {"allowed": False, "status": "OVERHEAD_INPUT_UNKNOWN", "rr": None}
    if not legs or not _positive(entry) or commission < 0 or not 0 <= exit_friction < 1:
        raise ValueError("INVALID_RR_INPUT")
    quantity = sum(leg.quantity for leg in legs)
    if any(leg.quantity <= 0 or not 0 < leg.stop < entry for leg in legs):
        raise ValueError("INVALID_STOP_LEG")
    costs = commission * (1 + len(legs))
    risk = sum(leg.quantity * (entry - leg.stop * (1 - exit_friction)) for leg in legs) + costs
    if overhead is None:
        return {"allowed": True, "status": "NO_KNOWN_OVERHEAD_IN_DEFINED_SET", "rr": None,
                "risk_net": risk, "reward_net": None, "identified_2r_target": False}
    if not _positive(overhead):
        raise ValueError("INVALID_OVERHEAD")
    reward = quantity * (overhead * (1 - exit_friction) - entry) - costs
    rr = reward / risk
    return {"allowed": rr >= 2 - 1e-12, "status": "RR_PASSED" if rr >= 2 - 1e-12 else "RR_BELOW_2",
            "rr": rr, "risk_net": risk, "reward_net": reward, "identified_2r_target": True}


def constrained_entry_limit(close: float, legs: Sequence[StopLeg], overhead: float | None,
                            commission: float = 1, exit_friction: float = .001) -> float:
    """Lower of close+1%, per-leg 7% distance and known-overhead net 2R.

    Input legs already express the proposed quantities; recalculate after any
    quote-driven partial fill because fixed fees per share can change net RR.
    """
    if not legs or not _positive(close):
        raise ValueError("INVALID_LIMIT_INPUT")
    limits = [close * 1.01, *[leg.stop / .93 for leg in legs]]
    if overhead is not None:
        quantity = sum(leg.quantity for leg in legs)
        costs = commission * (1 + len(legs))
        limits.append((quantity * overhead * (1 - exit_friction)
            + 2 * sum(leg.quantity * leg.stop * (1 - exit_friction) for leg in legs)
            - 3 * costs) / (3 * quantity))
    return floor_to_tick(min(limits))


@dataclass(frozen=True)
class Quote:
    bid: float
    ask: float
    ask_quantity: float
    event_time: object
    size_unit: str = "UNKNOWN"
    available_at: object | None = None
    source_received_at: object | None = None


def capped_quote_fill(quote: Quote, now: object, order_created_at: object, expires_at: object,
                      limit: float, target_quantity: int, budget: float,
                      short_ema_top: float, highest_stop: float,
                      commission: float = 1, exit_reserve: float = 1,
                      friction: float = .001) -> dict:
    """Single quote consumption proxy, explicitly not a guaranteed broker fill.

    Quote size must already be verified/normalized to shares. Caller persists
    quote consumption IDs and must not reuse displayed size across order events.
    """
    now, created, expires = aware(now), aware(order_created_at), aware(expires_at)
    result = {"status": "NO_FILL", "quantity": 0, "price": None, "reason": None,
              "execution_evidence": "HISTORICAL_QUOTE_EXECUTION_PROXY",
              "source_received_at": "UNKNOWN" if quote.source_received_at is None else str(quote.source_received_at)}
    reason = None
    if now < created:
        reason = "ORDER_NOT_CREATED"
    elif now >= expires:
        reason = "ORDER_EXPIRED"
    elif any(not _positive(x) for x in (quote.bid, quote.ask, limit, short_ema_top, highest_stop)):
        reason = "INVALID_OR_ZERO_QUOTE"
    elif quote.ask < quote.bid:
        reason = "CROSSED_QUOTE"
    elif quote.size_unit != "shares":
        reason = "QUOTE_SIZE_UNIT_UNKNOWN"
    elif not _positive(quote.ask_quantity):
        reason = "NO_DISPLAYED_ASK_QUANTITY"
    elif (quote.ask - quote.bid) / ((quote.ask + quote.bid) / 2) > .003 + 1e-12:
        reason = "SPREAD_TOO_WIDE"
    elif not 0 <= (now - aware(quote.event_time)).total_seconds() <= 5:
        reason = "STALE_OR_FUTURE_QUOTE"
    elif quote.available_at is not None and aware(quote.available_at) > now:
        reason = "QUOTE_NOT_YET_AVAILABLE"
    elif quote.ask < short_ema_top or quote.ask <= highest_stop:
        reason = "ENTRY_STRUCTURE_INVALIDATED"
    elif target_quantity <= 0 or budget < 0 or commission < 0 or exit_reserve < 0 or friction < 0:
        reason = "INVALID_ORDER_BUDGET"
    if reason:
        result["reason"] = reason
        return result
    price = quote.ask * (1 + friction)
    if price > limit + 1e-12:
        result["reason"] = "FRICTION_INCLUSIVE_PRICE_EXCEEDS_LIMIT"
        return result
    affordable = max(0, floor((budget - commission - exit_reserve) / price + 1e-12))
    quantity = min(target_quantity, floor(quote.ask_quantity), affordable)
    if quantity < 1:
        result["reason"] = "NO_AFFORDABLE_DISPLAYED_SHARES"
        return result
    return {**result, "status": "FILLED" if quantity == target_quantity else "PARTIAL_FILL",
            "quantity": quantity, "price": price, "commission": commission,
            "reason": "QUOTED_SIZE_AND_BUDGET_CAP", "remaining_quantity": target_quantity - quantity}


def stop_trigger_orders(legs: Sequence[StopLeg], bid: float | None, is_rth: bool,
                        commission: float = 1, friction: float = .001) -> list[dict]:
    """Gap price, not crossed stop level, is the exit proxy; one order per leg."""
    if not is_rth or bid is None or not _positive(bid):
        return []
    return [{"quantity": leg.quantity, "price": bid * (1 - friction), "stop": leg.stop,
             "commission": commission, "reason": "RTH_STOP", "leg_index": index}
            for index, leg in sorted(enumerate(legs), key=lambda pair: -pair[1].stop)
            if bid <= leg.stop]


def deviation_reduction(price: float, provisional5: float, provisional10: float, atr_previous: float,
                         original_quantity: int, current_quantity: int, entry_cost_per_share: float,
                         already_used: bool = False) -> dict:
    if already_used:
        return {"quantity": 0, "triggered": False, "reason": "ONCE_PER_CAMPAIGN_ALREADY_USED"}
    if any(not _positive(v) for v in (price, provisional5, provisional10, atr_previous, entry_cost_per_share)):
        return {"quantity": 0, "triggered": False, "reason": "DEVIATION_INPUT_UNKNOWN"}
    if original_quantity < current_quantity or current_quantity < 0:
        raise ValueError("INVALID_CAMPAIGN_QUANTITY")
    condition = (price - provisional5 >= max(.06 * provisional5, 2 * atr_previous)
                 and price - provisional10 >= max(.10 * provisional10, 3 * atr_previous))
    if not condition or price <= entry_cost_per_share:
        return {"quantity": 0, "triggered": False, "reason": "NO_PROFITABLE_DEVIATION"}
    # Integer executable quantity: round down the half target, keeping any odd share.
    desired_reduction = original_quantity // 2
    already_reduced = original_quantity - current_quantity
    quantity = max(0, min(current_quantity, desired_reduction - already_reduced))
    return {"quantity": quantity, "triggered": True,
            "reason": "REDUCE_TO_HALF_ORIGINAL" if quantity else "HALF_ALREADY_REDUCED"}


@dataclass(frozen=True)
class PressureState:
    anchor_id: str
    level: float
    first_failure_session: int | None = None


def pressure_failure(state: PressureState, anchor_id: str, session_index: int,
                     price: float, observed_high: float, atr_previous: float,
                     observation_time: object, active_decision_time: object,
                     session_close: object) -> dict:
    """The first failure requires the close; the second can be known at close-10m.

    ``observed_high`` must come from bars ended by ``observation_time``. A later
    failure receives next-RTH intent, never an invented earlier exit timestamp.
    Session index is the supplied exchange-calendar ordinal, not calendar days.
    """
    observed, decision, close = map(aware, (observation_time, active_decision_time, session_close))
    if anchor_id != state.anchor_id:
        return {"state": state, "action": "NONE", "reason": "DIFFERENT_ANCHOR_NOT_COMBINED"}
    if any(not _positive(x) for x in (state.level, price, observed_high, atr_previous)):
        return {"state": state, "action": "NONE", "reason": "PRESSURE_INPUT_UNKNOWN"}
    delta = min(.0075 * price, max(.0025 * price, .25 * atr_previous))
    first = state.first_failure_session
    if first is not None and (session_index < first or session_index - first > 5):
        state = replace(state, first_failure_session=None)
        first = None
    if observed >= close and price > state.level + delta:
        return {"state": replace(state, first_failure_session=None), "action": "NONE", "reason": "SUCCESSFUL_CLOSE_RESET"}
    failed = observed_high >= state.level - delta and price < state.level
    if not failed:
        return {"state": state, "action": "NONE", "reason": "NO_FAILURE"}
    if first is None:
        if observed >= close:
            return {"state": replace(state, first_failure_session=session_index), "action": "NONE", "reason": "FIRST_CLOSE_FAILURE"}
        return {"state": state, "action": "NONE", "reason": "FIRST_FAILURE_AWAITS_CLOSE"}
    if session_index == first:
        return {"state": state, "action": "NONE", "reason": "SAME_SESSION_COUNTS_ONCE"}
    if observed == decision:
        return {"state": state, "action": "EXIT_NOW", "reason": "SECOND_FAILURE_KNOWN_AT_DECISION"}
    if observed > decision and observed >= close:
        return {"state": state, "action": "EXIT_NEXT_RTH", "reason": "SECOND_FAILURE_CONFIRMED_AFTER_DECISION"}
    return {"state": state, "action": "NONE", "reason": "AWAIT_FIXED_DECISION_OR_CLOSE"}


def reentry_eligible(completed_sessions_after_exit: int, repaired_days: Sequence[dict],
                     close: float, previous_high: float, emas: Mapping[int, float],
                     decision_time: object | None = None) -> dict:
    """Require timestamped repair bars strictly before the new signal's NY date.

    The elapsed-session count must be produced by the supplied exchange calendar;
    the current date's move cannot retrospectively supply its own prior repair.
    """
    if completed_sessions_after_exit < 2:
        return {"allowed": False, "reason": "WAIT_TWO_COMPLETE_RTH_SESSIONS"}
    if decision_time is None:
        return {"allowed": False, "reason": "REPAIR_DECISION_TIMESTAMP_REQUIRED"}
    decision = aware(decision_time)
    signal_date = decision.tz_convert("America/New_York").date()
    repair_seen = False
    for day in repaired_days:
        if day.get("session_close") is None:
            continue
        ended = aware(day["session_close"])
        if ended > decision or ended.tz_convert("America/New_York").date() >= signal_date:
            continue
        available = day.get("available_at")
        if available is not None and str(available) != "UNKNOWN" and aware(available) > decision:
            continue
        if any(not _positive(day.get(k)) for k in ("low", "high", "close", "ema5", "ema10", "atr14_prev")):
            continue
        atr = day["atr14_prev"]
        touch = any(day["low"] <= day[k] + .5 * atr and day["high"] >= day[k] - .5 * atr
                    for k in ("ema5", "ema10"))
        if touch and abs(day["close"] - day["ema5"]) <= atr:
            repair_seen = True
            break
    if not repair_seen:
        return {"allowed": False, "reason": "NO_PRIOR_REPAIR_EVIDENCE"}
    if any(not _positive(emas.get(n)) for n in (5, 10, 20)) or not _positive(previous_high):
        return {"allowed": False, "reason": "REENTRY_INPUT_UNKNOWN"}
    allowed = close > previous_high and close > max(emas[n] for n in (5, 10, 20))
    return {"allowed": allowed, "reason": "REENTRY_STRUCTURE_PASSED_RECHECK_ALL_GATES" if allowed else "REENTRY_BREAKOUT_NOT_CONFIRMED"}
