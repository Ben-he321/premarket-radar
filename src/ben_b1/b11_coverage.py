"""Read-only aggregation of completed B1.1 evidence; never reruns research.

The displayed funnel order is conceptual. The engine actually checks earnings
and quotes before quantity-dependent stops/RR, so an unevaluated earlier display
column stays NOT_EVALUATED even when a later column contains a known failure.
Old coarse structure is never substituted for the full engine's own decision.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from .b11_runtime import ROOT, PREVIOUS, read, sha, utc, write


STAGES = ("identity", "warmup", "breakout", "stop_space", "earnings", "quote", "intent", "fill", "complete_campaign")
UNKNOWN_QUOTE_REASONS = {"QUOTE_MISSING", "QUOTE_SIZE_UNIT_UNKNOWN", "STALE_OR_FUTURE_QUOTE",
                         "QUOTE_NOT_YET_AVAILABLE", "INVALID_OR_CROSSED_QUOTE"}
STOP_FAILURES = {"NO_STRUCTURAL_STOP_WITHIN_7_PERCENT", "RR_BELOW_2", "ENTRY_PRICE_EXCEEDS_FIXED_LIMIT",
                 "ENTRY_STRUCTURE_INVALIDATED", "PARTIAL_ACTUAL_FILL_STOP_EXCEEDS_7_PERCENT",
                 "PARTIAL_QUANTITY_NET_RR_BELOW_2"}


def rows(path):
    path = Path(path)
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def parsed(value, default=None):
    if isinstance(value, (dict, list, bool, int, float)):
        return value
    if value in (None, "", "UNKNOWN"):
        return default
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default


def number(value):
    if value in (None, "", "UNKNOWN"):
        return None
    try:
        n = float(value)
        return int(n) if n.is_integer() else n
    except (ValueError, TypeError):
        return None


def truth(value):
    if isinstance(value, bool):
        return value
    if str(value).lower() in ("true", "1"):
        return True
    if str(value).lower() in ("false", "0"):
        return False
    return None


def _csv(path, records):
    fields = list(dict.fromkeys(key for row in records for key in row))
    with Path(path).open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(records)


def summarize_sample(sample, summary, decision, orders, tier, version):
    """Project known engine evidence; never infer downstream pass from coarse data."""
    present = summary is not None
    summary, decision = summary or {}, decision or {}
    reason = decision.get("reason") or "ENTRY_DECISION_OUTPUT_MISSING"
    out = {"sample_rank": sample["sample_rank"], "symbol": sample["symbol"],
        "sample_date": sample["trade_date"], "tier": tier, "run_version": version,
        "scope_policy": sample.get("scope_policy", "UNKNOWN"),
        "security_id": sample.get("local_security_id", "UNKNOWN"),
        "verified_global_security_id": sample.get("verified_global_security_id", "UNKNOWN"),
        "data_basis": summary.get("data_basis", "UNKNOWN"),
        **{stage: "NOT_EVALUATED" for stage in STAGES},
        "result_class": "DATA_UNKNOWN", "decisive_reason": reason,
        "actually_executed": summary.get("actually_executed", False),
        "engine_status": summary.get("status", "RUN_OUTPUT_MISSING"),
        "function_status": "NOT_EVALUATED" if not present else summary.get("engineering_status", "UNKNOWN"),
        "processed_through": summary.get("processed_through", "UNKNOWN"),
        "historical_network_received_at": "UNKNOWN",
        "recorded_download_received_at": decision.get("source_received_at", "UNKNOWN"),
        "signal_input_cutoff": decision.get("data_cutoff", "UNKNOWN"),
        "buy_intents": summary.get("buy_intents"), "buy_fills": summary.get("buy_fills"),
        "buy_shares": summary.get("buy_shares"), "sell_fills": summary.get("sell_fills"),
        "sell_shares": summary.get("sell_shares"),
        "closed_campaigns": summary.get("campaigns", {}).get("closed_campaigns"),
        "pending_exit_count": summary.get("pending_exit_count"),
        "unimplemented_features": json.dumps(summary.get("unimplemented_event_features", [])),
        "full_pool_account": False,
        "funnel_execution_order": "identity/warmup/signal/earnings/quote/ranking/quantity-dependent-stop-space/intent/fill/campaign",
    }
    scope = sample.get("scope_policy", "UNKNOWN")
    if scope != "KEEP":
        excluded = str(scope).startswith("EXCLUDE")
        out.update(identity="SCOPE_EXCLUDED" if excluded else "DATA_UNKNOWN",
                   result_class="SCOPE_EXCLUDED" if excluded else "DATA_UNKNOWN",
                   decisive_reason="SCOPE_EXCLUDED" if excluded else "IDENTITY_OR_SCOPE_UNKNOWN")
        return out
    out["identity"] = "PASS_LOCAL_IDENTITY_CURRENT_SCOPE"
    if not decision:
        return out
    count = number(decision.get("history_sessions"))
    out["history_sessions"] = count
    if count is not None:
        out["warmup"] = "PASS" if count >= 100 else "DATA_UNKNOWN"
    elif reason in ("CURRENT_SESSION_DAILY_INPUT_MISSING", "INDICATOR_WARMUP_INSUFFICIENT"):
        out["warmup"] = "DATA_UNKNOWN"
    detail = parsed(decision.get("signal_detail"), {})
    if detail and out["warmup"] != "DATA_UNKNOWN":
        signal = truth(detail.get("allowed"))
        if signal is not None:
            out["breakout"] = "PASS" if signal else "TRUE_RULE_FAILURE"
    rr, legs = parsed(decision.get("rr"), {}), parsed(decision.get("stop_legs"), [])
    if legs and rr:
        out["stop_space"] = "PASS" if truth(rr.get("allowed")) else "TRUE_RULE_FAILURE"
    if reason in STOP_FAILURES:
        out["stop_space"] = "TRUE_RULE_FAILURE"
    elif reason in ("OVERHEAD_INPUT_UNKNOWN", "SUPPORT_INPUT_UNKNOWN"):
        out["stop_space"] = "DATA_UNKNOWN"
    gate = parsed(decision.get("earnings"), {})
    if gate:
        if truth(gate.get("new_entry_allowed")):
            out["earnings"] = "PASS_PIT_A" if gate.get("strict_eligible") else "PASS_RETROSPECTIVE_B"
        else:
            out["earnings"] = "DATA_UNKNOWN" if gate.get("status") == "EARNINGS_UNKNOWN" else "TRUE_RULE_FAILURE"
        out["earnings_reason"] = gate.get("reason", "UNKNOWN")
        out["earnings_evidence_count"] = len(gate.get("evidence", []))
    if reason in UNKNOWN_QUOTE_REASONS:
        out["quote"] = "DATA_UNKNOWN"
    elif reason in ("SPREAD_TOO_WIDE", "DISPLAYED_QUANTITY_EXHAUSTED_OR_MISSING"):
        out["quote"] = "TRUE_RULE_FAILURE"
    elif number(decision.get("rank")) is not None or rr or legs or (number(summary.get("buy_intents")) or 0) > 0:
        out["quote"] = "PASS"
    buys = number(summary.get("buy_fills")) or 0
    intents = number(summary.get("buy_intents")) or 0
    if intents:
        out["intent"] = "CREATED"
        if buys:
            out["fill"] = "MODEL_FILLED"
        else:
            out["fill"] = "NO_MODEL_FILL"
            terminal = sorted({order.get("last_reason", "UNKNOWN") for order in orders if order.get("side") == "BUY"})
            out["fill_reasons"] = ";".join(terminal)
    if buys:
        closed = number(out["closed_campaigns"]) or 0
        if summary.get("stop_reason") == "NATURAL_CAMPAIGN_SETTLED" and closed > 0:
            out["complete_campaign"] = "COMPLETE_SETTLED"
        elif closed:
            out["complete_campaign"] = "CLOSED_PENDING_SETTLEMENT_OR_RECEIVABLE"
        else:
            out["complete_campaign"] = "OPEN_PENDING_EXIT" if (number(out["pending_exit_count"]) or 0) else "OPEN_AT_CHECKPOINT"
    if summary.get("unimplemented_event_features"):
        out["result_class"] = "FUNCTION_NOT_IMPLEMENTED"
    elif any(out[s] == "DATA_UNKNOWN" for s in STAGES) or decision.get("status") == "DATA_UNKNOWN":
        out["result_class"] = "DATA_UNKNOWN"
    elif out["complete_campaign"] == "OPEN_PENDING_EXIT":
        out.update(result_class="DATA_UNKNOWN", decisive_reason="HELD_POSITION_REQUIRED_EXIT_AWAITS_OBSERVED_EXECUTION_INPUT")
    elif any(out[s] == "TRUE_RULE_FAILURE" for s in STAGES):
        out["result_class"] = "TRUE_RULE_FAILURE"
    elif buys:
        out["result_class"] = "EVALUATED_WITH_MODEL_FILLS"
    elif intents:
        out["result_class"] = "ENGINE_EXECUTED_NO_MODEL_FILL"
    else:
        out["result_class"] = "DATA_UNKNOWN" if not detail else "TRUE_RULE_FAILURE"
    return out


def build(final_A_version="actual_v2", final_B_version="enriched_v3", *, output_dir=None):
    destination = Path(output_dir) if output_dir else ROOT
    destination.mkdir(parents=True, exist_ok=True)
    pinned = {}

    def record(path):
        path = Path(path)
        if path.exists():
            pinned[str(path)] = sha(path)
        return path

    selected = rows(record(ROOT / "data/FIRST20_FROZEN.csv"))
    policy = rows(record(PREVIOUS / "UNIVERSE_POLICY.csv"))
    if len(selected) != 20 or len(policy) != 66 or len({p["symbol"] for p in policy}) != 66:
        raise ValueError("FROZEN_COUNTS_REQUIRE_20_SAMPLES_AND_66_UNIQUE_CANDIDATES")
    funnel, missing = [], []
    for tier, version in (("A", final_A_version), ("B", final_B_version)):
        for rank, selected_row in enumerate(selected, 1):
            sample = {**selected_row, "sample_rank": rank}
            folder = ROOT / "replay" / version / f"tier_{tier}" / f"sample_{rank:02d}_{sample['symbol']}_{sample['trade_date']}_{tier}_{version}"
            summary_path = record(folder / "summary.json")
            summary = read(summary_path) if summary_path.exists() else None
            decisions = rows(record(folder / "coverage_funnel.csv"))
            candidate = [d for d in decisions if d.get("symbol") == sample["symbol"] and str(d.get("decision_time", ""))[:10] == sample["trade_date"]]
            if len(candidate) > 1:
                raise ValueError(f"AMBIGUOUS_FIRST_ENTRY_DECISION:{rank}:{tier}")
            if summary is None or not candidate:
                missing.append({"rank": rank, "tier": tier, "folder": str(folder), "missing_summary": summary is None,
                                "missing_entry_decision": not candidate})
            result = summarize_sample(sample, summary, candidate[0] if candidate else None,
                                      rows(record(folder / "orders.csv")), tier, version)
            result["evidence_directory"] = str(folder)
            funnel.append(result)
    old = {r["symbol"]: r for r in rows(record(PREVIOUS / "coarse/full_pool_coverage.csv"))}
    fixed = {r["symbol"]: r for r in rows(record(ROOT / "data/ALL66_FIXED_INTERVAL_COVERAGE.csv"))}
    quality = {r["symbol"]: r for r in rows(record(ROOT / "data/HISTORY_DATA_QUALITY.csv"))}
    histories = {r["symbol"]: r for r in read(record(ROOT / "data/HISTORY_INPUTS.json"))}
    earnings = read(record(ROOT / "earnings/PRIMARY_EARNINGS.json"))
    facts = {r["symbol"]: r for r in earnings["facts"]}
    inherited_path = record(ROOT / "data/INHERITED_TARGETED_EVIDENCE.json")
    inherited = read(inherited_path) if inherited_path.exists() else []
    universe = []
    for original in policy:
        symbol = original["symbol"]
        prior, current, hist, fact = old.get(symbol, {}), fixed.get(symbol, {}), histories.get(symbol), facts.get(symbol)
        plans = [p for p in earnings.get("plans", []) if p["symbol"] == symbol]
        sample_rows = [r for r in funnel if r["symbol"] == symbol]
        unknowns = []
        if original.get("global_security_id") == "UNKNOWN":
            unknowns.append("GLOBAL_SECURITY_ID_UNKNOWN_LOCAL_VERSION_RETAINED")
        if not hist:
            unknowns.append("NOT_IN_FROZEN_FIRST20_DEEP_INPUT_SAMPLE_NO_NEW_MINUTE_OR_QUOTE_COVERAGE_CLAIM")
        if not fact or not fact.get("coverage_complete"):
            unknowns.append((fact or {}).get("reason") or (fact or {}).get("limitations") or "CONTINUOUS_ACTUAL_EARNINGS_RELEASE_BOUNDARIES_UNKNOWN")
        if not plans:
            unknowns.append("PIT_PLANNED_RELEASE_VERSION_UNKNOWN")
        if current.get("fixed_calendar_gaps_within_observed") not in (None, "", "[]"):
            unknowns.append("FIXED_INTERVAL_CALENDAR_GAPS:" + current["fixed_calendar_gaps_within_observed"])
        unknowns.extend(f"{r['tier']}:{r['sample_date']}:{r['decisive_reason']}" for r in sample_rows if r["result_class"] == "DATA_UNKNOWN")
        identity_matches = any(r.get("symbol") == symbol and r.get("same_identity_version") for r in inherited)
        retained = [r for r in inherited if r.get("symbol") == symbol and
                    (r.get("same_identity_version") or (identity_matches and r.get("kind") == "SPECIFIC_HALT_EVIDENCE"))]
        universe.append({**original,
            "old_coarse_first": prior.get("first", "UNKNOWN"), "old_coarse_last": prior.get("last", "UNKNOWN"),
            "old_coarse_rows": prior.get("rows", "UNKNOWN"), "old_coarse_events": prior.get("coarse_events", "UNKNOWN"),
            "old_coarse_space_pass": prior.get("coarse_space_pass", "UNKNOWN"),
            "fixed_start": "2026-01-02", "fixed_end": "2026-09-11",
            "fixed_daily_rows": current.get("fixed_daily_rows", "UNKNOWN"),
            "fixed_calendar_gaps_within_observed": current.get("fixed_calendar_gaps_within_observed", "UNKNOWN"),
            "deep_input_status": "ACQUIRED_VENDOR_STANDARD_FIELDS" if hist else "NOT_IN_FIXED_DEEP_INPUT_SAMPLE",
            "deep_requested_start": hist.get("start") if hist else None,
            "deep_requested_end": hist.get("end") if hist else None,
            "deep_rth_rows": hist.get("rth", {}).get("rows") if hist else None,
            "deep_pre_start_sessions": hist.get("rth", {}).get("pre_start_valid_sessions") if hist else None,
            "deep_quality": json.dumps(quality.get(symbol, {}), ensure_ascii=False),
            "A_plan_status": "PIT_PLAN_AVAILABLE_ONLY_AFTER_RECORDED_BOUND" if plans else "DATA_UNKNOWN",
            "A_plan_known_bounds": json.dumps([p.get("conservative_known_at_ny") for p in plans]),
            "A_plan_release_dates": json.dumps([p.get("planned_release_date") for p in plans]),
            "B_earnings_status": "BOUNDED_RETROSPECTIVE_COVERAGE" if fact and fact.get("coverage_complete") else "DATA_UNKNOWN",
            "B_coverage_start": (fact or {}).get("coverage_start", "UNKNOWN"),
            "B_coverage_end": (fact or {}).get("coverage_end", "UNKNOWN"),
            "B_completeness_basis": (fact or {}).get("completeness_basis", "UNKNOWN"),
            "B_earnings_sources": json.dumps([(fact or {}).get("previous_source"), (fact or {}).get("next_source")]),
            "sample_events": len([r for r in sample_rows if r["tier"] == "A"]),
            "sample_A_result_classes": ";".join(sorted({r["result_class"] for r in sample_rows if r["tier"] == "A"})),
            "sample_B_result_classes": ";".join(sorted({r["result_class"] for r in sample_rows if r["tier"] == "B"})),
            "matching_inherited_targeted_evidence": json.dumps(retained, ensure_ascii=False),
            "specific_unknowns": json.dumps(sorted(set(unknowns)), ensure_ascii=False),
            "full_pool_account_coverage_established": False,
            "b1_original_scope_and_results_preserved": True})
    _csv(destination / "coverage_funnel.csv", funnel)
    _csv(destination / "UNIVERSE_COVERAGE.csv", universe)
    result = {"created_at": utc(), "versions": {"A": final_A_version, "B": final_B_version},
        "sample_rows": len(funnel), "universe_rows": len(universe), "deep_symbols": len(histories),
        "missing_outputs": missing, "categories": {c: sum(r["result_class"] == c for r in funnel) for c in sorted({r["result_class"] for r in funnel})},
        "source_files": pinned, "full_pool_account": False,
        "independent_first20_samples_are_not_a_common_capital_portfolio": True,
        "stage_not_reached": "NOT_EVALUATED; never false and never old-coarse substitution",
        "function_not_implemented_rows": sum(r["result_class"] == "FUNCTION_NOT_IMPLEMENTED" for r in funnel)}
    write(destination / "COVERAGE_RECONCILIATION.json", result)
    return result
