"""Approval cache: remembered approval grants (Epic 2, E2-09).

When the user approves a tool call they may scope the approval:
**once** (the default — nothing stored), **this file**, or **this
folder**. A stored grant is a (path prefix, risk ceiling) pair in
``~/.aether/approval-cache.db``: a later call whose policy verdict is
"ask" is allowed without pausing when a grant's prefix contains the
call's path and the grant's ceiling is at or above the tool's risk —
so a write grant covers reads under it, and never covers a riskier
call or a path outside the prefix.

The cache is an overlay ON the policy verdict, consulted only where a
pause would otherwise happen (the graph's tool_router and
``ToolRegistry.execute``). It can never override a deny — and it can
never cover a floored path: per ADR 0029 D3 nothing auto-allows a
call under ``~/.ssh`` / ``~/.aether`` (or a non-read-only call under
system prefixes), and a stored grant is exactly the kind of standing
configuration that rule names. Grants can be CREATED from an approval
there, but they simply never match a later call — the pause always
comes back for floored paths.

Revocation is a row delete; listing backs the Settings UI.

Threading (E2-F04): the process-wide singleton is created on
whichever thread touches it first and then called from everywhere —
the WS event loop (grant recording), ``asyncio.to_thread`` workers
(Settings list/revoke), and the graph router / executor threads
(``covering_grant``). The connection is therefore opened with
``check_same_thread=False`` and EVERY operation is serialised
through one re-entrant lock: a single shared connection is only
safe cross-thread when its use is serialised, and the lock also
keeps ``covering_grant``'s read-then-mark-used sequence atomic.

Restore quiesce (E10-F05): ``close()`` closes that connection
under the same lock (checkpointing its WAL into the current file)
and later operations reopen it lazily, so a profile restore can
replace approval-cache.db without the live connection's WAL state
resurrecting the pre-restore grants over the restored file.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path

from aether_engine.tools.paths import candidate_paths
from aether_engine.tools.registry import ToolRisk

logger = logging.getLogger("aether_engine.tools.approval_cache")

RISK_ORDER: dict[str, int] = {risk.value: idx for idx, risk in enumerate(ToolRisk)}


def default_cache_path() -> Path:
    return Path.home() / ".aether" / "approval-cache.db"


class ApprovalCache:
    """SQLite-backed store of approval grants."""

    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path) if db_path else default_cache_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # Guards every use of the shared connection below (E2-F04).
        # Re-entrant because covering_grant() composes list_grants().
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        with self._lock:
            self._ensure_conn()

    def _open_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS grants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prefix TEXT NOT NULL,
                ceiling TEXT NOT NULL,
                tool_name TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                last_used_at REAL
            )
            """
        )
        conn.commit()
        return conn

    def _ensure_conn(self) -> sqlite3.Connection:
        """The shared connection, reopened lazily after close().

        Callers hold ``self._lock``. Reopening is what lets a
        profile restore replace approval-cache.db under the running
        engine (E10-F05): the restore closes this cache so the
        connection's WAL cannot resurrect the pre-restore database
        over the restored file, and the next operation simply opens
        the restored file.
        """
        if self._conn is None:
            self._conn = self._open_connection()
        return self._conn

    def close(self) -> None:
        """Close the shared connection (checkpointing its WAL into
        the database file as it goes). Idempotent; the cache stays
        usable — the next operation reopens it lazily via
        ``_ensure_conn``."""
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None

    # ── writes ──────────────────────────────────────────────────────
    def grant(self, prefix: str, ceiling: str, tool_name: str = "") -> int:
        resolved = str(Path(prefix).expanduser().resolve())
        with self._lock:
            conn = self._ensure_conn()
            cur = conn.execute(
                "INSERT INTO grants (prefix, ceiling, tool_name, created_at) VALUES (?, ?, ?, ?)",
                (resolved, ceiling, tool_name, time.time()),
            )
            conn.commit()
            return int(cur.lastrowid)

    def revoke(self, grant_id: int) -> bool:
        with self._lock:
            conn = self._ensure_conn()
            cur = conn.execute("DELETE FROM grants WHERE id = ?", (grant_id,))
            conn.commit()
            return cur.rowcount > 0

    def clear(self) -> int:
        with self._lock:
            conn = self._ensure_conn()
            cur = conn.execute("DELETE FROM grants")
            conn.commit()
            return cur.rowcount

    # ── reads ───────────────────────────────────────────────────────
    def list_grants(self) -> list[dict]:
        with self._lock:
            rows = self._ensure_conn().execute(
                "SELECT id, prefix, ceiling, tool_name, created_at, last_used_at FROM grants ORDER BY created_at"
            ).fetchall()
        return [
            {
                "id": r[0],
                "prefix": r[1],
                "ceiling": r[2],
                "tool_name": r[3],
                "created_at": r[4],
                "last_used_at": r[5],
            }
            for r in rows
        ]

    def covering_grant(self, *, risk: str, args: dict | None) -> dict | None:
        """The grant covering this call, or None.

        A grant covers a call when every candidate path sits inside
        the grant prefix (a call always has exactly one path today;
        "every" keeps multi-path calls honest) and the call's risk is
        at or below the grant's ceiling.
        """
        candidates = candidate_paths(args)
        if not candidates:
            return None
        call_rank = RISK_ORDER.get(risk)
        if call_rank is None:
            return None
        # ADR 0029 D3: floored paths are ungrantable (see module docstring).
        from aether_engine.tools.policy import _floor_prefixes, _system_prefixes

        for candidate in candidates:
            if any(candidate.is_relative_to(p) for p in _floor_prefixes()):
                return None
            if risk != ToolRisk.READ_ONLY.value and any(
                candidate.is_relative_to(p) for p in _system_prefixes()
            ):
                return None
        # The match scan and the last_used_at write are one atomic
        # step under the cache lock (list_grants re-enters it).
        with self._lock:
            conn = self._ensure_conn()
            for grant_row in self.list_grants():
                if RISK_ORDER.get(grant_row["ceiling"], -1) < call_rank:
                    continue
                prefix = Path(grant_row["prefix"])
                if all(c.is_relative_to(prefix) for c in candidates):
                    conn.execute(
                        "UPDATE grants SET last_used_at = ? WHERE id = ?",
                        (time.time(), grant_row["id"]),
                    )
                    conn.commit()
                    return grant_row
        return None


_default_cache: ApprovalCache | None = None
_default_cache_lock = threading.Lock()


def get_default_cache() -> ApprovalCache:
    """The process-wide cache at ``~/.aether/approval-cache.db``."""
    global _default_cache
    if _default_cache is None:
        # First touch can race across the engine's threads (E2-F04);
        # double-checked locking keeps the singleton a singleton.
        with _default_cache_lock:
            if _default_cache is None:
                _default_cache = ApprovalCache()
    return _default_cache


def get_default_cache_if_open() -> ApprovalCache | None:
    """The process-wide cache if it has been created, else None.

    Never creates it — for callers (the profile restore, E10-F05)
    that must quiesce the live cache only when one is actually
    running against the file they are about to replace.
    """
    with _default_cache_lock:
        return _default_cache
