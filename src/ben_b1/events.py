"""Exchange-calendar clocks and versioned earnings gates for isolated B1 replay.

No calendar fetch, inferred release date, fabricated receipt time or account IO.
The schedule supplied by the caller must come from the exchange calendar and
span the necessary preceding/following sessions, including holidays/early closes.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Sequence

import pandas as pd

from .rules import aware


NY = "America/New_York"
MADRID = "Europe/Madrid"


def _sessions(schedule: pd.DataFrame) -> list[tuple[date, pd.Timestamp, pd.Timestamp]]:
    if not {"market_open", "market_close"}.issubset(schedule):
        raise ValueError("EXCHANGE_OPEN_CLOSE_REQUIRED")
    records = [(aware(row.market_open).tz_convert(NY).date(), aware(row.market_open), aware(row.market_close))
               for row in schedule.itertuples()]
    if any(end <= start for _, start, end in records):
        raise ValueError("INVALID_EXCHANGE_SESSION")
    records.sort(key=lambda item: item[0])
    if len({day for day, _, _ in records}) != len(records):
        raise ValueError("DUPLICATE_EXCHANGE_SESSION")
    return records


def session_clock(schedule: pd.DataFrame, session_date: object) -> dict:
    selected = pd.Timestamp(session_date).date()
    matches = [row for row in _sessions(schedule) if row[0] == selected]
    if len(matches) != 1:
        raise ValueError("NOT_AN_EXCHANGE_SESSION")
    _, opened, closed = matches[0]
    times = {"market_open": opened, "market_close": closed,
             "active_exit_decision": closed - pd.Timedelta(minutes=10),
             "entry_decision": closed + pd.Timedelta(minutes=5),
             "entry_expiry": closed + pd.Timedelta(minutes=15)}
    return {"session_date": str(selected), **{key: value.isoformat() for key, value in times.items()},
            "new_york": {key: value.tz_convert(NY).isoformat() for key, value in times.items()},
            "madrid": {key: value.tz_convert(MADRID).isoformat() for key, value in times.items()}}


def first_executable_rth(schedule: pd.DataFrame, known_at: object) -> pd.Timestamp | None:
    known = aware(known_at)
    for _, opened, closed in _sessions(schedule):
        if known < opened:
            return opened
        if opened <= known < closed:
            return known
    return None


def completed_sessions_since_exit(schedule: pd.DataFrame, exit_time: object,
                                   decision_time: object) -> int:
    """Count only entire sessions after the exit; a partly elapsed exit day fails."""
    exited, decision = aware(exit_time), aware(decision_time)
    if decision < exited:
        return 0
    return sum(opened >= exited and closed <= decision for _, opened, closed in _sessions(schedule))


@dataclass(frozen=True)
class EarningsRevision:
    """One immutable event revision. ``known_at`` is public/observed availability.

    ``actual_release_at`` alone permits only tier B; a separate announcement of
    the schedule with ``known_at`` before the decision is required for tier A.
    Financial quarter end, conference-call times and SEC filing dates must use
    a different event_kind and therefore cannot qualify as a release.
    """
    event_id: str
    planned_date: object | None = None
    known_at: object | None = None
    actual_release_at: object | None = None
    release_known_at: object | None = None
    event_kind: str = "earnings_release"
    time_class: str = "UNKNOWN"
    source_received_at: object | None = None
    source: str = "UNKNOWN"
    conflict: bool = False
    actual_release_date: object | None = None
    actual_time_precision: str = "UNKNOWN"


def _actual_date(event: EarningsRevision) -> date | None:
    """An issuer's verified date is useful evidence without inventing a time."""
    day = pd.Timestamp(event.actual_release_date).date() if event.actual_release_date is not None else None
    if event.actual_release_at is not None:
        exact_day = aware(event.actual_release_at).tz_convert(NY).date()
        if day is not None and day != exact_day:
            raise ValueError("CONFLICTING_RELEASE_DATE_AND_TIMESTAMP")
        day = exact_day
    return day


def _release_complete_asof(event: EarningsRevision, decision: pd.Timestamp) -> bool:
    if event.release_known_at is not None and aware(event.release_known_at) > decision:
        return False
    if event.actual_release_at is not None:
        return aware(event.actual_release_at) <= decision
    actual_day = _actual_date(event)
    # Date precision establishes that the release happened before the next NY
    # date. This boolean bound is not a fabricated 23:59 or receipt timestamp.
    return actual_day is not None and actual_day < decision.tz_convert(NY).date()


