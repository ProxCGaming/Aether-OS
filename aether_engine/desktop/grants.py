"""Remembered desktop grants (S20): "don't ask again for this
app + category", stored durably and revocable in Settings.

A remembered grant is the durable counterpart of a per-task
envelope: the owner grants it from the confirmation card ("Remember:
allow …"), it persists across tasks and restarts, and it is listed
with a Revoke button in Settings → Computer Use. Its scope is
exactly one (app, category) pair, and the hard rules live where
they cannot be bypassed:

- ``grant()`` REFUSES the hard-confirm categories
  (close_or_quit, system) — those confirm individually, always,
  so a remembered grant for them must not even exist as data.
- The gate (``desktop/policy.py``) checks grants only AFTER the
  hard-confirm, app-deny, pixel-consent and vision checks, so a
  grant can never outrank them.
- Revoking is immediate: the gate reads this store at decision
  time, so a revoked grant stops covering the very next action.

Storage mirrors the approval cache's discipline: a small SQLite
database under ``~/.aether/`` with WAL ensured per connection
(the stores' house helper, copied — the S5/S19 rule against a
shared util that would couple the stores).
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("aether_engine.desktop.grants")


def _ensure_wal_mode(conn: sqlite3.Connection) -> None:
    """WAL rollout helper (S1; copied per store — see module docs).

    One-time delete-to-WAL transition needs exclusive access and
    SQLite ignores busy_timeout there, so retry briefly instead of
    letting a racing second connection fail the open.
    """
    for _ in range(5):
        try:
            row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
        except sqlite3.OperationalError:
            time.sleep(0.05)
            continue
        if row and str(row[0]).lower() == "wal":
            return
        time.sleep(0.05)


def default_grants_path() -> Path:
    return Path.home() / ".aether" / "desktop-grants.db"


class DesktopGrantStore:
    """Durable (app, category) remembered grants."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self._db_path = Path(db_path) if db_path is not None \
            else default_grants_path()
        self._lock = threading.Lock()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS desktop_grants (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    app TEXT NOT NULL,
                    category TEXT NOT NULL,
                    granted_at REAL NOT NULL,
                    UNIQUE(app, category)
                )
                """
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), timeout=5.0)
        conn.execute("PRAGMA busy_timeout=5000")
        _ensure_wal_mode(conn)
        conn.row_factory = sqlite3.Row
        return conn

    # --------------------------------------------------------------
    # Writes
    # --------------------------------------------------------------

    def grant(self, app: str, category: str) -> bool:
        """Record a remembered grant. Returns True if stored.

        Refuses (returns False) the hard-confirm categories, unknown
        categories, and empty apps — the one write path is the
        confirmation card, and this is the second lock behind the
        gate/payload rules that never offer "remember" for them.
        Idempotent per (app, category): re-granting refreshes the
        timestamp rather than duplicating the row.
        """
        from aether_engine.desktop.policy import CATEGORIES, HARD_CONFIRM_CATEGORIES

        app = (app or "").strip()
        if not app or category not in CATEGORIES \
                or category in HARD_CONFIRM_CATEGORIES:
            logger.warning(
                "Refused remembered grant for app=%r category=%r", app, category)
            return False
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO desktop_grants (app, category, granted_at)
                VALUES (?, ?, ?)
                ON CONFLICT(app, category)
                DO UPDATE SET granted_at = excluded.granted_at
                """,
                (app, category, time.time()),
            )
            conn.commit()
        return True

    def revoke(self, grant_id: int) -> bool:
        """Revoke one grant by id (the Settings Revoke button).
        Returns True when a row actually went away."""
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM desktop_grants WHERE id = ?", (int(grant_id),))
            conn.commit()
            return cursor.rowcount > 0

    # --------------------------------------------------------------
    # Reads
    # --------------------------------------------------------------

    def covers(self, app: str, category: str) -> bool:
        """Whether a live remembered grant covers (app, category).
        App matching is case-insensitive (process names are)."""
        if not app:
            return False
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM desktop_grants "
                "WHERE lower(app) = lower(?) AND category = ? LIMIT 1",
                (app, category),
            ).fetchone()
        return row is not None

    def list_grants(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, app, category, granted_at FROM desktop_grants "
                "ORDER BY granted_at DESC",
            ).fetchall()
        return [
            {
                "id": row["id"],
                "app": row["app"],
                "category": row["category"],
                "granted_at": row["granted_at"],
            }
            for row in rows
        ]


_store: DesktopGrantStore | None = None


def get_default_grant_store() -> DesktopGrantStore:
    global _store
    if _store is None:
        _store = DesktopGrantStore()
    return _store


def reset_default_grant_store() -> None:
    """Drop the process-wide store (tests)."""
    global _store
    _store = None


def set_default_grant_store(store: DesktopGrantStore | None) -> None:
    """Replace the process-wide store (tests inject a tmp-path
    store so the suite never touches the owner's real grants)."""
    global _store
    _store = store
