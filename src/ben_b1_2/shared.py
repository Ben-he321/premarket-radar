"""B1.2 common P50 cash commitments, without changing any strategy threshold.

An already partially filled buy still owns the cash required by its fixed
remaining quantity/limit. Other candidates cannot reuse that commitment just
because the first symbol now appears in positions. Both Q0 and Q1 use this
accounting correction; old independent samples and old kernels are unchanged.
"""
from __future__ import annotations

import copy
from dataclasses import asdict
from decimal import Decimal, ROUND_CEILING
import json
import math
from pathlib import Path

from src.ben_b1 import rules
from src.ben_b1.ledger import Ledger, MODES
from src.ben_b1.replay import _clean, _hash
from .compact import CompactReplayEngine, CHECKPOINT_VERSION as COMPACT_VERSION, stream_hash
from .replay import ReplayConfig, Q1_HYPOTHESIS

RESERVATION_VERSION = "BEN_B1_2_SHARED_PARTIAL_ENTRY_CASH_V1"
CHECKPOINT_VERSION = COMPACT_VERSION+":"+RESERVATION_VERSION


def _cent_ceiling(value):
    # This is a conservative reservation bound, never an extra charge. Per-share
    # cent ceilings cover any original ledger rounding over multiple fills.
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_CEILING))


