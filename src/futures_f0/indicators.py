"""Bounded, causal Wilder ATR and prior-session Donchian channels."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True)
class Features:
    atr: float | None
    entry_high: float | None
    entry_low: float | None
    exit_high: float | None
    exit_low: float | None


class RollingFeatures:
    def __init__(self, atr_period: int = 20, entry_period: int = 55,
                 exit_period: int = 20):
        self.atr_period = atr_period
        self.entry_period = entry_period
        self.exit_period = exit_period
        self.history = deque(maxlen=max(entry_period, exit_period))
        self.seed_tr = deque(maxlen=atr_period)
        self.previous_close = None
        self.atr = None

    def update(self, high: float, low: float, close: float) -> Features:
        if not all(isfinite(v) for v in (high, low, close)) or not low <= close <= high:
            raise ValueError("INVALID_SIGNAL_OHLC")
        history = tuple(self.history)
        entry = history[-self.entry_period:]
        exit_ = history[-self.exit_period:]
        tr = high - low if self.previous_close is None else max(
            high - low, abs(high - self.previous_close), abs(low - self.previous_close))
        if self.atr is None:
            self.seed_tr.append(tr)
            if len(self.seed_tr) == self.atr_period:
                self.atr = sum(self.seed_tr) / self.atr_period
        else:
            self.atr = (self.atr * (self.atr_period - 1) + tr) / self.atr_period
        values = Features(
            self.atr,
            max(x[0] for x in entry) if len(entry) == self.entry_period else None,
            min(x[1] for x in entry) if len(entry) == self.entry_period else None,
            max(x[0] for x in exit_) if len(exit_) == self.exit_period else None,
            min(x[1] for x in exit_) if len(exit_) == self.exit_period else None,
        )
        self.history.append((high, low, close))
        self.previous_close = close
        return values

    def shift(self, offset: float) -> None:
        """Causal additive conversion leaves past true ranges unchanged."""
        if not isfinite(offset):
            raise ValueError("INVALID_ROLL_OFFSET")
        self.history = deque(((h + offset, l + offset, c + offset)
                              for h, l, c in self.history), maxlen=self.history.maxlen)
        if self.previous_close is not None:
            self.previous_close += offset

