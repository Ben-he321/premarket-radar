"""Dormant read-only B1.2 checkpoint evidence extractor, never an engine runner.

Run as python -m src.ben_b1_2.partial_evidence from any environment where this
repository's package is importable. No particular working directory is required.
Only saved JSON financial fields are copied. SQLite is never opened or copied.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path

from .compact import stream_hash, CHECKPOINT_VERSION as COMPACT_VERSION
from .shared import CHECKPOINT_VERSION as SHARED_VERSION
import pandas as pd

STATUS = "PARTIAL_CHECKPOINT_EVIDENCE_NOT_COMPLETED"
MAX_CHECKPOINT_BYTES = 128 * 1024 * 1024

# Explicit fields prevent bars, raw quote inventory, credentials, or arbitrary
# added payload fields from becoming part of the partial evidence package.
TABLES = {
    "orders": ("state", "pending", "map", "order_id symbol side quantity filled_quantity status campaign_id created_at active_at expires_at limit close emas legs overhead last_reason reason data_basis earnings_tier quote_mode execution_hypothesis first_qualifying_quote_inventory_id first_qualifying_quote_arrival_time fixed_initial_quantity fixed_initial_limit leg_index shared_reservation_version"),
    "ledger_orders": ("ledger", "orders", "map", "order_id symbol side order_quantity filled_quantity commission_charged status campaign_id limit created_at at"),
    "fills": ("ledger", "fills", "map", "fill_id order_id symbol side quantity execution_price observed_price at campaign_id commission outlay displayed_size_shares limit price_is_proxy reason settlement_date proceeds"),
    "campaigns": ("ledger", "campaigns", "map", "campaign_id symbol entry_quantity exit_quantity entry_outlay exit_proceeds commission friction interest status entry_at exit_at deviation_reduction_done dividends cash_in_lieu net_profit share_unit_note"),
    "account_events": ("ledger", "events", "list", "type date amount side order_id symbol quantity observed_price at fill_id campaign_id execution_price commission outlay displayed_size_shares limit price_is_proxy reason settlement_date proceeds action_id ratio amount_per_share pay_date entitled_quantity status fractional_quantity closing_debt day_count"),
    "decisions": ("state", "decisions", "list", "symbol decision_time status reason security_id data_basis earnings_tier signal input_version source_received_at data_cutoff history_sessions signal_detail earnings actual_quote_arrival_time quote_timestamp quote_inventory_id is_first_arrival_of_displayed_inventory historical_network_received_at availability_basis quote_mode execution_hypothesis evaluation_kind quote_age_seconds latest_bid latest_ask displayed_ask_size actual_intent_time fixed_intent_quantity fixed_intent_limit intent_quantity_remaining skip_reason window_start window_end_exclusive rank cash_before_candidate reserved_exit_fees_before_candidate net_equity_before_candidate equity_valuation_status_before_candidate displayed_ask_remaining_before_candidate stop_legs rr limit order_id shared_existing_entry_commitments shared_uncommitted_cash_before_plan"),
    "daily_equity": ("state", "equity", "list", "at reason asof_day initial_equity cash unsettled_cash dividend_receivable market_value debt accrued_interest interest_total interest_posted net_equity reserved_exit_fees available_settled_cash gross_exposure open_campaigns open_shares missing_current_marks valuation_status stopped_reason margin_evidence pending_exit_orders reserved_entry_cash data_basis earnings_tier execution_evidence shared_reservation_version shared_reserved_entry_costs shared_available_uncommitted_cash shared_reservation_status"),
    "positions": ("ledger", "positions", "map", "symbol quantity campaign_id cost_basis last_mark legs"),
    "settlements": ("ledger", "settlements", "list", "date amount order_id fill_id campaign_id"),
    "dividend_receivables": ("ledger", "dividend_receivables", "map", "type action_id symbol amount_per_share pay_date at entitled_quantity amount campaign_id status paid_at"),
    "errors": ("state", "errors", "list", "at event_id kind symbol error reason exception"),
    "data_gaps": ("state", "gaps", "list", "at symbol category reason action_id action_kind"),
}


def file_sha(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def timestamp(value):
    if not value or value == "UNKNOWN":
        return None
    result = pd.Timestamp(value)
    return result.tz_convert("UTC") if result.tzinfo is not None else None


def safe_session_cutoff(payload):
    state = payload["state"]
    cutoff = timestamp(state.get("at"))
    sessions = {row[0]: row[2] for row in payload.get("sessions", [])
                if isinstance(row, list) and len(row) == 3}
    candidates = []
    if cutoff is not None:
        for row in state["equity"]:
            if row.get("reason") != "AFTER_ENTRY_EXPIRY":
                continue
            at = timestamp(row.get("at"))
            if at is None or at > cutoff:
                continue
            day = str(at.tz_convert("America/New_York").date())
            close = timestamp(sessions.get(day))
            if close is not None and at >= close + pd.Timedelta(minutes=15):
                candidates.append(day)
    return {
        "checkpoint_event_cutoff_at": cutoff.isoformat() if cutoff is not None else "UNKNOWN",
        "latest_completed_trading_session_ny": max(candidates) if candidates else "UNKNOWN",
        "completion_day_basis": "STORED_AFTER_ENTRY_EXPIRY_SNAPSHOT_AND_SAVED_CALENDAR_ONLY" if candidates else "NO_SAVED_COMPLETED_SESSION_PROOF",
        "adapter_completed_day_marker": state.get("b12_completed_day", {}).get("day", "UNKNOWN"),
        "ledger_asof_day": payload["ledger"]["state"].get("asof_day") or "UNKNOWN",
        "warning": "These are saved JSON boundaries, not verification of SQLite history or completion of the intended research interval.",
    }


def extract(checkpoint, destination):
    source = Path(checkpoint).resolve(strict=True)
    target = Path(destination).resolve(strict=False)
    if source.name != "checkpoint.json" or not source.is_file():
        raise ValueError("EXPLICIT_SAVED_CHECKPOINT_JSON_REQUIRED")
    if "partial_evidence" not in target.parts:
        raise ValueError("INDEPENDENT_PARTIAL_EVIDENCE_DIRECTORY_REQUIRED")
    if target == source.parent or target.is_relative_to(source.parent) or source.parent.is_relative_to(target):
        raise ValueError("OUTPUT_MUST_BE_SEPARATE_FROM_SOURCE_ACCOUNT")
    if target.exists():
        raise FileExistsError("REFUSE_TO_OVERWRITE_EXISTING_PARTIAL_EVIDENCE")
    if source.stat().st_size > MAX_CHECKPOINT_BYTES:
        raise ValueError("CHECKPOINT_TOO_LARGE_FOR_BOUNDED_READ_NO_EXPORT")
    raw = source.read_bytes()
    source_sha = hashlib.sha256(raw).hexdigest()
    def reject_constant(value):
        raise ValueError("NONFINITE_JSON_CONSTANT:" + value)
    wrapper = json.loads(raw.decode("utf-8-sig"), parse_constant=reject_constant)
    if not isinstance(wrapper, dict) or not isinstance(wrapper.get("payload"), dict):
        raise ValueError("CHECKPOINT_WRAPPER_SCHEMA_MISSING")
    payload = wrapper["payload"]
    if stream_hash(payload) != wrapper.get("sha256"):
        raise ValueError("CHECKPOINT_WRAPPER_HASH_MISMATCH_NO_EXPORT")
    if payload.get("version") not in (COMPACT_VERSION, SHARED_VERSION):
        raise ValueError("UNREVIEWED_CHECKPOINT_VERSION_NO_EXPORT")
    if not isinstance(payload.get("state"), dict) or not isinstance(payload.get("ledger"), dict) or not isinstance(payload["ledger"].get("state"), dict) or not isinstance(payload.get("config"), dict):
        raise ValueError("REQUIRED_FINANCIAL_STATE_SCHEMA_MISSING")
    state, ledger = payload["state"], payload["ledger"]["state"]
    if not isinstance(ledger.get("cash"), (int, float)) or isinstance(ledger["cash"], bool) or not math.isfinite(ledger["cash"]):
        raise ValueError("SAVED_CASH_FIELD_MISSING_OR_INVALID")
    selected = {}
    for name, (owner, key, kind, fields) in TABLES.items():
        parent = state if owner == "state" else ledger
        if key not in parent or not isinstance(parent[key], dict if kind == "map" else list):
            raise ValueError("REQUIRED_SAVED_TABLE_MISSING:" + owner + "." + key)
        values = list(parent[key].values()) if kind == "map" else parent[key]
        if any(not isinstance(row, dict) for row in values):
            raise ValueError("INVALID_SAVED_FINANCIAL_ROW:" + key)
        if kind == "map":
            identity_field = {"orders": "order_id", "ledger_orders": "order_id", "fills": "fill_id",
                "campaigns": "campaign_id", "positions": "symbol", "dividend_receivables": "action_id"}[name]
            values = []
            for identity, row in parent[key].items():
                if row.get(identity_field) not in (None, identity):
                    raise ValueError("SAVED_MAPPING_ID_CONFLICT:" + name)
                values.append({**row, identity_field: identity})
        selected[name] = (values, fields.split())
    boundary = safe_session_cutoff(payload)
    if file_sha(source) != source_sha:
        raise ValueError("CHECKPOINT_CHANGED_DURING_READ_NO_EXPORT")
    target.mkdir(parents=True, exist_ok=False)
    artifacts = []
    for name, (values, fields) in selected.items():
        path = target / (name + ".csv")
        columns = ["evidence_status"] + fields
        with path.open("x", encoding="utf-8-sig", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=columns, extrasaction="raise")
            writer.writeheader()
            for value in values:
                row = {field: value.get(field) for field in fields}
                for field, item in row.items():
                    if isinstance(item, (dict, list)):
                        row[field] = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
                writer.writerow({"evidence_status": STATUS, **row})
        artifacts.append({"file": path.name, "rows": len(values), "sha256": file_sha(path)})
    if file_sha(source) != source_sha:
        raise ValueError("CHECKPOINT_CHANGED_DURING_EXTRACTION_PARTIAL_FILES_NOT_ATTESTED")
    config_fields = "run_id mode quote_mode earnings_tier data_basis start end entry_dates initial_equity commission friction_bps synthetic_test".split()
    archive_fields = "storage_version applied_sequence applied_hash cutoff archived_events".split()
    manifest = {
        "status": STATUS, "extracted_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_checkpoint": str(source), "source_checkpoint_sha256": source_sha,
        "checkpoint_wrapper_sha256": wrapper["sha256"], "checkpoint_wrapper_hash_verified": True,
        "checkpoint_version": payload["version"], "source_unchanged_during_extraction": True,
        "saved_config": {key: payload["config"].get(key) for key in config_fields},
        "saved_boundaries": boundary, "saved_events_processed": state.get("events_processed", "UNKNOWN"),
        "saved_interim_cash_not_terminal_result": ledger["cash"],
        "saved_debt": ledger.get("debt", "UNKNOWN"),
        "archive_claim_from_json_only": {key: state.get("archive", {}).get(key, "UNKNOWN") for key in archive_fields},
        "sqlite_opened": False, "sqlite_full_chain_verified": False, "sqlite_or_checkpoint_copied": False,
        "events_run": 0, "engine_restored": False, "recovery_validation": "NOT_PERFORMED",
        "full_interval_completed": False, "terminal_profit_or_return": "NOT_CALCULATED_PARTIAL_EVIDENCE_ONLY",
        "price_or_nav_recomputed": False, "stored_cash_used_as_terminal_equity": False,
        "tables_contain": "Selected existing saved financial fields only, through checkpoint_event_cutoff_at; may include an unfinished session after latest_completed_trading_session_ny.",
        "missing_financial_values": "Blank means absent in the saved record, never replaced with zero or an invented value.",
        "mapping_identity_fields": "Object keys are copied to their matching order_id/fill_id/campaign_id/symbol/action_id columns where the saved row omitted that identity; no identity is invented.",
        "synthetic_fixture": payload["config"].get("synthetic_test") is True,
        "files": artifacts,
    }
    with (target / "PARTIAL_EVIDENCE.json").open("x", encoding="utf-8") as output:
        json.dump(manifest, output, ensure_ascii=False, indent=2, allow_nan=False)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description="Extract existing partial financial evidence only; never restore or run events.")
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--output", required=True, help="A new, separate directory below partial_evidence")
    args = parser.parse_args(argv)
    result = extract(args.checkpoint, args.output)
    print(json.dumps({"status": result["status"], "saved_boundaries": result["saved_boundaries"],
                      "wrapper_hash_verified": True, "sqlite_chain_verified": False, "events_run": 0}, ensure_ascii=False))


if __name__ == "__main__":
    main()
