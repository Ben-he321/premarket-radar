"""Immutable Parquet objects, atomic manifests, read-only DuckDB and ZIP export."""
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import uuid
import zipfile

import duckdb
from filelock import FileLock
import pandas as pd

from .alpaca_client import DataAccessError, normalize_bars


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class ParquetStore:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()

    def initialize(self):
        if self.root.exists() and any(self.root.iterdir()) and not (self.root / "dataset.json").exists():
            raise DataAccessError("DATA_DIR_NOT_DEDICATED")
        self.root.mkdir(parents=True, exist_ok=True)
        if not (self.root / "dataset.json").exists():
            self.write_json("dataset.json", {"dataset": "AI-M1 Alpaca SIP", "schema_version": 1})
        # Protect manifests and metadata too, even when a custom path is in Git.
        (self.root / ".gitignore").write_text("*\n", encoding="utf-8")

    def lock(self):
        self.initialize()
        return FileLock(str(self.root / ".download.lock"), timeout=0)

    def read_json(self, name, default=None):
        path = self.root / name
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default

    def write_json(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        temp.replace(path)

    def active(self, adjustment):
        return self.read_json(f"{adjustment}/manifest.json", {})

    def write_chunk(self, adjustment, frame, metadata):
        object_id = uuid.uuid4().hex
        relative = f"{adjustment}/objects/{object_id}.parquet"
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        # Objects are invisible to queries until an atomic manifest publish.
        frame.to_parquet(path, index=False, engine="pyarrow")
        entry = {**metadata, "file": relative, "rows": len(frame),
                 "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        self.write_json(f"{adjustment}/objects/{object_id}.json", entry)
        return entry

    def read(self, adjustment, manifest=None, symbols=None):
        manifest = self.active(adjustment) if manifest is None else manifest
        paths = [str(self.root / e["file"]) for e in manifest.get("chunks", {}).values()]
        if not paths:
            return normalize_bars({}, ())
        with duckdb.connect(":memory:") as connection:
            relation = connection.read_parquet(paths, union_by_name=True)
            relation.create_view("bars")
            if symbols is None:
                return connection.execute("SELECT * FROM bars ORDER BY symbol, timestamp").df()
            return connection.execute(
                "SELECT * FROM bars WHERE symbol IN (SELECT unnest(?)) ORDER BY symbol, timestamp",
                [list(symbols)]).df()

    def inventory(self, symbols):
        rows = []
        for adjustment in ("raw", "all"):
            manifest = self.active(adjustment)
            frame = self.read(adjustment, manifest)
            for symbol in symbols:
                subset = frame[frame.symbol == symbol]
                issues = [i for i in manifest.get("quality", {}).get("issues", []) if i["symbol"] == symbol]
                rows.append({"symbol": symbol, "adjustment": adjustment, "source": "Alpaca / SIP",
                             "first_date": None if subset.empty else subset.trade_date.min(),
                             "last_date": None if subset.empty else subset.trade_date.max(),
                             "rows": len(subset), "updated_at": manifest.get("updated_at"),
                             "version": manifest.get("version"),
                             "quality": ", ".join(sorted({i["code"] for i in issues})) or
                                        ("未下载" if not manifest else "数值检查通过；证券资料 UNKNOWN")})
        return pd.DataFrame(rows)

    def export_zip(self):
        """Export only published snapshots + provenance, not unfinished objects."""
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("dataset.json", json.dumps({"dataset": "AI-M1 Alpaca SIP", "schema_version": 1}))
            archive.writestr(".gitignore", "*\n")
            for adjustment in ("raw", "all"):
                manifest = self.active(adjustment)
                if not manifest:
                    continue
                archive.writestr(f"{adjustment}/manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
                for entry in manifest["chunks"].values():
                    path = self.root / entry["file"]
                    archive.write(path, entry["file"])
                    archive.write(path.with_suffix(".json"), str(Path(entry["file"]).with_suffix(".json")))
            archive.writestr("README.txt", "Alpaca SIP research data. See manifests for adjustment, coverage, UNKNOWN metadata and quality. No credentials included.\n")
        return buffer.getvalue()
