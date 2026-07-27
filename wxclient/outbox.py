"""本地待发队列（SQLite）：断网/服务端重启时消息不丢，恢复后按序补发。"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,          -- message | file
    payload    TEXT NOT NULL,
    file_path  TEXT,
    attempts   INTEGER DEFAULT 0,
    next_at    REAL DEFAULT 0,
    last_error TEXT,
    created_at REAL
);
CREATE INDEX IF NOT EXISTS idx_outbox_next ON outbox(next_at);

CREATE TABLE IF NOT EXISTS seen (
    local_id TEXT PRIMARY KEY,
    ts       REAL
);
"""


class Outbox:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ---------------- 去重 ---------------- #
    def is_seen(self, local_id: str) -> bool:
        with self._lock:
            return (
                self._conn.execute(
                    "SELECT 1 FROM seen WHERE local_id = ?", (local_id,)
                ).fetchone()
                is not None
            )

    def mark_seen(self, local_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO seen (local_id, ts) VALUES (?, ?)",
                (local_id, time.time()),
            )
            self._conn.commit()

    def prune_seen(self, keep_days: float = 7) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM seen WHERE ts < ?", (time.time() - keep_days * 86400,))
            self._conn.commit()

    # ---------------- 队列 ---------------- #
    def put(self, kind: str, payload: dict[str, Any], file_path: str | None = None) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO outbox (kind, payload, file_path, created_at) VALUES (?, ?, ?, ?)",
                (kind, json.dumps(payload, ensure_ascii=False), file_path, time.time()),
            )
            self._conn.commit()
            return int(cur.lastrowid or 0)

    def take(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM outbox WHERE next_at <= ? ORDER BY id LIMIT ?",
                (time.time(), limit),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d["payload"])
            out.append(d)
        return out

    def done(self, row_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM outbox WHERE id = ?", (row_id,))
            self._conn.commit()

    def fail(self, row_id: int, error: str, max_attempts: int = 0) -> None:
        """指数退避重排；attempts 超过 max_attempts(>0) 时丢弃。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT attempts FROM outbox WHERE id = ?", (row_id,)
            ).fetchone()
            attempts = (row["attempts"] if row else 0) + 1
            if max_attempts and attempts >= max_attempts:
                self._conn.execute("DELETE FROM outbox WHERE id = ?", (row_id,))
            else:
                delay = min(300, 5 * (2 ** min(attempts - 1, 6)))
                self._conn.execute(
                    "UPDATE outbox SET attempts = ?, next_at = ?, last_error = ? WHERE id = ?",
                    (attempts, time.time() + delay, error[:500], row_id),
                )
            self._conn.commit()

    def pending(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS c FROM outbox").fetchone()
            return int(row["c"]) if row else 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()
