"""Exact economic and crash-recovery checks for B1.2 local storage compaction.

Only synthetic events and SQLite archives in pytest tmp_path are used. Archiving
must change storage costs, never the frozen trading calculation or old results.
"""
from __future__ import annotations

import copy
from datetime import date
import json
from pathlib import Path
import sqlite3

import numpy as np
import pandas as pd
import pytest

from test_ben_b1_replay import schedule, full_input, quote, daily, ev, ny, fills
from src.ben_b1.replay import _clean, _hash
from src.ben_b1.replay import ReplayEngine as B11ReplayEngine
from src.ben_b1_2.replay import ReplayEngine, ReplayConfig
from src.ben_b1_2.compact import CompactReplayEngine, stream_hash


def make(schedule, tmp_path, compact=True, symbols=("X",), mode="Q1"):
    cls = CompactReplayEngine if compact else ReplayEngine
    return cls(schedule, ReplayConfig(run_id="SYNTHETIC_EQUALITY", quote_mode=mode,
                      synthetic_test=True, checkpoint_every=0),
               {s: {"scope": "KEEP", "security_id": "MOCK_ONLY_" + s} for s in symbols},
               checkpoint_path=tmp_path / "checkpoint.json")


def initial(schedule, symbols=("X",)):
    return [e for e in full_input(schedule, symbols=symbols, quotes=False)
            if pd.Timestamp(e["at"]) <= pd.Timestamp(ny("2026-05-01 16:05:00"))]


def economic(engine):
    return _clean({"ledger": engine.ledger.to_dict(), "orders": engine.orders,
        "stops": engine.state["stops"], "trailing": engine.state["trailing"],
        "pressure": engine.state["pressure"], "last_exit": engine.state["last_exit"],
        "equity": engine.state["equity"], "errors": engine.state["errors"]})


def complete_cycle(schedule, noise=0):
    events = full_input(schedule, "2026-05-06") + [
        quote("2026-05-01 16:05:02", ask_size=100),
        quote("2026-05-04 09:30:00", bid=95.50, ask=95.55), daily("2026-05-04", 102.),
        quote("2026-05-05 09:30:00", bid=95.90, ask=95.95), daily("2026-05-05", 96.), daily("2026-05-06", 96.)]
    for n in range(noise):
        at = (pd.Timestamp(ny("2026-05-01 12:00:00")) + pd.Timedelta(milliseconds=n)).strftime("%Y-%m-%d %H:%M:%S.%f")
        events.append(quote(at, bid=100.8, ask=101., quote_id="MOCK_UNUSED_" + str(n)))
    return events


@pytest.mark.parametrize("mode", ["Q0", "Q1"])
def test_compacted_daily_blocks_preserve_every_economic_field(mode, schedule, tmp_path):
    ordinary = make(schedule, tmp_path / "ordinary", compact=False, mode=mode)
    compact = make(schedule, tmp_path / "compact", mode=mode)
    events = complete_cycle(schedule, noise=120)
    groups = {}
    for item in events:
        day = str(pd.Timestamp(item["at"]).tz_convert("America/New_York").date())
        groups.setdefault(day, []).append(item)
    for day in sorted(groups):
        ordinary.run(groups[day])
        compact.run(groups[day])
        assert compact.economic_state() == economic(ordinary)
    assert len(fills(compact, "BUY")) == len(fills(compact, "SELL")) == 2
    assert not compact.ledger.positions and not compact.ledger.settlements
    assert compact.state["archive"]["archived_zero_consumption_inventories"] >= 120
    assert len(compact.state["quote_inventory"]) < 10
    assert compact.state_digest() == _hash(compact.to_dict())
    compact.close()


def test_duplicate_archived_event_and_repeated_consumed_display_never_add_fee_or_quantity(schedule, tmp_path):
    engine = make(schedule, tmp_path)
    first = quote("2026-05-01 16:05:01", ask_size=10, quote_id="ONE_CONSUMED_DISPLAY")
    engine.run(initial(schedule) + [first])
    before = engine.economic_state()
    engine.run([first])
    assert engine.economic_state() == before
    engine.close()
    resumed = CompactReplayEngine.restore(tmp_path / "checkpoint.json", schedule)
    repeated = {**first, "at": ny("2026-05-01 16:05:02"), "event_id": "LATER_POLL_IDENTICAL_DISPLAY"}
    resumed.run([repeated])
    assert sum(fill["quantity"] for fill in fills(resumed, "BUY")) == 10
    assert sum(fill["commission"] for fill in fills(resumed, "BUY")) == 1
    resumed.close()


