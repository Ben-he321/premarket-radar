"""Small, event-ordered futures engine for the frozen F0/F1 pilot.

Daily bars provide execution proxies, not proof of historical quote liquidity.
Stops without intraday timestamps release capital only at the close event; this
conservative convention prevents reusing money before its release is knowable.
No data fetch, broker action, fallback prices, stock settlement or parameter search.
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import asdict, fields, is_dataclass
from datetime import date, datetime
import hashlib
import heapq
import json
import math
from statistics import median

from .indicators import RollingFeatures
from .model import (Campaign, ContractSpec, EngineConfig, EngineResult, Intent,
                    Layer, MarketDay, SessionBar, RollInstruction)


class FuturesEngine:
    def __init__(self, specs: dict[str, ContractSpec], config: EngineConfig | None = None):
        self.specs = dict(specs)
        self.config = config or EngineConfig()
        self._validate_config()
        self.cash = self.config.initial_equity
        self.positions: dict[str, Campaign] = {}
        self.indicators: dict[str, RollingFeatures] = {}
        self.signal_contract: dict[str, str] = {}
        self.indicator_sessions: dict[str, date] = {}
        self.expected_signal_session: dict[str, date | None] = {}
        self.expected_contract_session: dict[str, date | None] = {}
        self.continuity_reset_session: dict[str, date] = {}
        self._session_stops: dict[tuple[str, str, date], tuple[str, float, bool]] = {}
        self.volumes = defaultdict(lambda: deque(maxlen=20))
        self.intents: dict[str, Intent] = {}
        self.add_intents: dict[str, Intent] = {}
        self.last_exit_session: dict[str, date] = {}
        self.result = EngineResult()
        self.last_session: date | None = None
        self.last_batch_hash: str | None = None
        self.last_event_at: datetime | None = None
        self._heap = []
        self._sequence = 0
        self._campaign_sequence = 0
        self._peak_equity = self.cash
        self._finished = False

    def _validate_config(self):
        c = self.config
        if c.version not in {"F0", "F1"}:
            raise ValueError("ONLY_FROZEN_F0_F1_ALLOWED")
        if (c.slippage_ticks, c.commission_multiplier) not in {(2, 1), (4, 1), (4, 2)}:
            raise ValueError("ONLY_THREE_FROZEN_COST_SCENARIOS_ALLOWED")
        frozen = EngineConfig()
        for name in ("initial_equity", "atr_period", "entry_period", "exit_period", "stop_atr",
                     "initial_risk_fraction", "layer_margin_cap", "campaign_margin_cap",
                     "portfolio_margin_fraction", "gross_leverage_cap", "max_markets",
                     "total_giveback_fraction", "campaign_giveback_fraction",
                     "metals_giveback_fraction", "liquidity_lookback", "participation_fraction"):
            if getattr(c, name) != getattr(frozen, name):
                raise ValueError("FROZEN_RULE_CHANGED:" + name)
        if c.initial_margin_fraction not in {0.1, 0.2} or c.maintenance_fraction_of_initial != 0.75:
            raise ValueError("UNFROZEN_MARGIN_ASSUMPTION")
        if c.margin_scenario not in {"ASSUMED_MARGIN_10_PERCENT", "ASSUMED_MARGIN_20_PERCENT", "VERIFIED_HISTORICAL"}:
            raise ValueError("UNKNOWN_MARGIN_EVIDENCE")
        if c.margin_scenario == "ASSUMED_MARGIN_10_PERCENT" and c.initial_margin_fraction != 0.1:
            raise ValueError("MARGIN_LABEL_MISMATCH")
        if c.margin_scenario == "ASSUMED_MARGIN_20_PERCENT" and c.initial_margin_fraction != 0.2:
            raise ValueError("MARGIN_LABEL_MISMATCH")
        if not c.allow_mock and (c.trading_start != date(2022, 1, 1)
                                 or c.trading_end != date(2025, 12, 31)
                                 or c.evaluation_start != date(2024, 1, 1)):
            raise ValueError("FROZEN_RESEARCH_DATES_CHANGED")

    @staticmethod
    def _aware(value):
        return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None

    def _bar_problem(self, bar, *, execution=False):
        if bar.is_mock and not self.config.allow_mock:
            return "MOCK_FORBIDDEN_IN_RESEARCH"
        if not all(self._aware(x) for x in (bar.opens_at, bar.closes_at, bar.available_at)):
            return "UNKNOWN_OR_NAIVE_AVAILABILITY"
        if not bar.opens_at < bar.closes_at <= bar.available_at:
            return "INVALID_SESSION_AVAILABILITY"
        if bar.next_session is not None and bar.next_session <= bar.session:
            return "INVALID_NEXT_EXCHANGE_SESSION"
        if bar.received_at is not None and not self._aware(bar.received_at):
            return "INVALID_RECEIPT_TIMESTAMP"
        if not bar.session_verified:
            return "EXCHANGE_SESSION_UNVERIFIED"
        if bar.status != "QUALIFIED":
            return bar.status
        prices = (bar.open, bar.high, bar.low, bar.close)
        if not all(isinstance(x, (int, float)) and math.isfinite(x) for x in prices):
            return "MISSING_OR_INVALID_PRICE"
        if not bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high:
            return "INVALID_OHLC_RELATION"
        if bar.volume is None:
            return "MISSING_VOLUME"
        if not isinstance(bar.volume, int) or bar.volume < 0:
            return "INVALID_VOLUME"
        if not bar.source_hash:
            return "MISSING_INPUT_VERSION"
        if bar.settlement is not None:
            if not math.isfinite(bar.settlement) or not self._aware(bar.settlement_available_at):
                return "UNKNOWN_SETTLEMENT_PUBLICATION"
            if bar.settlement_reference_at is None:
                if not self.config.allow_mock:
                    return "SETTLEMENT_REFERENCE_UNKNOWN_ACCOUNT_EVIDENCE_BLOCKED"
                if bar.settlement_available_at < bar.closes_at:
                    return "SETTLEMENT_REFERENCE_UNKNOWN_ACCOUNT_EVIDENCE_BLOCKED"
            elif (not self._aware(bar.settlement_reference_at)
                  or not bar.opens_at <= bar.settlement_reference_at <= bar.closes_at
                  or bar.settlement_reference_at > bar.settlement_available_at):
                return "INVALID_SETTLEMENT_REFERENCE_OR_PUBLICATION"
        elif execution and not self.config.allow_mock:
            return "SETTLEMENT_DATA_MISSING_ACCOUNT_EVIDENCE_BLOCKED"
        return None

    def _spec_problem(self, spec, session, at, *, entry=True):
        if not (spec.verified and spec.vendor_definition_verified and spec.calendar_verified):
            return "SPEC_OR_VENDOR_OR_CALENDAR_UNKNOWN"
        if spec.currency != "USD" or spec.quote_unit == "UNKNOWN":
            return "CURRENCY_OR_QUOTE_UNIT_UNKNOWN"
        allowed = {"GOLD": {"MGC", "1OZ"}, "COPPER": {"MHG"}, "OIL": {"MCL"},
                   "CORN": {"MZC"}, "EURUSD": {"M6E"}, "SP500": {"MES"}}
        if not self.config.allow_mock and spec.root not in allowed.get(spec.market, set()):
            return "OUTSIDE_FROZEN_EXECUTION_MARKET_ROOTS"
        if spec.multiplier <= 0 or spec.tick_size <= 0:
            return "INVALID_CONTRACT_UNITS"
        if not spec.listed <= session <= spec.last_trade:
            return "OUTSIDE_ACTUAL_CONTRACT_LIFETIME"
        if not spec.boundary_verified or spec.safe_exit_session is None:
            return "DELIVERY_BOUNDARY_UNKNOWN"
        if entry and session >= spec.safe_exit_session:
            return "DELIVERY_SAFETY_BOUNDARY"
        if self.config.margin_scenario == "VERIFIED_HISTORICAL":
            if (spec.initial_margin is None or spec.maintenance_margin is None
                    or not self._aware(spec.margin_asof) or spec.margin_asof > at
                    or spec.margin_valid_until is None or session > spec.margin_valid_until
                    or not 0 < spec.maintenance_margin <= spec.initial_margin):
                return "HISTORICAL_MARGIN_UNKNOWN_OR_FUTURE"
        if spec.commission_per_side is not None and (not math.isfinite(spec.commission_per_side)
                                                    or spec.commission_per_side < 0):
            return "INVALID_COMMISSION"
        return None

    def _skip(self, market, reason, at, **details):
        self.result.skips.append({"market": market, "reason": reason,
                                  "timestamp": at.isoformat() if hasattr(at, "isoformat") else str(at),
                                  **details})

    def _event(self, kind, at, **details):
        self.result.events.append({"kind": kind, "timestamp": at.isoformat(), **details})

    def _push(self, at, priority, kind, data):
        self._sequence += 1
        heapq.heappush(self._heap, (at, priority, self._sequence, kind, data))

    def process_batch(self, days: list[MarketDay] | tuple[MarketDay, ...]):
        """Queue one exchange-session batch; retain at most adjacent daily inputs.

        Repeating the immediately preceding identical batch is idempotent. An
        older/out-of-order or altered duplicate batch is rejected, not sorted by
        loading all history. The input adapter must enforce session continuity.
        """
        if self._finished:
            raise RuntimeError("ENGINE_FINISHED")
        if not days:
            raise ValueError("EMPTY_SESSION_BATCH")
        if len(days) > 6 or len({d.market for d in days}) != len(days):
            raise ValueError("DUPLICATE_OR_TOO_MANY_MARKETS")
        sessions = {d.signal.session for d in days}
        if len(sessions) != 1:
            raise ValueError("MIXED_SESSION_BATCH")
        session = next(iter(sessions))
        if not self.config.allow_mock and not date(2021, 1, 1) <= session <= date(2025, 12, 31):
            raise ValueError("OUTSIDE_FROZEN_INPUT_RANGE")
        for day in days:
            contracts = [bar.contract_id for bar in day.execution]
            if len(contracts) != len(set(contracts)):
                raise ValueError("DUPLICATE_EXECUTION_CONTRACT_SESSION")
        digest = hashlib.sha256(json.dumps([asdict(d) for d in days], default=str,
                                          sort_keys=True).encode()).hexdigest()
        if session == self.last_session and digest == self.last_batch_hash:
            return False
        if self.last_session is not None and session <= self.last_session:
            raise ValueError("OUT_OF_ORDER_OR_CONFLICTING_SESSION")
        earliest = min([d.signal.opens_at for d in days]
                       + [b.opens_at for d in days for b in d.execution])
        self._drain(before=earliest)
        self._check_continuity(days, earliest)
        self.last_session, self.last_batch_hash = session, digest
        self.result.input_hashes.append(digest)
        self.result.processed_sessions += 1
        qualified = []
        for day in days:
            problem = self._bar_problem(day.signal)
            if problem or not day.mapping_verified:
                self._skip(day.market, problem or "STANDARD_EXECUTION_MAPPING_UNKNOWN", session)
                self.intents.pop(day.market, None)
                self._expire_add(day.market, session, "ADD_INPUT_UNQUALIFIED_TIER_NOT_RETRIED")
                self.indicators.pop(day.market, None)
                continue
            bars = []
            for bar in day.execution:
                problem = self._bar_problem(bar, execution=True)
                if problem or bar.session != session:
                    self._skip(day.market, problem or "EXECUTION_SESSION_MISMATCH", session,
                               contract_id=bar.contract_id)
                    self.volumes.pop(bar.contract_id, None)
                    continue
                if bar.contract_id not in self.specs:
                    self._skip(day.market, "MISSING_CONTRACT_DEFINITION", session,
                               contract_id=bar.contract_id)
                    continue
                if self.specs[bar.contract_id].market != day.market:
                    raise ValueError("CROSS_MARKET_CONTRACT_MAPPING")
                bars.append(bar)
                if bar.settlement is not None:
                    self._push(bar.settlement_available_at, 3, "settlement", (day.market, bar))
            if not bars:
                self.intents.pop(day.market, None)
                self._expire_add(day.market, session, "ADD_EXECUTION_INPUT_MISSING_TIER_NOT_RETRIED")
                continue
            # Only contracts opening at a given actual UTC instant compete then.
            for opening in sorted({b.opens_at for b in bars}):
                self._push(opening, 1, "open", (day, tuple(b for b in bars if b.opens_at == opening)))
            for bar in bars:
                self._push(bar.available_at, 2, "bar_close", (day.market, bar))
            known = max([day.signal.available_at] + [b.available_at for b in bars])
            self._push(known, 4, "signal_close", (day, tuple(bars)))
            qualified.append(known)
        if qualified:
            self._push(max(qualified), 5, "daily", session)
        if qualified:
            self.result.status = "MOCK_ENGINEERING_RUN" if self.config.allow_mock else "EXECUTED"
            if self.result.coverage_status == "NOT_RUN":
                self.result.coverage_status = "NO_DETECTED_GAP_IN_SUPPLIED_CALENDAR_SEQUENCE"
        elif not self.result.daily_equity and not self._heap:
            self.result.status = "DATA_GATED_NOT_EXECUTED"
        return True

    def _check_continuity(self, days, at):
        """Respect the prior verified calendar's next session, including holidays.

        A missing expected session is not compressed out of a rolling window.
        Reset signal/liquidity warmup while preserving all actual account state;
        an already-held campaign's path over the gap is explicitly unknowable.
        """
        for day in days:
            issues = []
            if day.market in self.expected_signal_session:
                expected = self.expected_signal_session[day.market]
                if expected != day.signal.session:
                    issues.append({"input": "SIGNAL", "contract_id": day.signal.contract_id,
                                   "expected_session": str(expected) if expected else "UNKNOWN",
                                   "observed_session": str(day.signal.session)})
            for bar in day.execution:
                if bar.contract_id in self.expected_contract_session:
                    expected = self.expected_contract_session[bar.contract_id]
                    if expected != bar.session:
                        issues.append({"input": "EXECUTION", "contract_id": bar.contract_id,
                                       "expected_session": str(expected) if expected else "UNKNOWN",
                                       "observed_session": str(bar.session)})
                self.expected_contract_session[bar.contract_id] = (
                    bar.next_session if bar.session_verified else None)
            self.expected_signal_session[day.market] = (
                day.signal.next_session if day.signal.session_verified else None)
            if not issues:
                continue
            self.indicators.pop(day.market, None)
            self.indicator_sessions.pop(day.market, None)
            self.intents.pop(day.market, None)
            self._expire_add(day.market, day.signal.session, "CONTINUITY_GAP_ADD_TIER_NOT_RETRIED")
            self.continuity_reset_session[day.market] = day.signal.session
            for contract_id, spec in self.specs.items():
                if spec.market == day.market:
                    self.volumes.pop(contract_id, None)
            p = self.positions.get(day.market)
            evidence = {"market": day.market, "timestamp": at.isoformat(),
                        "status": "PERFORMANCE_VALIDITY_BLOCKER", "coverage": "UNKNOWN",
                        "reason": "EXPECTED_EXCHANGE_SESSION_MISSING_OR_INCONSISTENT",
                        "issues": issues, "warmup_restarted_at": str(day.signal.session),
                        "held_campaign_id": p.campaign_id if p else None,
                        "held_quantity": p.quantity if p else 0,
                        "action": "PRESERVE_ACCOUNT_NO_MISSING_DAY_FILL_RESET_SIGNAL_AND_VOLUME_WINDOWS"}
            self.result.coverage_status = "UNKNOWN_MISSING_OR_INCONSISTENT_SESSIONS"
            self.result.coverage_issues.append(evidence)
            self._event("SESSION_CONTINUITY_GAP", at, **{
                key: value for key, value in evidence.items() if key != "timestamp"})
            self._skip(day.market, evidence["reason"], at,
                       expected_observed_sessions=issues, existing_position_preserved=p is not None)

    def _drain(self, before=None):
        while self._heap and (before is None or self._heap[0][0] < before):
            at, priority, _, kind, data = heapq.heappop(self._heap)
            if self.last_event_at is not None and at < self.last_event_at:
                raise ValueError("EVENT_TIME_REVERSED")
            self.last_event_at = at
            if kind == "open":
                group = [data]
                while self._heap and self._heap[0][0] == at and self._heap[0][3] == "open":
                    group.append(heapq.heappop(self._heap)[4])
                self._opens(group, at)
            elif kind == "bar_close":
                self._bar_close(*data, at)
            elif kind == "signal_close":
                self._signal_close(*data, at)
            elif kind == "settlement":
                self._settle(*data, at)
            elif kind == "daily":
                self._snapshot(data, at)

    def run(self, batches):
        for days in batches:
            self.process_batch(days)
        return self.finish()

    def finish(self):
        if not self._finished:
            self._drain()
            self._finished = True
            self._event("END_MARK_TO_MARKET_NO_FORCED_LIQUIDATION", self.last_event_at,
                        open_campaigns=len(self.positions)) if self.last_event_at else None
            self.result.open_positions = [
                {"market": p.market, "campaign_id": p.campaign_id, "contract_id": p.contract_id,
                 "direction": p.direction, "quantity": p.quantity, "mark": p.mark,
                 "mark_at": p.mark_at.isoformat(), "stop": p.stop,
                 "unsettled_mark_to_market": self._unsettled(p),
                 "campaign_total_net_marked_profit": self._campaign_net(p),
                 "exit_pending": p.exit_pending}
                for p in sorted(self.positions.values(), key=lambda x: x.market)]
            self.result.reconciliation = reconcile_fills(self.result, self.specs,
                                                         self.config.initial_equity, self.equity())
        return self.result

    def _commission(self, spec, *, base=False):
        amount = spec.commission_per_side if spec.commission_per_side is not None else 2.0
        return amount * (1 if base else self.config.commission_multiplier)

    def _fill(self, price, side, spec):
        units = price / spec.tick_size
        rounded = math.ceil(units - 1e-10) if side > 0 else math.floor(units + 1e-10)
        return (rounded + side * self.config.slippage_ticks) * spec.tick_size

    def _margin(self, spec, price, *, maintenance=False):
        if self.config.margin_scenario == "VERIFIED_HISTORICAL":
            return spec.maintenance_margin if maintenance else spec.initial_margin
        initial = abs(price) * spec.multiplier * self.config.initial_margin_fraction
        return initial * (self.config.maintenance_fraction_of_initial if maintenance else 1)

    def equity(self):
        return self.cash + sum(self._unsettled(p) for p in self.positions.values())

    def _unsettled(self, p):
        mult = self.specs[p.contract_id].multiplier
        return sum((p.mark - layer.settlement_basis) * p.direction * layer.quantity * mult
                   for layer in p.layers)

    def _campaign_net(self, p):
        return p.realized_gross + self._unsettled(p) - p.fees

    def _open_layers_net(self, p):
        """Remaining layers' campaign profit, excluding sold layers' gains.

        Settlement merely moves cash and does not erase floating economic P&L.
        A physical roll carries the same layer's accumulated contract P&L, while
        profits from a genuinely reduced quantity cannot finance a losing add.
        """
        spec = self.specs[p.contract_id]
        return sum(((p.mark - layer.entry) * p.direction * spec.multiplier
                    + layer.realized_roll_per_contract - layer.fees_per_contract)
                   * layer.quantity for layer in p.layers)

    def _risk(self, p):
        spec = self.specs[p.contract_id]
        giveback = max(0.0, (p.mark - p.stop) * p.direction) * p.quantity * spec.multiplier
        return giveback + p.quantity * (self._commission(spec) + self.config.slippage_ticks * spec.tick_value)

    def _totals(self):
        equity = self.equity()
        margin = sum(self._margin(self.specs[p.contract_id], p.mark) * p.quantity for p in self.positions.values())
        maintenance = sum(self._margin(self.specs[p.contract_id], p.mark, maintenance=True) * p.quantity
                          for p in self.positions.values())
        gross = sum(abs(p.mark) * self.specs[p.contract_id].multiplier * p.quantity for p in self.positions.values())
        risks = {m: self._risk(p) for m, p in self.positions.items()}
        return equity, margin, maintenance, gross, risks

    def _breaches(self):
        c = self.config
        equity, margin, maintenance, gross, risks = self._totals()
        problems = []
        if equity <= 0:
            problems.append("NONPOSITIVE_EQUITY")
        if self.cash < -1e-8 or self.cash + 1e-8 < maintenance:
            problems.append("MARGIN_BREACH")
        if self.cash + 1e-8 < margin:
            problems.append("INITIAL_MARGIN_CASH_SHORTFALL")
        if margin > max(0, equity) * c.portfolio_margin_fraction + 1e-8:
            problems.append("PORTFOLIO_MARGIN_LIMIT")
        if gross > max(0, equity) * c.gross_leverage_cap + 1e-8:
            problems.append("GROSS_LEVERAGE_LIMIT")
        if sum(risks.values()) > max(0, equity) * c.total_giveback_fraction + 1e-8:
            problems.append("PORTFOLIO_GIVEBACK_LIMIT")
        if any(r > max(0, equity) * c.campaign_giveback_fraction + 1e-8 for r in risks.values()):
            problems.append("CAMPAIGN_GIVEBACK_LIMIT")
        if sum(r for m, r in risks.items() if m in {"GC", "HG", "GOLD", "COPPER"}) > max(0, equity) * c.metals_giveback_fraction + 1e-8:
            problems.append("METALS_GIVEBACK_LIMIT")
        for p in self.positions.values():
            if self._margin(self.specs[p.contract_id], p.mark) * p.quantity > c.campaign_margin_cap + 1e-8:
                problems.append("CAMPAIGN_MARGIN_LIMIT")
                break
        return problems

    def _liquidity_cap(self, contract_id):
        values = self.volumes[contract_id]
        if len(values) < self.config.liquidity_lookback or any(v <= 0 for v in values):
            return 0
        return math.floor(self.config.participation_fraction * median(values))

    def _size(self, spec, raw_price, stop, direction, at, *, campaign=None, max_qty=None):
        c = self.config
        entry = self._fill(raw_price, direction, spec)
        distance = (entry - stop) * direction
        if (raw_price - stop) * direction <= 0 or distance <= 0:
            return 0, "OPEN_CROSSED_STOP_OR_INVALID_DISTANCE"
        equity, used_margin, _, gross, risks = self._totals()
        if equity <= 0 or self._breaches():
            return 0, "EXISTING_PORTFOLIO_BREACH"
        margin = self._margin(spec, entry)
        if margin <= 0:
            return 0, "NONPOSITIVE_MARGIN"
        entry_cost = self._commission(spec)
        # Actual entry already embeds entry friction: add both commissions and
        # the baseline exit friction exactly once, never double-count entry slip.
        initial_loss = distance * spec.multiplier + 2 * self._commission(spec, base=True) + 2 * spec.tick_value
        risk_per = distance * spec.multiplier + self._commission(spec) + c.slippage_ticks * spec.tick_value
        campaign_margin = self._margin(spec, campaign.mark) * campaign.quantity if campaign else 0.0
        caps = {
            "PRIOR_LIQUIDITY_LIMIT": self._liquidity_cap(spec.contract_id),
            "LAYER_MARGIN_LIMIT": math.floor(c.layer_margin_cap / margin),
            "CAMPAIGN_MARGIN_LIMIT": math.floor(max(0, c.campaign_margin_cap - campaign_margin) / margin),
            "PORTFOLIO_MARGIN_LIMIT": math.floor(max(0, equity * c.portfolio_margin_fraction - used_margin) / margin),
            "CASH_MARGIN_LIMIT": math.floor(max(0, self.cash - used_margin) / (margin + entry_cost)),
            "GROSS_LEVERAGE_LIMIT": math.floor(max(0, equity * c.gross_leverage_cap - gross) / max(abs(entry) * spec.multiplier, 1e-12)),
            "PORTFOLIO_GIVEBACK_LIMIT": math.floor(max(0, equity * c.total_giveback_fraction - sum(risks.values())) / risk_per),
            "CAMPAIGN_GIVEBACK_LIMIT": math.floor(max(0, equity * c.campaign_giveback_fraction - risks.get(spec.market, 0)) / risk_per),
        }
        if spec.market in {"GC", "HG", "GOLD", "COPPER"}:
            current_metals = sum(r for m, r in risks.items() if m in {"GC", "HG", "GOLD", "COPPER"})
            caps["METALS_GIVEBACK_LIMIT"] = math.floor(max(0, equity * c.metals_giveback_fraction - current_metals) / risk_per)
        if campaign is None:
            caps["INITIAL_RISK_OR_MINIMUM_CONTRACT"] = math.floor(equity * c.initial_risk_fraction / initial_loss)
            if len(self.positions) >= c.max_markets:
                caps["MAX_FOUR_MARKETS"] = 0
        if max_qty is not None:
            caps["FIRST_LAYER_QUANTITY_CAP"] = max_qty
        reason, quantity = min(caps.items(), key=lambda item: (item[1], item[0]))
        quantity = max(0, quantity)
        # Fees and adverse entry fill immediately reduce equity. Recheck every
        # portfolio cap on that smaller equity, not only the pretrade balance.
        while quantity > 0:
            commission = entry_cost * quantity
            friction = abs(entry - raw_price) * spec.multiplier * quantity
            after_equity = equity - commission - friction
            after_margin = used_margin + self._margin(spec, raw_price) * quantity
            new_risk = (max(0, (raw_price - stop) * direction) * spec.multiplier
                        + self._commission(spec) + c.slippage_ticks * spec.tick_value) * quantity
            metals = sum(r for m, r in risks.items() if m in {"GC", "HG", "GOLD", "COPPER"})
            fits = (after_equity > 0 and self.cash - commission >= after_margin - 1e-8
                    and after_margin <= after_equity * c.portfolio_margin_fraction + 1e-8
                    and gross + abs(raw_price) * spec.multiplier * quantity <= after_equity * c.gross_leverage_cap + 1e-8
                    and sum(risks.values()) + new_risk <= after_equity * c.total_giveback_fraction + 1e-8
                    and risks.get(spec.market, 0) + new_risk <= after_equity * c.campaign_giveback_fraction + 1e-8
                    and (spec.market not in {"GC", "HG", "GOLD", "COPPER"}
                         or metals + new_risk <= after_equity * c.metals_giveback_fraction + 1e-8))
            if fits:
                break
            quantity -= 1
            reason = "POST_COST_EQUITY_CONSTRAINT"
        return quantity, reason

    def _trade(self, p, spec, quantity, raw, fill, at, kind, reason, tier=0):
        fee = quantity * self._commission(spec)
        self.cash -= fee
        p.fees += fee
        self.result.trades.append({"campaign_id": p.campaign_id, "market": p.market,
                                  "contract_id": spec.contract_id, "timestamp": at.isoformat(),
                                  "kind": kind, "direction": p.direction, "quantity": quantity,
                                  "raw_price": raw, "fill_price": fill, "fee": fee,
                                  "slippage_cost": abs(fill - raw) * quantity * spec.multiplier,
                                  "reason": reason, "tier": tier,
                                  "execution_evidence": "DAILY_BAR_PROXY",
                                  "commission_source": spec.commission_source})

    def _enter(self, intent, spec, bar, at):
        basis_map = dict(intent.execution_closes)
        if spec.contract_id not in basis_map:
            self._skip(spec.market, "PRIOR_STANDARD_EXECUTION_BASIS_UNKNOWN", at)
            return
        stop = intent.stop + basis_map[spec.contract_id] - intent.signal_close
        quantity, reason = self._size(spec, bar.open, stop, intent.direction, at)
        if quantity < 1:
            self._skip(spec.market, reason, at, contract_id=spec.contract_id, intent_hash=intent.signal_hash)
            return
        fill = self._fill(bar.open, intent.direction, spec)
        self._campaign_sequence += 1
        p = Campaign(f"{self.config.version}-{self._campaign_sequence:06d}", spec.market,
                     spec.contract_id, intent.direction,
                     [Layer(quantity, fill, fill, at, 0, fees_per_contract=self._commission(spec))],
                     fill, stop, stop, abs(fill - stop), quantity, fill, fill, at, bar.open, at)
        self.positions[spec.market] = p
        self._trade(p, spec, quantity, bar.open, fill, at, "ENTRY", "PRIOR_55_SESSION_BREAKOUT")
        self._event("INTENT_EXECUTED", at, market=spec.market,
                    signal_session=str(intent.signal_session), signal_known_at=intent.known_at.isoformat(),
                    input_hash=intent.signal_hash)

    def _close_quantity(self, p, quantity, raw, at, reason, session):
        spec = self.specs[p.contract_id]
        fill = self._fill(raw, -p.direction, spec)
        remaining = quantity
        gross = 0.0
        for layer in reversed(p.layers):
            take = min(remaining, layer.quantity)
            pnl = (fill - layer.settlement_basis) * p.direction * take * spec.multiplier
            gross += pnl
            layer.quantity -= take
            remaining -= take
            if not remaining:
                break
        if remaining:
            raise AssertionError("EXIT_EXCEEDS_POSITION")
        self.cash += gross
        p.realized_gross += gross
        self._trade(p, spec, quantity, raw, fill, at, "EXIT", reason)
        p.layers[:] = [layer for layer in p.layers if layer.quantity]
        if not p.layers:
            self.result.campaigns.append({"campaign_id": p.campaign_id, "market": p.market,
                                          "direction": p.direction, "opened_at": p.opened_at.isoformat(),
                                          "closed_at": at.isoformat(), "gross_profit": p.realized_gross,
                                          "fees": p.fees, "net_profit": p.realized_gross - p.fees,
                                          "exit_reason": reason,
                                          "consumed_add_tiers": sorted(p.consumed_tiers)})
            del self.positions[p.market]
            self.add_intents.pop(p.market, None)
            self.last_exit_session[p.market] = session

    def _opens(self, group, at):
        contexts = {day.market: (day, {b.contract_id: b for b in bars}) for day, bars in group}
        for market, p in list(self.positions.items()):
            if market not in contexts:
                continue
            day, bars = contexts[market]
            bar = bars.get(p.contract_id)
            if bar is None:
                self._skip(market, "OPEN_POSITION_CONTRACT_BAR_MISSING", at)
                continue
            if p.pending_stop is not None and p.pending_stop_known_at < at:
                p.stop = p.pending_stop
                p.pending_stop = None
                p.pending_stop_known_at = None
            p.mark, p.mark_at = bar.open, at
            spec = self.specs[p.contract_id]
            reason = None
            if (bar.open - p.stop) * p.direction <= 0:
                reason = "GAP_THROUGH_STOP"
            elif p.exit_pending:
                reason = p.exit_pending
            elif spec.safe_exit_session is not None and bar.session >= spec.safe_exit_session:
                reason = "DELIVERY_SAFETY_EXIT"
            # A known roll can replace boundary liquidation, but never a stop/exit.
            roll = day.roll
            if reason == "DELIVERY_SAFETY_EXIT" and roll and roll.old_contract == p.contract_id:
                reason = None
            if reason:
                if bar.tradable_open:
                    self._close_quantity(p, p.quantity, bar.open, at, reason, bar.session)
                else:
                    p.exit_pending = reason
                    self._skip(market, "EXIT_TRIGGERED_EXECUTION_UNPROVEN", at, trigger=reason)
            elif roll and roll.old_contract == p.contract_id:
                self._roll(p, roll, bars, at)
                if (market in self.positions and p.contract_id == spec.contract_id
                        and spec.safe_exit_session is not None and bar.session >= spec.safe_exit_session
                        and bar.tradable_open):
                    self._close_quantity(p, p.quantity, bar.open, at,
                                         "ROLL_BLOCKED_DELIVERY_SAFETY_EXIT", bar.session)
        self._reduce(contexts, at)
        # Existing positions' approved additions precede new campaign allocation.
        for market in sorted(contexts):
            p = self.positions.get(market)
            intent = self.add_intents.get(market)
            if p is None or intent is None:
                continue
            day, bars = contexts[market]
            bar = bars.get(p.contract_id)
            if bar is None or intent.known_at >= at or intent.signal_session >= bar.session:
                continue
            self.add_intents.pop(market, None)
            if intent.execution_session != bar.session:
                p.consumed_tiers.add(intent.tier)
                if intent.tier == 1:
                    p.tier_one_checked_session = intent.execution_session or bar.session
                self._skip(market, "MISSED_OR_UNKNOWN_NEXT_SESSION_ADD_NOT_BACKFILLED", at,
                           expected_session=str(intent.execution_session))
                continue
            self._add(p, intent, bar, at)
        candidates = []
        for market, (day, bars) in contexts.items():
            intent = self.intents.get(market)
            if intent is None or market in self.positions:
                continue
            if intent.known_at >= at or intent.signal_session >= day.signal.session:
                continue
            self.intents.pop(market, None)
            if intent.execution_session != day.signal.session:
                self._skip(market, "MISSED_OR_UNKNOWN_NEXT_SESSION_ENTRY_NOT_BACKFILLED", at,
                           expected_session=str(intent.execution_session))
                continue
            if self.last_exit_session.get(market, date.min) >= intent.signal_session:
                self._skip(market, "FRESH_COMPLETE_SESSION_REQUIRED_AFTER_EXIT", at)
                continue
            if not self.config.trading_start <= day.signal.session <= self.config.trading_end:
                continue
            eligible = []
            for bar in bars.values():
                spec = self.specs[bar.contract_id]
                problem = self._spec_problem(spec, bar.session, at)
                if problem or not bar.tradable_open:
                    self._skip(market, problem or "OPEN_EXECUTION_UNPROVEN", at, contract_id=spec.contract_id)
                    continue
                if self._liquidity_cap(spec.contract_id) < 1:
                    self._skip(market, "PRIOR_LIQUIDITY_LIMIT", at, contract_id=spec.contract_id)
                    continue
                prior_close = dict(intent.execution_closes).get(spec.contract_id)
                if prior_close is None:
                    continue
                stop = intent.stop + prior_close - intent.signal_close
                qty, _ = self._size(spec, bar.open, stop, intent.direction, at)
                if qty > 0:
                    # Selection uses previous known close, not future return or volume.
                    eligible.append((abs(prior_close) * spec.multiplier, spec.root, spec.contract_id, spec, bar))
                else:
                    _, why = self._size(spec, bar.open, stop, intent.direction, at)
                    self._skip(market, why, at, contract_id=spec.contract_id)
            if eligible:
                chosen = min(eligible, key=lambda x: x[:3])
                candidates.append((-intent.strength, market, intent, chosen[3], chosen[4]))
        for _, _, intent, spec, bar in sorted(candidates, key=lambda x: x[:2]):
            self._enter(intent, spec, bar, at)
        for market, (_, bars) in contexts.items():
            p = self.positions.get(market)
            if p is not None and p.contract_id in bars:
                b = bars[p.contract_id]
                self._session_stops[(market, p.contract_id, b.session)] = (
                    p.campaign_id, p.stop, bool(p.exit_pending and not b.tradable_open))
        self._record_margin(at)

    def _reduce(self, contexts, at):
        breaches = self._breaches()
        if not breaches:
            return
        self._event("MARGIN_BREACH" if "MARGIN_BREACH" in breaches else "HOLDING_LIMIT_BREACH",
                    at, reasons=breaches)
        def priority(p):
            urgency = self._margin(self.specs[p.contract_id], p.mark, maintenance=True) * p.quantity
            latest = (p.last_added_at or p.opened_at).timestamp()
            return (-urgency, -latest, p.market)
        for p in sorted(list(self.positions.values()), key=priority):
            if p.market not in contexts:
                continue
            _, bars = contexts[p.market]
            bar = bars.get(p.contract_id)
            if bar is None or not bar.tradable_open:
                self._skip(p.market, "DELEVERAGING_EXECUTION_UNPROVEN", at)
                continue
            while p.market in self.positions and self._breaches():
                self._close_quantity(p, 1, bar.open, at, "FROZEN_LIMIT_DELEVERAGING", bar.session)
            if not self._breaches():
                break

    def _add(self, p, intent, bar, at):
        tier = intent.tier
        if tier not in {1, 2}:
            raise ValueError("UNFROZEN_ADD_TIER")
        if tier in p.consumed_tiers:
            return
        p.consumed_tiers.add(tier)
        if tier == 1:
            p.tier_one_checked_session = bar.session
        self._event("ADD_TIER_CONSUMED", at, campaign_id=p.campaign_id, tier=tier)
        spec = self.specs[p.contract_id]
        problem = self._spec_problem(spec, bar.session, at)
        threshold = p.first_entry + p.direction * p.d0 * tier
        protected = p.first_entry + p.direction * p.d0 * (tier - 1)
        if (problem or not bar.tradable_open or p.exit_pending
                or self._open_layers_net(p) <= 0
                or (bar.open - threshold) * p.direction < 0
                or (p.stop - protected) * p.direction < -1e-9):
            self._skip(p.market, problem or "ADD_OPEN_PROFIT_OR_PROTECTION_RECHECK_FAILED", at,
                       campaign_id=p.campaign_id, tier=tier)
            return
        qty, reason = self._size(spec, bar.open, p.stop, p.direction, at, campaign=p, max_qty=p.q0)
        if qty < 1:
            self._skip(p.market, reason, at, campaign_id=p.campaign_id, tier=tier)
            return
        fill = self._fill(bar.open, p.direction, spec)
        p.layers.append(Layer(qty, fill, fill, at, tier, fees_per_contract=self._commission(spec)))
        p.last_added_at = at
        self._trade(p, spec, qty, bar.open, fill, at, "ADD", "FROZEN_FLOATING_PROFIT_TIER", tier)

    def _expire_add(self, market, session, reason):
        intent = self.add_intents.get(market)
        if intent is None or (intent.execution_session is not None and session < intent.execution_session):
            return
        self.add_intents.pop(market, None)
        p = self.positions.get(market)
        if p is not None:
            p.consumed_tiers.add(intent.tier)
            if intent.tier == 1:
                p.tier_one_checked_session = intent.execution_session or session
        self._skip(market, reason, session, tier=intent.tier,
                   expected_session=str(intent.execution_session))

    def _bar_close(self, market, bar, at):
        if bar.session >= self.continuity_reset_session.get(market, date.min):
            self.volumes[bar.contract_id].append(bar.volume)
        protection = self._session_stops.pop((market, bar.contract_id, bar.session), None)
        p = self.positions.get(market)
        if p is None or p.contract_id != bar.contract_id:
            return
        if (p.opened_at >= bar.closes_at or protection is None
                or protection[0] != p.campaign_id):
            self._skip(market, "BAR_PRECEDES_CURRENT_CAMPAIGN_OR_SESSION_PROTECTION_UNKNOWN", at)
            return
        stop = protection[1]
        touched = bar.low <= stop if p.direction > 0 else bar.high >= stop
        if protection[2]:
            self._skip(market, "UNRESOLVED_OPEN_EXIT_NEEDS_INTRADAY_QUOTES", at,
                       original_trigger=p.exit_pending)
            self._event("UNRESOLVED_OPEN_EXIT_NEEDS_INTRADAY_QUOTES", at,
                        market=market, status="PERFORMANCE_VALIDITY_BLOCKER",
                        action="DO_NOT_REFILL_FAILED_GAP_EXIT_AT_IDEAL_STOP")
            if bar.closes_at >= p.mark_at:
                p.mark, p.mark_at = bar.close, bar.closes_at
            return
        if p.mark_at > bar.closes_at:
            self._skip(market, "LATE_BAR_CANNOT_REPLACE_NEWER_MARK", at,
                       stale_session=str(bar.session), current_mark_at=p.mark_at.isoformat())
            if touched:
                p.exit_pending = "LATE_SESSION_STOP_REQUIRES_INTRADAY_REVIEW"
                self._event("LATE_SESSION_STOP_REQUIRES_INTRADAY_REVIEW", at,
                            market=market, status="PERFORMANCE_VALIDITY_BLOCKER",
                            action="NO_RETROACTIVE_FILL_EXIT_AT_NEXT_TRADABLE_OPEN")
            return
        p.mark, p.mark_at = bar.close, bar.closes_at
        if touched:
            if bar.tradable_stop and bar.low <= stop <= bar.high:
                self._close_quantity(p, p.quantity, stop, at, "DAILY_STOP_PROXY", bar.session)
            else:
                p.exit_pending = "UNEXECUTED_STOP"
                self._skip(market, "STOP_TRIGGERED_EXECUTION_UNPROVEN", at,
                           stop=stop, contract_id=p.contract_id)

    def _signal_close(self, day, bars, at):
        signal = day.signal
        market = day.market
        if signal.session < self.continuity_reset_session.get(market, date.min):
            self._skip(market, "SIGNAL_BEFORE_CONTINUITY_RESET_NOT_REUSED", at)
            return
        previous = self.signal_contract.get(market)
        indicator = self.indicators.setdefault(market, RollingFeatures())
        if signal.session <= self.indicator_sessions.get(market, date.min):
            self._skip(market, "OUT_OF_ORDER_SIGNAL_PUBLICATION_REQUIRES_REVIEW", at)
            self.indicators.pop(market, None)
            self.intents.pop(market, None)
            self.add_intents.pop(market, None)
            return
        if previous is not None and previous != signal.contract_id:
            roll = day.roll
            if (roll is None or roll.new_signal_contract != signal.contract_id
                    or roll.known_at >= signal.opens_at or not roll.evidence_hash):
                self._skip(market, "CAUSAL_SIGNAL_ROLL_EVIDENCE_MISSING", at)
                self.intents.pop(market, None)
                self.add_intents.pop(market, None)
                return
            indicator.shift(roll.signal_offset)
        self.signal_contract[market] = signal.contract_id
        self.indicator_sessions[market] = signal.session
        features = indicator.update(signal.high, signal.low, signal.close)
        p = self.positions.get(market)
        execution_closes = tuple((b.contract_id, b.close) for b in bars)
        if p is not None:
            execution = next((b for b in bars if b.contract_id == p.contract_id), None)
            if execution is None:
                self._skip(market, "POSITION_CLOSE_MAPPING_MISSING", at)
                return
            if execution.closes_at < p.opened_at:
                self._skip(market, "SIGNAL_PRECEDES_CURRENT_CAMPAIGN", at)
                return
            if p.completed_holding_sessions == 0:
                p.highest_close = p.lowest_close = execution.close
            else:
                p.highest_close = max(p.highest_close, execution.close)
                p.lowest_close = min(p.lowest_close, execution.close)
            p.completed_holding_sessions += 1
            if features.atr is not None:
                current = p.stop if p.pending_stop is None else p.pending_stop
                p.pending_stop = (max(current, p.highest_close - 2 * features.atr) if p.direction > 0
                                  else min(current, p.lowest_close + 2 * features.atr))
                p.pending_stop_known_at = at
            exit_signal = (p.direction > 0 and features.exit_low is not None and signal.close < features.exit_low
                           or p.direction < 0 and features.exit_high is not None and signal.close > features.exit_high)
            if exit_signal:
                p.exit_pending = "PRIOR_20_SESSION_CHANNEL_EXIT"
                self.add_intents.pop(market, None)
            elif self.config.version == "F1" and not p.exit_pending and features.atr is not None:
                tier = 1 if 1 not in p.consumed_tiers else 2 if 2 not in p.consumed_tiers else 0
                separate_day = (tier == 1 or p.tier_one_checked_session is not None
                                and signal.session > p.tier_one_checked_session)
                threshold = p.first_entry + p.direction * p.d0 * tier
                protected = p.first_entry + p.direction * p.d0 * (tier - 1)
                if (tier and separate_day and self._open_layers_net(p) > 0
                        and (execution.close - threshold) * p.direction >= 0
                        and ((p.pending_stop if p.pending_stop is not None else p.stop) - protected) * p.direction >= -1e-9):
                    self.add_intents[market] = Intent(market, p.direction, signal.session, at,
                                                     signal.close, features.atr, p.stop, 0,
                                                     signal.source_hash, execution_closes, tier,
                                                     signal.next_session)
            return
        if self.last_exit_session.get(market) == signal.session:
            return
        if features.atr is None or features.atr <= 0 or features.entry_high is None:
            return
        direction = 1 if signal.close > features.entry_high else -1 if signal.close < features.entry_low else 0
        if not direction:
            self.intents.pop(market, None)
            return
        channel = features.entry_high if direction > 0 else features.entry_low
        stop = signal.close - direction * 2 * features.atr
        self.intents[market] = Intent(market, direction, signal.session, at, signal.close,
                                      features.atr, stop, abs(signal.close - channel) / features.atr,
                                      signal.source_hash, execution_closes,
                                      execution_session=signal.next_session)
        self._event("SIGNAL_CREATED", at, market=market, direction=direction,
                    session=str(signal.session), signal_hash=signal.source_hash,
                    received_at=signal.received_at.isoformat() if signal.received_at else "UNKNOWN")

    def _settle(self, market, bar, at):
        p = self.positions.get(market)
        if p is None or p.contract_id != bar.contract_id:
            return
        spec = self.specs[p.contract_id]
        reference = bar.settlement_reference_at or bar.closes_at  # Mock-only explicit engineering fallback.
        amount = 0.0
        for layer in p.layers:
            if layer.created_at > reference or (layer.settled_through and layer.settled_through >= reference):
                continue
            amount += (bar.settlement - layer.settlement_basis) * p.direction * layer.quantity * spec.multiplier
            layer.settlement_basis = bar.settlement
            layer.settled_through = reference
        self.cash += amount
        p.realized_gross += amount
        if reference > p.mark_at:
            p.mark, p.mark_at = bar.settlement, reference
        self._event("VARIATION_MARGIN", at, market=market, contract_id=bar.contract_id,
                    session=str(bar.session), amount=amount,
                    settlement=bar.settlement, settlement_available_at=at.isoformat(),
                    settlement_reference_at=reference.isoformat(),
                    cash_posting_model="PUBLICATION_TIME_RESEARCH_PROXY_NOT_VERIFIED_BANK_POSTING")
        breaches = self._breaches()
        if breaches:
            self._event("MARGIN_BREACH" if "MARGIN_BREACH" in breaches else "HOLDING_LIMIT_BREACH",
                        at, reasons=breaches, action="FREEZE_ADDITIONS_REDUCE_AT_NEXT_TRADABLE_OPEN")

    def _roll(self, p, roll, bars, at):
        old_bar, new_bar = bars.get(roll.old_contract), bars.get(roll.new_contract)
        if (not self._aware(roll.known_at) or roll.known_at >= at or not roll.evidence_hash
                or old_bar is None or new_bar is None
                or not old_bar.tradable_open or not new_bar.tradable_open
                or roll.reason not in {"FIVE_SESSION_DELIVERY_BOUNDARY", "TWO_PRIOR_SESSION_VOLUME_CROSS"}):
            p.exit_pending = "ROLL_EVIDENCE_OR_EXECUTION_UNPROVEN"
            self._skip(p.market, "ROLL_EVIDENCE_OR_EXECUTION_UNPROVEN", at)
            return
        new = self.specs[roll.new_contract]
        old = self.specs[roll.old_contract]
        problem = self._spec_problem(new, new_bar.session, at)
        if problem or new.multiplier != old.multiplier or new.quote_unit != old.quote_unit:
            self._skip(p.market, problem or "ROLL_UNIT_CONVERSION_UNSUPPORTED", at)
            p.exit_pending = "ROLL_UNQUALIFIED"
            return
        qty = p.quantity
        new_margin = self._margin(new, new_bar.open) * qty
        new_cash_cost = qty * (self._commission(old) + self._commission(new))
        equity, total_margin, _, gross, risks = self._totals()
        old_fill = self._fill(old_bar.open, -p.direction, old)
        new_fill = self._fill(new_bar.open, p.direction, new)
        realized = sum((old_fill - layer.settlement_basis) * p.direction * layer.quantity * old.multiplier
                       for layer in p.layers)
        projected_cash = self.cash + realized - new_cash_cost
        cost = new_cash_cost + qty * (abs(old_fill - old_bar.open) * old.multiplier
                                     + abs(new_fill - new_bar.open) * new.multiplier)
        after_equity = equity - cost
        after_margin = total_margin - self._margin(old, p.mark) * qty + new_margin
        after_gross = gross - abs(p.mark) * old.multiplier * qty + abs(new_bar.open) * new.multiplier * qty
        after_risk = (max(0, (new_bar.open - p.stop - roll.execution_offset) * p.direction) * new.multiplier
                      + self._commission(new) + self.config.slippage_ticks * new.tick_value) * qty
        total_risk = sum(risks.values()) - risks[p.market] + after_risk
        metal_risk = sum(r for m, r in risks.items() if m in {"GC", "HG", "GOLD", "COPPER"})
        if p.market in {"GC", "HG", "GOLD", "COPPER"}:
            metal_risk += after_risk - risks[p.market]
        if (self._liquidity_cap(new.contract_id) < qty or new_margin > self.config.campaign_margin_cap
                or projected_cash < after_margin or after_equity <= 0
                or after_margin > after_equity * self.config.portfolio_margin_fraction
                or after_gross > after_equity * self.config.gross_leverage_cap
                or total_risk > after_equity * self.config.total_giveback_fraction
                or after_risk > after_equity * self.config.campaign_giveback_fraction
                or metal_risk > after_equity * self.config.metals_giveback_fraction):
            self._skip(p.market, "ROLL_MARGIN_OR_LIQUIDITY_REJECTED_EXIT_OLD", at)
            self._close_quantity(p, qty, old_bar.open, at, "ROLL_REENTRY_CONSTRAINT", old_bar.session)
            return
        self.cash += realized
        p.realized_gross += realized
        self._trade(p, old, qty, old_bar.open, old_fill, at, "ROLL_EXIT", roll.reason)
        self._trade(p, new, qty, new_bar.open, new_fill, at, "ROLL_ENTRY", roll.reason)
        for layer in p.layers:
            layer.realized_roll_per_contract += (old_fill - layer.entry) * p.direction * old.multiplier
            layer.entry = new_fill
            layer.fees_per_contract += self._commission(old) + self._commission(new)
            layer.settlement_basis = new_fill
            layer.created_at = at
            layer.settled_through = None
        p.first_entry += roll.execution_offset
        p.initial_stop += roll.execution_offset
        p.stop += roll.execution_offset
        p.highest_close += roll.execution_offset
        p.lowest_close += roll.execution_offset
        if p.pending_stop is not None:
            p.pending_stop += roll.execution_offset
        p.contract_id = new.contract_id
        p.mark, p.mark_at = new_bar.open, at
        self.result.rolls.append({"campaign_id": p.campaign_id, "timestamp": at.isoformat(),
                                 "old_contract": old.contract_id, "new_contract": new.contract_id,
                                 "old_raw": old_bar.open, "new_raw": new_bar.open,
                                 "old_fill": old_fill, "new_fill": new_fill, "quantity": qty,
                                 "causal_offset": roll.execution_offset, "offset_known_at": roll.known_at.isoformat(),
                                 "evidence_hash": roll.evidence_hash, "opening_gap_is_profit": False})

    def _record_margin(self, at):
        equity, initial, maintenance, gross, risks = self._totals()
        self.result.margin_path.append({"timestamp": at.isoformat(), "cash": self.cash,
                                        "equity": equity, "initial_margin": initial,
                                        "maintenance_margin": maintenance, "available_cash": self.cash - initial,
                                        "pending_margin": 0.0, "gross_notional": gross,
                                        "gross_leverage": gross / equity if equity > 0 else None,
                                        "giveback_to_stops": sum(risks.values()),
                                        "margin_evidence": self.config.margin_scenario,
                                        "breaches": self._breaches()})

    def _snapshot(self, session, at):
        equity = self.equity()
        self._peak_equity = max(self._peak_equity, equity)
        self.result.daily_equity.append({"session": str(session), "timestamp": at.isoformat(),
                                          "cash": self.cash, "unsettled_mark_to_market": equity - self.cash,
                                          "equity": equity, "net_profit": equity - self.config.initial_equity,
                                          "open_campaigns": len(self.positions),
                                          "closed_campaigns": len(self.result.campaigns),
                                          "drawdown": equity / self._peak_equity - 1,
                                          "cash_interest": 0.0, "external_fixed_fees_included": False})
        self._record_margin(at)
        if self.cash < -1e-8:
            self._event("NEGATIVE_CASH_FUNDING_UNKNOWN", at,
                        cash=self.cash, status="PERFORMANCE_VALIDITY_BLOCKER",
                        action="NO_NEW_ENTRIES_NO_FICTITIOUS_CASH_INJECTION")

    def checkpoint(self) -> dict:
        """JSON-safe bounded engine state, including pending event boundaries.

        Caller writes atomically and retains the input cursor/hash. This is not
        a market-data archive and does not include any credential configuration.
        """
        indicators = {}
        for market, value in self.indicators.items():
            indicators[market] = {"history": list(value.history), "seed_tr": list(value.seed_tr),
                                  "previous_close": value.previous_close, "atr": value.atr}
        body = _encode({"specs": self.specs, "config": self.config, "cash": self.cash,
                        "positions": self.positions, "indicators": indicators,
                        "signal_contract": self.signal_contract,
                        "indicator_sessions": self.indicator_sessions,
                        "expected_signal_session": self.expected_signal_session,
                        "expected_contract_session": self.expected_contract_session,
                        "continuity_reset_session": self.continuity_reset_session,
                        "session_stops": [[list(key), value] for key, value in self._session_stops.items()],
                        "volumes": {k: list(v) for k, v in self.volumes.items()},
                        "intents": self.intents, "add_intents": self.add_intents,
                        "last_exit_session": self.last_exit_session, "result": self.result,
                        "last_session": self.last_session, "last_batch_hash": self.last_batch_hash,
                        "last_event_at": self.last_event_at, "heap": self._heap,
                        "sequence": self._sequence, "campaign_sequence": self._campaign_sequence,
                        "peak_equity": self._peak_equity, "finished": self._finished})
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return {"schema": "F0_ENGINE_STATE_V1", "sha256": digest, "state": body}

    @classmethod
    def restore(cls, checkpoint: dict):
        if checkpoint.get("schema") != "F0_ENGINE_STATE_V1":
            raise ValueError("UNKNOWN_CHECKPOINT_SCHEMA")
        body = checkpoint["state"]
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if digest != checkpoint.get("sha256"):
            raise ValueError("CHECKPOINT_HASH_MISMATCH")
        state = _decode(body)
        obj = cls(state["specs"], state["config"])
        for name in ("cash", "positions", "signal_contract", "indicator_sessions",
                     "expected_signal_session", "expected_contract_session", "continuity_reset_session",
                     "intents", "add_intents",
                     "last_exit_session", "result", "last_session", "last_batch_hash", "last_event_at"):
            setattr(obj, name, state[name])
        obj._session_stops = {tuple(key): tuple(value) for key, value in state["session_stops"]}
        for market, values in state["indicators"].items():
            indicator = RollingFeatures()
            indicator.history.extend(tuple(row) for row in values["history"])
            indicator.seed_tr.extend(values["seed_tr"])
            indicator.previous_close, indicator.atr = values["previous_close"], values["atr"]
            obj.indicators[market] = indicator
        for contract_id, values in state["volumes"].items():
            obj.volumes[contract_id].extend(values)
        obj._heap = state["heap"]
        heapq.heapify(obj._heap)
        obj._sequence = state["sequence"]
        obj._campaign_sequence = state["campaign_sequence"]
        obj._peak_equity = state["peak_equity"]
        obj._finished = state["finished"]
        return obj


_TYPES = {kind.__name__: kind for kind in (ContractSpec, EngineConfig, EngineResult,
          Campaign, Layer, Intent, MarketDay, SessionBar, RollInstruction)}


def _encode(value):
    if isinstance(value, datetime):
        return {"__datetime__": value.isoformat()}
    if isinstance(value, date):
        return {"__date__": value.isoformat()}
    if is_dataclass(value):
        return {"__class__": type(value).__name__, "fields": {
            f.name: _encode(getattr(value, f.name)) for f in fields(value)}}
    if isinstance(value, tuple):
        return {"__tuple__": [_encode(item) for item in value]}
    if isinstance(value, set):
        return {"__set__": [_encode(item) for item in sorted(value)]}
    if isinstance(value, list):
        return [_encode(item) for item in value]
    if isinstance(value, dict):
        return {key: _encode(item) for key, item in value.items()}
    return value


def _decode(value):
    if isinstance(value, list):
        return [_decode(item) for item in value]
    if not isinstance(value, dict):
        return value
    if "__datetime__" in value:
        return datetime.fromisoformat(value["__datetime__"])
    if "__date__" in value:
        return date.fromisoformat(value["__date__"])
    if "__class__" in value:
        return _TYPES[value["__class__"]](**{key: _decode(item) for key, item in value["fields"].items()})
    if "__tuple__" in value:
        return tuple(_decode(item) for item in value["__tuple__"])
    if "__set__" in value:
        return set(_decode(item) for item in value["__set__"])
    return {key: _decode(item) for key, item in value.items()}


def reconcile_fills(result: EngineResult, specs: dict[str, ContractSpec], initial_equity: float,
                    actual_final_equity: float) -> dict:
    """Independent FIFO ticket reconstruction, with no variation-margin inputs.

    The engine accounts using per-layer settlement bases and reduces newest
    layers first. This checker instead reconstructs original fill-to-fill FIFO
    P&L, then marks remaining tickets. Agreement therefore detects double-counted
    variation margin, notional cash debits and roll-gap-as-profit errors.
    """
    lots = defaultdict(deque)
    realized, fees = 0.0, 0.0
    for trade in result.trades:
        key = (trade["campaign_id"], trade["contract_id"])
        quantity = trade["quantity"]
        if not isinstance(quantity, int) or quantity < 1:
            raise AssertionError("NONINTEGER_OR_INVALID_FILL_QUANTITY")
        fees += trade["fee"]
        if trade["kind"] in {"ENTRY", "ADD", "ROLL_ENTRY"}:
            lots[key].append([quantity, trade["fill_price"], trade["direction"]])
            continue
        while quantity:
            if not lots[key]:
                raise AssertionError("RECONCILIATION_UNMATCHED_EXIT")
            lot = lots[key][0]
            take = min(quantity, lot[0])
            realized += ((trade["fill_price"] - lot[1]) * lot[2] * take
                         * specs[trade["contract_id"]].multiplier)
            lot[0] -= take
            quantity -= take
            if not lot[0]:
                lots[key].popleft()
    marks = {(p["campaign_id"], p["contract_id"]): p for p in result.open_positions}
    open_pnl = 0.0
    for key, values in lots.items():
        if not values:
            continue
        if key not in marks:
            raise AssertionError("RECONCILIATION_OPEN_MARK_MISSING")
        if sum(v[0] for v in values) != marks[key]["quantity"]:
            raise AssertionError("RECONCILIATION_OPEN_QUANTITY_MISMATCH")
        mark = marks[key]["mark"]
        open_pnl += sum((mark - price) * direction * qty * specs[key[1]].multiplier
                        for qty, price, direction in values)
    expected = initial_equity + realized + open_pnl - fees
    difference = actual_final_equity - expected
    return {"status": "PASS" if abs(difference) < 1e-7 else "FAIL",
            "method": "INDEPENDENT_FIFO_RAW_FILL_PNL_EXCLUDES_VARIATION_MARGIN",
            "initial_equity": initial_equity, "realized_fill_profit": realized,
            "unrealized_fill_profit": open_pnl, "all_commissions": fees,
            "expected_final_equity": expected, "engine_final_equity": actual_final_equity,
            "difference": difference, "provenance": "MOCK" if result.status == "MOCK_ENGINEERING_RUN" else "INPUT_AS_QUALIFIED"}
