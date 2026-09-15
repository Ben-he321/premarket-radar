"""Explicit synthetic engineering fixtures; never research performance data."""
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone
import json
import math
import unittest

from src.futures_f0.engine import FuturesEngine
from src.futures_f0.indicators import RollingFeatures
from src.futures_f0.model import (Campaign, ContractSpec, EngineConfig, Intent, Layer,
                                  MarketDay, RollInstruction, SessionBar)

UTC = timezone.utc
START = date(2022, 1, 1)


def spec(contract="MICRO1", market="ES", multiplier=1.0, **overrides):
    values = dict(contract_id=contract, market=market, root="MICRO", multiplier=multiplier,
                  tick_size=0.01, listed=date(2020, 1, 1), last_trade=date(2025, 12, 31),
                  safe_exit_session=date(2025, 12, 20), exchange="MOCK_EXCHANGE",
                  quote_unit="USD_PER_POINT", verified=True, calendar_verified=True,
                  vendor_definition_verified=True, boundary_verified=True, source="EXPLICIT_MOCK")
    values.update(overrides)
    return ContractSpec(**values)


def bar(index, close=100.0, *, contract="MICRO1", open_=None, low=None, high=None,
        settlement=None, settlement_delay=0, hour=10, **overrides):
    session = START + timedelta(days=index)
    opened = datetime.combine(session, datetime.min.time(), UTC) + timedelta(hours=hour)
    closed = opened + timedelta(hours=6)
    open_ = close if open_ is None else open_
    values = dict(contract_id=contract, session=session, opens_at=opened, closes_at=closed,
                  available_at=closed, open=open_, high=max(open_, close) + 1 if high is None else high,
                  low=min(open_, close) - 1 if low is None else low, close=close, volume=100000,
                  source_hash=f"MOCK-{contract}-{index}-{close}", settlement=settlement,
                  settlement_available_at=closed + timedelta(hours=settlement_delay) if settlement is not None else None,
                  session_verified=True, is_mock=True, next_session=session + timedelta(days=1))
    values.update(overrides)
    return SessionBar(**values)


def day(index, close=100.0, market="ES", **kwargs):
    b = bar(index, close, **kwargs)
    signal = replace(b, contract_id="STANDARD" + market)
    return MarketDay(market, signal, (b,), mapping_verified=True, mapping_source="EXPLICIT_MOCK")


def cfg(**kwargs):
    return EngineConfig(allow_mock=True, **kwargs)


def fixture(direction=1, length=25):
    days = [[day(i)] for i in range(55)]
    previous = 100.0
    for offset in range(length):
        close = 102 + 2 * offset if direction > 0 else 98 - 2 * offset
        days.append([day(55 + offset, close, open_=previous, settlement=close)])
        previous = close
    return days


class IndicatorTests(unittest.TestCase):
    def test_channels_exclude_current_bar_and_wilder_seed(self):
        f = RollingFeatures()
        for _ in range(55):
            result = f.update(101, 99, 100)
        result = f.update(112, 100, 111)
        self.assertEqual(result.entry_high, 101)
        self.assertEqual(result.exit_low, 99)
        self.assertAlmostEqual(result.atr, (19 * 2 + 12) / 20)
        self.assertEqual(len(f.history), 55)

    def test_causal_shift_preserves_atr_and_converts_channels(self):
        f = RollingFeatures()
        for _ in range(55):
            f.update(101, 99, 100)
        f.shift(20)
        result = f.update(123, 119, 122)
        self.assertEqual(result.entry_high, 121)
        self.assertAlmostEqual(result.atr, 2.1)


