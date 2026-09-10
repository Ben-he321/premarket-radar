"""Resumable monthly downloads, recent reconciliation, versioned publication."""
from datetime import date, timedelta
from importlib.metadata import version
import uuid

from .alpaca_calendar import finalized_day, request_bounds, sessions
from .alpaca_client import DataAccessError
from .alpaca_config import HISTORY_START, REFERENCES, SYMBOLS
from .alpaca_quality import inspect_quality
from .alpaca_store import utc_now


def months(start, end):
    cursor = start
    while cursor <= end:
        next_month = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
        finish = min(end, next_month - timedelta(days=1))
        yield cursor.strftime("%Y-%m"), cursor, finish
        cursor = next_month


def overlap_changed(old, new, old_end):
    """New dates don't imply a split; changed/deleted old adjusted bars do."""
    keys = ["symbol", "trade_date"]
    fields = ["open", "high", "low", "close", "volume", "vwap", "trade_count"]
    old = old[keys + fields].sort_values(keys).reset_index(drop=True)
    new = new[new.trade_date <= old_end][keys + fields].sort_values(keys).reset_index(drop=True)
    return not old.equals(new)


class DataSync:
    def __init__(self, client, store, config):
        self.client, self.store, self.config = client, store, config

    def update(self, *, full_refresh=False, now=None, progress=None):
        end = finalized_day(now, self.config.finalize_hour_ny)
        with self.store.lock():
            # Each actual download is gated by a short, three-symbol SIP request.
            probe = self.client.probe("historical", end)
            self.store.write_json("historical_check.json", probe)
            if probe["status"] != "ACCESS_OK":
                raise DataAccessError(probe["status"])
            results = {}
            for adjustment in ("raw", "all"):
                try:
                    results[adjustment] = self._update(adjustment, end, full_refresh, progress)
                    self.store.write_json(f"{adjustment}/last_attempt.json",
                                          {"status": "COMPLETE", "time": utc_now(), "end": str(end)})
                except DataAccessError as exc:
                    self.store.write_json(f"{adjustment}/last_attempt.json",
                                          {"status": exc.code, "time": utc_now(), "end": str(end),
                                           "note": "API failure; not a halt, holiday or pre-listing classification"})
                    raise
            return results

    def _update(self, adjustment, end, force, progress):
        active = self.store.active(adjustment)
        previous_end = date.fromisoformat(active["end"]) if active else HISTORY_START
        recent_sessions = sessions(previous_end - timedelta(days=60), previous_end)
        overlap = recent_sessions[-self.config.revision_sessions] if len(recent_sessions) >= self.config.revision_sessions else HISTORY_START
        overlap = max(HISTORY_START, overlap.replace(day=1))
        plan = list(months(HISTORY_START, end))
        pending = self.store.read_json(f"{adjustment}/pending.json", {})
        if pending.get("base_version") != active.get("version"):
            pending = {}
        # A partial all-adjusted rebuild crossing NY dates could mix adjustment bases.
        if adjustment == "all" and pending.get("end") != str(end):
            pending = {}
        if force and not pending.get("full_refresh"):
            pending = {}
        force = force or pending.get("full_refresh", False)
        fetched = {}
        if adjustment == "all" and active and not force:
            # Reconcile overlap BEFORE publishing any new adjusted data. A corporate
            # action can change the entire past: rebuild all, not just the tail.
            old = self.store.read(adjustment)
            for key, start, finish in plan:
                if start < overlap:
                    continue
                new = self.client.bars(SYMBOLS, start, finish, adjustment)
                fetched[key] = new
                old_chunk = old[old.trade_date.between(str(start), str(finish))]
                if overlap_changed(old_chunk, new, str(previous_end)):
                    force = True
            if force and not pending.get("full_refresh"):
                pending = {}
        pending = {"base_version": active.get("version"), "end": str(end),
                   "full_refresh": force, "completed": pending.get("completed", {})}
        chunks = {}
        self.store.write_json(f"{adjustment}/pending.json", pending)
        for index, (key, start, finish) in enumerate(plan):
            if progress:
                progress(f"{adjustment} {key} ({index + 1}/{len(plan)})")
            completed = pending["completed"].get(key)
            existing = active.get("chunks", {}).get(key)
            if completed and completed["end"] == str(finish):
                chunks[key] = completed
                continue
            if not force and existing and existing["end"] == str(finish) and start < overlap:
                chunks[key] = existing
                continue
            frame = fetched[key] if key in fetched else self.client.bars(SYMBOLS, start, finish, adjustment)
            quality = inspect_quality(frame, SYMBOLS, start, finish)
            # Keep the actual provider response, including bad rows, in immutable
            # objects for audit. Never publish numerically invalid research data.
            begin, stop = request_bounds(start, finish)
            entry = self.store.write_chunk(adjustment, frame, {
                "start": str(start), "end": str(finish), "downloaded_at": utc_now(),
                "source": "Alpaca", "feed": "sip", "adjustment": adjustment,
                "request": {"symbols": list(SYMBOLS), "start": begin.isoformat(),
                            "end": stop.isoformat(), "timeframe": "1Day", "feed": "sip",
                            "adjustment": adjustment, "asof": str(end), "limit": None},
                "quality": quality, "sdk_version": version("alpaca-py")})
            if not quality["passed_numeric_checks"]:
                self.store.write_json(f"{adjustment}/quarantine.json", entry)
                raise DataAccessError("QUALITY_REJECTED")
            chunks[key] = entry
            pending["completed"][key] = entry
            self.store.write_json(f"{adjustment}/pending.json", pending)
        manifest = {"schema_version": 1, "version": uuid.uuid4().hex, "updated_at": utc_now(),
                    "source": "Alpaca", "feed": "sip", "adjustment": adjustment,
                    "start": str(HISTORY_START), "end": str(end), "chunks": chunks,
                    "finalization": {"status": "OPERATIONAL_CUTOFF_NOT_PROVIDER_CERTIFIED",
                                     "next_day_hour_ny": self.config.finalize_hour_ny,
                                     "revision_sessions": self.config.revision_sessions},
                    "mapping": [{"requested_symbol": s, "provider_symbol": s,
                                 "role": "reference" if s in REFERENCES else "research_universe",
                                 "security_id": "UNKNOWN", "listing_date": "UNKNOWN",
                                 "symbol_history_verification": "UNKNOWN",
                                 "corporate_actions_verification": "UNKNOWN"} for s in SYMBOLS],
                    "full_refresh": bool(force), "calendar_version": version("pandas-market-calendars")}
        frame = self.store.read(adjustment, manifest)
        manifest["quality"] = inspect_quality(frame, SYMBOLS, HISTORY_START, end)
        self.store.write_json(f"{adjustment}/versions/{manifest['version']}.json", manifest)
        self.store.write_json(f"{adjustment}/manifest.json", manifest)
        self.store.write_json(f"{adjustment}/pending.json", {})
        return {"version": manifest["version"], "rows": len(frame),
                "end": str(end), "full_refresh": bool(force), "issues": len(manifest["quality"]["issues"])}
