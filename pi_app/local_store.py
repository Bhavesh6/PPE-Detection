"""Local mirror of the cloud state the gate needs to rule on its own.

Step one of offline mode: this only *fills* the cache. Nothing reads from it
yet, so landing it cannot change how the gate behaves — if the sync is wrong,
the gate carries on exactly as before.

Sync is deliberately one-way per kind, which is what keeps it free of merge
conflicts:

    cloud -> Pi    roster, site policy, active alerts   (cloud is authoritative)
    Pi   -> cloud  events the gate recorded             (see offline_queue.py)

The Pi never edits a worker or a policy, so there is never a version of those
to reconcile. It only ever reports things that happened at the gate, which the
cloud has no competing opinion about.

Freshness is stamped from the **server's** clock, not this machine's. A Pi
whose RTC battery is flat boots at the epoch, and a cache that looks 56 years
stale (or one dated in the future) would be judged wrong either way.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

DB_PATH = Path(__file__).with_name("local_cache.db")

# How long a cached roster may be trusted once the cloud is unreachable.
# A full shift plus slack, so a realistic outage doesn't strand anyone, while
# a revocation can never be more than a day stale.
DEFAULT_MAX_AGE_SECONDS = 24 * 60 * 60


class LocalStore:
    """SQLite mirror of roster, policy and alerts."""

    def __init__(self, path: Path = DB_PATH, max_age: float = DEFAULT_MAX_AGE_SECONDS):
        self._lock = threading.Lock()
        self._max_age = max_age
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._create()

    def _create(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS workers (
                    rfid_tag TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    profile TEXT NOT NULL,
                    already_present_today INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS kv (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            self._conn.commit()

    # -- writing ---------------------------------------------------------
    def replace_all(self, payload: dict) -> int:
        """Swap in a whole sync payload, atomically.

        Whole-roster replace rather than an upsert pass: an upsert leaves
        deleted workers behind forever, and a badge that still opens the gate
        after the worker was removed is the exact failure this cache must not
        introduce.
        """
        workers = payload.get("workers") or []
        rows = [
            (
                w["rfid_tag"].strip().lower(),
                w["id"],
                json.dumps(w),
                1 if w.get("already_present_today") else 0,
            )
            for w in workers
            if w.get("rfid_tag") and w.get("id") is not None
        ]

        with self._lock:
            with self._conn:                      # one transaction
                self._conn.execute("DELETE FROM workers")
                self._conn.executemany(
                    "INSERT OR REPLACE INTO workers "
                    "(rfid_tag, user_id, profile, already_present_today) VALUES (?,?,?,?)",
                    rows,
                )
                self._put("policy", payload.get("policy") or {})
                self._put("active_alerts", payload.get("active_alerts") or [])
                self._put("present_count", payload.get("present_count") or 0)
                # Server clock, not ours — see the module docstring.
                self._put("synced_at_server", payload.get("server_time") or "")
                self._put("synced_at_monotonic", time.time())
        return len(rows)

    def _put(self, key: str, value) -> None:
        """Caller must hold the lock and a transaction."""
        self._conn.execute(
            "INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)",
            (key, json.dumps(value)),
        )

    # -- reading ---------------------------------------------------------
    def _get(self, key: str, default=None):
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM kv WHERE key = ?", (key,)
            ).fetchone()
        return json.loads(row["value"]) if row else default

    def worker_by_tag(self, tag: str) -> tuple[dict | None, bool]:
        """(profile, already_present_today) for a badge, or (None, False)."""
        key = (tag or "").strip().lower()
        with self._lock:
            row = self._conn.execute(
                "SELECT profile, already_present_today FROM workers WHERE rfid_tag = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None, False
        return json.loads(row["profile"]), bool(row["already_present_today"])

    def policy(self) -> dict:
        return self._get("policy", {}) or {}

    def active_alerts(self) -> list:
        return self._get("active_alerts", []) or []

    def present_count(self) -> int:
        return self._get("present_count", 0) or 0

    def worker_count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM workers").fetchone()[0]

    # -- freshness -------------------------------------------------------
    def age_seconds(self) -> float | None:
        """Seconds since the last successful sync, or None if never synced."""
        stamped = self._get("synced_at_monotonic")
        if stamped is None:
            return None
        return max(0.0, time.time() - float(stamped))

    def is_usable(self) -> bool:
        """Whether the cache is fresh enough to rule on.

        A never-synced cache is not usable: an empty roster would turn every
        badge into "not recognised", which reads as a decision about the
        person rather than the truth, which is that the gate knows nothing.
        """
        age = self.age_seconds()
        if age is None or self.worker_count() == 0:
            return False
        return age <= self._max_age

    def summary(self) -> str:
        age = self.age_seconds()
        if age is None:
            return "never synced"
        return f"{self.worker_count()} workers, synced {int(age)}s ago"
