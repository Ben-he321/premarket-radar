"""Read completed Q0/Q1 sample evidence without rerunning any calculation."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from .runtime import ROOT, B11, read, write, sha, utc


def _table(path):
    if not Path(path).is_file():
        return pd.DataFrame()
    try:
        return pd.read_csv(path)
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def _known(value):
    return value is not None and not (isinstance(value, float) and pd.isna(value))


def build(version="initial_v1", output=None):
    output = Path(output) if output else ROOT/"samples"/version/"comparison"
    output.mkdir(parents=True, exist_ok=True)
    rows, sources = [], {}
    first20 = pd.read_csv(B11/"data/FIRST20_FROZEN.csv")
    for tier in ("A", "B"):
        for rank, sample in enumerate(first20.to_dict("records"), 1):
            symbol, day = sample["symbol"], sample["trade_date"]
            q0_dir = B11/"replay/final_v4"/f"tier_{tier}"/f"sample_{rank:02d}_{symbol}_{day}_{tier}_final_v4"
            q1_dir = ROOT/"samples"/version/f"tier_{tier}"/f"sample_{rank:02d}_{symbol}_{day}_{tier}_{version}"
            q0_path, q1_path = q0_dir/"summary.json", q1_dir/"summary.json"
            q0 = read(q0_path) if q0_path.exists() else {}
            q1 = read(q1_path) if q1_path.exists() else {}
            recovery_path = q1_dir/"RECOVERY_IDEMPOTENCY.json"
            recovery = read(recovery_path) if recovery_path.exists() else {}
            q0_decisions = _table(q0_dir/"coverage_funnel.csv")
            q1_evaluations = _table(q1_dir/"q1_quote_evaluations.csv")
            q1_decisions = _table(q1_dir/"coverage_funnel.csv")
            q1_orders = _table(q1_dir/"orders.csv")
            coverage_path = q1_dir/"WINDOW_QUOTE_COVERAGE.json"
            coverage = read(coverage_path) if coverage_path.exists() else {}
            for path in (q0_path, q1_path, q0_dir/"coverage_funnel.csv", q1_dir/"q1_quote_evaluations.csv", coverage_path, recovery_path):
                if path.is_file():
                    sources[str(path)] = sha(path)
            q0_reason = q0_decisions.iloc[0]["reason"] if len(q0_decisions) else "Q0_RESULT_MISSING"
            creates = q1_evaluations[q1_evaluations.status.eq("INTENT_CREATED")] if len(q1_evaluations) else pd.DataFrame()
            first_create = creates.sort_values("decision_time").iloc[0].to_dict() if len(creates) else {}
            first_intent = first_create.get("actual_intent_time")
            bad = q1_evaluations.copy()
            if _known(first_intent) and len(bad):
                bad = bad[pd.to_datetime(bad.decision_time, utc=True) < pd.Timestamp(first_intent)]
            if len(bad):
                bad = bad[~bad.status.isin(["HOLDING", "INTENT_EXISTS", "INTENT_CREATED"])]
            bad_reasons = list(dict.fromkeys(bad.reason.dropna().astype(str))) if len(bad) else []
            all_reasons = list(dict.fromkeys(q1_evaluations.reason.dropna().astype(str))) if len(q1_evaluations) else []
            if not all_reasons and len(q1_decisions):
                all_reasons = list(dict.fromkeys(q1_decisions.reason.dropna().astype(str)))
            buys = q1_orders[q1_orders.side.eq("BUY")] if len(q1_orders) else pd.DataFrame()
            complete = bool(q1.get("sample_run_complete"))
            if not complete:
                result = "NOT_COMPLETED_OR_NOT_RUN"
            elif q1.get("error_count", 0):
                result = "ENGINE_ERROR_NOT_ZERO_TRADE_SUCCESS"
            elif q1.get("buy_fills", 0):
                result = "ACTUAL_REPLAY_MODEL_FILL"
            elif coverage.get("window_new_quote_records") == 0:
                result = "NO_WINDOW_QUOTE"
            else:
                result = "WINDOW_QUOTES_DID_NOT_PASS_ALL_FROZEN_RULES_OR_EVIDENCE_GATES"
            pending = q1.get("pending_exit_count")
            positions = q1.get("open_positions", {})
            equity = q1.get("latest_equity") or {}
            if not q1.get("buy_fills", 0):
                exit_status = "NO_ENTRY_NO_EXIT_CLAIM"
            elif pending or positions:
                exit_status = "POSITION_OR_PENDING_EXIT_RETAINED"
            elif equity.get("unsettled_cash", 0) or equity.get("dividend_receivable", 0):
                exit_status = "EXITED_WITH_UNSETTLED_OR_RECEIVABLE_COMPONENT"
            else:
                exit_status = "NATURAL_CAMPAIGN_CLOSED_AND_SETTLED"
            rows.append({"sample_rank": rank, "symbol": symbol, "sample_date": day, "earnings_tier": tier,
                "Q0_reason": q0_reason, "original_fixed_point_quote_blocker": q0_reason in ("STALE_OR_FUTURE_QUOTE", "QUOTE_MISSING"),
                "Q0_buy_fills": q0.get("buy_fills"), "Q0_result_sha256": sources.get(str(q0_path)),
                "window_quote_count": coverage.get("window_new_quote_records"),
                "fixed_point_quotes_age_le5s": coverage.get("fixed_point_quote_records_age_le5s"),
                "first_window_at": coverage.get("first_window_quote_time"),
                "first_all_rule_pass_time": first_create.get("decision_time"), "actual_intention_time": first_intent,
                "first_bad_reason": bad_reasons[0] if bad_reasons else None,
                "first_bad_reasons": json.dumps(bad_reasons, ensure_ascii=False),
                "whole_window_reason": result, "whole_window_failed_checks": json.dumps(all_reasons, ensure_ascii=False),
                "Q1_status": q1.get("status", "NOT_COMPLETED_OR_NOT_RUN"), "Q1_buy_intents": q1.get("buy_intents"),
                "Q1_coverage_status": q1.get("coverage_status", "NOT_DECLARED_OR_NOT_RUN"),
                "Q1_data_unknown_decision_count": q1.get("data_unknown_decision_count"),
                "Q1_buy_fills": q1.get("buy_fills"), "Q1_buy_shares": q1.get("buy_shares"),
                "Q1_sell_fills": q1.get("sell_fills"), "Q1_sell_shares": q1.get("sell_shares"),
                "partial_intents": int((buys.filled_quantity.gt(0) & buys.filled_quantity.lt(buys.quantity)).sum()) if len(buys) else 0,
                "fully_filled_intents": int(buys.filled_quantity.eq(buys.quantity).sum()) if len(buys) else 0,
                "exit_coverage": exit_status, "pending_exits": pending, "positions_remaining": len(positions),
                "cash": equity.get("cash"), "net_equity": equity.get("net_equity"),
                "unsettled_cash": equity.get("unsettled_cash"), "dividend_receivable": equity.get("dividend_receivable"),
                "closed_campaign_net_profit": sum(c["net_profit"] for c in q1.get("campaigns", {}).get("campaigns", []) if c.get("net_profit") is not None) if q1 else None,
                "processed_through": q1.get("processed_through"), "errors": q1.get("error_count"),
                "sample_run_complete": complete, "earnings_evidence": "ORIGINAL_B11_UNCHANGED",
                "recovery_status": recovery.get("status", "MISSING_NOT_VERIFIED"),
                "recovery_same_state": recovery.get("same_state"),
                "completion_evidence_status": "EXECUTED_AND_RECOVERY_VERIFIED" if complete and recovery.get("status") == "PASS" and recovery.get("same_state") is True else "EXECUTION_OR_RECOVERY_NOT_FULLY_VERIFIED",
                "scope": "INDEPENDENT_ENGINEERING_SAMPLE_NOT_PORTFOLIO", "Q1_evidence": "POST_REVIEW_EXECUTION_HYPOTHESIS"})
    table = pd.DataFrame(rows)
    table.to_csv(output/"Q0_Q1_ORIGINAL20_COMPARISON.csv", index=False, encoding="utf-8-sig")
    blocked = table[table.earnings_tier.eq("B") & table.original_fixed_point_quote_blocker]
    blocked.to_csv(output/"ORIGINAL12_FIXED_POINT_BLOCKERS.csv", index=False, encoding="utf-8-sig")
    summary = {"created_at": utc(), "version": version, "rows": len(table), "all40_completed": bool(table.sample_run_complete.all()),
        "all40_recovery_pass": bool(table.recovery_status.eq("PASS").all() and table.recovery_same_state.eq(True).all()),
        "recovery_missing_or_failed_count": int((~table.recovery_status.eq("PASS") | ~table.recovery_same_state.eq(True)).sum()),
        "original_B_quote_blockers": len(blocked),
        "blockers_window_has_quotes": int(blocked.window_quote_count.fillna(0).gt(0).sum()),
        "blockers_window_has_no_quotes": int(blocked.window_quote_count.eq(0).sum()),
        "blockers_actual_Q1_model_entries": int(blocked.Q1_buy_fills.fillna(0).gt(0).sum()),
        "blockers_completed_with_quotes_but_no_entry": int((blocked.sample_run_complete & blocked.window_quote_count.fillna(0).gt(0) & blocked.Q1_buy_fills.fillna(0).eq(0)).sum()),
        "blockers_not_completed": int((~blocked.sample_run_complete).sum()),
        "input_files": sources, "interpretation": "Window availability is not executable qualification. A closed financial campaign or recovery PASS does not establish that every input and observation has complete coverage; per-row coverage status and unknown decision counts preserve the source summary. Independent5500 accounts are not a pooled strategy curve. Q1 is a post-review execution hypothesis, not blind out-of-sample validation."}
    write(output/"COMPARISON_SUMMARY.json", summary)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", default="initial_v1")
    args = parser.parse_args()
    print(json.dumps(build(args.version), ensure_ascii=False, default=str))