def earnings_window(schedule: pd.DataFrame, release_date: object) -> dict:
    """For non-session announcements, take the preceding three real sessions."""
    day = pd.Timestamp(release_date).date()
    sessions = _sessions(schedule)
    before = [s for s in sessions if s[0] < day]
    after = [s for s in sessions if s[0] > day]
    if len(before) < 4 or not after:
        raise ValueError("EARNINGS_EXCHANGE_CALENDAR_SPAN_INSUFFICIENT")
    embargo = before[-3:]
    prior_session = before[-4]
    return {"planned_release_date": str(day),
            "forbidden_preceding_sessions": [str(s[0]) for s in embargo],
            "release_day": str(day),
            "required_exit_time": (prior_session[2] - pd.Timedelta(minutes=10)).isoformat(),
            "earliest_resume_time": (after[0][2] + pd.Timedelta(minutes=5)).isoformat(),
            "resume_session": str(after[0][0])}


def _unknown(decision: pd.Timestamp, schedule: pd.DataFrame, reason: str,
             evidence: list[dict] | None = None) -> dict:
    next_rth = first_executable_rth(schedule, decision)
    return {"status": "EARNINGS_UNKNOWN", "tier": "C_UNKNOWN", "new_entry_allowed": False,
            "strict_eligible": False, "research_label": "UNKNOWN",
            "reason": reason, "required_exit_time": next_rth.isoformat() if next_rth is not None else None,
            "holding_action": "EXIT_FIRST_EXECUTABLE_RTH_OR_PENDING_NO_QUOTE",
            "decision_time": decision.isoformat(), "evidence": evidence or []}


