"""Read-only, one-integer-contract diagnostics for the frozen F0 protocol.

This is not an order generator, a qualification override or an account replay.
Unknown ATR, prior close or execution-contract liquidity stays unknown. Margin
failure can be established independently of those missing necessary inputs.
"""
from __future__ import annotations

from datetime import date
from statistics import median
from .risk import number, adverse_tick_fill, baseline_initial_risk

COSTS = {'BASE_2T': (2, 1), 'STRESS_4T': (4, 1),
         'DOUBLE_COMMISSION_4T': (4, 2)}


def adverse_fill(price, direction, tick, slip):
    return adverse_tick_fill(price, direction, tick, slip)


def liquidity(prior_volumes):
    if prior_volumes is None or len(prior_volumes) != 20:
        return dict(status='UNKNOWN', integer_cap=None, median_volume=None,
                    reason='TWENTY_PRIOR_COMPLETE_EXECUTION_CONTRACT_SESSIONS_REQUIRED')
    if any(v is None for v in prior_volumes):
        return dict(status='UNKNOWN', integer_cap=None, median_volume=None,
                    reason='MISSING_VOLUME_IS_NOT_ZERO')
    if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in prior_volumes):
        raise ValueError('INVALID_EXECUTION_CONTRACT_VOLUME')
    if any(v == 0 for v in prior_volumes):
        return dict(status='RULE_NOT_ALLOWED', integer_cap=0, median_volume=None,
                    reason='OBSERVED_NONPOSITIVE_PRIOR_VOLUME')
    med = number(median(prior_volumes)); cap = int(med * number('.001'))
    return dict(status='NECESSARY_CONDITION_PASS' if cap >= 1 else 'RULE_NOT_ALLOWED',
                integer_cap=cap, median_volume=str(med),
                reason='FROZEN_EXECUTION_CONTRACT_PARTICIPATION_CAP')


