"""Temporary-directory delivery safety and truthful-status boundary fixtures.

No production bundle/report is generated, no market data or real credentials
are read, and no strategy computation is performed by these tests.
"""
import json
from pathlib import Path
import zipfile

import pytest

from src.ben_b1_2 import delivery


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) if not isinstance(value, str) else value, encoding="utf-8")
    return path


@pytest.fixture
def isolated_delivery(tmp_path, monkeypatch):
    for field in ("ROOT", "REPO", "OLD_REPO", "B11", "B1"):
        folder = tmp_path / field.lower()
        folder.mkdir()
        monkeypatch.setattr(delivery, field, folder)
    put(delivery.ROOT / "BEN_B1_2_RESULTS.md", "Explicit MOCK packaging test. No research result.")
    return delivery.ROOT


@pytest.mark.parametrize("relative", [
    "market_dump.json",
    "engineering/database_dump.txt",
    "portfolio/base_v1/B12_Q0_P50_A_base_v1/raw_quotes.csv",
    "portfolio/base_v1/B12_Q0_P50_A_base_v1/credentials.json",
    "earnings/finnhub_credentials.json",
    "samples/sample_test/tier_A/sample_MOCK/checkpoint_backup.json",
])
def test_unknown_text_data_and_secret_dumps_cannot_enter_whitelist(isolated_delivery, relative):
    put(isolated_delivery / relative, "MOCK_DISALLOWED_PAYLOAD_ONLY")
    try:
        result = delivery.package("sample_test", "base_v1")
    except ValueError as exc:
        # Fail-closed rejection is also valid; packaging must never claim safe
        # inclusion merely because the dump has a JSON/CSV/TXT extension.
        assert any(word in str(exc).upper() for word in ("FORBIDDEN", "SECRET", "UNEXPECTED", "WHITELIST"))
        return
    with zipfile.ZipFile(result["zip"]) as bundle:
        assert relative not in bundle.namelist()
        assert all(b"MOCK_DISALLOWED_PAYLOAD_ONLY" not in bundle.read(name) for name in bundle.namelist())


def test_explicit_evidence_included_but_raw_db_and_configuration_excluded(isolated_delivery):
    account = "portfolio/base_v1/B12_Q0_P50_A_base_v1/"
    put(isolated_delivery / (account + "FINAL_ACCOUNT.json"), {"status": "MOCK_NOT_REAL_RESEARCH"})
    put(isolated_delivery / (account + "fills.csv"), "side,quantity\nBUY,1\n")
    for name in ("replay_archive.sqlite", "database.db", "bars.parquet", "secrets.toml", ".env"):
        put(isolated_delivery / (account + name), "MOCK_PRIVATE_NEVER_EXPORT")
    result = delivery.package("sample_test", "base_v1")
    with zipfile.ZipFile(result["zip"]) as bundle:
        names = bundle.namelist()
        assert "BEN_B1_2_RESULTS.md" in names
        assert account + "FINAL_ACCOUNT.json" in names
        assert account + "fills.csv" in names
        assert not any(name.endswith((".sqlite", ".db", ".parquet", ".toml", ".env")) for name in names)
        assert bundle.testzip() is None


def test_missing_quarter_evidence_does_not_claim_zero_actual_costs(tmp_path):
    row = delivery.audit_account(tmp_path)
    assert row["actually_completed"] is False
    assert row["quarter_end_equity"] is None
    assert row["commission_paid_through_quarter"] is None
    assert row["friction_paid_through_quarter"] is None


def test_partial_or_failed_final_file_is_not_completed_account(tmp_path):
    put(tmp_path / "FINAL_ACCOUNT.json", {"status": "REPLAY_FAILED", "processed_through": "2026-01-05",
                                        "actually_executed": True, "error_count": 1})
    row = delivery.audit_account(tmp_path)
    assert row["actually_completed"] is False


def test_tail_stale_mark_is_unknown_even_if_ledger_has_fallback_value(tmp_path):
    put(tmp_path / "FINAL_ACCOUNT.json", {"status": "ENGINE_EXECUTED_WITH_MODEL_FILLS", "processed_through": delivery.TAIL_END,
        "actually_executed": True, "error_count": 0, "portfolio_not_stitched": True,
        "latest_equity": {"cash": 100., "net_equity": 6000., "valuation_status": "STALE_MARK_UNKNOWN", "missing_current_marks": ["MOCK"]}})
    row = delivery.audit_account(tmp_path)
    assert row["tail_equity"] is None
    assert row["tail_cash"] == 100


def test_empty_incomplete_fill_export_does_not_crash_readonly_report(tmp_path):
    put(tmp_path / "fills.csv", "\n")
    row = delivery.audit_account(tmp_path)
    assert row["actually_completed"] is False
    assert row["traded_symbols"] == []


def test_known_quarter_costs_use_snapshot_campaigns_without_tail_charges(tmp_path):
    put(tmp_path / "QUARTER_END_ACCOUNT.json", {"campaigns": {"campaigns": [{"commission": 3., "friction": 1.25}]},
                                               "buy_intents": 1, "buy_fills": 2, "sell_fills": 1})
    put(tmp_path / "QUARTER_END_CLOSE_VALUATION.json", {"net_equity": 5400., "cash": 2000.,
                                                     "valuation_status": "CURRENT_MARKS", "missing_current_marks": []})
    put(tmp_path / "FINAL_ACCOUNT.json", {"campaigns": {"campaigns": [{"commission": 9., "friction": 4.}]},
                                          "processed_through": delivery.TAIL_END, "actually_executed": True, "error_count": 0,
                                          "status": "ENGINE_EXECUTED_WITH_MODEL_FILLS"})
    put(tmp_path / "RECOVERY_IDEMPOTENCY.json", {"status": "PASS"})
    row = delivery.audit_account(tmp_path)
    assert row["commission_paid_through_quarter"] == 3.
    assert row["friction_paid_through_quarter"] == 1.25
    assert row["quarter_profit"] == -100.


@pytest.mark.parametrize("recovery", [None, "FAIL", "PASS"])
def test_recovery_missing_or_failed_never_claims_engineering_complete(tmp_path, recovery):
    put(tmp_path / "FINAL_ACCOUNT.json", {"status": "ENGINE_EXECUTED_NO_QUALIFIED_NATURAL_ENTRY", "processed_through": delivery.TAIL_END,
                                          "actually_executed": True, "error_count": 0, "portfolio_not_stitched": True})
    put(tmp_path / "fills.csv", "fill_id,order_id,symbol,side,quantity,execution_price\n")
    put(tmp_path / "progress.json", {"processed_day": delivery.TAIL_END})
    if recovery is not None:
        put(tmp_path / "RECOVERY_IDEMPOTENCY.json", {"status": recovery})
    row = delivery.audit_account(tmp_path)
    assert row["processed_through"] == delivery.TAIL_END
    assert row["actually_completed"] is (recovery == "PASS")
