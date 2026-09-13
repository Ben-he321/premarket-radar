"""Isolated Ben B1 research accounting; no filesystem, market API or broker access.

Money is USD. Quote sizes passed here must already be normalized to shares.
Buy/sell prices are observed asks/bids; the extra execution friction is separate
from the observed spread. Sale proceeds settle on a caller-supplied exchange
session. Interest uses closing debt for each elapsed calendar day, actual/360.
The synthetic margin defaults are assumptions, never verified broker permission.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
import copy
import math
from typing import Mapping


VERSION = "BEN_B1_LEDGER_V1"
MODES = {
    "P50_PRIMARY": (0.5, 2, False),
    "P100_CONCENTRATED": (1.0, 1, False),
    "P200_MARGIN_RESEARCH": (2.0, 1, True),
}
EPS = 1e-8


def money(value: float) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _day(value: str | date | datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def _positive(value: float) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value) and value > 0


@dataclass(frozen=True)
class FinanceConfig:
    mode: str = "P50_PRIMARY"
    initial_equity: float = 5500.0
    commission: float = 1.0
    friction_bps: float = 10.0
    annual_interest_rate: float = 0.08
    initial_margin: float = 0.50
    maintenance_margin: float = 0.30
    margin_evidence: str = "HYPOTHETICAL_MARGIN_NOT_BROKER_VALIDATED"
    interest_evidence: str = "HYPOTHETICAL_FIXED_RATE_NOT_HISTORICAL_CURVE"

    def __post_init__(self):
        if self.mode not in MODES:
            raise ValueError("UNKNOWN_ACCOUNT_MODE")
        if not _positive(self.initial_equity):
            raise ValueError("INVALID_INITIAL_EQUITY")
        if not (0 <= self.commission and 0 <= self.friction_bps < 10000):
            raise ValueError("INVALID_COST_ASSUMPTION")
        if not (0 < self.initial_margin <= 1 and 0 < self.maintenance_margin <= 1):
            raise ValueError("INVALID_MARGIN_ASSUMPTION")
        if not (math.isfinite(self.annual_interest_rate) and self.annual_interest_rate >= 0):
            raise ValueError("INVALID_INTEREST_RATE")


class Ledger:
    """Small deterministic account model for an externally ordered event replay.

    ``buy`` and ``sell`` accept one actual partial fill at a time. Reuse order_id
    across fills and supply distinct fill_id values: each actual order is charged
    once. A resumed run restores both order and fill identities with from_dict.
    No additional purchase of an existing campaign is allowed except remaining
    fills of its original, explicitly bounded order.
    """

    def __init__(self, config: FinanceConfig | None = None):
        self.config = config or FinanceConfig()
        self.cash = money(self.config.initial_equity)
        self.debt = 0.0
        self.accrued_interest = 0.0
        self.interest_total = 0.0
        self.interest_posted = 0.0
        self.asof_day: str | None = None
        self.positions: dict = {}
        self.campaigns: dict = {}
        self.orders: dict = {}
        self.fills: dict = {}
        self.settlements: list = []
        self.events: list = []
        self.stopped_reason: str | None = None
        self.debt_campaign_id: str | None = None

    @property
    def margin_enabled(self) -> bool:
        return MODES[self.config.mode][2]

    @property
    def friction(self) -> float:
        return self.config.friction_bps / 10000.0

    @property
    def reserved_exit_fees(self) -> float:
        return money(sum(sum(leg["quantity"] > 0 for leg in p["legs"])
                         * self.config.commission for p in self.positions.values()))

    @property
    def unsettled_cash(self) -> float:
        return money(sum(x["amount"] for x in self.settlements))

    def _mark(self, symbol: str, marks: Mapping[str, float]) -> tuple[float, bool]:
        v = marks.get(symbol)
        if isinstance(v, (int, float)) and math.isfinite(v) and v >= 0:
            return float(v), False
        return float(self.positions[symbol]["last_mark"]), True

    def snapshot(self, marks: Mapping[str, float] | None = None,
                 at: str | date | datetime | None = None) -> dict:
        if at is not None:
            self.advance_day(at)
        marks = marks or {}
        values, missing = {}, []
        for symbol, pos in self.positions.items():
            px, stale = self._mark(symbol, marks)
            if stale:
                missing.append(symbol)
            else:
                pos["last_mark"] = px
            values[symbol] = px * pos["quantity"]
        market_value = sum(values.values())
        equity = self.cash + self.unsettled_cash + market_value - self.debt - self.accrued_interest
        if not missing and equity <= 0:
            self.stopped_reason = "NONPOSITIVE_NET_EQUITY"
        return {
            "mode": self.config.mode, "asof_day": self.asof_day,
            "initial_equity": self.config.initial_equity, "cash": self.cash,
            "unsettled_cash": self.unsettled_cash, "market_value": money(market_value),
            "debt": self.debt, "accrued_interest": round(self.accrued_interest, 10),
            "interest_total": round(self.interest_total, 10), "interest_posted": self.interest_posted,
            "net_equity": round(equity, 10), "reserved_exit_fees": self.reserved_exit_fees,
            "available_settled_cash": max(0.0, money(self.cash-self.reserved_exit_fees)),
            "gross_exposure": market_value/equity if equity > 0 else None,
            "open_campaigns": len(self.positions), "open_shares": sum(p["quantity"] for p in self.positions.values()),
            "missing_current_marks": missing, "valuation_status": "STALE_MARK_UNKNOWN" if missing else "CURRENT_MARKS",
            "stopped_reason": self.stopped_reason, "margin_evidence": self.config.margin_evidence if self.margin_enabled else "NO_BORROWING",
        }

    def _receive_cash(self, amount: float):
        repay = min(amount, self.debt)
        self.debt = money(self.debt - repay)
        self.cash = money(self.cash + amount - repay)
        if self.debt <= EPS:
            self.debt_campaign_id = None

    def _debit(self, amount: float):
        use_cash = min(amount, self.cash)
        self.cash = money(self.cash - use_cash)
        short = money(amount - use_cash)
        if short > EPS:
            if not self.margin_enabled:
                raise ValueError("INSUFFICIENT_SETTLED_CASH")
            self.debt = money(self.debt + short)

    def advance_day(self, at: str | date | datetime) -> None:
        """Accrue prior closing debt on every elapsed date, then settle cash.

        The caller supplies exchange-valid settlement dates. Months are posted at
        their final calendar day's end; posting transfers accrual to cash/debt,
        not to P/L a second time. Rounding occurs only when the month is posted.
        """
        target = _day(at)
        if self.asof_day is None:
            self.asof_day = target.isoformat()
        current = date.fromisoformat(self.asof_day)
        if target < current:
            raise ValueError("NON_MONOTONIC_ACCOUNT_CLOCK")
        while current < target:
            charge = self.debt * self.config.annual_interest_rate / 360 if self.margin_enabled else 0.0
            self.accrued_interest += charge
            self.interest_total += charge
            if self.debt_campaign_id and charge:
                self.campaigns[self.debt_campaign_id]["interest"] += charge
            if charge:
                self.events.append({"type": "INTEREST_ACCRUAL", "date": current.isoformat(),
                                    "closing_debt": self.debt, "amount": charge, "day_count": "CALENDAR_ACTUAL_360"})
            following = current + timedelta(days=1)
            if following.month != current.month and self.accrued_interest:
                posted = money(self.accrued_interest)
                self._debit(posted)
                self.interest_posted = money(self.interest_posted + posted)
                # Preserve sub-cent accrual so posting cannot create or destroy equity.
                self.accrued_interest -= posted
                self.events.append({"type": "MONTHLY_INTEREST_POST", "date": current.isoformat(), "amount": posted})
            current = following
            due = [x for x in self.settlements if x["date"] <= current.isoformat()]
            self.settlements = [x for x in self.settlements if x["date"] > current.isoformat()]
            for item in due:
                self._receive_cash(item["amount"])
                self.events.append({"type": "SALE_SETTLEMENT", "date": current.isoformat(), **item})
        self.asof_day = target.isoformat()

    def _capacity(self, base_price: float, equity: float, market_value: float,
                  target_budget: float, commission: float, extra_exit_fees: float,
                  displayed_size: int | None) -> int:
        exec_price = base_price * (1 + self.friction)
        reserve = self.reserved_exit_fees + extra_exit_fees
        quantity = max(0, math.floor((target_budget - commission - extra_exit_fees) / exec_price))
        if displayed_size is not None:
            quantity = min(quantity, displayed_size)
        if not self.margin_enabled:
            quantity = min(quantity, max(0, math.floor((self.cash-reserve-commission)/exec_price)))
        else:
            # Initial margin on the post-fill net equity, including entry costs
            # and protected exit fees. Hard gross limit remains at most 2x NAV.
            margin = max(0.5, self.config.initial_margin)
            numerator = equity - commission - reserve - margin * market_value
            denominator = base_price * (margin + self.friction)
            quantity = min(quantity, max(0, math.floor(numerator / denominator)))
        return quantity

    def plan_entry(self, symbol: str, price: float, marks: Mapping[str, float] | None = None,
                   stop_leg_count: int = 1, displayed_size: int | None = None,
                   limit: float | None = None) -> dict:
        if not _positive(price):
            raise ValueError("INVALID_ENTRY_PRICE")
        if stop_leg_count not in (1, 2):
            raise ValueError("INVALID_STOP_LEG_COUNT")
        if displayed_size is not None and (not isinstance(displayed_size, int) or displayed_size < 0):
            raise ValueError("QUOTE_SIZE_MUST_BE_NONNEGATIVE_SHARES")
        state = self.snapshot(marks)
        fraction, slots, _ = MODES[self.config.mode]
        reason = None
        if self.stopped_reason:
            reason = self.stopped_reason
        elif state["missing_current_marks"]:
            reason = "CURRENT_EQUITY_UNKNOWN"
        elif symbol in self.positions:
            reason = "NO_ADDING_TO_EXISTING_CAMPAIGN"
        elif len(self.positions) >= slots:
            reason = "POSITION_SLOTS_FULL"
        elif limit is not None and (not _positive(limit) or price * (1+self.friction) > limit+EPS):
            reason = "FRICTION_WOULD_EXCEED_LIMIT"
        budget = fraction * state["net_equity"]
        quantity = 0 if reason else self._capacity(price, state["net_equity"], state["market_value"], budget,
                                                 self.config.commission, stop_leg_count*self.config.commission,
                                                 displayed_size)
        if not reason and not quantity:
            reason = "INSUFFICIENT_CAPITAL_OR_DISPLAYED_SIZE"
        return {"status": reason or "READY", "quantity": quantity, "target_fraction": fraction,
                "target_budget": money(budget), "mode": self.config.mode,
                "expected_price": price*(1+self.friction), "commission": self.config.commission,
                "new_exit_fee_reserve": stop_leg_count*self.config.commission,
                "net_equity_at_plan": state["net_equity"]}

    def buy(self, order_id: str, symbol: str, quantity: int, price: float,
            at: str | date | datetime, marks: Mapping[str, float] | None = None,
            campaign_id: str | None = None, leg_quantities: list[int] | None = None,
            limit: float | None = None, displayed_size: int | None = None,
            fill_id: str | None = None, order_quantity: int | None = None) -> dict:
        """Book raw observed ASK ``price``; apply friction exactly once here.

        Do not pass another execution helper's already friction-adjusted result.
        The first fill freezes the limit for all later partial fills/restarts.
        """
        key = fill_id or order_id
        payload = {"side": "BUY", "order_id": order_id, "symbol": symbol, "quantity": quantity,
                   "observed_price": price, "at": str(at)}
        duplicate = self._duplicate(key, payload)
        if duplicate:
            return duplicate
        self._validate_fill(quantity, price, displayed_size)
        self.advance_day(at)
        if self.stopped_reason:
            raise ValueError(self.stopped_reason)
        exec_price = price * (1+self.friction)
        if limit is not None and (not _positive(limit) or exec_price > limit+EPS):
            raise ValueError("FRICTION_WOULD_EXCEED_LIMIT")
        legs = leg_quantities or [quantity]
        if len(legs) not in (1, 2) or sum(legs) != quantity or any(not isinstance(x, int) or x <= 0 for x in legs):
            raise ValueError("INVALID_LEG_QUANTITIES")
        existing_order = self.orders.get(order_id)
        if existing_order and existing_order.get("limit") is not None:
            frozen_limit = existing_order["limit"]
            if limit is not None and limit > frozen_limit+EPS:
                raise ValueError("CANNOT_RAISE_FROZEN_ORDER_LIMIT")
            limit = frozen_limit if limit is None else min(limit, frozen_limit)
            if exec_price > limit+EPS:
                raise ValueError("FRICTION_WOULD_EXCEED_LIMIT")
        commission = 0.0 if existing_order else self.config.commission
        cid = campaign_id or order_id
        if existing_order:
            if existing_order["side"] != "BUY" or existing_order["symbol"] != symbol:
                raise ValueError("ORDER_ID_CONFLICT")
            if existing_order["status"] == "CANCELLED":
                raise ValueError("ORDER_CANCELLED")
            cid = existing_order["campaign_id"]
            if symbol not in self.positions or self.positions[symbol]["campaign_id"] != cid:
                raise ValueError("CAMPAIGN_ALREADY_EXITED")
            if self.campaigns[cid]["exit_quantity"]:
                raise ValueError("NO_REFILL_AFTER_REDUCTION")
            maximum = existing_order["order_quantity"] - existing_order["filled_quantity"]
            state = self.snapshot(marks)
            if state["missing_current_marks"]:
                raise ValueError("CURRENT_EQUITY_UNKNOWN")
            extra_legs = max(0, len(legs)-len(self.positions[symbol]["legs"]))
            remaining_budget = maximum*exec_price + extra_legs*self.config.commission
            capacity = self._capacity(price, state["net_equity"], state["market_value"], remaining_budget,
                                      0, extra_legs*self.config.commission, displayed_size)
            maximum = min(maximum, capacity)
        else:
            plan = self.plan_entry(symbol, price, marks, len(legs), displayed_size, limit)
            maximum = plan["quantity"]
            if quantity > maximum:
                raise ValueError(plan["status"] if plan["status"] != "READY" else "ORDER_EXCEEDS_CAPACITY")
            requested = order_quantity if order_quantity is not None else quantity
            if not isinstance(requested, int) or requested < quantity:
                raise ValueError("INVALID_ORIGINAL_ORDER_QUANTITY")
            # A partial displayed fill may be smaller than a fixed planned target.
            full_plan = self.plan_entry(symbol, price, marks, len(legs), None, limit)
            if requested > full_plan["quantity"]:
                raise ValueError("ORIGINAL_ORDER_EXCEEDS_CAPACITY")
            if cid in self.campaigns:
                raise ValueError("CAMPAIGN_ID_REUSED")
        if quantity > maximum:
            raise ValueError("PARTIAL_FILL_EXCEEDS_REMAINING_CAPACITY")
        outlay = money(quantity*exec_price + commission)
        if not existing_order:
            self.campaigns[cid] = {"campaign_id": cid, "symbol": symbol, "entry_quantity": 0, "exit_quantity": 0,
                                   "entry_outlay": 0.0, "exit_proceeds": 0.0, "commission": 0.0,
                                   "friction": 0.0, "interest": 0.0, "status": "OPEN", "entry_at": str(at),
                                   "exit_at": None, "deviation_reduction_done": False}
            self.positions[symbol] = {"campaign_id": cid, "quantity": 0, "last_mark": price,
                                      "legs": [{"quantity": 0, "original_quantity": 0} for _ in legs]}
            self.orders[order_id] = {"side": "BUY", "symbol": symbol, "campaign_id": cid,
                                     "order_quantity": requested, "filled_quantity": 0,
                                     "commission_charged": commission, "status": "OPEN", "limit": limit}
        pos = self.positions[symbol]
        while len(pos["legs"]) < len(legs):
            pos["legs"].append({"quantity": 0, "original_quantity": 0})
        self._debit(outlay)
        if self.debt > 0:
            self.debt_campaign_id = cid
        pos["quantity"] += quantity
        pos["last_mark"] = price
        for i, q in enumerate(legs):
            pos["legs"][i]["quantity"] += q
            pos["legs"][i]["original_quantity"] += q
        campaign = self.campaigns[cid]
        campaign["entry_quantity"] += quantity
        campaign["entry_outlay"] = money(campaign["entry_outlay"]+outlay)
        campaign["commission"] += commission
        campaign["friction"] += quantity*price*self.friction
        order = self.orders[order_id]
        order["filled_quantity"] += quantity
        if order["filled_quantity"] == order["order_quantity"]:
            order["status"] = "FILLED"
        event = {**payload, "fill_id": key, "campaign_id": cid, "execution_price": exec_price,
                 "commission": commission, "outlay": outlay, "displayed_size_shares": displayed_size,
                 "limit": limit, "price_is_proxy": True}
        self.fills[key] = event
        self.events.append(event)
        self.assert_invariants()
        return copy.deepcopy(event)

    def _duplicate(self, key: str, payload: dict) -> dict | None:
        if key not in self.fills:
            return None
        event = self.fills[key]
        if any(event.get(k) != v for k, v in payload.items()):
            raise ValueError("FILL_ID_CONFLICT")
        return copy.deepcopy(event)

    @staticmethod
    def _validate_fill(quantity: int, price: float, displayed_size: int | None):
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
            raise ValueError("QUANTITY_MUST_BE_POSITIVE_INTEGER")
        if not _positive(price):
            raise ValueError("INVALID_FILL_PRICE")
        if displayed_size is not None:
            if not isinstance(displayed_size, int) or displayed_size < 0 or quantity > displayed_size:
                raise ValueError("FILL_EXCEEDS_DISPLAYED_SHARES")

    def cancel_order(self, order_id: str, at: str | date | datetime) -> dict:
        order = self.orders[order_id]
        if order["status"] == "OPEN":
            order["status"] = "CANCELLED"
            self.events.append({"type": "CANCEL_REMAINDER", "order_id": order_id, "at": str(at),
                                "unfilled_quantity": order["order_quantity"]-order["filled_quantity"]})
        return copy.deepcopy(order)

    def sell(self, order_id: str, symbol: str, quantity: int, price: float,
             at: str | date | datetime, settlement_date: str | date,
             leg_index: int | None = None, reason: str = "STRUCTURE_EXIT",
             fill_id: str | None = None, displayed_size: int | None = None,
             order_quantity: int | None = None) -> dict:
        """Book raw observed BID ``price``; apply friction exactly once here."""
        key = fill_id or order_id
        payload = {"side": "SELL", "order_id": order_id, "symbol": symbol, "quantity": quantity,
                   "observed_price": price, "at": str(at), "reason": reason,
                   "settlement_date": _day(settlement_date).isoformat()}
        duplicate = self._duplicate(key, payload)
        if duplicate:
            return duplicate
        self._validate_fill(quantity, price, displayed_size)
        if _day(settlement_date) <= _day(at):
            raise ValueError("SALE_REQUIRES_FUTURE_SETTLEMENT_SESSION")
        self.advance_day(at)
        if symbol not in self.positions or quantity > self.positions[symbol]["quantity"]:
            raise ValueError("INSUFFICIENT_POSITION")
        pos = self.positions[symbol]
        if leg_index is not None and (leg_index not in range(len(pos["legs"])) or quantity > pos["legs"][leg_index]["quantity"]):
            raise ValueError("INSUFFICIENT_STOP_LEG")
        existing = self.orders.get(order_id)
        if existing and (existing["side"] != "SELL" or existing["symbol"] != symbol or existing["campaign_id"] != pos["campaign_id"]):
            raise ValueError("ORDER_ID_CONFLICT")
        if existing and existing["status"] == "CANCELLED":
            raise ValueError("ORDER_CANCELLED")
        requested = order_quantity if order_quantity is not None else quantity
        if not existing and (not isinstance(requested, int) or requested < quantity or requested > pos["quantity"]):
            raise ValueError("INVALID_ORIGINAL_ORDER_QUANTITY")
        if existing and quantity > existing["order_quantity"]-existing["filled_quantity"]:
            raise ValueError("PARTIAL_FILL_EXCEEDS_ORDER")
        commission = 0.0 if existing else self.config.commission
        exec_price = price*(1-self.friction)
        proceeds = money(quantity*exec_price-commission)
        if proceeds < 0:
            # Reserved settled cash pays any closing commission above sale value.
            if not self.margin_enabled and self.cash+proceeds < -EPS:
                raise ValueError("EXIT_FEE_SHORTFALL")
            self._debit(-proceeds)
        cid = pos["campaign_id"]
        if not existing:
            self.orders[order_id] = {"side": "SELL", "symbol": symbol, "campaign_id": cid,
                                     "order_quantity": requested, "filled_quantity": 0,
                                     "commission_charged": commission, "status": "OPEN"}
        if proceeds > 0:
            self.settlements.append({"date": _day(settlement_date).isoformat(), "amount": proceeds,
                                     "order_id": order_id, "fill_id": key, "campaign_id": cid})
        pos["quantity"] -= quantity
        remaining = quantity
        for i in ([leg_index] if leg_index is not None else range(len(pos["legs"]))):
            take = min(remaining, pos["legs"][i]["quantity"])
            pos["legs"][i]["quantity"] -= take
            remaining -= take
        campaign = self.campaigns[cid]
        campaign["exit_quantity"] += quantity
        campaign["exit_proceeds"] = money(campaign["exit_proceeds"]+proceeds)
        campaign["commission"] += commission
        campaign["friction"] += quantity*price*self.friction
        if not pos["quantity"]:
            campaign.update(status="CLOSED", exit_at=str(at))
            del self.positions[symbol]
        order = self.orders[order_id]
        order["filled_quantity"] += quantity
        if order["filled_quantity"] == order["order_quantity"]:
            order["status"] = "FILLED"
        event = {**payload, "fill_id": key, "campaign_id": cid, "execution_price": exec_price,
                 "commission": commission, "proceeds": proceeds, "price_is_proxy": True}
        self.fills[key] = event
        self.events.append(event)
        self.assert_invariants()
        return copy.deepcopy(event)

    def deviation_quantity(self, symbol: str) -> int:
        """At most half the original shares, accounting for all earlier reductions."""
        pos = self.positions.get(symbol)
        if not pos:
            return 0
        c = self.campaigns[pos["campaign_id"]]
        if c["deviation_reduction_done"]:
            return 0
        return max(0, c["entry_quantity"]//2-c["exit_quantity"])

    def mark_deviation_done(self, symbol: str) -> None:
        self.campaigns[self.positions[symbol]["campaign_id"]]["deviation_reduction_done"] = True

    def margin_check(self, marks: Mapping[str, float], maintenance_margin: float | None = None) -> dict:
        state = self.snapshot(marks)
        requirement = self.config.maintenance_margin if maintenance_margin is None else maintenance_margin
        if not 0 < requirement <= 1:
            raise ValueError("INVALID_MAINTENANCE_MARGIN")
        if not self.margin_enabled:
            return {**state, "margin_status": "NOT_APPLICABLE_CASH_ACCOUNT"}
        required = requirement*state["market_value"]+state["reserved_exit_fees"]
        if state["missing_current_marks"]:
            status = "MARGIN_UNDETERMINED_MISSING_PRICE"
        elif state["net_equity"] <= 0:
            status = "INSOLVENT_STOP_NEW_ENTRIES"
        elif state["net_equity"]+EPS < required:
            status = "MARGIN_CALL"
        else:
            status = "MARGIN_OK"
        return {**state, "margin_status": status, "maintenance_margin": requirement,
                "required_equity": required, "margin_shortfall": max(0.0, required-state["net_equity"])}

    def liquidate_margin(self, order_prefix: str, marks: Mapping[str, float], bids: Mapping[str, float],
                         at: str | date | datetime, settlement_date: str | date,
                         maintenance_margin: float | None = None) -> dict:
        """Fixed minimal integer reduction, only on supplied executable bids.

        A missing current mark/bid leaves a pending breach, never a fabricated
        liquidation at a stop or stale mark. P200 has one symbol slot. The model
        reduces to the active maintenance threshold, not an optimized target.
        """
        self.advance_day(at)
        check = self.margin_check(marks, maintenance_margin)
        if check["margin_status"] not in ("MARGIN_CALL", "INSOLVENT_STOP_NEW_ENTRIES"):
            return {"status": check["margin_status"], "fills": [], "check": check}
        filled, missing = [], []
        requirement = check["maintenance_margin"]
        for symbol in sorted(self.positions):
            bid = bids.get(symbol)
            if not _positive(bid):
                missing.append(symbol)
                continue
            pos = self.positions[symbol]
            if check["net_equity"] <= 0:
                quantity = pos["quantity"]
            else:
                # Differences between current mark and actual bid are a real cost.
                mark = marks[symbol]
                gain_per_share = requirement*mark-(mark-bid*(1-self.friction))
                if gain_per_share <= 0:
                    quantity = pos["quantity"]
                else:
                    quantity = min(pos["quantity"], max(1, math.ceil((check["margin_shortfall"]+self.config.commission)/gain_per_share)))
            filled.append(self.sell(f"{order_prefix}:{symbol}", symbol, quantity, bid, at,
                                    settlement_date, reason="MARGIN_FORCED_REDUCTION"))
            check = self.margin_check(marks, maintenance_margin)
        result = {"status": "MARGIN_CALL_PENDING_EXECUTABLE_PRICE" if missing else check["margin_status"],
                  "fills": filled, "missing_executable_bids": missing, "check": check}
        self.events.append({"type": "MARGIN_ACTION", "at": str(at), "status": result["status"], "missing": missing})
        return result

    def campaign_summary(self) -> dict:
        rows = []
        for c in self.campaigns.values():
            complete = c["status"] == "CLOSED"
            rows.append({**copy.deepcopy(c), "net_profit": money(c["exit_proceeds"]-c["entry_outlay"]-c["interest"]) if complete else None})
        closed = [x for x in rows if x["status"] == "CLOSED"]
        wins = [x["net_profit"] for x in closed if x["net_profit"] > 0]
        losses = [x["net_profit"] for x in closed if x["net_profit"] < 0]
        return {"campaigns": rows, "closed_campaigns": len(closed), "open_campaigns": len(rows)-len(closed),
                "winning_campaigns": len(wins), "losing_campaigns": len(losses),
                "win_rate": len(wins)/len(closed) if closed else None,
                "net_expectancy": sum(x["net_profit"] for x in closed)/len(closed) if closed else None,
                "mean_win_loss_ratio": (sum(wins)/len(wins))/abs(sum(losses)/len(losses)) if wins and losses else None,
                "actual_orders": len(self.orders), "actual_fills": len(self.fills)}

    def assert_invariants(self) -> None:
        if self.cash < -EPS or self.debt < -EPS:
            raise AssertionError("NEGATIVE_CASH_OR_DEBT_COMPONENT")
        if not self.margin_enabled and self.debt > EPS:
            raise AssertionError("DEBT_IN_CASH_ACCOUNT")
        if len(self.positions) > MODES[self.config.mode][1]:
            raise AssertionError("POSITION_SLOT_LIMIT")
        for p in self.positions.values():
            if p["quantity"] <= 0 or sum(x["quantity"] for x in p["legs"]) != p["quantity"]:
                raise AssertionError("LEG_POSITION_RECONCILIATION")
        if any(x["amount"] <= 0 for x in self.settlements):
            raise AssertionError("INVALID_UNSETTLED_RECEIVABLE")

    def to_dict(self) -> dict:
        return {"version": VERSION, "config": asdict(self.config),
                "state": copy.deepcopy({k: v for k, v in vars(self).items() if k != "config"})}

    @classmethod
    def from_dict(cls, value: dict) -> "Ledger":
        if value.get("version") != VERSION:
            raise ValueError("LEDGER_VERSION_MISMATCH")
        result = cls(FinanceConfig(**value["config"]))
        allowed = set(vars(result)) - {"config"}
        if set(value["state"]) != allowed:
            raise ValueError("LEDGER_STATE_SCHEMA_MISMATCH")
        for key, item in value["state"].items():
            setattr(result, key, copy.deepcopy(item))
        result.assert_invariants()
        return result
