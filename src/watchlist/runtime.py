"""Durable paths, atomic state and a versioned task/event ledger."""
from pathlib import Path
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from src.data.alpaca_config import load_config, PROJECT_ROOT


def utc():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def root():
    p = Path(os.environ.get("WATCHLIST_RUN_DIR", str(load_config().data_dir.parent / "watchlist-research-v1")))
    p.mkdir(parents=True, exist_ok=True)
    return p


def read(path, default=None):
    return json.loads(Path(path).read_text(encoding="utf-8")) if Path(path).exists() else default


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".{os.getpid()}.tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str, allow_nan=False), encoding="utf-8")
    temp.replace(path)


def state(stage, **kwargs):
    p = root() / "status.json"
    data = read(p, {})
    data.update(stage=stage, heartbeat=utc(), pid=os.getpid(), **kwargs)
    write(p, data)
    write(PROJECT_ROOT / "TASK_STATE.json", data)
    print(json.dumps({"stage": stage, **kwargs}, ensure_ascii=False, default=str), flush=True)


def event(task, status, inputs=None, output=None, error=None):
    with sqlite3.connect(root() / "tasks.sqlite", timeout=30) as db:
        db.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, at TEXT, task TEXT, status TEXT, input_version TEXT, output TEXT, error TEXT)")
        db.execute("INSERT INTO events(at,task,status,input_version,output,error) VALUES (?,?,?,?,?,?)",
                   (utc(), task, status, digest(inputs), json.dumps(output, default=str), error))


def universe_config():
    return read(PROJECT_ROOT / "config/watchlist.json")