class SharedReplayEngine(CompactReplayEngine):
    def __init__(self, schedule, config=None, universe=None, checkpoint_path=None, archive_path=None):
        if config is not None and config.mode != "P50_PRIMARY":
            raise ValueError("B12_COMMON_ACCOUNT_IS_FIXED_P50_ONLY")
        super().__init__(schedule, config, universe, checkpoint_path, archive_path)
        self.state["shared_reservation_version"] = RESERVATION_VERSION

    def pending_entry_commitments(self, exclude_order_id=None):
        rows = []
        for order in self.orders.values():
            if order["side"] != "BUY" or order["status"] != "OPEN" or order["order_id"] == exclude_order_id:
                continue
            remaining = order["quantity"]-order["filled_quantity"]
            if remaining <= 0:
                continue
            commission = 0.0 if order["order_id"] in self.ledger.orders else self.config.commission
            position = self.ledger.positions.get(order["symbol"], {})
            funded_legs = sum(leg["quantity"] > 0 for leg in position.get("legs", []))
            future_exit_fees = max(0, len(order["legs"])-funded_legs)*self.config.commission
            bound = remaining*_cent_ceiling(order["limit"])+commission+future_exit_fees
            rows.append({"order_id": order["order_id"], "symbol": order["symbol"],
                "remaining_quantity": remaining, "fixed_limit": order["limit"],
                "unpaid_entry_commission": commission, "future_exit_fee_reserve": future_exit_fees,
                "maximum_remaining_cash_commitment": bound})
        return {"orders": rows, "total": sum(row["maximum_remaining_cash_commitment"] for row in rows)}

    def _shared_create(self, symbol, decision, daily, emas, overhead, quote):
        equity = self.ledger.snapshot(self._marks(fresh_today=True))
        inventory = self.state["quote_inventory"][quote["inventory_id"]]
        decision.update(cash_before_candidate=self.ledger.cash,
            reserved_exit_fees_before_candidate=self.ledger.reserved_exit_fees,
            net_equity_before_candidate=None if equity["missing_current_marks"] else equity["net_equity"],
            equity_valuation_status_before_candidate=equity["valuation_status"],
            displayed_ask_remaining_before_candidate=inventory["ask_remaining"],
            intended_execution_friction_bps=self.config.friction_bps)
        new_symbol_orders = [order for order in self.orders.values() if order["side"] == "BUY"
            and order["status"] == "OPEN" and order["symbol"] not in self.ledger.positions]
        if len(self.ledger.positions)+len(new_symbol_orders) >= MODES[self.config.mode][1]:
            decision.update(status="REJECTED", reason="POSITION_OR_PENDING_INTENT_SLOTS_FULL")
            return
        preliminary = self.ledger.plan_entry(symbol, quote["ask"], self._marks(fresh_today=True), stop_leg_count=2)
        if preliminary["status"] != "READY":
            decision.update(status="REJECTED", reason=preliminary["status"])
            return
        commitments = self.pending_entry_commitments()
        available = self.ledger.cash-self.ledger.reserved_exit_fees-commitments["total"]
        quantity = min(preliminary["quantity"], max(0, math.floor((available-3*self.config.commission)/(quote["ask"]*(1+self.ledger.friction)))))
        decision.update(shared_existing_entry_commitments=commitments,
            shared_uncommitted_cash_before_plan=available,
            cash_before_candidate=self.ledger.cash, reserved_exit_fees_before_candidate=self.ledger.reserved_exit_fees)
        plan = rules.initial_stop_plan(float(daily.close), quote["ask"]*(1+self.ledger.friction), emas, quantity)
        if plan.status != "VALID":
            decision.update(status="REJECTED", reason=plan.reason)
            return
        if len(plan.legs) == 1:
            precise = self.ledger.plan_entry(symbol, quote["ask"], self._marks(fresh_today=True), stop_leg_count=1)
            adjusted = min(precise["quantity"], max(0, math.floor((available-2*self.config.commission)/(quote["ask"]*(1+self.ledger.friction)))))
            revised = rules.initial_stop_plan(float(daily.close), quote["ask"]*(1+self.ledger.friction), emas, adjusted)
            if revised.status == "VALID" and len(revised.legs) == 1:
                quantity, plan = adjusted, revised
        # Solve only integer cash capacity. Quantity strictly decreases; there is
        # no strategy/price/holding-period search and no invented cash balance.
        while True:
            rr = rules.net_reward_risk(quote["ask"]*(1+self.ledger.friction), plan.legs, overhead["price"], self.config.commission, self.ledger.friction)
            limit = rules.constrained_entry_limit(float(daily.close), plan.legs, overhead["price"], self.config.commission, self.ledger.friction)
            decision.update(stop_legs=[asdict(leg) for leg in plan.legs], rr=rr, limit=limit)
            if not rr["allowed"] or quote["ask"]*(1+self.ledger.friction) > limit+1e-12:
                decision.update(status="REJECTED", reason=rr["status"] if not rr["allowed"] else "ENTRY_PRICE_EXCEEDS_FIXED_LIMIT")
                return
            allowed = max(0, math.floor((available-(1+len(plan.legs))*self.config.commission+1e-9)/_cent_ceiling(limit)))
            if quantity <= allowed:
                break
            quantity = allowed
            plan = rules.initial_stop_plan(float(daily.close), quote["ask"]*(1+self.ledger.friction), emas, quantity)
            if plan.status != "VALID":
                decision.update(status="REJECTED", reason="SHARED_CASH_COMMITMENT_CAPACITY_INSUFFICIENT", capacity_detail=plan.reason)
                return
        if quote["ask"] < max(emas[n] for n in (5, 10, 20)) or quote["ask"] <= max(leg.stop for leg in plan.legs):
            decision.update(status="REJECTED", reason="ENTRY_STRUCTURE_INVALIDATED")
            return
        oid = f"{self.config.run_id}:{symbol}:{self._day()}:entry"
        order = {"order_id": oid, "campaign_id": oid, "symbol": symbol, "side": "BUY", "quantity": quantity,
            "filled_quantity": 0, "status": "OPEN", "created_at": self.state["at"], "active_at": self.state["at"],
            "expires_at": self._clock()["entry_expiry"], "limit": limit, "close": float(daily.close),
            "emas": {str(n): value for n, value in emas.items()}, "legs": [asdict(leg) for leg in plan.legs],
            "overhead": overhead, "last_reason": "AWAITING_DISPLAYED_SIZE", "reason": "FRESH_OR_REPAIRED_BREAKOUT",
            "data_basis": self.config.data_basis, "earnings_tier": self.config.earnings_tier,
            "shared_reservation_version": RESERVATION_VERSION}
        self.orders[oid] = order
        decision.update(status="INTENT_CREATED", reason="FROZEN_RULES_PASSED", order_id=oid)
        self._record("BUY_INTENT", symbol, **order, signal_input_version=decision["input_version"],
            signal_data_cutoff=decision.get("data_cutoff"), source_received_at=quote.get("source_received_at", "UNKNOWN"))
        self._execute_buy(order)

    def _create_buy(self, symbol, decision, daily, emas, overhead, quote):
        if self.config.quote_mode == "Q1" and self._q1_watch_only:
            decision.update(status="WATCHING", reason="OBSERVATION_PLAN_NOT_ORDER")
            return
        oid = f"{self.config.run_id}:{symbol}:{self._day()}:entry"
        if oid in self.orders:
            decision.update(status="INTENT_EXISTS", reason="FIRST_INTENT_FIXED_NO_REPLACEMENT", order_id=oid)
            return
        trace_start = len(self.trace)
        self._shared_create(symbol, decision, daily, emas, overhead, quote)
        order = self.orders.get(oid)
        if self.config.quote_mode != "Q1" or order is None:
            return
        if order["filled_quantity"] == 0:
            if oid in self.ledger.orders:
                raise AssertionError("UNFILLED_Q1_PRECHECK_MUTATED_ACCOUNT_ORDER")
            reason = order["last_reason"]
            evidence = [{key: copy.deepcopy(value) for key, value in row.items() if key in ("kind", "reason", "rr", "quantity")}
                for row in self.trace[trace_start:] if row["kind"] in ("BUY_PARTIAL_REJECTED", "BUY_PENDING_NO_FILL")]
            del self.orders[oid]
            del self.trace[trace_start:]
            decision.pop("order_id", None)
            decision.update(status="REJECTED", reason=reason, actual_intent_time=None,
                first_arrival_quantity_check="NO_QUALIFIED_EXECUTABLE_QUANTITY", first_arrival_quantity_evidence=evidence)
            self._record("Q1_FIRST_QUANTITY_PRECHECK_REJECTED", symbol, reason=reason,
                quote_inventory_id=quote["inventory_id"], order_created=False, quantity_evidence=evidence)
        else:
            order.update(quote_mode="Q1", execution_hypothesis=Q1_HYPOTHESIS,
                first_qualifying_quote_inventory_id=quote["inventory_id"], first_qualifying_quote_arrival_time=self.state["at"],
                fixed_initial_quantity=order["quantity"], fixed_initial_limit=order["limit"])
            decision.update(actual_intent_time=order["created_at"], first_arrival_quantity_check="QUALIFIED")

    def _execute_buy(self, order):
        now = rules.aware(self.state["at"])
        if order["status"] == "OPEN" and rules.aware(order["active_at"]) <= now < rules.aware(order["expires_at"]):
            commitments = self.pending_entry_commitments()
            if commitments["total"]+self.ledger.reserved_exit_fees > self.ledger.cash+1e-8:
                order["last_reason"] = "SHARED_PENDING_CASH_COMMITMENT_INSUFFICIENT"
                self._record("BUY_PENDING_NO_FILL", order["symbol"], order_id=order["order_id"],
                    reason=order["last_reason"], commitments=commitments, cash=self.ledger.cash)
                return
        return super()._execute_buy(order)

    def _snapshot(self, reason):
        snapshot = super()._snapshot(reason)
        commitments = self.pending_entry_commitments()
        available = self.ledger.cash-self.ledger.reserved_exit_fees-commitments["total"]
        extra = {"shared_reservation_version": RESERVATION_VERSION,
            "shared_reserved_entry_costs": commitments["total"],
            "shared_available_uncommitted_cash": available,
            "shared_reservation_status": "FUNDED" if available >= -1e-8 else "UNFUNDED_COMMITMENT_NO_FURTHER_BUY",
            "cash_reservation_rounding": "PER_SHARE_CENT_CEILING_BOUND_NOT_ADDITIONAL_TRADING_FEE"}
        snapshot.update(extra)
        self.state["equity"][-1].update(_clean(extra))
        return snapshot

    def _payload(self):
        value = super()._payload()
        value["version"] = CHECKPOINT_VERSION
        return value

    def summary(self):
        result = super().summary()
        result.update(shared_reservation_version=RESERVATION_VERSION,
            shared_pending_entry_commitments=self.pending_entry_commitments(),
            shared_cash_rule="ALL_OPEN_BUY_REMAINDERS_RETAIN_FIXED_LIMIT_CASH_AND_UNPAID_FEES; Q0_AND_Q1_SAME_RULE")
        return _clean(result)

    @classmethod
    def restore(cls, path, schedule):
        wrapper = json.loads(Path(path).read_text(encoding="utf-8"))
        value = wrapper["payload"]
        if wrapper["sha256"] != stream_hash(value) or value["version"] != CHECKPOINT_VERSION:
            raise ValueError("CHECKPOINT_HASH_OR_VERSION_MISMATCH")
        archive = value["state"].get("archive")
        if not archive or not Path(archive["path"]).is_file():
            raise ValueError("REQUIRED_COMPACT_ARCHIVE_MISSING")
        engine = cls(schedule, ReplayConfig(**value["config"]), value["universe"], path, archive["path"])
        if _hash(engine.sessions) != _hash(value["sessions"]):
            raise ValueError("CHECKPOINT_CALENDAR_MISMATCH")
        engine.ledger, engine.state = Ledger.from_dict(value["ledger"]), value["state"]
        if engine.state.get("shared_reservation_version") != RESERVATION_VERSION:
            raise ValueError("SHARED_RESERVATION_STATE_VERSION_MISMATCH")
        engine._verify_archive()
        return engine
