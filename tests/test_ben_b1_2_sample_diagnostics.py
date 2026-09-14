"""Read-only reason classification with in-memory mock evidence only."""
import pandas as pd

from src.ben_b1_2.sample_diagnostics import _reasons


def test_success_held_or_existing_intent_remain_observations_not_failures():
    table = pd.DataFrame([
        {"status": "HOLDING", "reason": "ALREADY_HELD"},
        {"status": "INTENT_EXISTS", "reason": "FIXED_INTENT"},
        {"status": "INTENT_CREATED", "reason": "QUALIFIED_NEW_INTENT"},
        {"status": "WATCHING", "reason": "WAITING_FOR_WINDOW"},
    ])
    original = table.copy(deep=True)
    observed, failed = _reasons(table)
    assert observed == table.reason.tolist()
    assert failed == []
    pd.testing.assert_frame_equal(table, original)


def test_unknown_and_rejected_are_failures_with_ordered_deduplication():
    table = pd.DataFrame([
        {"status": "DATA_UNKNOWN", "reason": "EARNINGS_UNKNOWN"},
        {"status": "HOLDING", "reason": "ALREADY_HELD"},
        {"status": "REJECTED", "reason": "PRICE_CAP"},
        {"status": "REJECTED", "reason": "PRICE_CAP"},
        {"status": "DATA_UNKNOWN", "reason": None},
    ])
    observed, failed = _reasons(table)
    assert observed == ["EARNINGS_UNKNOWN", "ALREADY_HELD", "PRICE_CAP"]
    assert failed == ["EARNINGS_UNKNOWN", "PRICE_CAP"]


def test_no_quote_rows_does_not_invent_failure_reason():
    assert _reasons(pd.DataFrame()) == ([], [])