@pytest.mark.parametrize("move_time", [False, True])
def test_archived_event_id_content_conflict_is_checked_even_if_timestamp_is_changed(schedule, tmp_path, move_time):
    engine = make(schedule, tmp_path)
    original = ev("TRADE", "2026-05-01 16:05:01", price=101., size=10)
    engine.run(initial(schedule) + [original])
    changed = copy.deepcopy(original)
    changed["payload"]["size"] = 99
    if move_time:
        changed["at"] = ny("2026-05-01 16:05:02")
    with pytest.raises(ValueError, match="EVENT_ID_CONTENT_CONFLICT"):
        engine.process(changed)
    engine.close()


def test_unclosed_same_timestamp_group_is_not_ranked_or_lost_when_saved(schedule, tmp_path):
    symbols = ("EARLY", "BEST", "MIDDLE")
    engine = make(schedule, tmp_path, symbols=symbols)
    engine.run(initial(schedule, symbols))
    first = quote("2026-05-01 16:05:01", "EARLY", bid=100.75)
    engine.process(first)
    assert engine.compact_now()["status"] == "DEFERRED_UNCLOSED_QUOTE_TIMESTAMP_BATCH"
    assert not engine.orders
    engine.save()
    engine.close()
    resumed = CompactReplayEngine.restore(tmp_path / "checkpoint.json", schedule)
    resumed.process(first)
    resumed.process(quote("2026-05-01 16:05:01", "BEST", bid=100.99))
    resumed.process(quote("2026-05-01 16:05:01", "MIDDLE", bid=100.9))
    assert not resumed.orders
    resumed.flush_quote_batch()
    resumed.compact_now()
    assert [fill["symbol"] for fill in fills(resumed, "BUY")] == ["BEST", "MIDDLE"]
    assert resumed.ledger.cash >= 0 and len(resumed.ledger.positions) == 2
    resumed.close()


def test_archive_committed_before_old_checkpoint_replays_unaccounted_fill_exactly_once(schedule, tmp_path):
    ordinary = make(schedule, tmp_path / "ordinary", compact=False)
    compact = make(schedule, tmp_path / "compact")
    seed = initial(schedule)
    ordinary.run(seed)
    compact.run(seed)
    before = compact.state["archive"]["applied_sequence"]
    arrival = quote("2026-05-01 16:05:01")
    def crash(block):
        raise RuntimeError("MOCK_CRASH_AFTER_ARCHIVE_COMMIT_BEFORE_CHECKPOINT")
    compact.after_archive_commit_hook = crash
    with pytest.raises(RuntimeError, match="MOCK_CRASH_AFTER_ARCHIVE_COMMIT"):
        compact.run([arrival])
    compact.close()
    checkpoint = tmp_path / "compact/checkpoint.json"
    restored = CompactReplayEngine.restore(checkpoint, schedule)
    assert restored.state["archive"]["applied_sequence"] == before and not restored.ledger.fills
    ordinary.run([arrival])
    restored.run([arrival])
    assert restored.economic_state() == economic(ordinary)
    assert len(fills(restored, "BUY")) == 1
    state_hash = restored.state_digest()
    restored.run([arrival])
    assert restored.state_digest() == state_hash
    restored.close()


def test_same_session_close_and_entry_blocks_resume_from_last_durable_checkpoint(schedule, tmp_path):
    ordinary = make(schedule, tmp_path / "ordinary", compact=False)
    compact = make(schedule, tmp_path / "compact")
    events = full_input(schedule, quotes=False)
    cut = pd.Timestamp(ny("2026-05-01 16:01:00"))
    early = [item for item in events if pd.Timestamp(item["at"]) <= cut]
    late = [item for item in events if pd.Timestamp(item["at"]) > cut]
    late.append(quote("2026-05-01 16:05:01"))
    ordinary.run(early)
    compact.run(early)
    close_sequence = compact.state["archive"]["applied_sequence"]
    compact.close()
    restored = CompactReplayEngine.restore(tmp_path / "compact/checkpoint.json", schedule)
    restored.run(early)
    assert restored.state["archive"]["applied_sequence"] == close_sequence
    ordinary.run(late)
    restored.run(late)
    assert restored.state["archive"]["applied_sequence"] == close_sequence + 1
    assert restored.economic_state() == economic(ordinary)
    state_hash = restored.state_digest()
    restored.run(early + late)
    assert restored.state_digest() == state_hash
    restored.close()


