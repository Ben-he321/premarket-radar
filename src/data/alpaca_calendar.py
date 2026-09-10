"""Exchange sessions and timezone-aware, conservative daily-bar cutoff."""
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo
import pandas_market_calendars as mcal

NY = ZoneInfo("America/New_York")
MADRID = ZoneInfo("Europe/Madrid")


@lru_cache(maxsize=128)
def sessions(start: date, end: date) -> tuple[date, ...]:
    if start > end:
        return ()
    return tuple(mcal.get_calendar("NYSE").valid_days(start, end).date)


def finalized_day(now: datetime | None = None, cutoff_hour: int = 6) -> date:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    local = now.astimezone(NY)
    candidates = sessions(local.date() - timedelta(days=30), local.date())
    # Entire NY calendar day must have elapsed, including extended hours,
    # followed by a six-hour correction buffer. Never ingest today's bar.
    return max(d for d in candidates if
               datetime.combine(d + timedelta(days=1), time(cutoff_hour), NY) <= local)


def request_bounds(start: date, end: date):
    return (datetime.combine(start, time(), NY),
            datetime.combine(end + timedelta(days=1), time(), NY) - timedelta(microseconds=1))
