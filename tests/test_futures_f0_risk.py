"""Artificial arithmetic cases only; no market evidence, orders or account replay."""
from datetime import date, datetime, timezone
from decimal import Decimal
import math

import pytest

from src.futures_f0.engine import FuturesEngine
from src.futures_f0.fitness import COSTS, one_contract
from src.futures_f0.model import ContractSpec, EngineConfig
from src.futures_f0.risk import adverse_tick_fill, baseline_initial_risk


def diagnostic(direction, *, price=454, multiplier=5, tick=.5, atr=10.05,
               cost='BASE_2T'):
    spec = ContractSpec('ARTIFICIAL', 'CORN', 'MOCK', multiplier, tick,
                        None, date(2027, 1, 1), None)
    slip, cm = COSTS[cost]
    engine = FuturesEngine({'ARTIFICIAL': spec}, EngineConfig(
        slippage_ticks=slip, commission_multiplier=cm))
    engine.volumes['ARTIFICIAL'].extend([1000] * 20)
    stop = price - direction * 2 * atr
    fit = one_contract(raw_price=price, multiplier=multiplier, tick_size=tick,
                       margin_fraction=.1, direction=direction, cost=cost,
                       atr=atr, previous_execution_close=price, prior_volumes=[1000]*20)
    qty, reason = engine._size(spec, price, stop, direction,
                               datetime(2025, 3, 4, tzinfo=timezone.utc), max_qty=1)
    assert engine.result.trades == [] and engine.positions == {} and engine.cash == 11500
    return engine, spec, stop, fit, qty, reason


@pytest.mark.parametrize('direction,entry,stop,exit_fill', [
    (1, 455, 433.9, 432.5), (-1, 453, 474.1, 475.5)])
def test_user_counterexample_has_known_116_50_loss_and_is_rejected(direction, entry, stop, exit_fill):
    engine, spec, actual_stop, fit, qty, reason = diagnostic(direction)
    assert actual_stop == pytest.approx(stop)
    assert engine._fill(454, direction, spec) == entry
    assert engine._fill(stop, -direction, spec) == exit_fill
    # Independent cash arithmetic, rather than comparing two shared formulas.
    assert direction * (entry - exit_fill) * 5 + 4 == 116.5
    assert Decimal(fit['risk']['initial_size_loss_usd']) == Decimal('116.5')
    assert Decimal(fit['risk']['baseline_exit_rounding_cost_usd']) == Decimal('2')
    assert fit['risk']['initial_risk_integer_cap'] == qty == 0
    assert reason == 'INITIAL_RISK_OR_MINIMUM_CONTRACT'


@pytest.mark.parametrize('direction', [1, -1])
@pytest.mark.parametrize('multiplier,tick,price,atr,expected,quantity', [
    (6, .5, 454, 8.25, '115', 1),       # Exactly the original $115 budget.
    (6, .5, 454, 8.250001, '118', 0),  # Slightly beyond grid boundary, one extra tick.
    (5, .5, 454, 10, '114', 1),
    (5, .5, 454, 10.05, '116.5', 0),
    (1, .25, 454, 50.03, '105.25', 1),
    (2500, .005, 3, .01, '104', 1),
    (2500, .005, 3, .02, '154', 0),
])
def test_independent_cash_boundaries_and_tick_values(direction, multiplier, tick, price,
                                                    atr, expected, quantity):
    engine, spec, stop, fit, qty, reason = diagnostic(
        direction, multiplier=multiplier, tick=tick, price=price, atr=atr)
    assert Decimal(fit['risk']['initial_size_loss_usd']) == Decimal(expected)
    actual_cash_loss = direction * (engine._fill(price, direction, spec)
                                    - engine._fill(stop, -direction, spec)) * multiplier + 4
    assert actual_cash_loss == pytest.approx(float(expected), abs=1e-9)
    assert qty == quantity
    assert min(1, fit['risk']['initial_risk_integer_cap']) == quantity


@pytest.mark.parametrize('direction', [1, -1])
@pytest.mark.parametrize('cost,budget,scenario', [
    ('BASE_2T', '116.5', '116.5'),
    ('STRESS_4T', '121.5', '126.5'),
    ('DOUBLE_COMMISSION_4T', '121.5', '130.5'),
])
def test_stress_keeps_baseline_exit_budget_and_entry_slip_counted_once(direction, cost, budget, scenario):
    _, _, _, fit, qty, _ = diagnostic(direction, cost=cost)
    assert Decimal(fit['risk']['initial_size_loss_usd']) == Decimal(budget)
    assert Decimal(fit['risk']['scenario_stop_proxy_loss_usd']) == Decimal(scenario)
    assert qty == 0


@pytest.mark.parametrize('tick', [.005, .1, .25, .5, .0001])
@pytest.mark.parametrize('direction', [1, -1])
def test_preserve_existing_float_grid_tolerance_and_actual_fill(tick, direction):
    for price in (tick*123 - tick*21, tick*100, tick*100 + tick*.01,
                  tick*100 - tick*.01, -tick*100):
        units = price/tick
        for slip in (2, 4):
            old_units = math.ceil(units-1e-10) if direction > 0 else math.floor(units+1e-10)
            expected = (old_units+direction*slip)*tick
            assert float(adverse_tick_fill(price, direction, tick, slip)) == pytest.approx(expected, abs=1e-12)


def test_continuous_atr_bound_is_not_admission():
    _, _, _, fit, qty, _ = diagnostic(1, atr=10.1)
    assert fit['zero_gap_atr_ceiling_illustration'] == '10.1'
    assert fit['zero_gap_atr_ceiling_basis'] == 'UNROUNDED_CONTINUOUS_NECESSARY_UPPER_BOUND_NOT_ADMISSION'
    assert qty == 0


def test_baseline_accepts_contract_base_commission_not_stress_multiplier():
    result = baseline_initial_risk(455, '433.9', 1, 5, '.5', '3.5')
    assert result.exit_fill == Decimal('432.5')
    assert result.loss == Decimal('119.5')
