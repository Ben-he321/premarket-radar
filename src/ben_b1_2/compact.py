"""Bounded-memory B1.2 storage, without changing any trading calculation.

Exact processed event identities/digests and obsolete unconsumed inventories
are archived locally. A checkpoint claims only committed archive generations
whose accounting state it contains. A transaction committed before a failed
checkpoint is replayed, not incorrectly treated as already accounted for.
The original raw inputs remain immutable. This private SQLite archive is a
recovery aid and must not be included in the public verification ZIP.
"""
from __future__ import annotations

import csv
from dataclasses import asdict
import hashlib
import json
from json.encoder import _make_iterencode, encode_basestring
import math
import os
from pathlib import Path
import sqlite3
import time

import pandas as pd

from src.ben_b1 import rules
from src.ben_b1.ledger import Ledger
from src.ben_b1.replay import PRIORITY, _clean, _hash
from .replay import ReplayConfig, ReplayEngine, VERSION

STORAGE_VERSION = "BEN_B1_2_ARCHIVE_STORAGE_V1"
CHECKPOINT_VERSION = VERSION+":"+STORAGE_VERSION


class CanonicalEncoder(json.JSONEncoder):
    """Same canonical scalar semantics as legacy _clean/_json, streamed."""
    def __init__(self):
        super().__init__(ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)

    def default(self, value):
        return _clean(value)

    def iterencode(self, value, _one_shot=False):
        def finite_float(number):
            return float.__repr__(number) if math.isfinite(number) else "null"
        encoder = _make_iterencode({}, self.default, encode_basestring, None, finite_float,
            ":", ",", True, False, False)
        return encoder(value, 0)


def stream_hash(value):
    digest = hashlib.sha256()
    for part in CanonicalEncoder().iterencode(value):
        digest.update(part.encode("utf-8"))
    return digest.hexdigest()


