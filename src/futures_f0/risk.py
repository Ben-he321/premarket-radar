"""Deterministic tick pricing and the frozen baseline first-layer risk budget.

The budget includes the known adverse exit rounding, not a promise about future
gap losses. The scenario entry is already filled; its slippage is counted once.
"""
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR


def number(value):
    if isinstance(value, bool):
        raise ValueError('BOOLEAN_IS_NOT_NUMERIC_EVIDENCE')
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError('NONFINITE_RISK_INPUT')
    return result


def adverse_tick_fill(price, direction, tick, slip):
    if direction not in (-1, 1) or isinstance(direction, bool):
        raise ValueError('INTEGER_LONG_OR_SHORT_REQUIRED')
    price, tick = number(price), number(tick)
    if tick <= 0 or slip not in (2, 4):
        raise ValueError('FROZEN_TICK_OR_SLIPPAGE_REQUIRED')
    # Preserve the engine's existing 1e-10 tick tolerance for binary-float
    # arithmetic at a grid boundary; this is not an economic price concession.
    units = price / tick - direction * Decimal('1e-10')
    rounded = units.to_integral_value(rounding=ROUND_CEILING if direction == 1 else ROUND_FLOOR)
    return (rounded + direction * slip) * tick


@dataclass(frozen=True)
class BaselineInitialRisk:
    exit_fill: Decimal
    loss: Decimal
    exit_rounding_cost: Decimal


def baseline_initial_risk(entry_fill, stop, direction, multiplier, tick_size,
                          commission_per_side):
    entry, stop, multiplier, tick, commission = map(
        number, (entry_fill, stop, multiplier, tick_size, commission_per_side))
    if multiplier <= 0 or commission < 0:
        raise ValueError('INVALID_BASELINE_RISK_SPECIFICATION')
    exit_fill = adverse_tick_fill(stop, -direction, tick, 2)
    loss = direction * (entry - exit_fill) * multiplier + 2 * commission
    rounding = direction * (stop - exit_fill) * multiplier - 2 * tick * multiplier
    return BaselineInitialRisk(exit_fill, loss, rounding)
