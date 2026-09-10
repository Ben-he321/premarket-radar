"""One rolling-window quota across processes, pages and retries in this project."""
import sqlite3
import time
from pathlib import Path


class SharedRateLimiter:
    def __init__(self, path, limit=120):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.limit = limit
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS requests (at REAL NOT NULL)")

    def acquire(self):
        while True:
            now = time.time()
            with sqlite3.connect(self.path, timeout=30) as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("DELETE FROM requests WHERE at <= ?", (now - 60,))
                count, oldest = db.execute("SELECT count(*),min(at) FROM requests").fetchone()
                if count < self.limit:
                    db.execute("INSERT INTO requests VALUES (?)", (now,))
                    return
            time.sleep(max(.05, oldest + 60.05 - time.time()))