def test_committed_future_generation_cannot_be_claimed_by_different_replayed_input(schedule, tmp_path):
    engine = make(schedule, tmp_path)
    engine.run(initial(schedule))
    arrival = quote("2026-05-01 16:05:01")
    def crash(block):
        raise RuntimeError("MOCK_CRASH")
    engine.after_archive_commit_hook = crash
    with pytest.raises(RuntimeError, match="MOCK_CRASH"):
        engine.run([arrival])
    engine.close()
    restored = CompactReplayEngine.restore(tmp_path / "checkpoint.json", schedule)
    changed = copy.deepcopy(arrival)
    changed["payload"]["ask_size"] = 20
    with pytest.raises(ValueError, match="ARCHIVE_CHECKPOINT_REPLAY_DIVERGENCE|ARCHIVED_EVENT_DIGEST"):
        restored.run([changed])
    restored.close()


def test_obsolete_old_timestamp_cannot_revive_displayed_inventory_or_create_entry(schedule, tmp_path):
    engine = make(schedule, tmp_path)
    first = quote("2026-05-01 16:04:40", quote_id="OLD_UNUSED_DISPLAY")
    second = quote("2026-05-01 16:04:50", quote_id="MORE_RECENT_BUT_STALE")
    engine.run(initial(schedule) + [first, second])
    assert engine.state["archive"]["archived_zero_consumption_inventories"] == 1
    revived = {**first, "event_id": "NEW_POLL_OLD_TIMESTAMP", "at": ny("2026-05-01 16:05:01")}
    never_seen_stale = quote("2026-05-01 16:04:45", quote_id="NEW_ID_UNKNOWN_OLD_TIMESTAMP")
    never_seen_stale["at"] = ny("2026-05-01 16:05:02")
    engine.run([revived, never_seen_stale])
    assert not engine.orders and not engine.ledger.fills
    assert all(row["ask_consumed"] == row["bid_consumed"] == 0 for row in engine.state["quote_inventory"].values())
    engine.close()


@pytest.mark.parametrize("tamper", ["event_digest", "archive_missing"])
def test_corrupt_or_missing_archive_is_explicit_failure_not_empty_recovery(schedule, tmp_path, tamper):
    engine = make(schedule, tmp_path)
    engine.run(initial(schedule))
    path = engine.archive_path
    engine.close()
    if tamper == "event_digest":
        with sqlite3.connect(path) as db:
            db.execute("UPDATE archived_events SET digest=? WHERE event_id=(SELECT MIN(event_id) FROM archived_events)", ("0" * 64,))
    else:
        path.rename(path.with_name("MOCK_REMOVED_ARCHIVE_RETAINED.sqlite"))
    with pytest.raises(ValueError, match="ARCHIVED_EVENT_DIGEST_MISMATCH|REQUIRED_COMPACT_ARCHIVE_MISSING"):
        CompactReplayEngine.restore(tmp_path / "checkpoint.json", schedule)


@pytest.mark.parametrize("wrong_class", [ReplayEngine, B11ReplayEngine])
def test_compact_checkpoint_cannot_be_loaded_by_engine_that_ignores_archive(schedule, tmp_path, wrong_class):
    engine = make(schedule, tmp_path)
    engine.run(initial(schedule))
    assert engine.state_digest() == _hash(engine.to_dict())
    engine.close()
    with pytest.raises(ValueError, match="CHECKPOINT_HASH_OR_VERSION_MISMATCH"):
        wrong_class.restore(tmp_path / "checkpoint.json", schedule)


def test_streamed_json_hash_has_same_scalar_semantics_as_frozen_canonical_encoder():
    value = {"scalar": {"float": np.float64(1.2), "int": np.int64(7), "bool": np.bool_(True)},
             "special": [float("nan"), float("inf"), -float("inf"), None],
             "times": [pd.Timestamp("2026-05-01T16:05:00.486314Z"), date(2026, 5, 1)],
             "tuple": ("报价", 0.00000001)}
    assert stream_hash(value) == _hash(value)


def test_streaming_exports_preserve_economic_rows_and_mark_private_archive_not_for_zip(schedule, tmp_path):
    ordinary = make(schedule, tmp_path / "ordinary", compact=False)
    compact = make(schedule, tmp_path / "compact")
    events = complete_cycle(schedule)
    ordinary.run(events)
    compact.run(events)
    ordinary.export(tmp_path / "ordinary_export")
    compact.export(tmp_path / "compact_export")
    for name in ["fills", "orders", "campaigns", "account_events", "daily_equity"]:
        old = pd.read_csv(tmp_path / "ordinary_export" / (name + ".csv"))
        new = pd.read_csv(tmp_path / "compact_export" / (name + ".csv"))
        assert set(old) == set(new)
        pd.testing.assert_frame_equal(old, new[old.columns], check_dtype=False)
    manifest = json.loads((tmp_path / "compact_export/ARCHIVE_MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["include_private_sqlite_in_verification_zip"] is False
    compact.close()
