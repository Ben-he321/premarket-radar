"""SYNTHETIC report metadata only; no market data or strategy calculations."""
import json

from src.ben_b1 import b11_coverage as coverage


def sample():
    return {"sample_rank": 1, "symbol": "X", "trade_date": "2026-01-02", "scope_policy": "KEEP",
            "local_security_id": "MOCK_X", "verified_global_security_id": "UNKNOWN"}


def summary(**extra):
    return {"actually_executed": True, "engineering_status": "COMPLETED_EVENT_ORCHESTRATION",
            "unimplemented_event_features": [], "buy_intents": 0, "buy_fills": 0, "buy_shares": 0,
            "sell_fills": 0, "sell_shares": 0, "pending_exit_count": 0,
            "campaigns": {"closed_campaigns": 0}, **extra}


def decision(**extra):
    return {"decision_time": "2026-01-02T21:05:00+00:00", "symbol": "X", "history_sessions": "150",
            "signal_detail": json.dumps({"allowed": True, "reason": "FRESH_CROSS"}),
            "status": "DATA_UNKNOWN", "reason": "NEXT_RELEASE_DATE_UNKNOWN",
            "earnings": json.dumps({"status": "EARNINGS_UNKNOWN", "new_entry_allowed": False}), **extra}


def test_earnings_unknown_does_not_turn_unevaluated_stops_quotes_into_failures():
    result = coverage.summarize_sample(sample(), summary(), decision(), [], "A", "mock")
    assert result["identity"] == "PASS_LOCAL_IDENTITY_CURRENT_SCOPE"
    assert result["warmup"] == result["breakout"] == "PASS"
    assert result["earnings"] == "DATA_UNKNOWN"
    assert all(result[k] == "NOT_EVALUATED" for k in ("stop_space", "quote", "intent", "fill", "complete_campaign"))
    assert result["result_class"] == "DATA_UNKNOWN"
    assert result["buy_fills"] == 0  # Actual execution count, not a downstream test result.


def test_natural_false_signal_is_rule_failure_but_short_history_is_unknown():
    for count, reason, expected in [(150, "NO_FRESH_CROSS", "TRUE_RULE_FAILURE"),
                                     (36, "INDICATOR_WARMUP_INSUFFICIENT", "DATA_UNKNOWN")]:
        d = decision(history_sessions=count, signal_detail=json.dumps({"allowed": False, "reason": reason}),
                     earnings="", reason=reason, status="REJECTED" if count == 150 else "DATA_UNKNOWN")
        row = coverage.summarize_sample(sample(), summary(), d, [], "A", "mock")
        assert row["result_class"] == expected
        assert row["breakout"] == ("TRUE_RULE_FAILURE" if count == 150 else "NOT_EVALUATED")


def test_retrospective_gate_is_not_promoted_to_pit_and_completed_fills_remain_known():
    d = decision(status="INTENT_CREATED", reason="FROZEN_RULES_PASSED", rank="1",
                 earnings=json.dumps({"new_entry_allowed": True, "strict_eligible": False}),
                 rr=json.dumps({"allowed": True}), stop_legs=json.dumps([{"quantity": 1, "stop": 1}]))
    s = summary(buy_intents=1, buy_fills=1, buy_shares=1, sell_fills=1, sell_shares=1,
                campaigns={"closed_campaigns": 1}, stop_reason="NATURAL_CAMPAIGN_SETTLED")
    row = coverage.summarize_sample(sample(), s, d, [], "B", "mock")
    assert row["earnings"] == "PASS_RETROSPECTIVE_B"
    assert row["quote"] == row["stop_space"] == "PASS"
    assert row["fill"] == "MODEL_FILLED" and row["complete_campaign"] == "COMPLETE_SETTLED"
    assert row["result_class"] == "EVALUATED_WITH_MODEL_FILLS"


