"""Dormant extractor engineering checks: handwritten mock JSON in tmp only."""
from __future__ import annotations

import copy
import csv
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from src.ben_b1_2 import partial_evidence as extractor


def write_mock(tmp_path, traded=True, completion=True):
    """No engine events run; this is plainly labelled fixture data, not research."""
    source = tmp_path / "original_account" / "checkpoint.json"
    source.parent.mkdir(parents=True)
    fill = {"fill_id": "MOCK_FILL", "order_id": "MOCK_ORDER", "symbol": "MOCK_X", "side": "BUY", "quantity": 1,
            "at": "2026-05-04T14:00:00+00:00", "execution_price": 100., "observed_price": 100., "commission": 1., "outlay": 101.}
    ledger = {"cash": 5399. if traded else 5500., "debt": 0., "asof_day": "2026-05-04",
        "positions": {"MOCK_X": {"quantity": 1, "campaign_id": "MOCK_CAMPAIGN", "last_mark": 100., "legs": [{"quantity": 1}]}} if traded else {},
        "orders": {"MOCK_ORDER": {"symbol": "MOCK_X", "side": "BUY", "order_quantity": 3, "filled_quantity": 1, "commission_charged": 1., "status": "OPEN"}} if traded else {},
        "fills": {"MOCK_FILL": fill} if traded else {}, "campaigns": {},
        "events": [copy.deepcopy(fill)] if traded else [], "settlements": [], "dividend_receivables": {}}
    state = {"at": "2026-05-04T14:00:00+00:00", "pending": {"MOCK_ORDER": {"symbol": "MOCK_X", "side": "BUY", "quantity": 3, "filled_quantity": 1, "status": "OPEN"}} if traded else {},
        "decisions": [], "equity": [{"at": "2026-05-01T20:15:00+00:00", "reason": "AFTER_ENTRY_EXPIRY", "cash": 5500., "net_equity": 5500.}] if completion else [],
        "errors": [], "gaps": [], "events_processed": 5 if traded else 0,
        "archive": {"path": str(source.parent / "DO_NOT_OPEN.sqlite"), "applied_sequence": 1, "applied_hash": "MOCK_UNVERIFIED_ARCHIVE_HASH"},
        "bars": {"MOCK_RAW_MARKET_SENTINEL": [100., 200.]}, "quotes": {"MOCK_RAW_QUOTES_SENTINEL": []}}
    payload = {"version": extractor.SHARED_VERSION,
        "config": {"run_id": "MOCK_PARTIAL_ONLY", "mode": "P50_PRIMARY", "quote_mode": "Q1", "initial_equity": 5500.,
                   "start": "2026-01-02", "end": "2026-04-30", "synthetic_test": True, "api_key": "MOCK_SECRET_SENTINEL"},
        "ledger": {"state": ledger}, "state": state,
        "sessions": [["2026-05-01", "2026-05-01T13:30:00+00:00", "2026-05-01T20:00:00+00:00"]]}
    wrapper = {"payload": payload, "sha256": extractor.stream_hash(payload)}
    source.write_text(json.dumps(wrapper, ensure_ascii=False), encoding="utf-8")
    return source, payload


def destination(tmp_path):
    return tmp_path / "partial_evidence" / "new_snapshot"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rewrite(source, payload, valid_hash=True):
    source.write_text(json.dumps({"payload": payload, "sha256": extractor.stream_hash(payload) if valid_hash else "0"*64}), encoding="utf-8")


def test_valid_extraction_keeps_interim_cash_dates_identities_and_original_bytes(monkeypatch, tmp_path):
    source, payload = write_mock(tmp_path)
    original, modified = sha(source), source.stat().st_mtime_ns
    def forbidden(*args, **kwargs):
        raise AssertionError("EXTRACTOR_MUST_NOT_RESTORE_RUN_OR_OPEN_SQLITE")
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    from src.ben_b1_2.compact import CompactReplayEngine
    from src.ben_b1_2.shared import SharedReplayEngine
    monkeypatch.setattr(CompactReplayEngine, "run", forbidden)
    monkeypatch.setattr(SharedReplayEngine, "restore", forbidden)
    called = []
    original_hash = extractor.stream_hash
    def track_hash(value):
        called.append(value)
        return original_hash(value)
    monkeypatch.setattr(extractor, "stream_hash", track_hash)
    target = destination(tmp_path)
    result = extractor.extract(source, target)
    assert len(called) == 1 and called[0] == payload
    assert sha(source) == original and source.stat().st_mtime_ns == modified
    assert result["status"] == "PARTIAL_CHECKPOINT_EVIDENCE_NOT_COMPLETED"
    assert result["saved_interim_cash_not_terminal_result"] == 5399.
    assert result["saved_boundaries"]["latest_completed_trading_session_ny"] == "2026-05-01"
    assert result["saved_boundaries"]["checkpoint_event_cutoff_at"] == "2026-05-04T14:00:00+00:00"
    assert result["sqlite_full_chain_verified"] is False and result["engine_restored"] is False
    assert result["recovery_validation"] == "NOT_PERFORMED" and result["events_run"] == 0
    assert result["full_interval_completed"] is False
    assert result["terminal_profit_or_return"] == "NOT_CALCULATED_PARTIAL_EVIDENCE_ONLY"
    assert result["synthetic_fixture"] is True
    with (target / "ledger_orders.csv").open(encoding="utf-8-sig", newline="") as stream:
        row = next(csv.DictReader(stream))
    assert row["order_id"] == "MOCK_ORDER" and row["order_quantity"] == "3" and row["commission_charged"] == "1.0"
    with (target / "positions.csv").open(encoding="utf-8-sig", newline="") as stream:
        assert next(csv.DictReader(stream))["symbol"] == "MOCK_X"
    assert not list(source.parent.glob("*.sqlite"))
    all_output = "\n".join(p.read_text(encoding="utf-8-sig") for p in target.iterdir())
    assert "MOCK_RAW_MARKET_SENTINEL" not in all_output and "MOCK_SECRET_SENTINEL" not in all_output
    assert not (target / "FINAL_ACCOUNT.json").exists() and not (target / "RECOVERY_IDEMPOTENCY.json").exists()
    assert not (target / "checkpoint.json").exists()


