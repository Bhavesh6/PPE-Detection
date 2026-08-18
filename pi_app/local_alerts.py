"""Hazard alerts raised while the backend is unreachable.

A missed PPE check is an inconvenience — the worker scans again. A missed
**gas alert** is not: it is the one signal in this system with no second
chance, and today it is simply lost when the network is down, because the
ESP32 posts it straight to the cloud and nothing else ever hears it.

So the Pi keeps its own copy. Alerts land here first, hold the gate locally,
and are replayed to the cloud when it comes back.

Clearing is deliberately one-directional. A locally-raised critical alert
holds the gate until it has been **synced**, after which the cloud owns it
and the normal acknowledge flow in the console applies. There is no way to
dismiss one from the gate itself: the screen is watched by the people the
alert is about, and letting the thing being warned about clear its own
warning is not a safety mechanism.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

DB_PATH = Path(__file__).with_name("local_alerts.db")


def evaluate(kind: str, value: float, thresholds: dict) -> tuple[str | None, dict | None]:
    """Classify a raw reading against the cached site thresholds.

    Mirrors backend/alerts.py evaluate_reading() exactly, including the
    "no threshold configured means log it but raise nothing" case. If the
    two ever disagree, the same gas level would hold the gate online and
    not offline, which is worse than either behaviour on its own.
    """
    cfg = (thresholds or {}).get(kind)
    if not cfg:
        return None, None

    direction = cfg.get("direction", "above")

    def crosses(level):
        if level is None:
            return False
        return value >= level if direction == "above" else value <= level

    if crosses(cfg.get("critical_at")):
        return "critical", cfg
    if crosses(cfg.get("warning_at")):
        return "warning", cfg
    return None, cfg


class LocalAlerts:
    def __init__(self, path: Path = DB_PATH):
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    message TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    value REAL,
                    created_at REAL NOT NULL,
                    synced INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            self._conn.commit()

    def record(self, kind: str, severity: str, message: str = "",
               source: str = "", value: float | None = None) -> dict:
        """Store an alert raised while offline. Returns a cloud-shaped dict."""
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO alerts (kind, severity, message, source, value, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (kind, severity, message or "", source or "", value, now),
            )
            self._conn.commit()
            row_id = cur.lastrowid
        return {
            "id": f"local-{row_id}",
            "kind": kind,
            "severity": severity,
            "message": message or "",
            "source": source or "",
            "acknowledged_at": None,
            "local": True,
        }

    def active(self) -> list[dict]:
        """Locally-raised alerts the cloud hasn't taken yet.

        Once an alert is synced it disappears from here, because from that
        moment the cloud's own active-alert list is the authority and
        counting it twice would hold the gate after an operator cleared it.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, kind, severity, message, source FROM alerts "
                "WHERE synced = 0 ORDER BY id ASC"
            ).fetchall()
        return [
            {
                "id": f"local-{r['id']}",
                "kind": r["kind"],
                "severity": r["severity"],
                "message": r["message"],
                "source": r["source"],
                "acknowledged_at": None,
                "local": True,
            }
            for r in rows
        ]

    def pending(self) -> list[tuple]:
        """(row_id, kind, severity, message, source) oldest first, for replay."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, kind, severity, message, source FROM alerts "
                "WHERE synced = 0 ORDER BY id ASC"
            ).fetchall()
        return [
            (r["id"], r["kind"], r["severity"], r["message"], r["source"])
            for r in rows
        ]

    def mark_synced(self, row_id: int) -> None:
        with self._lock:
            self._conn.execute("UPDATE alerts SET synced = 1 WHERE id = ?", (row_id,))
            self._conn.commit()

    def unsynced_count(self) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM alerts WHERE synced = 0"
            ).fetchone()[0]