class EngineTests(unittest.TestCase):
    def test_long_and_short_actual_engine_paths(self):
        for direction in (1, -1):
            engine = FuturesEngine({"MICRO1": spec()}, cfg())
            result = engine.run(fixture(direction))
            entries = [t for t in result.trades if t["kind"] == "ENTRY"]
            self.assertGreater(len(entries), 0)
            self.assertEqual(entries[0]["direction"], direction)
            self.assertEqual(entries[0]["timestamp"], bar(56).opens_at.isoformat())
            self.assertTrue(all(isinstance(t["quantity"], int) for t in result.trades))
            self.assertEqual(result.status, "MOCK_ENGINEERING_RUN")

    def test_production_rejects_mock_no_empty_account_result(self):
        result = FuturesEngine({"MICRO1": spec()}).run([[day(0)]])
        self.assertEqual(result.status, "DATA_GATED_NOT_EXECUTED")
        self.assertEqual(result.daily_equity, [])
        self.assertIn("MOCK_FORBIDDEN_IN_RESEARCH", [s["reason"] for s in result.skips])

    def test_unknown_boundary_never_enters(self):
        result = FuturesEngine({"MICRO1": spec(safe_exit_session=None, boundary_verified=False)}, cfg()).run(fixture())
        self.assertEqual(result.trades, [])
        self.assertIn("DELIVERY_BOUNDARY_UNKNOWN", [s["reason"] for s in result.skips])

    def test_no_prelisting_micro_substitution(self):
        result = FuturesEngine({"MICRO1": spec(listed=date(2025, 1, 1))}, cfg()).run(fixture())
        self.assertEqual(result.trades, [])
        self.assertIn("OUTSIDE_ACTUAL_CONTRACT_LIFETIME", [s["reason"] for s in result.skips])

    def test_gap_stop_uses_adverse_open_and_no_same_day_reentry(self):
        days = fixture(length=3)
        days.append([day(58, 90, open_=90, high=91, low=89, settlement=90)])
        result = FuturesEngine({"MICRO1": spec()}, cfg()).run(days)
        gap = [t for t in result.trades if t["reason"] == "GAP_THROUGH_STOP"]
        self.assertEqual(len(gap), 1)
        self.assertAlmostEqual(gap[0]["fill_price"], 89.98)
        same_day_entries = [t for t in result.trades if t["kind"] == "ENTRY" and t["timestamp"].startswith(str(bar(58).session))]
        self.assertEqual(same_day_entries, [])

    def test_futures_variation_and_exit_independent_money_reconciliation(self):
        days = fixture(length=6)
        days.append([day(61, 90, open_=90, high=91, low=89, settlement=90)])
        engine = FuturesEngine({"MICRO1": spec()}, cfg())
        result = engine.run(days)
        entry = next(t for t in result.trades if t["kind"] == "ENTRY")
        exits = [t for t in result.trades if t["kind"] == "EXIT"]
        gross = sum((t["fill_price"] - entry["fill_price"]) * t["quantity"] for t in exits)
        fees = sum(t["fee"] for t in result.trades)
        self.assertAlmostEqual(engine.cash, 11500 + gross - fees, places=7)
        self.assertAlmostEqual(sum(c["net_profit"] for c in result.campaigns), gross - fees, places=7)
        self.assertTrue(any(e["kind"] == "VARIATION_MARGIN" for e in result.events))
        # No full-notional cash purchase: entry only pays its commission.
        first_mark = next(m for m in result.margin_path if m["timestamp"] == entry["timestamp"])
        self.assertAlmostEqual(first_mark["cash"], 11500 - entry["fee"])

    def test_settlement_not_available_before_publication(self):
        engine = FuturesEngine({"MICRO1": spec()}, cfg())
        days = fixture(length=3)
        delayed = days[-1][0]
        b = replace(delayed.execution[0], settlement_available_at=delayed.execution[0].closes_at + timedelta(hours=20))
        days[-1] = [replace(delayed, execution=(b,))]
        result = engine.run(days)
        last = [e for e in result.events if e["kind"] == "VARIATION_MARGIN"][-1]
        self.assertEqual(last["timestamp"], b.settlement_available_at.isoformat())

    def test_checkpoint_resume_idempotency_and_hash_gate(self):
        days = fixture(length=25)
        baseline_engine = FuturesEngine({"MICRO1": spec()}, cfg(version="F1"))
        baseline = baseline_engine.run(days)
        engine = FuturesEngine({"MICRO1": spec()}, cfg(version="F1"))
        for values in days[:62]:
            engine.process_batch(values)
        self.assertFalse(engine.process_batch(days[61]))
        cp = json.loads(json.dumps(engine.checkpoint()))
        resumed = FuturesEngine.restore(cp)
        result = resumed.run(days[62:])
        self.assertEqual(asdict(result), asdict(baseline))
        self.assertEqual(resumed.checkpoint(), baseline_engine.checkpoint())
        cp["sha256"] = "invalid"
        with self.assertRaisesRegex(ValueError, "HASH_MISMATCH"):
            FuturesEngine.restore(cp)

    def test_changed_duplicate_and_old_session_rejected(self):
        engine = FuturesEngine({"MICRO1": spec()}, cfg())
        engine.process_batch([day(0)])
        with self.assertRaisesRegex(ValueError, "OUT_OF_ORDER"):
            engine.process_batch([day(0, 101)])

    def test_no_current_volume_for_sizing(self):
        days = fixture(length=3)
        days = [[replace(d, execution=tuple(replace(b, volume=1) for b in d.execution)) for d in batch] for batch in days]
        last = days[-1][0]
        days[-1] = [replace(last, execution=(replace(last.execution[0], volume=10000000),))]
        result = FuturesEngine({"MICRO1": spec()}, cfg()).run(days)
        self.assertFalse(result.trades)
        self.assertIn("PRIOR_LIQUIDITY_LIMIT", [s["reason"] for s in result.skips])

    def test_corn_cents_multiplier_risk_is_not_100x_smaller(self):
        engine = FuturesEngine({"MICRO1": spec(market="ZC", multiplier=10.0, quote_unit="US_CENTS_PER_BUSHEL")}, cfg())
        engine.volumes["MICRO1"].extend([100000] * 20)
        qty, _ = engine._size(engine.specs["MICRO1"], 500, 498, 1, bar(0).opens_at)
        # Adverse execution makes margin $500.02: two exceed the $1000 layer cap.
        self.assertEqual(qty, 1)

    def test_adds_bounded_and_rejected_tier_consumed_once(self):
        engine = FuturesEngine({"MICRO1": spec()}, cfg(version="F1"))
        result = engine.run(fixture(length=25))
        adds = [t for t in result.trades if t["kind"] == "ADD"]
        self.assertLessEqual(len(adds), 2)
        first = next(t for t in result.trades if t["kind"] == "ENTRY")
        self.assertTrue(all(t["quantity"] <= first["quantity"] for t in adds))
        consumed = [e for e in result.events if e["kind"] == "ADD_TIER_CONSUMED"]
        self.assertEqual(len(consumed), len({(e["campaign_id"], e["tier"]) for e in consumed}))
        # Explicit bad-open check consumes tier1, so a later better price is not retried.
        e = FuturesEngine({"MICRO1": spec()}, cfg(version="F1"))
        at = bar(1).opens_at
        p = Campaign("test", "ES", "MICRO1", 1, [Layer(1, 100, 100, at, 0)],
                     100, 96, 100, 4, 1, 110, 100, at, 103, at)
        e.positions["ES"] = p
        intent = Intent("ES", 1, START, at - timedelta(days=1), 110, 2, 100, 1, "MOCK", (), 1)
        e._add(p, intent, bar(2, 103), bar(2).opens_at)
        e._add(p, intent, bar(3, 110), bar(3).opens_at)
        self.assertEqual(p.consumed_tiers, {1})
        self.assertFalse(e.result.trades)

    def test_stop_trigger_without_execution_not_filled(self):
        days = fixture(length=3)
        days.append([day(58, 100, open_=106, high=107, low=90, tradable_stop=False)])
        engine = FuturesEngine({"MICRO1": spec()}, cfg())
        result = engine.run(days)
        self.assertIn("STOP_TRIGGERED_EXECUTION_UNPROVEN", [s["reason"] for s in result.skips])
        self.assertTrue(engine.positions)

    def test_later_market_close_not_used_at_earlier_market_open(self):
        specs = {"MICRO1": spec(), "OTHER": spec("OTHER", "CL")}
        batches = []
        for i in range(58):
            close = 100 if i < 55 else 102 + (i - 55) * 2
            a = day(i, close, hour=10)
            b = day(i, close, market="CL", contract="OTHER", hour=15)
            batches.append([a, b])
        engine = FuturesEngine(specs, cfg())
        result = engine.run(batches)
        times = [datetime.fromisoformat(e["timestamp"]) for e in result.events]
        self.assertEqual(times, sorted(times))
        for trade in result.trades:
            if trade["kind"] == "ENTRY":
                expected_hour = 10 if trade["market"] == "ES" else 15
                self.assertEqual(datetime.fromisoformat(trade["timestamp"]).hour, expected_hour)

    def test_realized_roll_gap_not_profit_and_campaign_preserved(self):
        specs = {"OLD": spec("OLD"), "NEW": spec("NEW")}
        engine = FuturesEngine(specs, cfg())
        at = bar(1).opens_at
        p = Campaign("C1", "ES", "OLD", 1, [Layer(2, 100, 100, at, 0)],
                     100, 96, 98, 4, 2, 104, 100, at, 104, at)
        engine.positions["ES"] = p
        engine.volumes["NEW"].extend([100000] * 20)
        old, new = bar(2, 104, contract="OLD"), bar(2, 124, contract="NEW")
        roll = RollInstruction("ES", "OLD", "NEW", at, 20,
                               "TWO_PRIOR_SESSION_VOLUME_CROSS", "MOCK_PRIOR_CLOSE_PAIR")
        equity_before = engine.equity()
        engine._roll(p, roll, {"OLD": old, "NEW": new}, old.opens_at)
        expected_cost = 2 * (4 + .04)
        self.assertAlmostEqual(engine.equity(), equity_before - expected_cost)
        self.assertEqual(p.campaign_id, "C1")
        self.assertEqual(p.first_entry, 120)
        self.assertEqual(p.stop, 118)
        self.assertEqual(p.d0, 4)
        self.assertFalse(engine.result.rolls[0]["opening_gap_is_profit"])

    def test_unknown_signal_roll_does_not_invent_continuity(self):
        days = fixture(length=3)
        changed = days[-1][0]
        days[-1] = [replace(changed, signal=replace(changed.signal, contract_id="DIFFERENT"))]
        result = FuturesEngine({"MICRO1": spec()}, cfg()).run(days)
        self.assertIn("CAUSAL_SIGNAL_ROLL_EVIDENCE_MISSING", [s["reason"] for s in result.skips])

    def test_frozen_config_cannot_search_other_periods(self):
        with self.assertRaisesRegex(ValueError, "FROZEN_RULE_CHANGED"):
            FuturesEngine({}, cfg(entry_period=30))

    def test_future_margin_table_not_used(self):
        specs = {"MICRO1": spec(initial_margin=10, maintenance_margin=7.5,
                                margin_asof=datetime(2026, 1, 1, tzinfo=UTC))}
        result = FuturesEngine(specs, cfg(margin_scenario="VERIFIED_HISTORICAL")).run(fixture())
        self.assertFalse(result.trades)
        self.assertIn("HISTORICAL_MARGIN_UNKNOWN_OR_FUTURE", [s["reason"] for s in result.skips])

    def test_production_dates_and_settlement_evidence_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "FROZEN_RESEARCH_DATES"):
            FuturesEngine({}, EngineConfig(trading_end=date(2026, 1, 1)))
        e = FuturesEngine({})
        real_shaped_mock = replace(bar(0), is_mock=False)
        self.assertEqual(e._bar_problem(real_shaped_mock, execution=True),
                         "SETTLEMENT_DATA_MISSING_ACCOUNT_EVIDENCE_BLOCKED")

    def test_missing_calendar_next_session_never_backfills_entry(self):
        days = fixture(length=3)
        days[55] = [replace(days[55][0], signal=replace(days[55][0].signal, next_session=None))]
        result = FuturesEngine({"MICRO1": spec()}, cfg()).run(days[:57])
        self.assertFalse(result.trades)
        self.assertIn("EXPECTED_EXCHANGE_SESSION_MISSING_OR_INCONSISTENT",
                      [s["reason"] for s in result.skips])

    def test_omitted_expected_session_does_not_execute_old_intent_later(self):
        days = fixture(length=3)
        result = FuturesEngine({"MICRO1": spec()}, cfg()).run(days[:56] + days[57:])
        self.assertFalse(result.trades)
        self.assertIn("EXPECTED_EXCHANGE_SESSION_MISSING_OR_INCONSISTENT",
                      [s["reason"] for s in result.skips])

    def test_late_previous_bar_does_not_apply_new_trail_retroactively(self):
        days = fixture(length=3)
        previous = days[56][0]
        available = bar(57).opens_at + timedelta(hours=2)
        previous_bar = replace(previous.execution[0], available_at=available, settlement=None,
                               settlement_available_at=None)
        days[56] = [replace(previous, signal=replace(previous.signal, available_at=available),
                            execution=(previous_bar,))]
        current = days[57][0]
        current_bar = replace(current.execution[0], low=99)
        days[57] = [replace(current, execution=(current_bar,))]
        engine = FuturesEngine({"MICRO1": spec()}, cfg())
        result = engine.run(days)
        self.assertTrue(engine.positions)
        self.assertFalse([t for t in result.trades if t["kind"] == "EXIT"])
        self.assertIn("LATE_BAR_CANNOT_REPLACE_NEWER_MARK", [s["reason"] for s in result.skips])

    def test_late_bar_stop_is_blocker_not_retroactive_fill(self):
        days = fixture(length=3)
        previous = days[56][0]
        available = bar(57).opens_at + timedelta(hours=2)
        previous_bar = replace(previous.execution[0], available_at=available, low=90,
                               settlement=None, settlement_available_at=None)
        days[56] = [replace(previous, signal=replace(previous.signal, available_at=available),
                            execution=(previous_bar,))]
        engine = FuturesEngine({"MICRO1": spec()}, cfg())
        result = engine.run(days)
        self.assertTrue(engine.positions)
        self.assertFalse([t for t in result.trades if t["kind"] == "EXIT"])
        self.assertTrue(any(e["kind"] == "LATE_SESSION_STOP_REQUIRES_INTRADAY_REVIEW" for e in result.events))

    def test_first_trailing_extreme_uses_completed_close_not_entry_gap(self):
        engine = FuturesEngine({"MICRO1": spec()}, cfg())
        at = bar(60).opens_at
        p = Campaign("GAP", "ES", "MICRO1", 1, [Layer(1, 120, 120, at, 0)],
                     120, 105, 105, 15, 1, 120, 120, at, 112, at)
        engine.positions["ES"] = p
        indicator = RollingFeatures()
        for _ in range(55):
            indicator.update(113, 111, 112)
        engine.indicators["ES"] = indicator
        d = day(60, 112, open_=120)
        # Signal and execution micro basis match, but signal ATR remains a separate input.
        d = replace(d, signal=replace(d.signal, open=112, high=113, low=111))
        engine._signal_close(d, d.execution, d.signal.available_at)
        self.assertEqual(p.highest_close, 112)
        self.assertEqual(p.pending_stop, 108)
        self.assertEqual(p.stop, 105)  # New trailing protection starts only next session.

    def test_roll_residual_loss_cannot_overdraw_available_margin_cash(self):
        specs = {"OLD": spec("OLD"), "NEW": spec("NEW"), "OTHER": spec("OTHER", "CL")}
        e = FuturesEngine(specs, cfg())
        at = bar(1).opens_at
        p = Campaign("OLD_CAMPAIGN", "ES", "OLD", 1, [Layer(1, 100, 100, at, 0)],
                     100, 70, 70, 30, 1, 100, 80, at, 80, at)
        q = Campaign("OTHER_CAMPAIGN", "CL", "OTHER", 1, [Layer(1, 1, 1, at, 0)],
                     1, 1, 9999, 1, 1, 10000, 1, at, 10000, at)
        e.positions = {"ES": p, "CL": q}
        e.cash = 1115
        e.volumes["NEW"].extend([100000] * 20)
        old, new = bar(2, 80, contract="OLD"), bar(2, 1080, contract="NEW")
        roll = RollInstruction("ES", "OLD", "NEW", at, 1000,
                               "TWO_PRIOR_SESSION_VOLUME_CROSS", "MOCK")
        self.assertEqual(e._breaches(), [])
        e._roll(p, roll, {"OLD": old, "NEW": new}, old.opens_at)
        self.assertEqual(e.result.rolls, [])
        self.assertNotIn("ES", e.positions)  # Old contract exits rather than fund an unaffordable roll.
        self.assertGreaterEqual(e.cash, e._totals()[1])

    def test_margin_breach_reduces_only_when_execution_is_proven(self):
        e = FuturesEngine({"MICRO1": spec()}, cfg())
        at = bar(1).opens_at
        p = Campaign("BREACH", "ES", "MICRO1", 1, [Layer(20, 100, 100, at, 0)],
                     100, 90, 90, 10, 20, 100, 100, at, 100, at)
        e.positions["ES"] = p
        e.cash = 100
        d = day(2, 100, tradable_open=False)
        e._reduce({"ES": (d, {"MICRO1": d.execution[0]})}, d.signal.opens_at)
        self.assertEqual(p.quantity, 20)
        self.assertFalse(e.result.trades)
        self.assertTrue(any(x["kind"] == "MARGIN_BREACH" for x in e.result.events))
        d = day(3, 100)
        e._reduce({"ES": (d, {"MICRO1": d.execution[0]})}, d.signal.opens_at)
        self.assertLess(sum(p.quantity for p in e.positions.values()), 20)

    def test_shared_capital_max_four_markets_and_fixed_priority(self):
        markets = ["ES", "CL", "ZC", "6E", "GC", "HG"]
        specs = {market: spec(market, market) for market in markets}
        batches = []
        for i in range(57):
            close = 100 if i < 55 else 102 + (i - 55) * 2
            batches.append([day(i, close, market=market, contract=market) for market in reversed(markets)])
        engine = FuturesEngine(specs, cfg())
        result = engine.run(batches)
        entries = [t for t in result.trades if t["kind"] == "ENTRY"]
        self.assertEqual([t["market"] for t in entries], sorted(markets)[:4])
        self.assertEqual(len(engine.positions), 4)
        self.assertAlmostEqual(engine.cash, 11500 - sum(t["fee"] for t in entries))
        self.assertTrue(all(m["gross_leverage"] <= 10 for m in result.margin_path))

    def test_independent_fifo_reconciliation_with_adds_and_open_position(self):
        engine = FuturesEngine({"MICRO1": spec()}, cfg(version="F1"))
        result = engine.run(fixture(length=25))
        self.assertEqual(result.reconciliation["status"], "PASS")
        self.assertTrue(result.open_positions)
        self.assertAlmostEqual(result.reconciliation["engine_final_equity"], engine.equity())

    def test_independent_fifo_reconciliation_after_real_engine_partial_reduction(self):
        days = fixture(length=12)
        days.append([day(67, 150, open_=150, low=149, high=151, settlement=150)])
        engine = FuturesEngine({"MICRO1": spec()}, cfg(version="F1"))
        result = engine.run(days)
        reductions = [t for t in result.trades if t["reason"] == "FROZEN_LIMIT_DELEVERAGING"]
        self.assertTrue(reductions)
        self.assertTrue(engine.positions)
        self.assertEqual(result.reconciliation["status"], "PASS")
        self.assertGreater(result.reconciliation["realized_fill_profit"], 0)
        self.assertGreater(result.reconciliation["unrealized_fill_profit"], 0)

    def test_realized_sold_layer_profit_cannot_authorize_losing_add(self):
        e = FuturesEngine({"MICRO1": spec()}, cfg(version="F1"))
        at = bar(1).opens_at
        p = Campaign("PARTIAL", "ES", "MICRO1", 1,
                     [Layer(1, 110, 110, at, 1, fees_per_contract=2)],
                     100, 96, 100, 4, 1, 120, 100, at, 109, at, realized_gross=500)
        e.positions["ES"] = p
        e.volumes["MICRO1"].extend([100000] * 20)
        intent = Intent("ES", 1, START, at - timedelta(days=1), 109, 2, 100, 1, "MOCK", (), 1)
        e._add(p, intent, bar(2, 109), bar(2).opens_at)
        self.assertGreater(e._campaign_net(p), 0)
        self.assertLess(e._open_layers_net(p), 0)
        self.assertFalse(e.result.trades)

    def test_early_settlement_reference_excludes_later_layer(self):
        e = FuturesEngine({"MICRO1": spec()}, cfg())
        b = bar(2, 110, settlement=105)
        reference = b.opens_at + timedelta(hours=3)
        publication = reference + timedelta(minutes=5)
        b = replace(b, settlement_reference_at=reference, settlement_available_at=publication)
        early = Layer(1, 100, 100, b.opens_at, 0)
        later = Layer(1, 108, 108, reference + timedelta(minutes=1), 1)
        p = Campaign("EARLY", "ES", "MICRO1", 1, [early, later], 100, 96, 100,
                     4, 1, 110, 100, b.opens_at, 108, later.created_at)
        e.positions["ES"] = p
        self.assertIsNone(e._bar_problem(b, execution=True))
        e._settle("ES", b, publication)
        self.assertEqual(e.cash, 11505)
        self.assertEqual(early.settlement_basis, 105)
        self.assertEqual(later.settlement_basis, 108)
        self.assertEqual(p.mark, 108)  # Settlement reference older than later known fill.
        e._settle("ES", b, publication)
        self.assertEqual(e.cash, 11505)  # Repeated settlement event is idempotent.

    def test_early_settlement_without_reference_is_unknown_not_delayed(self):
        b = bar(2, 110, settlement=105)
        b = replace(b, settlement_available_at=b.closes_at - timedelta(hours=2))
        e = FuturesEngine({}, cfg())
        self.assertEqual(e._bar_problem(b, execution=True),
                         "SETTLEMENT_REFERENCE_UNKNOWN_ACCOUNT_EVIDENCE_BLOCKED")

    def test_existing_initial_margin_cash_shortfall_is_explicit(self):
        e = FuturesEngine({"MICRO1": spec()}, cfg())
        at = bar(2).opens_at
        p = Campaign("CASH", "ES", "MICRO1", 1, [Layer(1, 1, 1, at, 0)],
                     1, 1, 99, 1, 1, 100, 1, at, 100, at)
        e.positions["ES"] = p
        e.cash = 9  # initial10, maintenance7.5, equity108: initial shortfall alone.
        reasons = e._breaches()
        self.assertIn("INITIAL_MARGIN_CASH_SHORTFALL", reasons)
        self.assertNotIn("MARGIN_BREACH", reasons)

    def test_duplicate_contract_session_cannot_double_liquidity_samples(self):
        d = day(0)
        d = replace(d, execution=d.execution + d.execution)
        e = FuturesEngine({"MICRO1": spec()}, cfg())
        with self.assertRaisesRegex(ValueError, "DUPLICATE_EXECUTION_CONTRACT_SESSION"):
            e.process_batch([d])
        self.assertFalse(e.volumes)

    def test_2026_data_cannot_change_frozen_terminal_equity(self):
        e = FuturesEngine({})
        d = day(0)
        d = replace(d, signal=replace(d.signal, session=date(2026, 1, 1)))
        with self.assertRaisesRegex(ValueError, "OUTSIDE_FROZEN_INPUT_RANGE"):
            e.process_batch([d])

    def test_unexecutable_gap_never_recovers_ideal_stop_from_full_day_range(self):
        for direction in (1, -1):
            e = FuturesEngine({"MICRO1": spec()}, cfg())
            at = bar(1).opens_at
            stop = 96 if direction > 0 else 104
            p = Campaign("GAP", "ES", "MICRO1", direction, [Layer(1, 100, 100, at, 0)],
                         100, stop, stop, 4, 1, 100, 100, at, 100, at)
            e.positions["ES"] = p
            if direction > 0:
                d = day(2, 92, open_=90, high=95, low=89, tradable_open=False, tradable_stop=True)
            else:
                d = day(2, 108, open_=110, high=111, low=105, tradable_open=False, tradable_stop=True)
            e._opens([(d, d.execution)], d.signal.opens_at)
            e._bar_close("ES", d.execution[0], d.signal.available_at)
            self.assertFalse(e.result.trades)
            self.assertTrue(e.positions)
            self.assertTrue(any(s["reason"] == "UNRESOLVED_OPEN_EXIT_NEEDS_INTRADAY_QUOTES" for s in e.result.skips))

    def test_missed_add_session_expires_tier_instead_of_choosing_later_price(self):
        e = FuturesEngine({"MICRO1": spec()}, cfg(version="F1"))
        at = bar(1).opens_at
        p = Campaign("MISSED", "ES", "MICRO1", 1, [Layer(1, 100, 100, at, 0)],
                     100, 96, 100, 4, 1, 110, 100, at, 110, at)
        e.positions["ES"] = p
        e.add_intents["ES"] = Intent("ES", 1, START, at - timedelta(days=1), 110, 2,
                                      100, 1, "MOCK", (), 1, START + timedelta(days=1))
        later = day(2, 110)
        e._opens([(later, later.execution)], later.signal.opens_at)
        self.assertEqual(p.consumed_tiers, {1})
        self.assertFalse(e.result.trades)

    def test_missing_expected_session_resets_windows_and_preserves_existing_account(self):
        e = FuturesEngine({"MICRO1": spec()}, cfg())
        for values in fixture(length=3):
            e.process_batch(values)
        e._drain()  # Finish the observed day; do not synthesize the missing next day.
        before_position = asdict(e.positions["ES"])
        before_cash = e.cash
        observed = day(59, 110, open_=108, low=107, high=111, settlement=110)
        e.process_batch([observed])  # Day58 was expected and is absent.
        self.assertEqual(asdict(e.positions["ES"]), before_position)
        self.assertEqual(e.cash, before_cash)
        self.assertNotIn("ES", e.indicators)
        self.assertNotIn("MICRO1", e.volumes)
        self.assertEqual(e.expected_signal_session["ES"], observed.signal.next_session)
        self.assertEqual(e.expected_contract_session["MICRO1"], observed.signal.next_session)
        restored = FuturesEngine.restore(json.loads(json.dumps(e.checkpoint())))
        result = restored.finish()
        self.assertEqual(result.coverage_status, "UNKNOWN_MISSING_OR_INCONSISTENT_SESSIONS")
        self.assertEqual(len(restored.indicators["ES"].history), 1)
        self.assertEqual(len(restored.volumes["MICRO1"]), 1)
        issue = result.coverage_issues[0]
        self.assertEqual(issue["status"], "PERFORMANCE_VALIDITY_BLOCKER")
        self.assertEqual(issue["held_quantity"], before_position["layers"][0]["quantity"])
        self.assertFalse(any(t["timestamp"].startswith(str(bar(58).session)) for t in result.trades))

    def test_calendar_weekend_and_holiday_jumps_do_not_create_false_gaps(self):
        # Explicit mock calendar: Friday Jan7 -> Monday Jan10, and
        # Friday Jan14 -> Tuesday Jan18 (Monday holiday). No weekday inference.
        for previous_index, next_index in ((6, 9), (13, 17)):
            earlier, later = day(previous_index), day(next_index)
            next_date = later.signal.session
            earlier = replace(earlier, signal=replace(earlier.signal, next_session=next_date),
                              execution=tuple(replace(b, next_session=next_date) for b in earlier.execution))
            e = FuturesEngine({"MICRO1": spec()}, cfg())
            result = e.run([[earlier], [later]])
            self.assertEqual(result.coverage_issues, [])
            self.assertEqual(result.coverage_status, "NO_DETECTED_GAP_IN_SUPPLIED_CALENDAR_SEQUENCE")
            self.assertEqual(len(e.indicators["ES"].history), 2)
            self.assertEqual(len(e.volumes["MICRO1"]), 2)

    def test_execution_contract_gap_alone_resets_signal_and_liquidity_history(self):
        e = FuturesEngine({"MICRO1": spec(), "OTHER": spec("OTHER")}, cfg())
        batches = [[day(i)] for i in range(25)]
        batches.append([day(25, contract="OTHER")])
        batches.append([day(26)])
        result = e.run(batches)
        issues = result.coverage_issues[-1]["issues"]
        self.assertEqual([issue["input"] for issue in issues], ["EXECUTION"])
        self.assertEqual(issues[0]["expected_session"], str(bar(25).session))
        self.assertEqual(len(e.indicators["ES"].history), 1)
        self.assertEqual(len(e.volumes["MICRO1"]), 1)


if __name__ == "__main__":
    unittest.main()
