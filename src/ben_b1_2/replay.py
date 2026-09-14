"""Frozen Q0 versus causal Q1 entry timing, reusing the B1.1 event kernel.

Q1 is POST_REVIEW_EXECUTION_HYPOTHESIS. Its observation window is inclusive
at exchange close + 5 minutes and exclusive at close + 15 minutes. Only a
newly arrived displayed quote inside this window can create a Q1 intent.
An observation plan is never an order. The original signal, earnings, sizing,
stops, cost, execution, exit, finance and settlement implementations remain
in ``src.ben_b1``. No API or service is accessed here.

``run`` accepts complete timestamp groups. For streaming, ``process`` buffers
new-entry evaluation until a later timestamp or ``flush_quote_batch``. All
quotes at the same timestamp must be delivered before that explicit flush.
A checkpoint preserves an unfinished batch rather than prematurely ranking
its first symbol. A late addition to an already closed timestamp is rejected.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import copy
import json
from pathlib import Path
from typing import Iterable

import pandas as pd

from src.ben_b1 import rules
from src.ben_b1.ledger import Ledger
from src.ben_b1.replay import (PRIORITY, ReplayConfig as LegacyReplayConfig,
    ReplayEngine as LegacyReplayEngine, VERSION as LEGACY_VERSION,
    _clean, _hash, _json, calendar_events)


VERSION = "BEN_B1_2_WINDOW_REPLAY_V1"
Q1_HYPOTHESIS = "POST_REVIEW_EXECUTION_HYPOTHESIS"


@dataclass(frozen=True)
class ReplayConfig(LegacyReplayConfig):
    quote_mode: str = "Q0"

    def __post_init__(self):
        super().__post_init__()
        if self.quote_mode not in ("Q0", "Q1"):
            raise ValueError("UNFROZEN_QUOTE_MODE")


class ReplayEngine(LegacyReplayEngine):
    def __init__(self, schedule, config=None, universe=None, checkpoint_path=None):
        super().__init__(schedule, config or ReplayConfig(), universe, checkpoint_path)
        if not isinstance(self.config, ReplayConfig):
            raise ValueError("B12_REPLAY_CONFIG_REQUIRED")
        self.state["q1_batch"] = {"at": None, "arrivals": {}}
        self.state["q1_closed_quote_time"] = None
        self.state["coverage_limitations"] = []
        self._q1_evaluating = False
        self._q1_watch_only = False

    def _in_window(self):
        if self._day() not in self._session_bounds:
            return False
        clock, now = self._clock(), rules.aware(self.state["at"])
        return rules.aware(clock["entry_decision"]) <= now < rules.aware(clock["entry_expiry"])

    def mark_coverage_limited(self, reason, symbol=None, start=None, end=None):
        """Adapter evidence only: does not alter any strategy decision or book."""
        item = _clean({"reason": reason, "symbol": symbol, "start": start, "end": end})
        if item not in self.state["coverage_limitations"]:
            self.state["coverage_limitations"].append(item)

    def _quote(self, symbol, payload):
        if self.config.quote_mode == "Q0":
            return super()._quote(symbol, payload)
        normalized = dict(payload, timestamp=rules.aware(payload.get("timestamp", self.state["at"])).isoformat())
        key = self._quote_key(symbol, normalized)
        is_first_arrival = key not in self.state["quote_inventory"]
        super()._quote(symbol, payload)
        inventory = self.state["quote_inventory"].get(key)
        if inventory is not None:
            inventory.setdefault("first_arrived_in_replay_at", self.state["at"])
        if not self._in_window():
            return
        if not is_first_arrival:
            self._record("Q1_DUPLICATE_QUOTE_NO_NEW_ENTRY_EVALUATION", symbol,
                         quote_inventory_id=key, original_first_arrival_at=inventory.get("first_arrived_in_replay_at"))
            return
        current = self.state["quotes"].get(symbol)
        if current is None or current["inventory_id"] != key:
            self._record("Q1_OUT_OF_ORDER_QUOTE_NOT_AN_ENTRY_TRIGGER", symbol,
                         quote_inventory_id=key, quote_timestamp=normalized["timestamp"])
            return
        batch = self.state["q1_batch"]
        if batch["at"] is not None and rules.aware(batch["at"]) != rules.aware(self.state["at"]):
            raise ValueError("Q1_UNFLUSHED_TIMESTAMP_BATCH")
        batch["at"] = self.state["at"]
        prior = batch["arrivals"].get(symbol)
        arrivals_at_timestamp = (prior.get("arrivals_at_timestamp", 0) if prior else 0) + 1
        batch["arrivals"][symbol] = {
            "actual_quote_arrival_time": self.state["at"],
            "quote_timestamp": normalized["timestamp"],
            "quote_inventory_id": key,
            "is_first_arrival_of_displayed_inventory": True,
            "arrivals_at_timestamp": arrivals_at_timestamp,
            "source_received_at": payload.get("source_received_at", "UNKNOWN"),
            "historical_network_received_at": payload.get("historical_network_received_at", "UNKNOWN"),
            "availability_basis": payload.get("availability_basis", "UNKNOWN"),
        }

    def _entry_decision(self):
        if self.config.quote_mode == "Q0":
            return super()._entry_decision()
        if self._q1_evaluating:
            return self._evaluate_arrived_candidates()
        # The fixed clock records all symbols' then-known eligibility and updates
        # held stops exactly once as before. It creates no Q1 buy order from a
        # pre-window cached quote. Actual quote arrivals are handled separately.
        first = len(self.state["decisions"])
        self._q1_watch_only = True
        try:
            super()._entry_decision()
        finally:
            self._q1_watch_only = False
        for decision in self.state["decisions"][first:]:
            decision.update(quote_mode="Q1", execution_hypothesis=Q1_HYPOTHESIS,
                evaluation_kind="OBSERVATION_PLAN_NOT_ORDER", actual_intent_time=None,
                entry_window_start=self._clock()["entry_decision"],
                entry_window_end_exclusive=self._clock()["entry_expiry"])
        self._record("Q1_OBSERVATION_WINDOW_OPENED", window_start=self._clock()["entry_decision"],
                     window_end_exclusive=self._clock()["entry_expiry"],
                     all_universe_symbols=len(self.universe), order_created_by_watch_plan=False)

    def _create_buy(self, symbol, decision, daily, emas, overhead, quote):
        if self.config.quote_mode == "Q0":
            return super()._create_buy(symbol, decision, daily, emas, overhead, quote)
        if self._q1_watch_only:
            decision.update(status="WATCHING", reason="OBSERVATION_PLAN_NOT_ORDER")
            return
        oid = f"{self.config.run_id}:{symbol}:{self._day()}:entry"
        if oid in self.orders:
            decision.update(status="INTENT_EXISTS", reason="FIRST_INTENT_FIXED_NO_REPLACEMENT", order_id=oid)
            return
        inventory = self.state["quote_inventory"][quote["inventory_id"]]
        equity = self.ledger.snapshot(self._marks(fresh_today=True))
        decision.update(cash_before_candidate=self.ledger.cash,
            reserved_exit_fees_before_candidate=self.ledger.reserved_exit_fees,
            net_equity_before_candidate=None if equity["missing_current_marks"] else equity["net_equity"],
            equity_valuation_status_before_candidate=equity["valuation_status"],
            displayed_ask_remaining_before_candidate=inventory["ask_remaining"],
            intended_execution_friction_bps=self.config.friction_bps)
        trace_start = len(self.trace)
        super()._create_buy(symbol, decision, daily, emas, overhead, quote)
        order = self.orders.get(oid)
        if order is not None and order["filled_quantity"] == 0:
            # The original execution kernel has already checked actual displayed
            # quantity, costs, partial-quantity 2R and maximum stop distance. A
            # failed first quantity is an evaluation, not an earlier order that
            # may be filled with a later recovered quote. No ledger fill exists.
            if oid in self.ledger.orders:
                raise AssertionError("UNFILLED_Q1_PRECHECK_MUTATED_ACCOUNT_ORDER")
            reason = order["last_reason"]
            quantity_evidence = [{k: copy.deepcopy(v) for k, v in row.items()
                if k in ("kind", "reason", "rr", "quantity")}
                for row in self.trace[trace_start:] if row["kind"] in ("BUY_PARTIAL_REJECTED", "BUY_PENDING_NO_FILL")]
            del self.orders[oid]
            del self.trace[trace_start:]
            decision.pop("order_id", None)
            decision.update(status="REJECTED", reason=reason,
                first_arrival_quantity_check="NO_QUALIFIED_EXECUTABLE_QUANTITY",
                first_arrival_quantity_evidence=quantity_evidence, actual_intent_time=None)
            self._record("Q1_FIRST_QUANTITY_PRECHECK_REJECTED", symbol, reason=reason,
                         quote_inventory_id=quote["inventory_id"], order_created=False,
                         quantity_evidence=quantity_evidence)
        elif order is not None:
            order.update(quote_mode="Q1", execution_hypothesis=Q1_HYPOTHESIS,
                first_qualifying_quote_inventory_id=quote["inventory_id"],
                first_qualifying_quote_arrival_time=self.state["at"],
                fixed_initial_quantity=order["quantity"], fixed_initial_limit=order["limit"])
            decision.update(actual_intent_time=order["created_at"], first_arrival_quantity_check="QUALIFIED")

    def _evaluate_arrived_candidates(self):
        batch = self.state["q1_batch"]
        if not batch["arrivals"] or not self._in_window():
            return
        original_universe = self.universe
        first = len(self.state["decisions"])
        eligible = {}
        for symbol in sorted(batch["arrivals"]):
            oid = f"{self.config.run_id}:{symbol}:{self._day()}:entry"
            if symbol in self.ledger.positions or oid in self.orders:
                self.state["decisions"].append({"decision_time": self.state["at"], "symbol": symbol,
                    "security_id": self.universe[symbol].get("security_id", symbol),
                    "signal": False, "status": "HOLDING" if symbol in self.ledger.positions else "INTENT_EXISTS",
                    "reason": "FIRST_INTENT_FIXED_NO_REPLACEMENT", "order_id": oid if oid in self.orders else None,
                    "data_basis": self.config.data_basis, "earnings_tier": self.config.earnings_tier})
            else:
                eligible[symbol] = self.universe[symbol]
        self.universe = eligible
        try:
            # Original sorting is current spread, known prior-20 dollar volume,
            # then stable identity hash. All timestamp books were updated before
            # this call; future quotes never participate in this candidate set.
            if eligible:
                super()._entry_decision()
        finally:
            self.universe = original_universe
        for decision in self.state["decisions"][first:]:
            symbol = decision["symbol"]
            arrival = batch["arrivals"][symbol]
            quote = self.state["quotes"].get(symbol, {})
            inventory = self.state["quote_inventory"].get(arrival["quote_inventory_id"], {})
            oid = decision.get("order_id")
            order = self.orders.get(oid, {})
            decision.update(**arrival, quote_mode="Q1", execution_hypothesis=Q1_HYPOTHESIS,
                evaluation_kind="NEW_QUOTE_TIMESTAMP_BATCH",
                quote_age_seconds=(rules.aware(self.state["at"])-rules.aware(arrival["quote_timestamp"])).total_seconds(),
                latest_bid=quote.get("bid"), latest_ask=quote.get("ask"),
                displayed_ask_size=quote.get("ask_size"),
                displayed_ask_remaining_after_batch=inventory.get("ask_remaining"),
                displayed_ask_consumed_after_batch=inventory.get("ask_consumed"),
                actual_intent_time=order.get("created_at"),
                fixed_intent_quantity=order.get("quantity"), fixed_intent_limit=order.get("limit"),
                intent_quantity_remaining=order.get("quantity", 0)-order.get("filled_quantity", 0) if order else None,
                skip_reason=decision["reason"] if decision["status"] != "INTENT_CREATED" else None,
                window_start=self._clock()["entry_decision"], window_end_exclusive=self._clock()["entry_expiry"])
            self._record("Q1_CAUSAL_QUOTE_EVALUATION", symbol, **{k: v for k, v in decision.items() if k != "symbol"})

    def flush_quote_batch(self):
        """Close a complete timestamp group, atomically evaluating new intents."""
        if self.config.quote_mode != "Q1" or not self.state["q1_batch"]["arrivals"]:
            return None
        batch = copy.deepcopy(self.state["q1_batch"])
        event = {"event_id": f"q1:timestamp-batch:{batch['at']}:{_hash(batch['arrivals'])}",
                 "kind": "ENTRY_DECISION", "at": batch["at"],
                 "payload": {"quote_mode": "Q1", "arrivals": batch["arrivals"],
                             "execution_hypothesis": Q1_HYPOTHESIS}}
        self._q1_evaluating = True
        try:
            result = super().process(event)
        finally:
            self._q1_evaluating = False
        self.state["q1_closed_quote_time"] = batch["at"]
        self.state["q1_batch"] = {"at": None, "arrivals": {}}
        return result

    def process(self, event):
        if self.config.quote_mode == "Q0":
            return super().process(event)
        # Duplicate retries must not close an unfinished group or reorder it.
        identity = str(event.get("event_id"))
        if identity in self.state["handled"]:
            return super().process(event)
        now, kind = rules.aware(event["at"]), event["kind"]
        batch_at = self.state["q1_batch"]["at"]
        if batch_at and now == rules.aware(batch_at) and kind == "ENTRY_DECISION":
            # At the exact opening endpoint, keep the once-daily held-position
            # trailing update before new entries, just as in Q0. All quote books
            # are already present; the watch plan itself cannot create an order.
            result = super().process(event)
            self.flush_quote_batch()
            return result
        if batch_at and (now > rules.aware(batch_at) or
                (now == rules.aware(batch_at) and PRIORITY.get(kind, 99) > PRIORITY["QUOTE"])):
            self.flush_quote_batch()
        if kind == "QUOTE" and self.state["q1_closed_quote_time"] is not None and now <= rules.aware(self.state["q1_closed_quote_time"]):
            # A fresh inventory added after an explicit timestamp watermark
            # would permit input-chunk order to replace the frozen same-time
            # ranking. Repeated inventory may still be recorded harmlessly.
            normalized = dict(event.get("payload", {}))
            normalized["timestamp"] = rules.aware(normalized.get("timestamp", event["at"])).isoformat()
            key = self._quote_key(event.get("symbol"), normalized)
            if key not in self.state["quote_inventory"]:
                raise ValueError("INCOMPLETE_SAME_TIMESTAMP_BATCH_LATE_NEW_QUOTE")
        return super().process(event)

    def run(self, events: Iterable[dict]):
        if self.config.quote_mode == "Q0":
            return super().run(events)
        ordered = sorted(events, key=lambda e: (rules.aware(e["at"]), PRIORITY.get(e["kind"], 99), str(e["event_id"])))
        for event in ordered:
            self.process(event)
        self.flush_quote_batch()
        if self.checkpoint_path:
            self.save()
        return self.summary()

    def summary(self):
        result = super().summary()
        evaluations = [d for d in self.state["decisions"] if d.get("evaluation_kind") == "NEW_QUOTE_TIMESTAMP_BATCH"]
        limitations = copy.deepcopy(self.state["coverage_limitations"])
        # An adapter cannot accidentally hide a processed input gap by omitting
        # its optional declaration. This is a report label, never a trading gate.
        for gap in self.state["gaps"]:
            item = {"reason": gap.get("reason", "INPUT_DATA_GAP"), "symbol": gap.get("symbol"),
                    "at": gap.get("at"), "source": "PROCESSED_DATA_GAP_EVENT"}
            if item not in limitations:
                limitations.append(item)
        result.update(version=VERSION, inherited_kernel_version=LEGACY_VERSION,
            quote_mode=self.config.quote_mode,
            execution_hypothesis=Q1_HYPOTHESIS if self.config.quote_mode == "Q1" else "UNCHANGED_B1_1_FIXED_CLOCK",
            q1_new_quote_evaluations=len(evaluations),
            q1_pending_timestamp_batch=self.state["q1_batch"],
            coverage_status="COVERAGE_LIMITED" if limitations else "ADAPTER_COVERAGE_NOT_DECLARED",
            coverage_limitations=limitations,
            same_timestamp_contract="ALL_BOOKS_UPDATED_THEN_CURRENT_SPREAD_VOLUME_IDENTITY_RANK",
            current_free_feed_realtime_qualification="NOT_VERIFIED_NOT_AUTHORIZATION_TO_START_FORWARD")
        return _clean(result)

    def to_dict(self):
        value = super().to_dict()
        value["version"] = VERSION
        return value

    @classmethod
    def restore(cls, path, schedule):
        wrapper = json.loads(Path(path).read_text(encoding="utf-8"))
        value = wrapper["payload"]
        if wrapper["sha256"] != _hash(value) or value["version"] != VERSION:
            raise ValueError("CHECKPOINT_HASH_OR_VERSION_MISMATCH")
        engine = cls(schedule, ReplayConfig(**value["config"]), value["universe"], path)
        if _hash(engine.sessions) != _hash(value["sessions"]):
            raise ValueError("CHECKPOINT_CALENDAR_MISMATCH")
        engine.ledger, engine.state = Ledger.from_dict(value["ledger"]), value["state"]
        return engine

    def export(self, directory):
        result = super().export(directory)
        rows = [d for d in self.state["decisions"] if d.get("evaluation_kind") == "NEW_QUOTE_TIMESTAMP_BATCH"]
        encoded = [{k: _json(v) if isinstance(v, (dict, list, tuple)) else _clean(v) for k, v in row.items()} for row in rows]
        pd.DataFrame(encoded, columns=None if encoded else ["symbol", "decision_time", "status", "reason"]).to_csv(
            Path(directory)/"q1_quote_evaluations.csv", index=False, encoding="utf-8-sig")
        return result
