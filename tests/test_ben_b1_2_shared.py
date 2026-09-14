"""Shared P50 cash obligations, using natural synthetic events only in tmp_path."""
from __future__ import annotations

import pandas as pd
import pytest

from test_ben_b1_replay import schedule, full_input, quote, ny, fills
from src.ben_b1_2.replay import ReplayConfig
from src.ben_b1_2.compact import CompactReplayEngine


def create(schedule, path, quote_mode="Q1", cls=None, mode="P50_PRIMARY"):
    from src.ben_b1_2.shared import SharedReplayEngine
    return (cls or SharedReplayEngine)(schedule,
        ReplayConfig(run_id="SYNTHETIC_COMMON_CASH", quote_mode=quote_mode, mode=mode,
                     synthetic_test=True, checkpoint_every=0),
        {s: {"scope": "KEEP", "security_id": "MOCK_"+s} for s in ("FIRST", "SECOND")},
        checkpoint_path=path / "checkpoint.json")


def initial(schedule):
    return [e for e in full_input(schedule, symbols=("FIRST", "SECOND"), quotes=False)
            if pd.Timestamp(e["at"]) <= pd.Timestamp(ny("2026-05-01 16:05:00"))]


def commitments(engine):
    pending = [o for o in engine.orders.values() if o["side"] == "BUY" and o["status"] == "OPEN"]
    return (sum((o["quantity"]-o["filled_quantity"])*o["limit"] for o in pending)
            + sum(engine.config.commission for o in pending if o["order_id"] not in engine.ledger.orders)
            + engine.ledger.reserved_exit_fees)


@pytest.mark.parametrize("quote_mode", ["Q0", "Q1"])
def test_no_competing_remainders_preserves_original_ledger_and_financial_fields(schedule, tmp_path, quote_mode):
    legacy = create(schedule, tmp_path / "legacy", quote_mode, cls=CompactReplayEngine)
    shared = create(schedule, tmp_path / "shared", quote_mode)
    at = "2026-05-01 16:05:00" if quote_mode == "Q0" else "2026-05-01 16:05:01"
    events = full_input(schedule, symbols=("FIRST", "SECOND"), quotes=False)
    events += [quote(at, "FIRST", bid=100.99), quote(at, "SECOND", bid=100.95)]
    legacy.run(events); shared.run(events)
    assert len(fills(shared, "BUY")) == 2
    assert shared.ledger.to_dict() == legacy.ledger.to_dict()
    for key in ("stops", "trailing", "pressure", "last_exit", "errors"):
        assert shared.state[key] == legacy.state[key]
    for oid, order in legacy.orders.items():
        assert {key: shared.orders[oid][key] for key in order} == order
    assert len(shared.state["equity"]) == len(legacy.state["equity"])
    for original, current in zip(legacy.state["equity"], shared.state["equity"]):
        assert {key: current[key] for key in original} == original
    legacy.close(); shared.close()


def test_each_later_partial_fill_preserves_other_fixed_intent_cash_through_recovery(schedule, tmp_path):
    from src.ben_b1_2.shared import SharedReplayEngine
    engine = create(schedule, tmp_path)
    first_quote = quote("2026-05-01 16:05:01", "FIRST", ask_size=10)
    engine.run(initial(schedule) + [first_quote])
    engine.run([quote("2026-05-01 16:05:02", "FIRST", bid=130., ask=130.05),
                quote("2026-05-01 16:05:03", "SECOND", ask_size=10)])
    fixed = {oid: (o["quantity"], o["limit"]) for oid, o in engine.orders.items() if o["side"] == "BUY"}
    assert len(fixed) == 2 and commitments(engine) <= engine.ledger.cash + .01
    before = engine.state_digest()
    engine.close()
    engine = SharedReplayEngine.restore(tmp_path / "checkpoint.json", schedule)
    engine.run([first_quote])
    assert engine.state_digest() == before
    for symbol, second in [("FIRST", "04"), ("SECOND", "05")]:
        engine.run([quote(f"2026-05-01 16:05:{second}", symbol, bid=101.75, ask=101.8)])
        assert commitments(engine) <= engine.ledger.cash + .01
        assert engine.ledger.cash >= 0
        assert {oid: (engine.orders[oid]["quantity"], engine.orders[oid]["limit"]) for oid in fixed} == fixed
    assert sum(f["commission"] for f in fills(engine, "BUY")) == 2
    assert not engine.state["errors"]
    engine.close()


def test_shared_checkpoint_cannot_be_silently_restored_without_cash_guard(schedule, tmp_path):
    engine = create(schedule, tmp_path)
    engine.run(initial(schedule) + [quote("2026-05-01 16:05:01", "FIRST", ask_size=10)])
    engine.close()
    with pytest.raises(ValueError, match="[Vv]ersion|[Ss]hared|CHECKPOINT"):
        CompactReplayEngine.restore(tmp_path / "checkpoint.json", schedule)


@pytest.mark.parametrize("mode", ["P100_CONCENTRATED", "P200_MARGIN_RESEARCH"])
def test_this_round_shared_guard_does_not_enable_unrequested_position_or_margin_modes(schedule, tmp_path, mode):
    with pytest.raises(ValueError, match="P50|[Ss]cope|[Mm]ode"):
        create(schedule, tmp_path, mode=mode)