def test_held_missing_exit_does_not_delete_buy_or_report_completed_campaign():
    d = decision(status="INTENT_CREATED", reason="FROZEN_RULES_PASSED", rank="1",
                 earnings=json.dumps({"new_entry_allowed": True, "strict_eligible": False}))
    row = coverage.summarize_sample(sample(), summary(buy_intents=1, buy_fills=1, buy_shares=1, pending_exit_count=1), d, [], "B", "mock")
    assert row["fill"] == "MODEL_FILLED" and row["buy_shares"] == 1
    assert row["complete_campaign"] == "OPEN_PENDING_EXIT" and row["result_class"] == "DATA_UNKNOWN"


def test_missing_run_has_unknown_counts_and_function_failure_is_separate():
    row = coverage.summarize_sample(sample(), None, None, [], "B", "missing")
    assert row["buy_fills"] is None and not row["actually_executed"]
    assert row["function_status"] == "NOT_EVALUATED"
    unimplemented = coverage.summarize_sample(sample(), summary(unimplemented_event_features=["TEST_UNIMPLEMENTED"]), decision(), [], "A", "mock")
    assert unimplemented["result_class"] == "FUNCTION_NOT_IMPLEMENTED"


def test_scope_exclusion_does_not_evaluate_a_false_breakout():
    row = coverage.summarize_sample({**sample(), "scope_policy": "EXCLUDE_OIL_GAS"}, summary(), decision(), [], "A", "mock")
    assert row["result_class"] == row["identity"] == "SCOPE_EXCLUDED"
    assert row["breakout"] == "NOT_EVALUATED"


def test_full_build_keeps_all_66_and_40_rows_without_missing_version_fallback(tmp_path, monkeypatch):
    root, previous = tmp_path / "current", tmp_path / "previous"
    for p in (root / "data", root / "earnings", previous / "coarse"):
        p.mkdir(parents=True)
    monkeypatch.setattr(coverage, "ROOT", root)
    monkeypatch.setattr(coverage, "PREVIOUS", previous)
    policies = [{"symbol": f"S{i:02d}", "scope_policy": "KEEP", "global_security_id": "UNKNOWN", "identity_version": str(i)} for i in range(66)]
    selected = [{"symbol": p["symbol"], "trade_date": "2026-01-02", "scope_policy": "KEEP", "local_security_id": "MOCK_" + p["symbol"]} for p in policies[:20]]
    coverage._csv(root / "data/FIRST20_FROZEN.csv", selected)
    coverage._csv(previous / "UNIVERSE_POLICY.csv", policies)
    for path in (previous / "coarse/full_pool_coverage.csv", root / "data/ALL66_FIXED_INTERVAL_COVERAGE.csv", root / "data/HISTORY_DATA_QUALITY.csv"):
        coverage._csv(path, [{"symbol": p["symbol"]} for p in policies])
    (root / "data/HISTORY_INPUTS.json").write_text("[]")
    (root / "earnings/PRIMARY_EARNINGS.json").write_text(json.dumps({"facts": [], "plans": []}))
    for i, s in enumerate(selected, 1):
        folder = root / "replay/known_A/tier_A" / f"sample_{i:02d}_{s['symbol']}_2026-01-02_A_known_A"
        folder.mkdir(parents=True)
        (folder / "summary.json").write_text(json.dumps(summary()))
        coverage._csv(folder / "coverage_funnel.csv", [decision(symbol=s["symbol"])])
    result = coverage.build("known_A", "missing_B")
    assert result["sample_rows"] == 40 and result["universe_rows"] == 66
    assert len(result["missing_outputs"]) == 20
    all_rows = coverage.rows(root / "coverage_funnel.csv")
    b = [r for r in all_rows if r["tier"] == "B"]
    assert len(b) == 20 and all(r["run_version"] == "missing_B" for r in b)
    assert all(r["buy_fills"] == "" and r["warmup"] == "NOT_EVALUATED" for r in b)
    assert len(coverage.rows(root / "UNIVERSE_COVERAGE.csv")) == 66
