"""Sensor readings taken while the backend was unreachable.

A reading that crosses a threshold becomes an alert, and local_alerts
already keeps those — they hold the gate, so losing one is unacceptable.
Everything below the threshold was simply dropped: `evaluate()` returned
None and the value went nowhere.

Online that is fine, because the backend logs every reading into
SensorReadingLog whether it crossed anything or not. Offline it means the
history has holes exactly where the network was worst, and a gas trend
that was climbing towards a threshold looks like it began the moment the
connection came back.

So readings are buffered here and replayed to /api/gate/sensors when the
cloud returns. The backend classifies them on arrival exactly as it would
have live, so a value that crossed a threshold while offline still raises
its alert — just late, and marked with when it was actually taken.

Bounded on purpose. A gate left offline for a week at one reading every
few seconds would otherwise fill the SD card, and a full disk takes the
whole gate down to preserve telemetry nobody will read. Oldest go first:
recent readings are the ones that describe the situation now.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

DB_PATH = Path(__file__).with_name("local_readings.db")

# Roughly a day at one reading per kind every few seconds, and a few MB of
# SQLite at most. Past this the oldest are discarded rather than the newest
# refused — a buffer that stops accepting is a buffer that hides the present
# to protect the past.
MAX_ROWS = 20000

# Trim in batches rather than on every insert: a DELETE per reading turns a
# cheap append into a scan, and the point of this table is that writing to
# it never costs the gate anything.
#
# Scaled to the budget rather than fixed, because a fixed batch is only
# cheap relative to the table it guards. At 20000 rows, 200 inserts between
# trims is a 1% overshoot; against a 50-row buffer the same number would let
# it grow to five times its limit before anything noticed.
PRUNE_EVERY = 200


def _prune_every(max_rows: int) -> int:
    return max(1, min(PRUNE_EVERY, max_rows // 10))


class LocalReadings:
    """Append-only buffer of raw sensor values, replayed when possible."""

    def __init__(self, path: Path = DB_PATH, max_rows: int = MAX_ROWS):
        self._lock = threading.Lock()
        self._max_rows = max_rows
        self._prune_every = _prune_every(max_rows)
        self._since_prune = 0
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._create()

    def _create(self) -> None:
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS readings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    value REAL NOT NULL,
                    unit TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    taken_at REAL NOT NULL,
                    synced INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_unsynced ON readings (synced, id)")
            self._conn.commit()

    def record(self, kind: str, value: float, unit: str = "", source: str = "") -> None:
        """Buffer one reading. Never raises: telemetry must not stop a gate."""
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO readings (kind, value, unit, source, taken_at) "
                    "VALUES (?,?,?,?,?)",
                    (str(kind)[:40], float(value), str(unit)[:20], str(source)[:80], time.time()),
                )
                self._since_prune += 1
                if self._since_prune >= self._prune_every:
                    self._since_prune = 0
                    self._prune_locked()
                self._conn.commit()
        except Exception:  # noqa: BLE001 - a full disk or a bad value, neither fatal
            pass

    def _prune_locked(self) -> None:
        total = self._conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
        if total <= self._max_rows:
            return
        # Drop synced rows before unsynced ones: a synced reading already
        # exists in the cloud, so losing the local copy costs nothing.
        self._conn.execute(
            "DELETE FROM readings WHERE id IN ("
            "  SELECT id FROM readings ORDER BY synced DESC, id ASC LIMIT ?)",
            (total - self._max_rows,),
        )

    def pending(self, limit: int = 100) -> list[tuple]:
        """Oldest unsynced readings first, so the history replays in order."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, kind, value, unit, source, taken_at FROM readings "
                "WHERE synced = 0 ORDER BY id ASC LIMIT ?", (limit,)
            ).fetchall()
        return [(r["id"], r["kind"], r["value"], r["unit"], r["source"], r["taken_at"])
                for r in rows]

    def mark_synced(self, row_id: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE readings SET synced = 1 WHERE id = ?", (row_id,))
            self._conn.commit()

    def unsynced_count(self) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM readings WHERE synced = 0").fetchone()[0]

    def latest(self, kind: str) -> dict | None:
        """Most recent value for one kind, for a local readout."""
        with self._lock:
            row = self._conn.execute(
                "SELECT kind, value, unit, taken_at FROM readings "
                "WHERE kind = ? ORDER BY id DESC LIMIT 1", (kind,)
            ).fetchone()
        return dict(row) if row else None

    def summary(self) -> str:
        pending = self.unsynced_count()
        return "all readings synced" if not pending else f"{pending} readings waiting"
