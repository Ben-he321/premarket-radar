"""Ben B1.1 deterministic, isolated event replay. No market or broker API access.

The input adapter supplies immutable events and an exchange schedule. This
module derives signals from bars, creates intents, consumes quote inventory,
books fills, carries positions across sessions, and persists all state needed
for an atomic restart. Historical L1 fills are models, never proof of queue
priority or a broker execution. Unknown receipt times remain UNKNOWN.

``calendar_events`` supplies the fixed decision clocks; each data event has
``event_id, kind, at, symbol, payload``. ``at`` is simulated availability, not
the download/receipt timestamp. Input evidence must explain that distinction.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from datetime import date
import copy
import hashlib
import json
import math
import os
import time
from pathlib import Path
from typing import Iterable

import pandas as pd

from . import rules
from .events import (EarningsRevision, completed_sessions_since_exit, earnings_gate,
                     first_executable_rth, session_clock)
from .ledger import FinanceConfig, Ledger, MODES


VERSION = "BEN_B1_1_EVENT_REPLAY_V1"
PRIORITY = {"SPLIT": 0, "CASH_IN_LIEU": 1, "DIVIDEND_EX": 1, "DIVIDEND_PAY": 2,
            "EARNINGS_REVISION": 3, "SESSION_START": 4, "DAILY_BAR": 5,
            "MINUTE_BAR": 6, "QUOTE": 7, "TRADE": 8, "DATA_GAP": 9,
            "ACTIVE_DECISION": 20, "SESSION_CLOSE": 21,
            "ENTRY_DECISION": 22, "ENTRY_EXPIRY": 23, "CALENDAR_DAY": 24}


def _clean(value):
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, (pd.Timestamp, date)):
        return value.isoformat()
    if hasattr(value, "item"):
        return _clean(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _json(value) -> str:
    return json.dumps(_clean(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _positive(x) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x) and x > 0


def _known_time(x):
    return None if x is None or str(x) in ("UNKNOWN", "None", "NaT") else rules.aware(x)


def calendar_events(schedule: pd.DataFrame, start: object, end: object) -> list[dict]:
    """Calendar-derived clocks, including non-session accrual/settlement days."""
    result = []
    first, last = pd.Timestamp(start).date(), pd.Timestamp(end).date()
    for row in schedule.itertuples():
        day = rules.aware(row.market_open).tz_convert("America/New_York").date()
        if not first <= day <= last:
            continue
        c = session_clock(schedule, day)
        for kind, key in (("SESSION_START", "market_open"), ("ACTIVE_DECISION", "active_exit_decision"),
                          ("SESSION_CLOSE", "market_close"), ("ENTRY_DECISION", "entry_decision"),
                          ("ENTRY_EXPIRY", "entry_expiry")):
            result.append({"event_id": f"calendar:{day}:{kind}", "kind": kind,
                           "at": c[key], "payload": {"session_date": str(day)}})
    for d in pd.date_range(first, last):
        at = d.tz_localize("America/New_York")
        result.append({"event_id": f"calendar:{d.date()}:accrual", "kind": "CALENDAR_DAY", "at": at.isoformat(), "payload": {}})
    return result


@dataclass(frozen=True)
class ReplayConfig:
    run_id: str = "BEN_B1_1_P50_BASE"
    mode: str = "P50_PRIMARY"
    rule: str = "B1_FULL"
    initial_equity: float = 5500.0
    commission: float = 1.0
    friction_bps: float = 10.0
    annual_interest_rate: float = 0.08
    maintenance_margin: float = 0.30
    initial_margin: float = 0.50
    earnings_tier: str = "A"
    data_basis: str = "INDEPENDENTLY_VERIFIED"
    start: str | None = None
    end: str | None = None
    checkpoint_every: int = 250
    synthetic_test: bool = False
    entry_dates: tuple[str, ...] | None = None
    research_scope: str = "CONTINUOUS_ACCOUNT"

    def __post_init__(self):
        if self.mode not in MODES or self.rule not in ("B1_FULL", "B1_NO_DEVIATION", "B1_NO_TWO_FAILURE"):
            raise ValueError("UNFROZEN_MODE_OR_RULE")
        if self.earnings_tier not in ("A", "B"):
            raise ValueError("EARNINGS_FILTER_CANNOT_BE_DISABLED")
        if self.data_basis not in ("INDEPENDENTLY_VERIFIED", "VENDOR_STANDARD_FIELDS"):
            raise ValueError("UNDECLARED_DATA_BASIS")


class ReplayEngine:
    def __init__(self, schedule: pd.DataFrame, config: ReplayConfig | None = None,
                 universe: dict | None = None, checkpoint_path: str | Path | None = None):
        self.config = config or ReplayConfig()
        self.schedule = schedule.copy()
        self.sessions = sorted([(rules.aware(r.market_open).tz_convert("America/New_York").date().isoformat(),
                                 rules.aware(r.market_open).isoformat(), rules.aware(r.market_close).isoformat())
                                for r in self.schedule.itertuples()])
        if not self.sessions or len({s[0] for s in self.sessions}) != len(self.sessions):
            raise ValueError("VALID_EXCHANGE_SCHEDULE_REQUIRED")
        self.universe = copy.deepcopy(universe or {})
        self.checkpoint_path = Path(checkpoint_path) if checkpoint_path else None
        self.ledger = Ledger(FinanceConfig(mode=self.config.mode, initial_equity=self.config.initial_equity,
            commission=self.config.commission, friction_bps=self.config.friction_bps,
            annual_interest_rate=self.config.annual_interest_rate, initial_margin=self.config.initial_margin,
            maintenance_margin=self.config.maintenance_margin))
        self.state = {"at": None, "handled": {}, "bars": {}, "minutes": {}, "quotes": {},
            "quote_inventory": {}, "earnings": {}, "earnings_coverage": {}, "stops": {},
            "trailing": {}, "pressure": {}, "pending": {}, "last_exit": {}, "repairs": {},
            "marks": {}, "mark_times": {}, "trace": [], "decisions": [], "equity": [],
            "errors": [], "gaps": [], "quote_rejections": [], "sessions_processed": [],
            "input_versions": {}, "corporate_unit_uncertainty": {}, "active_gaps": {}, "close_finalized": {}, "unit_versions": {}, "events_processed": 0}
        self._feature_cache = {}
        self._session_bounds = {d: (rules.aware(o), rules.aware(c)) for d, o, c in self.sessions}
        self._clock_cache = {}
        self._gate_cache = {}

    @property
    def trace(self):
        return self.state["trace"]

    @property
    def orders(self):
        return self.state["pending"]

    def _record(self, kind, security=None, **details):
        event = {"sequence": len(self.trace), "at": self.state["at"], "kind": kind,
                 "symbol": security, **_clean(details)}
        self.trace.append(event)
        return event

    def _day(self, at=None):
        return rules.aware(at or self.state["at"]).tz_convert("America/New_York").date().isoformat()

    def _clock(self, day=None):
        selected = day or self._day()
        if selected not in self._clock_cache:
            self._clock_cache[selected] = session_clock(self.schedule, selected)
        return self._clock_cache[selected]

    def _is_rth(self):
        now = rules.aware(self.state["at"])
        bounds = self._session_bounds.get(self._day())
        return bool(bounds and bounds[0] <= now < bounds[1])

    def _next_open(self):
        now = rules.aware(self.state["at"])
        return next((o for _, o, _ in self.sessions if rules.aware(o) > now), None)

    def _settlement(self):
        day = self._day()
        future = [d for d, _, _ in self.sessions if d > day]
        if not future:
            raise ValueError("SETTLEMENT_CALENDAR_INSUFFICIENT")
        # Frozen 2026 engineering window is US T+1. No historical T+2 inference.
        if day < "2024-05-28":
            if len(future) < 2:
                raise ValueError("SETTLEMENT_CALENDAR_INSUFFICIENT")
            return future[1]
        return future[0]

    def _marks(self, fresh_today=False):
        if not fresh_today:
            return dict(self.state["marks"])
        return {s: p for s, p in self.state["marks"].items()
                if self._day(self.state["mark_times"][s]) == self._day()}

    def _gate(self, symbol):
        if self.config.earnings_tier == "B" and not self.state["earnings_coverage"].get(symbol, {}).get("complete", False):
            return {"new_entry_allowed": False, "strict_eligible": False, "tier": "C_UNKNOWN",
                    "status": "EARNINGS_UNKNOWN", "reason": "CONTINUOUS_ACTUAL_RELEASE_COVERAGE_NOT_VERIFIED",
                    "required_exit_time": self.state["at"], "evidence": []}
        coverage = self.state["earnings_coverage"].get(symbol, {})
        if self.config.earnings_tier == "B" and ((coverage.get("start") and self._day() < coverage["start"])
                or (coverage.get("end") and self._day() > coverage["end"])):
            return {"new_entry_allowed": False, "strict_eligible": False, "tier": "C_UNKNOWN",
                    "status": "EARNINGS_UNKNOWN", "reason": "OUTSIDE_VERIFIED_CONTINUOUS_ACTUAL_RELEASE_WINDOW",
                    "required_exit_time": self.state["at"], "evidence": []}
        revisions = self.state["earnings"].get(symbol, [])
        now = rules.aware(self.state["at"])
        boundaries = []
        for revision in revisions:
            for key in ("known_at", "release_known_at", "actual_release_at"):
                value = _known_time(revision.get(key))
                if value is not None:
                    boundaries.append(value <= now)
        bounds = self._session_bounds.get(self._day())
        if bounds:
            boundaries.extend((now >= bounds[1]-pd.Timedelta(minutes=10), now >= bounds[1]+pd.Timedelta(minutes=5)))
        cache_key = (symbol, self._day(), len(revisions), tuple(boundaries))
        if cache_key not in self._gate_cache:
            events = [EarningsRevision(**r) for r in revisions]
            self._gate_cache[cache_key] = earnings_gate(self.state["at"], self.schedule, events, self.config.earnings_tier == "B")
        result = copy.deepcopy(self._gate_cache[cache_key])
        result["decision_time"] = self.state["at"]
        # UNKNOWN exits are known now; the cache never retains an earlier
        # invented detection time or an already passed RTH exit timestamp.
        if result["status"] == "EARNINGS_UNKNOWN":
            first = first_executable_rth(self.schedule, now)
            result["required_exit_time"] = first.isoformat() if first is not None else None
        return result

    def _features(self, symbol):
        values = self.state["bars"].get(symbol, {})
        if not values:
            return pd.DataFrame()
        revision = self.state["input_versions"].get(symbol, 0)
        if symbol in self._feature_cache and self._feature_cache[symbol][0] == revision:
            return self._feature_cache[symbol][1]
        frame = pd.DataFrame([values[d] for d in sorted(values)])
        frame.index = pd.to_datetime(frame.session_date)
        frame = rules.daily_features(frame, regular_session_verified=True)
        self._feature_cache[symbol] = revision, frame
        return frame

    def _bar_eligibility(self, frame):
        if frame.empty:
            return "DAILY_INPUT_MISSING"
        recent = frame.tail(100)
        if not recent.get("regular_session_verified", pd.Series(False, index=recent.index)).fillna(False).all():
            return "RTH_BASIS_UNVERIFIED"
        if not recent.get("action_units_verified", pd.Series(False, index=recent.index)).fillna(False).all():
            return "SHARE_UNIT_BASIS_UNVERIFIED"
        return None

    def _quote_key(self, symbol, p):
        # An adapter's polling event id is not a fresh displayed quantity. This
        # fingerprint intentionally ignores poll/download time and event_id.
        return _hash({"symbol": symbol, "timestamp": p.get("timestamp"),
                      **{k: p.get(k) for k in ("bid", "ask", "bid_size", "ask_size", "bid_exchange", "ask_exchange", "conditions", "size_unit")}})

    def _quote_valid(self, symbol, side="BUY"):
        p = self.state["quotes"].get(symbol)
        if not p:
            return None, "QUOTE_MISSING"
        now = rules.aware(self.state["at"])
        if not 0 <= (now-rules.aware(p["timestamp"])).total_seconds() <= 5:
            return None, "STALE_OR_FUTURE_QUOTE"
        available = _known_time(p.get("available_at"))
        if available is not None and available > now:
            return None, "QUOTE_NOT_YET_AVAILABLE"
        if not _positive(p.get("bid")) or not _positive(p.get("ask")) or p["ask"] < p["bid"]:
            return None, "INVALID_OR_CROSSED_QUOTE"
        if p.get("size_unit") != "shares":
            return None, "QUOTE_SIZE_UNIT_UNKNOWN"
        if side == "BUY" and (p["ask"]-p["bid"])/((p["ask"]+p["bid"])/2) > .003+1e-12:
            return None, "SPREAD_TOO_WIDE"
        inventory = self.state["quote_inventory"][p["inventory_id"]]
        remaining = inventory["ask_remaining" if side == "BUY" else "bid_remaining"]
        if remaining < 1:
            return None, "DISPLAYED_QUANTITY_EXHAUSTED_OR_MISSING"
        return p, None

    def _data_bar(self, symbol, payload):
        p = copy.deepcopy(payload)
        day = str(p.get("session_date", self._day()))[:10]
        c = self._clock(day)
        if rules.aware(self.state["at"]) < rules.aware(c["market_close"]):
            raise ValueError("FINAL_DAILY_BAR_BEFORE_SESSION_CLOSE")
        known = _known_time(p.get("available_at"))
        if known is not None and known > rules.aware(self.state["at"]):
            raise ValueError("DAILY_BAR_NOT_YET_AVAILABLE")
        if any(not _positive(p.get(k)) for k in ("open", "high", "low", "close")) or not p["low"] <= min(p["open"], p["close"]) <= max(p["open"], p["close"]) <= p["high"]:
            raise ValueError("INVALID_DAILY_OHLC")
        p.update(session_date=day, session_close=c["market_close"], observed_at=self.state["at"],
                 source_received_at=p.get("source_received_at", "UNKNOWN"))
        self.state["bars"].setdefault(symbol, {})[day] = p
        self.state["input_versions"][symbol] = self.state["input_versions"].get(symbol, 0)+1
        if symbol not in self.state["mark_times"] or rules.aware(c["market_close"]) >= rules.aware(self.state["mark_times"][symbol]):
            self.state["marks"][symbol] = p["close"]
            self.state["mark_times"][symbol] = c["market_close"]
        self._feature_cache.pop(symbol, None)
        if day == self._day() and day in self.state["sessions_processed"]:
            self._finalize_symbol_close(symbol)
            if symbol in self.ledger.positions:
                self._snapshot("DELAYED_FINAL_SESSION_BAR")

    def _new_exit(self, symbol, reason, quantity=None, leg_index=None, active_at=None):
        pos = self.ledger.positions.get(symbol)
        if not pos:
            return
        cid = pos["campaign_id"]
        quantity = int(quantity if quantity is not None else pos["quantity"])
        if quantity <= 0:
            self._record("EXIT_PENDING_FRACTIONAL_ENTITLEMENT", symbol, reason=reason)
            return
        oid = f"{cid}:exit:{reason}:{leg_index if leg_index is not None else 'all'}"
        if self.state["unit_versions"].get(symbol, 0):
            oid += f":unit{self.state['unit_versions'][symbol]}"
        if oid in self.orders:
            return
        at = active_at or self.state["at"]
        self.orders[oid] = {"order_id": oid, "symbol": symbol, "campaign_id": cid, "side": "SELL",
            "quantity": quantity, "filled_quantity": 0, "leg_index": leg_index, "reason": reason,
            "created_at": self.state["at"], "active_at": at, "status": "OPEN", "last_reason": "AWAITING_RTH_QUOTE"}
        self._record("EXIT_INTENT", symbol, **self.orders[oid])
        # Once a reduction exists, its campaign may never refill unfilled buys.
        for pending in self.orders.values():
            if pending["symbol"] == symbol and pending["side"] == "BUY" and pending["status"] == "OPEN":
                self._cancel(pending, "PROTECTION_EXIT_CANCELS_ENTRY_REMAINDER")

    def _cancel(self, order, reason):
        if order["status"] != "OPEN":
            return
        order.update(status="CANCELLED", cancelled_at=self.state["at"], last_reason=reason)
        if order["order_id"] in self.ledger.orders:
            self.ledger.cancel_order(order["order_id"], self.state["at"])
        self._record("ORDER_CANCELLED", order["symbol"], order_id=order["order_id"], reason=reason,
                     unfilled_quantity=order["quantity"]-order["filled_quantity"])

    def _earnings_protection(self, symbol):
        gate = self._gate(symbol)
        if symbol in self.ledger.positions and not gate["new_entry_allowed"]:
            now = rules.aware(self.state["at"])
            required = _known_time(gate.get("required_exit_time")) or now
            first = first_executable_rth(self.schedule, max(required, now))
            self._new_exit(symbol, "EARNINGS_FORCED_EXIT", active_at=first.isoformat() if first is not None else "9999-01-01T00:00:00+00:00")
            self._record("EARNINGS_HOLDING_ACTION", symbol, gate=gate,
                         prior_exit_not_backfilled=required < now, actual_intent_time=self.state["at"])
        return gate

    def _session_start(self):
        opened = rules.aware(self._clock()["market_open"])
        # Minute source files and input hashes retain history. Only this session's
        # observations are required for today's causal active-exit calculation.
        for symbol in list(self.state["minutes"]):
            self.state["minutes"][symbol] = {k: v for k, v in self.state["minutes"][symbol].items() if rules.aware(k) > opened}
        for symbol, updates in list(self.state["trailing"].items()):
            if rules.aware(updates["active_at"]) <= rules.aware(self.state["at"]):
                if symbol in self.state["stops"]:
                    for old, new in zip(self.state["stops"][symbol], updates["legs"]):
                        old["stop"] = max(old["stop"], new["stop"])
                        old["support"] = new["support"]
                    self._record("TRAILING_STOPS_ACTIVATED", symbol, legs=self.state["stops"][symbol])
                del self.state["trailing"][symbol]
        for symbol in list(self.ledger.positions):
            self._earnings_protection(symbol)
            if symbol not in self.state["quotes"]:
                self._record("RTH_PROTECTION_AWAITING_QUOTE", symbol, reason="OPEN_QUOTE_MISSING")
        self._margin_actions()

    def _minute(self, symbol, payload):
        p = copy.deepcopy(payload)
        end = rules.aware(p["bar_end"])
        if end > rules.aware(self.state["at"]):
            raise ValueError("MINUTE_NOT_ENDED")
        available = _known_time(p.get("available_at"))
        if available is not None and available > rules.aware(self.state["at"]):
            raise ValueError("MINUTE_NOT_YET_AVAILABLE")
        if any(not _positive(p.get(k)) for k in ("high", "low", "close")) or not p["low"] <= p["close"] <= p["high"]:
            raise ValueError("INVALID_MINUTE")
        p.update(bar_end=end.isoformat(), source_received_at=p.get("source_received_at", "UNKNOWN"))
        self.state["minutes"].setdefault(symbol, {})[end.isoformat()] = p
        if symbol not in self.state["mark_times"] or end >= rules.aware(self.state["mark_times"][symbol]):
            self.state["marks"][symbol] = p["close"]
            self.state["mark_times"][symbol] = end.isoformat()
        if self._is_rth() and self._day(end) == self._day() and symbol in self.ledger.positions:
            # A completed bar may establish a crossed stop, but cannot establish
            # the quote or intraminute fill price. Retain a pending market exit.
            for i, leg in enumerate(self.state["stops"].get(symbol, [])):
                if self.ledger.positions[symbol]["legs"][i]["quantity"] > 0 and p["low"] <= leg["stop"]:
                    self._new_exit(symbol, "RTH_STOP", self.ledger.positions[symbol]["legs"][i]["quantity"], i)
            self._margin_actions()
            self._execute_exits(symbol)

    def _quote(self, symbol, payload):
        p = copy.deepcopy(payload)
        p["timestamp"] = rules.aware(p.get("timestamp", self.state["at"])).isoformat()
        if rules.aware(p["timestamp"]) > rules.aware(self.state["at"]):
            raise ValueError("QUOTE_EVENT_FROM_FUTURE")
        p.setdefault("source_received_at", "UNKNOWN")
        key = self._quote_key(symbol, p)
        p["inventory_id"] = key
        inventory = self.state["quote_inventory"]
        duplicate = key in inventory
        if not duplicate:
            def shares(field):
                v = p.get(field)
                return max(0, math.floor(v)) if _positive(v) and p.get("size_unit") == "shares" else 0
            inventory[key] = {"symbol": symbol, "timestamp": p["timestamp"], "ask_remaining": shares("ask_size"),
                              "bid_remaining": shares("bid_size"), "ask_consumed": 0, "bid_consumed": 0}
        prior_quote = self.state["quotes"].get(symbol)
        if prior_quote and rules.aware(p["timestamp"]) < rules.aware(prior_quote["timestamp"]):
            self._record("OUT_OF_ORDER_OLD_QUOTE_IGNORED", symbol, timestamp=p["timestamp"])
            return
        availability = _known_time(p.get("available_at"))
        causal = (0 <= (rules.aware(self.state["at"])-rules.aware(p["timestamp"])).total_seconds() <= 5
                  and (availability is None or availability <= rules.aware(self.state["at"])))
        self.state["quotes"][symbol] = p
        if not causal or not _positive(p.get("bid")) or not _positive(p.get("ask")) or p["ask"] < p["bid"]:
            self._record("QUOTE_UNUSABLE_FOR_CURRENT_MARK_OR_TRIGGER", symbol, timestamp=p["timestamp"],
                         reason="STALE_UNAVAILABLE_OR_INVALID_QUOTE")
            return
        if symbol not in self.state["mark_times"] or rules.aware(p["timestamp"]) >= rules.aware(self.state["mark_times"][symbol]):
            self.state["marks"][symbol] = p["bid"]
            self.state["mark_times"][symbol] = p["timestamp"]
        if duplicate:
            self._record("DUPLICATE_QUOTE_INVENTORY_REUSED", symbol, inventory_id=key)
        if self._is_rth():
            if symbol in self.ledger.positions:
                legs = [rules.StopLeg(int(self.ledger.positions[symbol]["legs"][i]["quantity"]), s["support"], s["stop"])
                        for i, s in enumerate(self.state["stops"].get(symbol, []))]
                for trigger in rules.stop_trigger_orders(legs, p.get("bid"), True, self.config.commission, self.ledger.friction):
                    if trigger["quantity"]:
                        self._new_exit(symbol, "RTH_STOP", trigger["quantity"], trigger["leg_index"])
                self._earnings_protection(symbol)
            self._margin_actions()
            self._execute_exits(symbol)
        for order in list(self.orders.values()):
            if order["symbol"] == symbol and order["side"] == "BUY" and order["status"] == "OPEN":
                self._execute_buy(order)

    def _margin_actions(self):
        if not self.ledger.margin_enabled or not self.ledger.positions:
            return
        check = self.ledger.margin_check(self._marks(fresh_today=True))
        if check["margin_status"] in ("MARGIN_CALL", "INSOLVENT_STOP_NEW_ENTRIES"):
            for symbol, pos in list(self.ledger.positions.items()):
                q, reason = self._quote_valid(symbol, "SELL")
                if q is None or not self._is_rth():
                    self._record("MARGIN_CALL_PENDING_EXECUTABLE_PRICE", symbol, check=check, reason=reason or "OUTSIDE_RTH")
                    continue
                mark, bid = self.state["marks"][symbol], q["bid"]
                gain = check["maintenance_margin"]*mark-(mark-bid*(1-self.ledger.friction))
                quantity = int(pos["quantity"]) if check["net_equity"] <= 0 or gain <= 0 else min(int(pos["quantity"]), max(1, math.ceil((check["margin_shortfall"]+self.config.commission)/gain)))
                # A new breached episode after completion receives another id;
                # repeated same pending reduction retains the original order.
                existing = any(o["symbol"] == symbol and o["reason"].startswith("MARGIN_FORCED_REDUCTION") and o["status"] == "OPEN" for o in self.orders.values() if o["side"] == "SELL")
                if not existing:
                    count = sum(o["symbol"] == symbol and o.get("reason", "").startswith("MARGIN_FORCED_REDUCTION") for o in self.orders.values())
                    self._new_exit(symbol, f"MARGIN_FORCED_REDUCTION_{count}", quantity)

    def _execute_exits(self, symbol):
        if not self._is_rth() or symbol not in self.ledger.positions:
            return
        now = rules.aware(self.state["at"])
        pending = [o for o in self.orders.values() if o["symbol"] == symbol and o["side"] == "SELL" and o["status"] == "OPEN" and rules.aware(o["active_at"]) <= now]
        def priority(o):
            r = o["reason"]
            return (0 if r == "RTH_STOP" else 1 if r.startswith("MARGIN") else 2 if r == "EARNINGS_FORCED_EXIT" else 3,
                    o.get("leg_index") if o.get("leg_index") is not None else 99, o["order_id"])
        for order in sorted(pending, key=priority):
            pos = self.ledger.positions.get(symbol)
            if not pos:
                self._cancel(order, "POSITION_ALREADY_CLOSED")
                continue
            q, reason = self._quote_valid(symbol, "SELL")
            if q is None:
                order["last_reason"] = reason
                self._record("EXIT_PENDING_NO_EXECUTABLE_QUOTE", symbol, order_id=order["order_id"], reason=reason)
                continue
            index = order["leg_index"]
            capacity = pos["legs"][index]["quantity"] if index is not None else pos["quantity"]
            inventory = self.state["quote_inventory"][q["inventory_id"]]
            n = min(int(capacity), order["quantity"]-order["filled_quantity"], inventory["bid_remaining"])
            if n <= 0:
                self._cancel(order, "TARGET_ALREADY_REDUCED_BY_OTHER_EXIT")
                continue
            oid = order["order_id"]
            fill_id = f"{oid}:{q['inventory_id']}:{order['filled_quantity']}"
            fill = self.ledger.sell(oid, symbol, int(n), q["bid"], self.state["at"], self._settlement(),
                leg_index=index, reason=order["reason"], fill_id=fill_id, displayed_size=inventory["bid_remaining"],
                order_quantity=min(order["quantity"], int(pos["quantity"])))
            inventory["bid_remaining"] -= n
            inventory["bid_consumed"] += n
            order["filled_quantity"] += n
            order["status"] = "FILLED" if order["filled_quantity"] >= order["quantity"] else "OPEN"
            order["last_reason"] = "HISTORICAL_L1_MODEL_FILL"
            self._record("SELL_FILL", symbol, **fill, quote_inventory_id=q["inventory_id"],
                         source_received_at=q.get("source_received_at", "UNKNOWN"),
                         historical_network_received_at=q.get("historical_network_received_at", "UNKNOWN"),
                         availability_basis=q.get("availability_basis", "UNKNOWN"))
            if symbol not in self.ledger.positions:
                self.state["last_exit"][symbol] = self.state["at"]
                self.state["stops"].pop(symbol, None)
                self.state["trailing"].pop(symbol, None)
                self.state["pressure"].pop(symbol, None)
                for other in self.orders.values():
                    if other["symbol"] == symbol and other["status"] == "OPEN":
                        self._cancel(other, "CAMPAIGN_CLOSED")

    def _active(self):
        for symbol in list(self.ledger.positions):
            self._earnings_protection(symbol)
            frame = self._features(symbol)
            prior = frame[frame.index.date < pd.Timestamp(self._day()).date()]
            minutes = list(self.state["minutes"].get(symbol, {}).values())
            if prior.empty or not minutes:
                self._record("ACTIVE_SIGNAL_UNKNOWN", symbol, reason="PRIOR_DAILY_OR_COMPLETED_MINUTE_MISSING")
                self._execute_exits(symbol)
                continue
            minute_frame = rules.completed_minutes_asof(pd.DataFrame(minutes), self.state["at"])
            opened = rules.aware(self._clock()["market_open"])
            minute_frame = minute_frame[minute_frame.bar_end.map(rules.aware) > opened]
            if minute_frame.empty:
                self._record("ACTIVE_SIGNAL_UNKNOWN", symbol, reason="CURRENT_SESSION_COMPLETED_MINUTE_MISSING")
                self._execute_exits(symbol)
                continue
            minute_frame = minute_frame.sort_values("bar_end")
            last, d = minute_frame.iloc[-1], prior.iloc[-1]
            p = float(last.close)
            ema5, ema10 = rules.provisional_ema(d.ema5, p, 5), rules.provisional_ema(d.ema10, p, 10)
            pos = self.ledger.positions[symbol]
            campaign = self.ledger.campaigns[pos["campaign_id"]]
            result = rules.deviation_reduction(p, ema5, ema10, d.atr14,
                int(campaign["entry_quantity"]), int(pos["quantity"]), campaign["entry_outlay"]/campaign["entry_quantity"],
                campaign["deviation_reduction_done"])
            self._record("ACTIVE_CAUSAL_EVALUATION", symbol, last_bar_end=last.bar_end, price=p,
                         provisional_ema5=ema5, provisional_ema10=ema10, prior_atr14=d.atr14,
                         daily_input_cutoff=str(prior.index[-1].date()), deviation=result)
            if self.config.rule != "B1_NO_DEVIATION" and result["triggered"]:
                self.ledger.mark_deviation_done(symbol)
                if result["quantity"]:
                    self._new_exit(symbol, "DEVIATION_REDUCTION", result["quantity"])
            self._pressure(symbol, p, float(minute_frame.high.max()), float(d.atr14), "ACTIVE")
            self._execute_exits(symbol)

    def _pressure(self, symbol, price, high, atr, stage):
        if self.config.rule == "B1_NO_TWO_FAILURE" or symbol not in self.state["pressure"]:
            return
        p = rules.PressureState(**self.state["pressure"][symbol])
        clock = self._clock()
        index = next(i for i, s in enumerate(self.sessions) if s[0] == self._day())
        result = rules.pressure_failure(p, p.anchor_id, index, price, high, atr,
            self.state["at"], clock["active_exit_decision"], clock["market_close"])
        self.state["pressure"][symbol] = asdict(result["state"])
        self._record("PRESSURE_EVALUATION", symbol, stage=stage, action=result["action"], reason=result["reason"], state=asdict(result["state"]))
        if result["action"] == "EXIT_NOW":
            self._new_exit(symbol, "TWO_PRESSURE_FAILURES")
        elif result["action"] == "EXIT_NEXT_RTH":
            self._new_exit(symbol, "TWO_PRESSURE_FAILURES", active_at=self._next_open() or "9999-01-01T00:00:00+00:00")

    def _finalize_symbol_close(self, symbol):
        day = self._day()
        frame = self._features(symbol)
        if frame.empty or str(frame.index[-1].date()) != day:
            if symbol in self.ledger.positions:
                self._record("HELD_DAILY_MARK_MISSING", symbol, session_date=day)
            return
        key = f"{symbol}:{day}"
        version = self.state["input_versions"].get(symbol)
        if self.state["close_finalized"].get(key) == version:
            return
        d = frame.iloc[-1]
        if symbol in self.ledger.positions:
            self._pressure(symbol, float(d.close), float(d.high), float(d.atr14_prev), "CLOSE_AVAILABLE")
        record = {"session_close": d.session_close, "available_at": d.get("available_at", "UNKNOWN"),
            "observed_in_replay_at": self.state["at"], "low": d.low, "high": d.high,
            "close": d.close, "ema5": d.ema5, "ema10": d.ema10, "atr14_prev": d.atr14_prev}
        repairs = self.state["repairs"].setdefault(symbol, [])
        if repairs and repairs[-1]["session_close"] == record["session_close"]:
            repairs[-1] = record
        else:
            repairs.append(record)
        self.state["close_finalized"][key] = version
        self._record("SESSION_CLOSE_DATA_FINALIZED", symbol, session_date=day, input_version=version,
                     data_cutoff=d.session_close, replay_available_at=self.state["at"])

    def _session_close(self):
        day = self._day()
        self.state["sessions_processed"].append(day)
        for symbol in sorted(self.universe):
            self._finalize_symbol_close(symbol)
        self._snapshot("SESSION_CLOSE")

    def _trailing_update(self, symbol, frame):
        if symbol not in self.ledger.positions or frame.empty:
            return
        d = frame.iloc[-1]
        if str(frame.index[-1].date()) != self._day() or self._bar_eligibility(frame):
            self._record("TRAILING_UPDATE_UNKNOWN", symbol, reason="CONFIRMED_SAME_UNIT_SUPPORT_UNAVAILABLE")
            return
        emas = {n: float(d[f"ema{n}"]) for n in rules.EMA_PERIODS}
        plan = rules.initial_stop_plan(float(d.close), float(d.close)*(1+self.ledger.friction), emas,
                                      max(1, int(self.ledger.positions[symbol]["quantity"])))
        if plan.status != "VALID":
            self._record("TRAILING_UNCHANGED", symbol, reason=plan.reason)
            return
        old = self.state["stops"].get(symbol, [])
        supports = sorted([l.support for l in plan.legs], reverse=True)
        new = [{**leg, "support": supports[min(i, len(supports)-1)],
                "stop": rules.tighten_stop(leg["stop"], supports[min(i, len(supports)-1)])}
               for i, leg in enumerate(old)]
        opened = self._next_open()
        if opened:
            self.state["trailing"][symbol] = {"active_at": opened, "legs": new, "decided_at": self.state["at"]}
            self._record("TRAILING_STOPS_SCHEDULED", symbol, **self.state["trailing"][symbol])

    def _post_exit_repairs(self, symbol):
        exited = rules.aware(self.state["last_exit"][symbol])
        result = []
        for record in self.state["repairs"].get(symbol, []):
            day = rules.aware(record["session_close"]).tz_convert("America/New_York").date().isoformat()
            bounds = self._session_bounds.get(day)
            if bounds and bounds[0] >= exited:
                result.append(record)
        return result

    def _entry_decision(self):
        day = self._day()
        if (self.config.start and day < self.config.start) or (self.config.end and day > self.config.end):
            return
        candidates = []
        for symbol in sorted(self.universe):
            u = self.universe[symbol]
            d = {"decision_time": self.state["at"], "symbol": symbol, "security_id": u.get("security_id", symbol),
                 "data_basis": self.config.data_basis, "earnings_tier": self.config.earnings_tier,
                 "signal": False, "status": "REJECTED", "reason": None,
                 "input_version": self.state["input_versions"].get(symbol), "source_received_at": "UNKNOWN"}
            frame = self._features(symbol)
            if symbol in self.ledger.positions:
                self._trailing_update(symbol, frame)
                d.update(reason="EXISTING_CAMPAIGN_NO_ADDING", status="HOLDING")
            elif self.config.entry_dates is not None and day not in self.config.entry_dates:
                d.update(reason="OUTSIDE_FIXED_ENGINEERING_ENTRY_SAMPLE", status="NOT_SAMPLED")
            elif u.get("scope") != "KEEP":
                d["reason"] = "SCOPE_EXCLUDED" if str(u.get("scope", "")).startswith("EXCLUDE") else "IDENTITY_OR_SCOPE_UNKNOWN"
            elif symbol in self.state["active_gaps"] and self.state["active_gaps"][symbol]["session_date"] == day:
                d.update(reason=self.state["active_gaps"][symbol]["reason"], status="DATA_UNKNOWN")
            elif frame.empty or str(frame.index[-1].date()) != day:
                d.update(reason="CURRENT_SESSION_DAILY_INPUT_MISSING", status="DATA_UNKNOWN")
            else:
                latest = frame.iloc[-1]
                d.update(data_cutoff=latest.session_close, source_received_at=latest.get("source_received_at", "UNKNOWN"),
                         history_sessions=int(latest.valid_sessions))
                if len(frame) < 2:
                    d.update(reason="INDICATOR_WARMUP_INSUFFICIENT", status="DATA_UNKNOWN")
                else:
                    prev = frame.iloc[-2]
                    emas = {n: float(latest[f"ema{n}"]) for n in rules.EMA_PERIODS}
                    previous_emas = {n: float(prev[f"ema{n}"]) for n in rules.EMA_PERIODS}
                    signal = rules.entry_signal(float(latest.close), float(prev.close), emas, previous_emas, int(latest.valid_sessions))
                    if symbol in self.state["last_exit"]:
                        repair = rules.reentry_eligible(completed_sessions_since_exit(self.schedule, self.state["last_exit"][symbol], self.state["at"]),
                            self._post_exit_repairs(symbol), float(latest.close), float(prev.high), emas, self.state["at"])
                        signal = {"allowed": repair["allowed"], "reason": repair["reason"]}
                    d.update(signal=signal["allowed"], signal_detail=signal)
                    basis_reason = self._bar_eligibility(frame)
                    if not signal["allowed"]:
                        d["reason"] = signal["reason"]
                        if signal["reason"] == "INDICATOR_WARMUP_INSUFFICIENT":
                            d["status"] = "DATA_UNKNOWN"
                    elif basis_reason:
                        d.update(reason=basis_reason, status="DATA_UNKNOWN")
                    elif symbol in self.state["corporate_unit_uncertainty"]:
                        d.update(reason="CORPORATE_ACTION_UNIT_UNKNOWN", status="DATA_UNKNOWN")
                    else:
                        gate = self._gate(symbol)
                        d["earnings"] = gate
                        if not gate["new_entry_allowed"]:
                            d.update(reason=gate["reason"], status="DATA_UNKNOWN" if gate["status"] == "EARNINGS_UNKNOWN" else "REJECTED")
                        else:
                            quote, reason = self._quote_valid(symbol)
                            if quote is None:
                                d.update(reason=reason, status="DATA_UNKNOWN" if reason in ("QUOTE_MISSING", "STALE_OR_FUTURE_QUOTE", "QUOTE_SIZE_UNIT_UNKNOWN") else "REJECTED")
                            else:
                                overhead = rules.nearest_overhead(float(latest.close), emas, float(latest.prior_high30))
                                if overhead["status"] == "OVERHEAD_INPUT_UNKNOWN":
                                    d.update(reason="OVERHEAD_INPUT_UNKNOWN", status="DATA_UNKNOWN")
                                else:
                                    spread = (quote["ask"]-quote["bid"])/((quote["ask"]+quote["bid"])/2)
                                    prior20 = frame.iloc[-21:-1]
                                    volumes = pd.to_numeric(prior20.volume, errors="coerce") if "volume" in prior20 else pd.Series(dtype=float)
                                    dollar_values = prior20.close*volumes
                                    complete_volume = (len(prior20) == 20 and len(volumes) == 20
                                        and volumes.map(lambda x: pd.notna(x) and math.isfinite(x) and x >= 0).all()
                                        and dollar_values.map(lambda x: pd.notna(x) and math.isfinite(x)).all())
                                    dollar = float(dollar_values.mean()) if complete_volume else None
                                    identity_hash = u.get("identity_sort_hash") or _hash(u.get("security_id", symbol))
                                    candidates.append((spread, -dollar if dollar is not None else None, identity_hash, symbol, d, latest, emas, overhead, quote))
                                    d.update(status="CANDIDATE", reason="READY_FOR_SHARED_CAPITAL_RANK",
                                             decision_spread_fraction=spread, prior20_dollar_volume=dollar,
                                             dollar_volume_status="KNOWN" if complete_volume else "UNKNOWN",
                                             identity_sort_hash=identity_hash,
                                             ranking_cutoff=str(prior20.index[-1].date()) if len(prior20) else None)
            self.state["decisions"].append(d)
        groups = {}
        for candidate in candidates:
            groups.setdefault(candidate[0], []).append(candidate)
        ordered = []
        for spread in sorted(groups):
            group = groups[spread]
            if len(group) == 1:
                group[0][4]["ranking_secondary_status"] = "RANKING_SECONDARY_NOT_NEEDED"
                ordered.extend(group)
            elif any(candidate[1] is None for candidate in group):
                for candidate in group:
                    candidate[4].update(status="DATA_UNKNOWN", reason="TIED_SPREAD_SECONDARY_DOLLAR_VOLUME_UNKNOWN",
                                        ranking_secondary_status="UNRESOLVED_TIE_NO_FABRICATED_VOLUME")
            else:
                for candidate in group:
                    candidate[4]["ranking_secondary_status"] = "KNOWN_SECONDARY_VOLUME_THEN_IDENTITY_HASH"
                ordered.extend(sorted(group, key=lambda x: (x[1], x[2])))
        for rank, (_, _, _, symbol, d, latest, emas, overhead, quote) in enumerate(ordered, 1):
            d["rank"] = rank
            self._create_buy(symbol, d, latest, emas, overhead, quote)
        self._snapshot("ENTRY_DECISION")

    def _create_buy(self, symbol, decision, daily, emas, overhead, quote):
        open_buys = [o for o in self.orders.values() if o["side"] == "BUY" and o["status"] == "OPEN" and o["symbol"] not in self.ledger.positions]
        if len(self.ledger.positions)+len(open_buys) >= MODES[self.config.mode][1]:
            decision.update(status="REJECTED", reason="POSITION_OR_PENDING_INTENT_SLOTS_FULL")
            return
        preliminary = self.ledger.plan_entry(symbol, quote["ask"], self._marks(fresh_today=True), stop_leg_count=2)
        if preliminary["status"] != "READY":
            decision.update(status="REJECTED", reason=preliminary["status"])
            return
        quantity = preliminary["quantity"]
        if not self.ledger.margin_enabled:
            reserved = sum((o["quantity"]-o["filled_quantity"])*o["limit"]+self.config.commission for o in open_buys)
            quantity = min(quantity, max(0, math.floor((self.ledger.cash-self.ledger.reserved_exit_fees-reserved-3*self.config.commission)/(quote["ask"]*(1+self.ledger.friction)))))
        plan = rules.initial_stop_plan(float(daily.close), quote["ask"]*(1+self.ledger.friction), emas, quantity)
        if plan.status != "VALID":
            decision.update(status="REJECTED", reason=plan.reason)
            return
        if len(plan.legs) == 1:
            precise = self.ledger.plan_entry(symbol, quote["ask"], self._marks(fresh_today=True), stop_leg_count=1)
            adjusted = precise["quantity"]
            if not self.ledger.margin_enabled:
                adjusted = min(adjusted, max(0, math.floor((self.ledger.cash-self.ledger.reserved_exit_fees-reserved-2*self.config.commission)/(quote["ask"]*(1+self.ledger.friction)))))
            revised = rules.initial_stop_plan(float(daily.close), quote["ask"]*(1+self.ledger.friction), emas, adjusted)
            if revised.status == "VALID" and len(revised.legs) == 1:
                quantity, plan = adjusted, revised
        rr = rules.net_reward_risk(quote["ask"]*(1+self.ledger.friction), plan.legs, overhead["price"], self.config.commission, self.ledger.friction)
        limit = rules.constrained_entry_limit(float(daily.close), plan.legs, overhead["price"], self.config.commission, self.ledger.friction)
        decision.update(stop_legs=[asdict(l) for l in plan.legs], rr=rr, limit=limit)
        if not rr["allowed"] or quote["ask"]*(1+self.ledger.friction) > limit+1e-12:
            decision.update(status="REJECTED", reason=rr["status"] if not rr["allowed"] else "ENTRY_PRICE_EXCEEDS_FIXED_LIMIT")
            return
        if quote["ask"] < max(emas[n] for n in (5, 10, 20)) or quote["ask"] <= max(l.stop for l in plan.legs):
            decision.update(status="REJECTED", reason="ENTRY_STRUCTURE_INVALIDATED")
            return
        oid = f"{self.config.run_id}:{symbol}:{self._day()}:entry"
        order = {"order_id": oid, "campaign_id": oid, "symbol": symbol, "side": "BUY", "quantity": quantity,
            "filled_quantity": 0, "status": "OPEN", "created_at": self.state["at"], "active_at": self.state["at"],
            "expires_at": self._clock()["entry_expiry"], "limit": limit, "close": float(daily.close),
            "emas": {str(n): v for n, v in emas.items()}, "legs": [asdict(l) for l in plan.legs],
            "overhead": overhead, "last_reason": "AWAITING_DISPLAYED_SIZE", "reason": "FRESH_OR_REPAIRED_BREAKOUT",
            "data_basis": self.config.data_basis, "earnings_tier": self.config.earnings_tier}
        self.orders[oid] = order
        decision.update(status="INTENT_CREATED", reason="FROZEN_RULES_PASSED", order_id=oid)
        self._record("BUY_INTENT", symbol, **order, signal_input_version=decision["input_version"],
                     signal_data_cutoff=decision.get("data_cutoff"), source_received_at=quote.get("source_received_at", "UNKNOWN"))
        self._execute_buy(order)

    def _execute_buy(self, order):
        now = rules.aware(self.state["at"])
        if now >= rules.aware(order["expires_at"]):
            self._cancel(order, "ENTRY_EXPIRED")
            return
        if now < rules.aware(order["active_at"]):
            return
        symbol = order["symbol"]
        gate = self._gate(symbol)
        if not gate["new_entry_allowed"]:
            self._cancel(order, f"EARNINGS_GATE_CHANGED:{gate['reason']}")
            return
        p, reason = self._quote_valid(symbol)
        if p is None:
            order["last_reason"] = reason
            self._record("BUY_PENDING_NO_FILL", symbol, order_id=order["order_id"], reason=reason)
            return
        inventory = self.state["quote_inventory"][p["inventory_id"]]
        existing = self.ledger.orders.get(order["order_id"])
        commission = 0 if existing else self.config.commission
        remaining = order["quantity"]-order["filled_quantity"]
        budget = remaining*order["limit"]+commission+len(order["legs"])*self.config.commission
        if not self.ledger.margin_enabled:
            budget = min(budget, self.ledger.cash-self.ledger.reserved_exit_fees)
        quote = rules.Quote(p["bid"], p["ask"], inventory["ask_remaining"], p["timestamp"], "shares",
                           _known_time(p.get("available_at")), None if p.get("source_received_at") == "UNKNOWN" else p.get("source_received_at"))
        result = rules.capped_quote_fill(quote, now, order["created_at"], order["expires_at"], order["limit"],
            remaining, budget, max(order["emas"][str(n)] for n in (5, 10, 20)), max(l["stop"] for l in order["legs"]),
            commission, 0 if existing else len(order["legs"])*self.config.commission, self.ledger.friction)
        if not result["quantity"]:
            order["last_reason"] = result["reason"]
            self._record("BUY_PENDING_NO_FILL", symbol, order_id=order["order_id"], reason=result["reason"])
            return
        n = int(result["quantity"])
        previous = order["filled_quantity"]
        total = previous+n
        if len(order["legs"]) == 2:
            cumulative = [(total+1)//2, total//2]
            prev_quantities = [(previous+1)//2, previous//2]
            quantities = [c-p for c, p in zip(cumulative, prev_quantities)]
        else:
            quantities, cumulative = [n], [total]
        expected = p["ask"]*(1+self.ledger.friction)
        if any(q and (expected-leg["stop"])/expected > .07+1e-12 for q, leg in zip(quantities, order["legs"])):
            order["last_reason"] = "PARTIAL_ACTUAL_FILL_STOP_EXCEEDS_7_PERCENT"
            self._record("BUY_PARTIAL_REJECTED", symbol, reason=order["last_reason"], quantity=n)
            return
        campaign = self.ledger.campaigns.get(order["campaign_id"])
        prior_gross = (campaign["entry_outlay"]-self.config.commission) if campaign else 0.0
        mean_entry = (prior_gross+n*expected)/total
        legs = [rules.StopLeg(q, l["support"], l["stop"]) for q, l in zip(cumulative, order["legs"]) if q]
        rr = rules.net_reward_risk(mean_entry, legs, order["overhead"]["price"], self.config.commission, self.ledger.friction)
        if not rr["allowed"]:
            order["last_reason"] = "PARTIAL_QUANTITY_NET_RR_BELOW_2"
            self._record("BUY_PARTIAL_REJECTED", symbol, reason=order["last_reason"], rr=rr, quantity=n)
            return
        fill_id = f"{order['order_id']}:{p['inventory_id']}:{previous}"
        fill = self.ledger.buy(order["order_id"], symbol, n, p["ask"], self.state["at"], self._marks(fresh_today=True),
            order["campaign_id"], quantities, order["limit"], inventory["ask_remaining"], fill_id, order["quantity"])
        inventory["ask_remaining"] -= n
        inventory["ask_consumed"] += n
        order["filled_quantity"] = total
        order["status"] = "FILLED" if total == order["quantity"] else "OPEN"
        order["last_reason"] = "HISTORICAL_L1_MODEL_FILL"
        if symbol not in self.state["stops"]:
            self.state["stops"][symbol] = [{"support": l["support"], "stop": l["stop"]} for l in order["legs"]]
            if order["overhead"]["price"] is not None:
                self.state["pressure"][symbol] = asdict(rules.PressureState(order["overhead"]["source"], order["overhead"]["price"]))
        self._record("BUY_FILL", symbol, **fill, quote_inventory_id=p["inventory_id"],
                     source_received_at=p.get("source_received_at", "UNKNOWN"),
                     historical_network_received_at=p.get("historical_network_received_at", "UNKNOWN"),
                     availability_basis=p.get("availability_basis", "UNKNOWN"), actual_quantity_rr=rr,
                     actual_leg_quantities=quantities)

    def _split(self, symbol, payload):
        ratio = float(payload["ratio"])
        if not _positive(ratio):
            raise ValueError("SPLIT_RATIO_UNKNOWN")
        action_id = payload.get("action_id", f"split:{symbol}:{self._day()}")
        if action_id in self.ledger.corporate_actions:
            self.ledger.apply_split(action_id, symbol, ratio, self.state["at"])
            return
        self.ledger.apply_split(action_id, symbol, ratio, self.state["at"])
        pending_exits = [copy.deepcopy(o) for o in self.orders.values() if o["symbol"] == symbol and o["side"] == "SELL" and o["status"] == "OPEN"]
        self.state["unit_versions"][symbol] = self.state["unit_versions"].get(symbol, 0)+1
        for o in self.orders.values():
            if o["symbol"] == symbol and o["status"] == "OPEN":
                self._cancel(o, "CORPORATE_ACTION_CANCELS_PRIOR_UNIT_ORDER")
        for bar in self.state["bars"].get(symbol, {}).values():
            for k in ("open", "high", "low", "close"):
                bar[k] /= ratio
            if bar.get("volume") is not None:
                bar["volume"] *= ratio
            bar.setdefault("unit_actions", []).append(action_id)
        for l in self.state["stops"].get(symbol, []):
            l["support"] /= ratio
            l["stop"] /= ratio
        if symbol in self.state["trailing"]:
            for l in self.state["trailing"][symbol]["legs"]:
                l["support"] /= ratio
                l["stop"] /= ratio
        self.state["pressure"].pop(symbol, None)
        self.state["minutes"].pop(symbol, None)
        self.state["quotes"].pop(symbol, None)
        for r in self.state["repairs"].get(symbol, []):
            for k in ("low", "high", "close", "ema5", "ema10", "atr14_prev"):
                if r[k] is not None:
                    r[k] /= ratio
        if symbol in self.state["marks"]:
            self.state["marks"][symbol] /= ratio
        self.state["input_versions"][symbol] = self.state["input_versions"].get(symbol, 0)+1
        self._feature_cache.pop(symbol, None)
        self._record("SPLIT_UNITS_APPLIED", symbol, ratio=ratio, action_id=action_id,
                     original_inputs_and_fills_unchanged=True)
        for o in pending_exits:
            self._new_exit(symbol, o["reason"], int((o["quantity"]-o["filled_quantity"])*ratio), o["leg_index"],
                           max(rules.aware(o["active_at"]), rules.aware(self.state["at"])).isoformat())

    def _snapshot(self, reason):
        snap = self.ledger.snapshot(self._marks(fresh_today=True))
        if snap["missing_current_marks"]:
            # Stale carry is retained for ledger reconciliation only. Published
            # evaluated equity is null and never a fabricated flat return.
            snap["reconciliation_equity_with_stale_marks"] = snap["net_equity"]
            snap["net_equity"] = None
        pending_exits = [o["order_id"] for o in self.orders.values() if o["side"] == "SELL" and o["status"] == "OPEN"]
        snap.update(at=self.state["at"], reason=reason, pending_exit_orders=pending_exits,
            reserved_entry_cash=sum((o["quantity"]-o["filled_quantity"])*o.get("limit", 0) for o in self.orders.values() if o["side"] == "BUY" and o["status"] == "OPEN"),
            data_basis=self.config.data_basis, earnings_tier=self.config.earnings_tier,
            execution_evidence="HISTORICAL_L1_MODEL_NOT_BROKER_FILL")
        self.state["equity"].append(_clean(snap))
        return snap

    def _rollback_state(self, kind, heavy):
        return copy.deepcopy({k: v for k, v in self.state.items() if k not in heavy})

    def process(self, event: dict) -> dict:
        e = _clean(copy.deepcopy(event))
        for key in ("event_id", "kind", "at"):
            if key not in e:
                raise ValueError(f"MISSING_EVENT_{key.upper()}")
        identity = str(e["event_id"])
        digest = _hash(e)
        if identity in self.state["handled"]:
            if self.state["handled"][identity] != digest:
                raise ValueError("EVENT_ID_CONTENT_CONFLICT")
            return {"status": "DUPLICATE_EVENT_SKIPPED", "event_id": identity}
        now = rules.aware(e["at"])
        if self.state["at"] and now < rules.aware(self.state["at"]):
            raise ValueError("NON_MONOTONIC_REPLAY_EVENT")
        self.state["at"] = now.isoformat()
        # Accrual belongs to the simulated NY calendar date, never UTC midnight.
        self.ledger.advance_day(self._day())
        symbol, payload, kind = e.get("symbol"), e.get("payload", {}), e["kind"]
        if kind not in PRIORITY:
            raise ValueError("UNIMPLEMENTED_EVENT_KIND")
        if symbol and symbol not in self.universe:
            raise ValueError("SYMBOL_NOT_IN_FROZEN_UNIVERSE")
        ledger_before = self.ledger.to_dict()
        # Undo snapshots cover precisely the mutable collections this event may
        # touch. Copying all past quotes/minutes per tick would be quadratic.
        heavy = {"bars", "minutes", "quote_inventory", "trace", "handled", "equity", "decisions", "errors", "gaps", "repairs"}
        backup = self._rollback_state(kind, heavy)
        lengths = {k: len(self.state[k]) for k in ("trace", "equity", "decisions", "errors", "gaps")}
        missing = object()
        bar_backup = copy.deepcopy(self.state["bars"].get(symbol, {})) if kind in ("DAILY_BAR", "SPLIT") else None
        minute_key = str(payload.get("bar_end")) if kind == "MINUTE_BAR" else None
        if minute_key is not None:
            minute_key = rules.aware(minute_key).isoformat()
        minute_before = self.state["minutes"].get(symbol, {}).get(minute_key, missing)
        if minute_before is not missing:
            minute_before = copy.deepcopy(minute_before)
        minute_symbol_backup = copy.deepcopy(self.state["minutes"].get(symbol, {})) if kind == "SPLIT" else None
        session_minutes_backup = dict(self.state["minutes"]) if kind == "SESSION_START" else None
        repair_lengths = {s: len(v) for s, v in self.state["repairs"].items()}
        repair_symbol_backup = copy.deepcopy(self.state["repairs"].get(symbol, [])) if kind in ("SPLIT", "DAILY_BAR") else None
        inventory_keys = {q["inventory_id"] for q in self.state["quotes"].values()}
        if kind == "QUOTE":
            normal_quote = dict(payload, timestamp=rules.aware(payload.get("timestamp", self.state["at"])).isoformat())
            inventory_keys.add(self._quote_key(symbol, normal_quote))
        inventory_before = {k: copy.deepcopy(self.state["quote_inventory"][k]) if k in self.state["quote_inventory"] else missing for k in inventory_keys}
        try:
            if kind == "DAILY_BAR":
                self._data_bar(symbol, payload)
            elif kind == "MINUTE_BAR":
                self._minute(symbol, payload)
            elif kind == "QUOTE":
                self._quote(symbol, payload)
            elif kind == "EARNINGS_REVISION":
                allowed = {f.name for f in fields(EarningsRevision)}
                rev = {k: v for k, v in payload.get("revision", payload).items() if k in allowed}
                EarningsRevision(**rev)
                self.state["earnings"].setdefault(symbol, []).append(rev)
                self.state["earnings_coverage"][symbol] = {"complete": bool(payload.get("coverage_complete", False)),
                    "source": payload.get("coverage_source", "UNKNOWN"), "at": self.state["at"],
                    "start": payload.get("coverage_start"), "end": payload.get("coverage_end")}
                self._record("EARNINGS_VERSION_RECEIVED", symbol, revision=rev,
                             source_received_at=payload.get("source_received_at", rev.get("source_received_at", "UNKNOWN")),
                             historical_network_received_at=payload.get("historical_network_received_at", "UNKNOWN"),
                             availability_basis=payload.get("availability_basis", "UNKNOWN"))
                self._earnings_protection(symbol)
                self._execute_exits(symbol)
            elif kind == "SESSION_START":
                self._session_start()
            elif kind == "ACTIVE_DECISION":
                self._active()
            elif kind == "SESSION_CLOSE":
                self._session_close()
            elif kind == "ENTRY_DECISION":
                self._entry_decision()
            elif kind == "ENTRY_EXPIRY":
                for order in list(self.orders.values()):
                    if order["side"] == "BUY" and order["status"] == "OPEN" and now >= rules.aware(order["expires_at"]):
                        self._cancel(order, "ENTRY_EXPIRED")
                self._snapshot("AFTER_ENTRY_EXPIRY")
            elif kind == "SPLIT":
                self._split(symbol, payload)
            elif kind == "CASH_IN_LIEU":
                result = self.ledger.cash_in_lieu(payload["action_id"], symbol, payload["confirmed_amount"], self.state["at"])
                self._record("CONFIRMED_CASH_IN_LIEU_RECEIVED", symbol, result=result)
                if symbol not in self.ledger.positions:
                    self.state["last_exit"][symbol] = self.state["at"]
                    self.state["stops"].pop(symbol, None)
                    self.state["trailing"].pop(symbol, None)
                    for order in self.orders.values():
                        if order["symbol"] == symbol and order["status"] == "OPEN":
                            self._cancel(order, "FRACTIONAL_CAMPAIGN_CLOSED")
            elif kind == "DIVIDEND_EX":
                self._record("DIVIDEND_RECEIVABLE_CREATED", symbol, dividend=self.ledger.dividend_ex(payload["action_id"], symbol, payload["amount_per_share"], payload.get("pay_date", "UNKNOWN"), self.state["at"]))
            elif kind == "DIVIDEND_PAY":
                self._record("DIVIDEND_PAID", symbol, dividend=self.ledger.dividend_pay(payload["action_id"], self.state["at"]))
            elif kind == "DATA_GAP":
                item = {"at": self.state["at"], "symbol": symbol, **payload}
                self.state["gaps"].append(item)
                if symbol:
                    self.state["active_gaps"][symbol] = {"session_date": self._day(), "reason": payload.get("reason", "INPUT_DATA_GAP")}
                self._record("DATA_GAP", symbol, **payload)
                if payload.get("category") == "CORPORATE_ACTION_UNITS":
                    self.state["corporate_unit_uncertainty"][symbol] = item
            elif kind == "TRADE":
                self._record("TRADE_EVIDENCE_ONLY", symbol, **payload)
            elif kind == "CALENDAR_DAY":
                self._record("CALENDAR_ACCRUAL_SETTLEMENT", cash=self.ledger.cash, debt=self.ledger.debt,
                             accrued_interest=self.ledger.accrued_interest, unsettled_cash=self.ledger.unsettled_cash)
            self.ledger.assert_invariants()
            result = {"status": "PROCESSED", "event_id": identity}
        except (ValueError, KeyError, TypeError, IndexError, AssertionError, OverflowError) as exc:
            self.ledger = Ledger.from_dict(ledger_before)
            for key, val in backup.items():
                self.state[key] = val
            for key, length in lengths.items():
                del self.state[key][length:]
            if bar_backup is not None:
                self.state["bars"][symbol] = bar_backup
            if session_minutes_backup is not None:
                self.state["minutes"] = session_minutes_backup
            elif minute_symbol_backup is not None:
                self.state["minutes"][symbol] = minute_symbol_backup
            elif minute_key is not None:
                if minute_before is missing:
                    self.state["minutes"].get(symbol, {}).pop(minute_key, None)
                else:
                    self.state["minutes"].setdefault(symbol, {})[minute_key] = minute_before
            if repair_symbol_backup is not None:
                self.state["repairs"][symbol] = repair_symbol_backup
            else:
                for s in list(self.state["repairs"]):
                    del self.state["repairs"][s][repair_lengths.get(s, 0):]
            for k, v in inventory_before.items():
                if v is missing:
                    self.state["quote_inventory"].pop(k, None)
                else:
                    self.state["quote_inventory"][k] = v
            self._feature_cache = {}
            error = {"event_id": identity, "at": now.isoformat(), "kind": kind, "symbol": symbol,
                     "reason": str(exc), "exception": type(exc).__name__, "status": "EVENT_FAILED_STATE_ROLLED_BACK"}
            self.state["errors"].append(error)
            self._record("EVENT_ERROR_ISOLATED", symbol, error=error)
            result = error
        self.state["handled"][identity] = digest
        self.state["events_processed"] += 1
        if self.checkpoint_path and self.config.checkpoint_every > 0 and self.state["events_processed"] % self.config.checkpoint_every == 0:
            self.save()
        return result

    def run(self, events: Iterable[dict]) -> dict:
        ordered = sorted(events, key=lambda e: (rules.aware(e["at"]), PRIORITY.get(e["kind"], 99), str(e["event_id"])))
        for event in ordered:
            self.process(event)
        if self.checkpoint_path:
            self.save()
        return self.summary()

    def summary(self):
        decisions = self.state["decisions"]
        unknown = sum(d["status"] == "DATA_UNKNOWN" for d in decisions)
        buys = [f for f in self.ledger.fills.values() if f["side"] == "BUY"]
        sells = [f for f in self.ledger.fills.values() if f["side"] == "SELL"]
        evaluated = sum(d["status"] not in ("DATA_UNKNOWN", "HOLDING", "NOT_SAMPLED") and d["reason"] not in ("SCOPE_EXCLUDED", "IDENTITY_OR_SCOPE_UNKNOWN") for d in decisions)
        if buys:
            status = "ENGINE_EXECUTED_WITH_MODEL_FILLS"
        elif unknown:
            status = "ENGINE_EXECUTED_ZERO_ENTRY_DATA_INSUFFICIENT"
        elif evaluated:
            status = "ENGINE_EXECUTED_NO_QUALIFIED_NATURAL_ENTRY"
        else:
            status = "ENGINE_EXECUTED_NO_EVALUABLE_DECISIONS"
        pending = [o for o in self.orders.values() if o["side"] == "SELL" and o["status"] == "OPEN"]
        latest = self.state["equity"][-1] if self.state["equity"] else None
        return _clean({"version": VERSION, "run_id": self.config.run_id, "status": status,
            "actually_executed": self.state["events_processed"] > 0,
            "synthetic_engineering_only": self.config.synthetic_test,
            "data_basis": self.config.data_basis, "earnings_tier": self.config.earnings_tier,
            "research_scope": self.config.research_scope, "entry_dates": self.config.entry_dates,
            "events_processed": self.state["events_processed"], "decision_count": len(decisions),
            "evaluated_decision_count": evaluated, "data_unknown_decision_count": unknown,
            "natural_signal_count": sum(bool(d["signal"]) for d in decisions),
            "buy_intents": sum(o["side"] == "BUY" for o in self.orders.values()),
            "buy_fills": len(buys), "buy_shares": sum(f["quantity"] for f in buys),
            "sell_fills": len(sells), "sell_shares": sum(f["quantity"] for f in sells),
            "open_positions": self.ledger.positions, "pending_exit_count": len(pending),
            "campaigns": self.ledger.campaign_summary(), "latest_equity": latest,
            "error_count": len(self.state["errors"]), "errors": self.state["errors"],
            "session_count": len(set(self.state["sessions_processed"])),
            "input_event_hash": _hash(self.state["handled"]), "calendar_hash": _hash(self.sessions),
            "execution_evidence": "HISTORICAL_L1_MODEL_NOT_BROKER_EXECUTION",
            "engineering_status": "COMPLETED_EVENT_ORCHESTRATION", "unimplemented_event_features": [],
            "fractional_entitlement_limit": "RETAINED_UNTIL_CONFIRMED_CASH_IN_LIEU_EVENT; NEVER_ROUNDED_AWAY_OR_PRICED_FROM_FUTURE"})

    def to_dict(self):
        return _clean({"version": VERSION, "config": asdict(self.config), "universe": self.universe,
                       "sessions": self.sessions, "ledger": self.ledger.to_dict(), "state": self.state})

    def save(self, path: str | Path | None = None):
        target = Path(path) if path else self.checkpoint_path
        if target is None:
            raise ValueError("CHECKPOINT_PATH_REQUIRED")
        target.parent.mkdir(parents=True, exist_ok=True)
        body = self.to_dict()
        wrapper = {"payload": body, "sha256": _hash(body)}
        tmp = target.with_suffix(target.suffix+".tmp")
        with tmp.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(_json(wrapper))
            stream.flush()
            os.fsync(stream.fileno())
        # Windows readers/antivirus can briefly hold the destination open. Keep
        # the last committed checkpoint intact and retry only this atomic swap;
        # persistent permission problems remain explicit failures. No unlink,
        # truncation, or non-atomic fallback is permitted.
        replace_delays = (0.05, 0.10, 0.20, 0.40, 0.80)
        for attempt in range(len(replace_delays)+1):
            try:
                os.replace(tmp, target)
                break
            except PermissionError:
                if attempt == len(replace_delays):
                    raise
                time.sleep(replace_delays[attempt])
        return {"path": str(target), "sha256": wrapper["sha256"], "events_processed": self.state["events_processed"]}

    @classmethod
    def restore(cls, path: str | Path, schedule: pd.DataFrame):
        wrapper = json.loads(Path(path).read_text(encoding="utf-8"))
        value = wrapper["payload"]
        if wrapper["sha256"] != _hash(value) or value["version"] != VERSION:
            raise ValueError("CHECKPOINT_HASH_OR_VERSION_MISMATCH")
        engine = cls(schedule, ReplayConfig(**value["config"]), value["universe"], path)
        if _hash(engine.sessions) != _hash(value["sessions"]):
            raise ValueError("CHECKPOINT_CALENDAR_MISMATCH")
        engine.ledger = Ledger.from_dict(value["ledger"])
        engine.state = value["state"]
        return engine

    def export(self, directory: str | Path):
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        tables = {"orders": list(self.orders.values()), "fills": list(self.ledger.fills.values()),
                  "campaigns": self.ledger.campaign_summary()["campaigns"], "daily_equity": self.state["equity"],
                  "coverage_funnel": self.state["decisions"], "event_trace": self.trace,
                  "quote_inventory": [{"inventory_id": k, **v} for k, v in self.state["quote_inventory"].items()],
                  "account_events": self.ledger.events, "data_gaps": self.state["gaps"]}
        schemas = {"orders": ["order_id", "symbol", "side", "quantity", "status"],
                   "fills": ["fill_id", "order_id", "symbol", "side", "quantity", "execution_price"],
                   "campaigns": ["campaign_id", "symbol", "status", "net_profit"],
                   "daily_equity": ["at", "net_equity", "cash", "debt", "accrued_interest"],
                   "coverage_funnel": ["decision_time", "symbol", "status", "reason"],
                   "event_trace": ["sequence", "at", "kind", "symbol"],
                   "quote_inventory": ["inventory_id", "symbol", "ask_consumed", "bid_consumed"],
                   "account_events": ["type", "date", "amount"], "data_gaps": ["at", "symbol", "reason"]}
        for name, rows in tables.items():
            encoded = [{k: _json(v) if isinstance(v, (dict, list, tuple)) else _clean(v) for k, v in row.items()} for row in rows]
            pd.DataFrame(encoded, columns=None if encoded else schemas[name]).to_csv(root/f"{name}.csv", index=False, encoding="utf-8-sig")
        (root/"summary.json").write_text(json.dumps(self.summary(), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        self.save(root/"checkpoint.json")
        return self.summary()