def test_corrupted_wrapper_hash_is_rejected_before_any_export(tmp_path):
    source, payload = write_mock(tmp_path)
    payload["ledger"]["state"]["cash"] = 123.
    rewrite(source, payload, valid_hash=False)
    original = sha(source)
    with pytest.raises(ValueError, match="HASH_MISMATCH"):
        extractor.extract(source, destination(tmp_path))
    assert not destination(tmp_path).exists() and sha(source) == original


def test_existing_output_is_never_overwritten(tmp_path):
    source, _ = write_mock(tmp_path)
    target = destination(tmp_path)
    target.mkdir(parents=True)
    sentinel = target / "PRESERVE.txt"
    sentinel.write_text("MOCK_OLDER_PARTIAL_EVIDENCE", encoding="utf-8")
    with pytest.raises(FileExistsError, match="OVERWRITE"):
        extractor.extract(source, target)
    assert sentinel.read_text(encoding="utf-8") == "MOCK_OLDER_PARTIAL_EVIDENCE"
    assert len(list(target.iterdir())) == 1


def test_cannot_write_partial_files_inside_original_account(tmp_path):
    source, _ = write_mock(tmp_path)
    with pytest.raises(ValueError, match="SEPARATE_FROM_SOURCE"):
        extractor.extract(source, source.parent / "partial_evidence")
    assert not (source.parent / "partial_evidence").exists()


def test_no_trades_still_exports_explicit_column_schemas_without_fake_rows(tmp_path):
    source, _ = write_mock(tmp_path, traded=False, completion=False)
    result = extractor.extract(source, destination(tmp_path))
    for name in ("orders", "ledger_orders", "fills", "campaigns", "positions", "account_events"):
        with (destination(tmp_path) / (name+".csv")).open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            assert "evidence_status" in reader.fieldnames
            assert list(reader) == []
    assert result["saved_boundaries"]["latest_completed_trading_session_ny"] == "UNKNOWN"
    assert result["saved_interim_cash_not_terminal_result"] == 5500
    result = subprocess.run([sys.executable, "-m", "src.ben_b1_2.partial_evidence", "--help"],
        cwd=tmp_path, env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
        text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert "--checkpoint" in result.stdout and "--output" in result.stdout


def test_missing_saved_orders_is_not_replaced_with_empty_success_table(tmp_path):
    source, payload = write_mock(tmp_path)
    del payload["state"]["pending"]
    rewrite(source, payload)
    with pytest.raises(ValueError, match="REQUIRED_SAVED_TABLE_MISSING"):
        extractor.extract(source, destination(tmp_path))
    assert not destination(tmp_path).exists()


def test_a_future_completion_marker_does_not_claim_current_day_finished(tmp_path):
    source, payload = write_mock(tmp_path, completion=False)
    payload["state"]["b12_completed_day"] = {"day": "2026-05-04"}
    rewrite(source, payload)
    result = extractor.extract(source, destination(tmp_path))
    assert result["saved_boundaries"]["adapter_completed_day_marker"] == "2026-05-04"
    assert result["saved_boundaries"]["latest_completed_trading_session_ny"] == "UNKNOWN"


def test_bounded_memory_guard_rejects_before_loading_large_saved_file(monkeypatch, tmp_path):
    source, _ = write_mock(tmp_path)
    monkeypatch.setattr(extractor, "MAX_CHECKPOINT_BYTES", 10)
    with pytest.raises(ValueError, match="TOO_LARGE"):
        extractor.extract(source, destination(tmp_path))
    assert not destination(tmp_path).exists()


def test_source_changed_during_read_is_not_attested(monkeypatch, tmp_path):
    source, _ = write_mock(tmp_path)
    monkeypatch.setattr(extractor, "file_sha", lambda path: "f"*64)
    with pytest.raises(ValueError, match="CHANGED_DURING_READ"):
        extractor.extract(source, destination(tmp_path))
    assert not destination(tmp_path).exists()


def test_unreviewed_version_with_valid_hash_is_rejected(tmp_path):
    source, payload = write_mock(tmp_path)
    payload["version"] = "MOCK_UNREVIEWED_FUTURE_SCHEMA"
    rewrite(source, payload)
    with pytest.raises(ValueError, match="UNREVIEWED_CHECKPOINT_VERSION"):
        extractor.extract(source, destination(tmp_path))
    assert not destination(tmp_path).exists()