def _write_csv(path, factory, empty_schema):
    fields = list(empty_schema)
    for row in factory():
        for key in row:
            if key not in fields:
                fields.append(key)
    with Path(path).open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in factory():
            writer.writerow({key: json.dumps(_clean(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
                if isinstance(value, (dict, list, tuple)) else _clean(value) for key, value in row.items()})


class CompactReplayEngine(ReplayEngine):
    def __init__(self, schedule, config=None, universe=None, checkpoint_path=None, archive_path=None):
        super().__init__(schedule, config, universe, checkpoint_path)
        if archive_path is None:
            if checkpoint_path is None:
                raise ValueError("COMPACT_REPLAY_REQUIRES_LOCAL_CHECKPOINT_OR_ARCHIVE_PATH")
            archive_path = Path(checkpoint_path).parent/"replay_archive.sqlite"
        self.archive_path = Path(archive_path).resolve()
        self.archive_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.archive_path), timeout=30)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS archived_events(event_id TEXT PRIMARY KEY,digest TEXT NOT NULL,block_sequence INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS archived_quotes(inventory_id TEXT PRIMARY KEY,payload TEXT NOT NULL,digest TEXT NOT NULL,block_sequence INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS archive_blocks(sequence INTEGER PRIMARY KEY,block_hash TEXT UNIQUE NOT NULL,payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS events_by_block ON archived_events(block_sequence,event_id);
            CREATE INDEX IF NOT EXISTS quotes_by_block ON archived_quotes(block_sequence,inventory_id);
        """)
        self._db.commit()
        self.state["archive"] = {"storage_version": STORAGE_VERSION, "path": str(self.archive_path),
            "applied_sequence": 0, "applied_hash": None, "cutoff": None,
            "archived_events": 0, "archived_zero_consumption_inventories": 0}
        self.after_archive_commit_hook = None

    def _known_archived_event(self, identity):
        return self._db.execute("SELECT digest FROM archived_events WHERE event_id=? AND block_sequence<=?",
            (identity, self.state["archive"]["applied_sequence"])).fetchone()

    def process(self, event):
        archive = self.state["archive"]
        identity = str(event.get("event_id"))
        # Identity is checked regardless of the submitted timestamp. Otherwise
        # a changed old event could evade its digest by claiming a future time.
        if archive["applied_sequence"] and identity not in self.state["handled"]:
            known = self._known_archived_event(identity)
            if known is not None:
                if known[0] != _hash(_clean(event)):
                    raise ValueError("EVENT_ID_CONTENT_CONFLICT")
                return {"status": "DUPLICATE_ARCHIVED_EVENT_SKIPPED", "event_id": identity}
        return super().process(event)

    def _quote(self, symbol, payload):
        archive = self.state["archive"]
        timestamp = rules.aware(payload.get("timestamp", self.state["at"]))
        if archive["cutoff"] is not None and timestamp <= rules.aware(archive["cutoff"]):
            normal = dict(payload, timestamp=timestamp.isoformat())
            key = self._quote_key(symbol, normal)
            if key not in self.state["quote_inventory"]:
                known = self._db.execute("SELECT 1 FROM archived_quotes WHERE inventory_id=? AND block_sequence<=?",
                    (key, archive["applied_sequence"])).fetchone()
                if known or (rules.aware(self.state["at"])-timestamp).total_seconds() > 5:
                    self._record("ARCHIVED_OR_OBSOLETE_QUOTE_INVENTORY_NOT_REVIVED", symbol,
                        quote_inventory_id=key, quote_timestamp=timestamp.isoformat(),
                        original_raw_input_retained=True, reason="NO_FRESH_CONSUMABLE_INVENTORY")
                    return
        return super()._quote(symbol, payload)

    def compact_now(self):
        """Commit one input block, then claim it in state. No financial mutation."""
        if self.state["q1_batch"]["arrivals"]:
            return {"status": "DEFERRED_UNCLOSED_QUOTE_TIMESTAMP_BATCH"}
        if not self.state["handled"]:
            return {"status": "NO_NEW_HANDLED_EVENTS"}
        archive = self.state["archive"]
        sequence = archive["applied_sequence"]+1
        current = {quote["inventory_id"] for quote in self.state["quotes"].values()}
        cutoff = rules.aware(self.state["at"])-pd.Timedelta(seconds=5)
        obsolete = {key: value for key, value in self.state["quote_inventory"].items()
            if key not in current and not value["ask_consumed"] and not value["bid_consumed"]
            and rules.aware(value["timestamp"]) < cutoff}
        block = {"sequence": sequence, "previous_hash": archive["applied_hash"],
            "cutoff": self.state["at"], "event_count": len(self.state["handled"]),
            "zero_consumption_inventory_count": len(obsolete),
            "event_digest": stream_hash(self.state["handled"]), "inventory_digest": stream_hash(obsolete)}
        block_hash = _hash(block)
        prior = self._db.execute("SELECT block_hash FROM archive_blocks WHERE sequence=?", (sequence,)).fetchone()
        if prior is not None and prior[0] != block_hash:
            raise ValueError("ARCHIVE_CHECKPOINT_REPLAY_DIVERGENCE")
        with self._db:
            for identity, digest in self.state["handled"].items():
                known = self._db.execute("SELECT digest,block_sequence FROM archived_events WHERE event_id=?", (identity,)).fetchone()
                if known and (known[0] != digest or known[1] != sequence):
                    raise ValueError("ARCHIVED_EVENT_DIGEST_OR_GENERATION_CONFLICT")
            self._db.executemany("INSERT OR IGNORE INTO archived_events VALUES(?,?,?)",
                ((identity, digest, sequence) for identity, digest in self.state["handled"].items()))
            def quote_rows():
                for key, value in obsolete.items():
                    digest = _hash(value)
                    known = self._db.execute("SELECT digest,block_sequence FROM archived_quotes WHERE inventory_id=?", (key,)).fetchone()
                    if known and (known[0] != digest or known[1] != sequence):
                        raise ValueError("ARCHIVED_QUOTE_DIGEST_OR_GENERATION_CONFLICT")
                    yield (key, json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")), digest, sequence)
            self._db.executemany("INSERT OR IGNORE INTO archived_quotes VALUES(?,?,?,?)",
                quote_rows())
            self._db.execute("INSERT OR IGNORE INTO archive_blocks VALUES(?,?,?)",
                (sequence, block_hash, json.dumps(block, sort_keys=True, separators=(",", ":"))))
        if self.after_archive_commit_hook is not None:
            self.after_archive_commit_hook(block)
        archive.update(applied_sequence=sequence, applied_hash=block_hash, cutoff=self.state["at"],
            archived_events=archive["archived_events"]+len(self.state["handled"]),
            archived_zero_consumption_inventories=archive["archived_zero_consumption_inventories"]+len(obsolete))
        self.state["handled"].clear()
        for key in obsolete:
            del self.state["quote_inventory"][key]
        return {"status": "ARCHIVED_WITHOUT_FINANCIAL_MUTATION", **block, "block_hash": block_hash}

    def _payload(self):
        # State is referenced directly; no full _clean/deepcopy before writing.
        return {"version": CHECKPOINT_VERSION, "config": asdict(self.config), "universe": self.universe,
            "sessions": self.sessions, "ledger": self.ledger.to_dict(), "state": self.state}

    def to_dict(self):
        return _clean(self._payload())

    def state_digest(self):
        return stream_hash(self._payload())

    def save(self, path=None):
        target = Path(path) if path else self.checkpoint_path
        if target is None:
            raise ValueError("CHECKPOINT_PATH_REQUIRED")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix+".tmp")
        digest = hashlib.sha256()
        with tmp.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write('{"payload":')
            for part in CanonicalEncoder().iterencode(self._payload()):
                stream.write(part)
                digest.update(part.encode("utf-8"))
            stream.write(',"sha256":"'+digest.hexdigest()+'"}')
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(6):
            try:
                os.replace(tmp, target)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(.05*2**attempt)
        return {"path": str(target), "sha256": digest.hexdigest(), "events_processed": self.state["events_processed"]}

    def run(self, events):
        ordered = sorted(events, key=lambda e: (rules.aware(e["at"]), PRIORITY.get(e["kind"], 99), str(e["event_id"])))
        for event in ordered:
            self.process(event)
        self.flush_quote_batch()
        self.compact_now()
        if self.checkpoint_path:
            self.save()
        return self.summary()

    def summary(self):
        result = super().summary()
        archive = self.state["archive"]
        result.update(storage_version=STORAGE_VERSION, archive=archive,
            live_quote_inventory_count=len(self.state["quote_inventory"]),
            input_hash_format="ARCHIVE_GENERATION_HASH_CHAIN_PLUS_LIVE_EVENT_DIGEST_NOT_LEGACY_FLAT_HASH",
            input_event_hash=_hash({"archive_hash": archive["applied_hash"],
                "archive_event_count": archive["archived_events"], "live_event_digest": stream_hash(self.state["handled"])}))
        return _clean(result)

    def _verify_archive(self):
        if self._db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("ARCHIVE_INTEGRITY_FAILURE")
        previous, count_events, count_quotes = None, 0, 0
        for sequence in range(1, self.state["archive"]["applied_sequence"]+1):
            row = self._db.execute("SELECT block_hash,payload FROM archive_blocks WHERE sequence=?", (sequence,)).fetchone()
            if row is None:
                raise ValueError("ARCHIVE_GENERATION_MISSING")
            block = json.loads(row[1])
            if _hash(block) != row[0] or block["previous_hash"] != previous:
                raise ValueError("ARCHIVE_HASH_CHAIN_MISMATCH")
            events = {r[0]: r[1] for r in self._db.execute("SELECT event_id,digest FROM archived_events WHERE block_sequence=?", (sequence,))}
            if len(events) != block["event_count"] or stream_hash(events) != block["event_digest"]:
                raise ValueError("ARCHIVED_EVENT_DIGEST_MISMATCH")
            quotes = {}
            for record in self._db.execute("SELECT inventory_id,payload,digest FROM archived_quotes WHERE block_sequence=?", (sequence,)):
                value = json.loads(record[1])
                if _hash(value) != record[2]:
                    raise ValueError("ARCHIVED_QUOTE_DIGEST_MISMATCH")
                quotes[record[0]] = value
            if len(quotes) != block["zero_consumption_inventory_count"] or stream_hash(quotes) != block["inventory_digest"]:
                raise ValueError("ARCHIVED_QUOTE_DIGEST_MISMATCH")
            count_events += len(events)
            count_quotes += len(quotes)
            previous = row[0]
        archive = self.state["archive"]
        if previous != archive["applied_hash"] or count_events != archive["archived_events"] or count_quotes != archive["archived_zero_consumption_inventories"]:
            raise ValueError("ARCHIVE_CHECKPOINT_GENERATION_MISMATCH")

    @classmethod
    def restore(cls, path, schedule):
        wrapper = json.loads(Path(path).read_text(encoding="utf-8"))
        value = wrapper["payload"]
        if wrapper["sha256"] != stream_hash(value) or value["version"] != CHECKPOINT_VERSION:
            raise ValueError("CHECKPOINT_HASH_OR_VERSION_MISMATCH")
        archive = value["state"].get("archive")
        if not archive or archive.get("storage_version") != STORAGE_VERSION or not Path(archive["path"]).is_file():
            raise ValueError("REQUIRED_COMPACT_ARCHIVE_MISSING")
        engine = cls(schedule, ReplayConfig(**value["config"]), value["universe"], path, archive["path"])
        if _hash(engine.sessions) != _hash(value["sessions"]):
            raise ValueError("CHECKPOINT_CALENDAR_MISMATCH")
        engine.ledger, engine.state = Ledger.from_dict(value["ledger"]), value["state"]
        engine._verify_archive()
        return engine

    def economic_state(self):
        return _clean({"ledger": self.ledger.to_dict(), "orders": self.orders,
            "stops": self.state["stops"], "trailing": self.state["trailing"], "pressure": self.state["pressure"],
            "last_exit": self.state["last_exit"], "equity": self.state["equity"], "errors": self.state["errors"]})

    def export(self, directory):
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        tables = {
            "orders": (lambda: iter(self.orders.values()), ["order_id", "symbol", "side", "quantity", "status"]),
            "fills": (lambda: iter(self.ledger.fills.values()), ["fill_id", "order_id", "symbol", "side", "quantity", "execution_price"]),
            "campaigns": (lambda: iter(self.ledger.campaign_summary()["campaigns"]), ["campaign_id", "symbol", "status", "net_profit"]),
            "daily_equity": (lambda: iter(self.state["equity"]), ["at", "net_equity", "cash", "debt", "accrued_interest"]),
            "coverage_funnel": (lambda: iter(self.state["decisions"]), ["decision_time", "symbol", "status", "reason"]),
            "event_trace": (lambda: iter(self.trace), ["sequence", "at", "kind", "symbol"]),
            "quote_inventory": (lambda: ({"inventory_id": key, **value} for key, value in self.state["quote_inventory"].items()), ["inventory_id", "symbol", "ask_consumed", "bid_consumed"]),
            "account_events": (lambda: iter(self.ledger.events), ["type", "date", "amount"]),
            "data_gaps": (lambda: iter(self.state["gaps"]), ["at", "symbol", "reason"]),
            "q1_quote_evaluations": (lambda: (d for d in self.state["decisions"] if d.get("evaluation_kind") == "NEW_QUOTE_TIMESTAMP_BATCH"), ["symbol", "decision_time", "status", "reason"]),
        }
        for name, (factory, schema) in tables.items():
            _write_csv(root/(name+".csv"), factory, schema)
        result = self.summary()
        (root/"summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        manifest = {"storage_version": STORAGE_VERSION, "archive": self.state["archive"],
            "blocks": [{"block_hash": row[0], **json.loads(row[1])} for row in self._db.execute(
                "SELECT block_hash,payload FROM archive_blocks WHERE sequence<=? ORDER BY sequence", (self.state["archive"]["applied_sequence"],))],
            "quote_inventory_csv_scope": "CURRENT_BOOK_AND_CONSUMED_INVENTORIES; OBSOLETE_ZERO_CONSUMPTION_RECORDS_IN_PRIVATE_ARCHIVE",
            "raw_input_policy": "ORIGINAL_VENDOR_FILES_RETAINED_UNCHANGED", "include_private_sqlite_in_verification_zip": False}
        (root/"ARCHIVE_MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        self.save(root/"checkpoint.json")
        return result

    def close(self):
        self._db.close()
