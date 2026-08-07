"""Local backup for attendance records the backend hasn't confirmed yet.

The cloud stays authoritative — this is not a second source of truth and
not a sync engine. It exists for one narrow gap: a PPE check can finish
and reach a verdict, and then the POST that records it can fail because
the network drops in exactly those few seconds. Without this, that
decision is gone forever with nothing to show for it ever having
happened. Everything queued here is replayed, oldest first, the next
time the backend accepts it. Nothing is deduplicated — this only exists
to stop a real decision from vanishing, not to guard against being
recorded twice, and the site explicitly doesn't care about the latter.

Badge lookups and the PPE check itself aren't queued here, because
there's nothing to queue: both require the backend to run at all (worker
identity and inference are both server-side), so if the connection is
down before a verdict exists, there's no decision yet to lose.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

DB_PATH = Path(__file__).with_name("offline_queue.db")


class OfflineQueue:
    def __init__(self, path: Path = DB_PATH):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS pending_attendance ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "user_id INTEGER NOT NULL, "
            "granted INTEGER NOT NULL, "
            "missing_ppe TEXT NOT NULL, "
            "queued_at REAL NOT NULL)"
        )
        self._conn.commit()

    def enqueue(self, user_id: int, granted: bool, missing: list) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO pending_attendance (user_id, granted, missing_ppe, queued_at) "
                "VALUES (?, ?, ?, ?)",
                (user_id, int(granted), json.dumps(missing), time.time()),
            )
            self._conn.commit()

    def pending(self) -> list[tuple[int, int, bool, list]]:
        """Oldest first — (row_id, user_id, granted, missing_ppe)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, user_id, granted, missing_ppe FROM pending_attendance ORDER BY id ASC"
            ).fetchall()
        return [(rid, uid, bool(granted), json.loads(missing)) for rid, uid, granted, missing in rows]

    def discard(self, row_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM pending_attendance WHERE id = ?", (row_id,))
            self._conn.commit()

    def count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM pending_attendance").fetchone()[0]