def earnings_gate(decision_time: object, schedule: pd.DataFrame,
                  revisions: Sequence[EarningsRevision], retrospective: bool = False) -> dict:
    """Evaluate only event versions knowable by decision, except explicit B replay.

    Tier B is a separate actual-release-date exclusion experiment. Its future
    release dates are deliberately hindsight inputs and its result never has
    ``strict_eligible=True``. No events/no known next date fail closed.

    Conflicting/unknown dates for a held campaign require its first possible RTH
    exit; this function never fabricates an executable quote or a fill.
    """
    decision = aware(decision_time)
    known: dict[str, tuple[pd.Timestamp | None, EarningsRevision, str]] = {}
    relevant = [event for event in revisions if event.event_kind == "earnings_release"]
    for event in relevant:
        if retrospective:
            try:
                actual_day = _actual_date(event)
            except ValueError:
                return _unknown(decision, schedule, "CONFLICTING_RELEASE_DATE_AND_TIMESTAMP")
            if actual_day is None:
                continue
            # All historical actual dates are deliberately visible in tier B only.
            when = aware(event.known_at) if event.known_at is not None else None
            event_date = str(actual_day)
        else:
            if event.known_at is None or aware(event.known_at) > decision:
                continue
            when = aware(event.known_at)
            event_date = str(pd.Timestamp(event.planned_date).date()) if event.planned_date is not None else "UNKNOWN"
        if (event.event_id in known and when == known[event.event_id][0]
                and event_date != known[event.event_id][2]):
            return _unknown(decision, schedule, "CONFLICTING_SIMULTANEOUS_DATE_VERSIONS")
        previous_when = known[event.event_id][0] if event.event_id in known else None
        if event.event_id not in known or (when is not None and (previous_when is None or when > previous_when)):
            known[event.event_id] = (when, event, event_date)
    if not known:
        return _unknown(decision, schedule, "NO_KNOWN_RELEASE_CALENDAR_EMPTY_IS_NOT_CLEAR")
    evidence: list[dict] = []
    candidates: list[tuple[dict, EarningsRevision, bool]] = []
    for _, event, event_date in sorted(known.values(), key=lambda item: (item[2], item[1].event_id)):
        receipt = "UNKNOWN" if event.source_received_at is None else aware(event.source_received_at).isoformat()
        if event.conflict or event_date == "UNKNOWN":
            return _unknown(decision, schedule, "DATE_CONFLICT_OR_UNKNOWN", evidence)
        try:
            window = earnings_window(schedule, event_date)
        except ValueError:
            return _unknown(decision, schedule, "EXCHANGE_CALENDAR_SPAN_INSUFFICIENT", evidence)
        try:
            actual_day = _actual_date(event)
            release_confirmed = _release_complete_asof(event, decision)
        except ValueError:
            return _unknown(decision, schedule, "CONFLICTING_RELEASE_DATE_AND_TIMESTAMP", evidence)
        if actual_day is not None:
            # A newly known early release changes today's exit/recovery handling,
            # but cannot rewrite any prior decision or claim three-day avoidance.
            if not retrospective and release_confirmed and actual_day != pd.Timestamp(event_date).date():
                window = earnings_window(schedule, actual_day)
                window["unexpected_date_change"] = True
                window["hypothetical_prior_exit_not_knowable"] = window["required_exit_time"]
                # For date-only evidence there is no historical exact detection
                # time. Use the present decision's first possible RTH exit and
                # retain the unknown bound, never a reconstructed earlier fill.
                first_known = (aware(event.release_known_at) if event.release_known_at is not None
                               else aware(event.actual_release_at) if event.actual_release_at is not None
                               else decision)
                first_possible = first_executable_rth(schedule, first_known)
                if first_possible is None:
                    return _unknown(decision, schedule, "NO_EXECUTABLE_RTH_AFTER_UNEXPECTED_RELEASE", evidence)
                window["required_exit_time"] = first_possible.isoformat()
        evidence.append({"event_id": event.event_id, "source": event.source,
                         "plan_known_at": "UNKNOWN" if event.known_at is None else aware(event.known_at).isoformat(),
                         "source_received_at": receipt, "release_confirmed_asof": release_confirmed,
                         "actual_release_date": str(actual_day) if actual_day is not None else "UNKNOWN",
                         "actual_release_at": aware(event.actual_release_at).isoformat() if event.actual_release_at is not None else "UNKNOWN",
                         "actual_time_precision": "TIMESTAMP" if event.actual_release_at is not None else "DATE" if actual_day is not None else "UNKNOWN",
                         "release_completion_basis": "EXACT_TIME" if event.actual_release_at is not None else "AFTER_RELEASE_NY_DATE_BOUND" if actual_day is not None else "UNKNOWN",
                         "time_class": event.time_class, **window})
        candidates.append((window, event, release_confirmed))
    # Select upcoming or still unreleased/unrecovered event. Fully old events do
    # not imply that the next quarter is safely known.
    active = [(window, event, confirmed) for window, event, confirmed in candidates
              if decision < aware(window["earliest_resume_time"]) or not confirmed]
    if not active:
        return _unknown(decision, schedule, "NEXT_RELEASE_DATE_UNKNOWN", evidence)
    tier = "B_ACTUAL_RELEASE_ONLY" if retrospective else "A_VERIFIED_PIT"
    required_exits = []
    reasons = []
    for window, event, confirmed in active:
        required = aware(window["required_exit_time"])
        resume = aware(window["earliest_resume_time"])
        if decision >= required and (decision < resume or not confirmed):
            required_exits.append(required)
            reasons.append("REPORT_NOT_CONFIRMED_RELEASED" if decision >= resume and not confirmed else "EARNINGS_NO_HOLD_WINDOW")
    allowed = not required_exits
    # Historical tier A needs both the prior public plan and actual release
    # evidence. Absence of an actual record is an evidence gap, not a safe pass.
    if not retrospective and any(_actual_date(event) is None for _, event, _ in active):
        return _unknown(decision, schedule, "ACTUAL_RELEASE_EVIDENCE_MISSING_FOR_PIT_RESEARCH", evidence)
    return {"status": "EARNINGS_CLEAR" if allowed else "EARNINGS_BLACKOUT",
            "tier": tier, "new_entry_allowed": allowed, "strict_eligible": allowed and not retrospective,
            "research_label": "RETROSPECTIVE_EARNINGS_EXCLUSION" if retrospective else "VERIFIED_PIT",
            "reason": "KNOWN_NEXT_EVENT_OUTSIDE_WINDOW" if allowed else ";".join(sorted(set(reasons))),
            "required_exit_time": min(required_exits).isoformat() if required_exits else None,
            "holding_action": "NONE" if allowed else "EXIT_AT_REQUIRED_TIME_OR_FIRST_EXECUTABLE_RTH",
            "decision_time": decision.isoformat(), "evidence": evidence}