def one_contract(*, raw_price, multiplier, tick_size, margin_fraction, direction,
                 cost, atr=None, previous_execution_close=None, prior_volumes=None,
                 root_launch=None, decision_session=None):
    """Diagnose one first layer against original $11,500/$1,000/$115 caps.

    ``raw_price`` may be a dated observed price only. Its label/causal permission
    is an independent source-audit responsibility, never established here.
    No position is created and an analytical failure is not a rejection event.
    """
    p, m, tick, alpha = map(number, (raw_price, multiplier, tick_size, margin_fraction))
    if m <= 0 or tick <= 0 or alpha not in (number('.1'), number('.2')) or cost not in COSTS:
        raise ValueError('INVALID_CONTRACT_OR_FROZEN_SCENARIO')
    slip, cm = COSTS[cost]; base_commission = number(2); commission = base_commission * cm
    f = adverse_fill(p, direction, tick, slip); tv = m * tick
    margin = abs(f) * m * alpha; raw_margin = abs(p) * m * alpha
    margin_cap = int(number(1000) // margin) if margin > 0 else 0
    friction = abs(f-p)*m
    liq = liquidity(prior_volumes)
    if root_launch is not None and decision_session is not None:
        maximum_prior_dates=(date.fromisoformat(decision_session)-date.fromisoformat(root_launch)).days
        # Calendar-day upper bound, not a fabricated exchange calendar. At most
        # one completed daily session per labelled date; holidays only reduce it.
        if maximum_prior_dates < 20:
            liq=dict(status='RULE_NOT_ALLOWED',integer_cap=0,median_volume=None,
                     reason='INSUFFICIENT_SESSIONS_SINCE_ROOT_LAUNCH',
                     maximum_possible_prior_dates=max(0,maximum_prior_dates),
                     root_launch=root_launch,decision_session=decision_session,
                     observed_volume_was_not_changed_to_zero=True)
    blockers = []
    if margin_cap < 1: blockers.append('LAYER_MARGIN_LIMIT')
    if liq['status'] == 'RULE_NOT_ALLOWED': blockers.append(liq['reason'])
    risk = dict(status='UNKNOWN', stop=None, initial_size_loss_usd=None,
                initial_risk_integer_cap=None, scenario_stop_proxy_loss_usd=None,
                open_crosses_stop=None)
    if atr is not None and previous_execution_close is not None:
        a, previous = number(atr), number(previous_execution_close)
        if a <= 0: raise ValueError('POSITIVE_OBSERVED_ATR_REQUIRED')
        stop = previous - direction * 2 * a; distance = direction * (f-stop)
        crosses = direction * (p-stop) <= 0 or distance <= 0
        baseline = baseline_initial_risk(f, stop, direction, m, tick, base_commission)
        loss = baseline.loss
        q = 0 if crosses or loss <= 0 else int(number(115) // loss)
        exit_fill = adverse_fill(stop, -direction, tick, slip)
        scenario_loss = direction*(f-exit_fill)*m + 2*commission
        risk = dict(status='RULE_NOT_ALLOWED' if q < 1 else 'NECESSARY_CONDITION_PASS',
                    atr=str(a), previous_execution_close=str(previous), stop=str(stop),
                    directional_raw_gap=str(direction*(p-previous)),
                    initial_size_loss_usd=str(loss), initial_risk_integer_cap=q,
                    baseline_exit_fill=str(baseline.exit_fill),
                    baseline_exit_rounding_cost_usd=str(baseline.exit_rounding_cost),
                    scenario_stop_proxy_loss_usd=str(scenario_loss),
                    scenario_exit_fill=str(exit_fill), open_crosses_stop=crosses)
        if q < 1: blockers.append('OPEN_CROSSED_STOP' if crosses else 'INITIAL_RISK_OR_MINIMUM_CONTRACT')
    # Continuous necessary upper bound only; the tick grid can lower the actual
    # admissible ATR (e.g. the artificial MZC example: 10.1 bound, 10.0 on-grid).
    zero_gap_max_atr = (number(115)-friction-2*base_commission-2*tv)/(2*m)
    unknown = []
    if risk['status'] == 'UNKNOWN': unknown.append('ATR_OR_PREVIOUS_EXECUTION_CLOSE_UNKNOWN')
    if liq['status'] == 'UNKNOWN': unknown.append('PRIOR_EXECUTION_LIQUIDITY_UNKNOWN')
    return dict(kind='ONE_INTEGER_CONTRACT_NECESSARY_CONDITION_DIAGNOSTIC',
        actual_order_created=False, actual_rejection_event=False, quantity_evaluated=1,
        initial_equity_usd='11500', layer_margin_cap_usd='1000', initial_risk_budget_usd='115',
        direction=direction, cost=cost, multiplier=str(m), tick_size=str(tick), tick_value_usd=str(tv),
        raw_price=str(p), hypothetical_entry_fill=str(f), margin_fraction=str(alpha),
        margin_source='FROZEN_ASSUMPTION_NOT_HISTORICAL_EXCHANGE_OR_BROKER_MARGIN',
        raw_notional_usd=str(abs(p)*m), raw_margin_usd=str(raw_margin),
        entry_margin_usd=str(margin), maintenance_margin_usd=str(margin*number('.75')),
        layer_margin_integer_cap=margin_cap, entry_commission_usd=str(commission),
        entry_friction_usd=str(friction), roundtrip_commission_usd=str(2*commission),
        roundtrip_slippage_on_tick_usd=str(2*slip*tv),
        roundtrip_cost_on_tick_usd=str(2*commission+2*slip*tv),
        zero_gap_atr_ceiling_illustration=str(zero_gap_max_atr),
        zero_gap_atr_ceiling_basis='UNROUNDED_CONTINUOUS_NECESSARY_UPPER_BOUND_NOT_ADMISSION',
        zero_gap_is_observed=False, risk=risk, liquidity=liq,
        financial_status='RULE_NOT_ALLOWED' if blockers else 'INSUFFICIENT_EVIDENCE' if unknown else 'NECESSARY_CONDITIONS_PASS',
        blockers=blockers, unknown=unknown,
        admission_status='NOT_ASSESSED_REQUIRES_QUALIFIED_INPUTS_AND_SHARED_ACCOUNT_STATE',
        scenario_loss_limitations='Stop proxy excludes a worse opening gap and unknown intraday execution; $115 is a sizing budget, not guaranteed maximum loss.')
