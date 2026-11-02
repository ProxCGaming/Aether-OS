"""Rewind execution service (Epic 8, E8-1.1b).

Executes the rewind E8-1.1a prepared: the per-task manifest in
``manifest.py`` records the points; this service rolls a session back
to one of them. Settled semantics (grill Q2/Q3/Q5):

  * STATE-ONLY. Conversation rows (``session_messages``), manifest
    entries past the point, and LangGraph checkpoints past the point
    roll back. Files on disk are NEVER touched — the response lists
    the files written after the point as ``files_not_reverted`` so
    the confirm modal can say so honestly.
  * LAST TASK ONLY, validated against the manifest: the task id must
    be the session's latest, the step must exist, and the checkpoint
    id must match the recorded one. Anything else is refused before
    any state changes.
  * 5-SECOND UNDO WINDOW. Staging is a hard rollback with a backup:
    the discarded rows are copied into ``rewind_backup_*`` tables and
    deleted from the live ones, so the engine is in the rewound state
    at every instant (a reload inside the window shows the rewound
    conversation). ``undo_staged`` restores the backups in their
    original per-session order (rows are re-inserted in backup order
    under FRESH rowids — the message/manifest ids are the stable
    identity, so a freed global rowid another session has since
    reused can never collide with the restore). ``commit_staged``
    (window expiry, or a new message in the session — the websocket
    START_TASK hook calls the same function) simply drops the
    backups: the discarded steps are pruned outright, no branch kept.

Checkpoint pruning (checkpoints.db, written by LangGraph's sqlite
saver — rows are treated opaquely, copied/deleted by id):
  * Main thread (checkpoint_ns = ''): strict descendants of the
    target checkpoint via the parent chain. A target of "" is the
    fresh-thread step 0: the thread was empty when the task began,
    so every checkpoint in it belongs to this task.
  * Subgraph namespaces (``planner:<uuid>`` etc., one per invocation,
    each with its own chain): an invocation belongs to a discarded
    step iff its earliest checkpoint postdates the target —
    checkpoint ids are time-ordered (the saver itself relies on
    ``ORDER BY checkpoint_id DESC`` for "latest").
  * Fast-chat tasks record no checkpoints (honest "" ids), so their
    rewinds never touch checkpoints.db — including checkpoints an
    earlier graph task left in the same session.

The pending/backup tables live beside the data they mirror:
messages + manifest in memory.db (the session store's database),
checkpoint rows in checkpoints.db.

Crash safety (M11): staging is a recoverable protocol, not two
bare store updates. The pending row is written FIRST, as a staging
intent carrying the checkpoint prune plan and the deadline; the
memory side then backs up + deletes and flips the row's phase to
``memory`` in the SAME transaction; the checkpoint side stages
last and the phase flips to ``complete``. The persisted row is
therefore always an accurate account of how far staging got, and
``reconcile_pending`` — run when the service initialises (i.e. on
the first touch after a restart) and before each new staging —
converges any interrupted state: an intent whose memory
transaction never committed is dropped (nothing was staged), a
``memory``-phase row has its checkpoint staging completed from
the persisted plan (idempotently), and any row past its persisted
``expires_at`` is pruned. The deadline lives in that persisted
timestamp, not in the process-local expiry timer: ``undo_staged``
refuses — and commits the rewind — once it has passed, timer or
no timer.

The async shell (``rewind`` / ``undo`` / ``commit``) adds the
5-second expiry timer on top of the synchronous core; the core is
what tests drive directly.
"""
import asyncio
import json
import logging
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from aether_engine.memory import session_store
from aether_engine.rewind import manifest as rewind_manifest

logger = logging.getLogger("aether_engine.rewind.service")

UNDO_WINDOW_SECONDS = 5.0

# Init guard holding the memory.db path the pending/backup tables
# were created in — the same path-keyed pattern the sibling modules
# use, so a changed DB path re-initialises.
_initialized_for: Any = False


def _db_path() -> Path:
    return Path(session_store.DB_PATH)


def _get_conn() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _checkpoint_db_path() -> Path:
    """The checkpoints.db the task runners actually write.

    The executor binds ``DEFAULT_CHECKPOINT_DB`` into its own module
    namespace (and tests patch it there), so its current value is
    the operative one; the checkpointer module's constant is the
    fallback if the executor cannot be imported.
    """
    try:
        from aether_engine.langgraph import executor as executor_module

        return Path(executor_module.DEFAULT_CHECKPOINT_DB)
    except Exception:  # noqa: BLE001 — path resolution must not fail the rewind
        from aether_engine.langgraph.checkpointer import DEFAULT_CHECKPOINT_DB

        return Path(DEFAULT_CHECKPOINT_DB)


def init_rewind_service_db() -> None:
    global _initialized_for
    path = _db_path()
    if _initialized_for == path:
        return
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rewind_pending (
                session_id TEXT PRIMARY KEY,
                rewind_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                step_index INTEGER NOT NULL,
                checkpoint_id TEXT NOT NULL,
                expires_at REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rewind_backup_messages (
                rewind_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                orig_rowid INTEGER NOT NULL,
                id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                thoughts TEXT,
                attachments TEXT,
                timestamp INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rewind_backup_manifest (
                rewind_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                orig_rowid INTEGER NOT NULL,
                id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                step_index INTEGER NOT NULL,
                checkpoint_id TEXT NOT NULL,
                label TEXT NOT NULL,
                file_writes_json TEXT NOT NULL DEFAULT '[]',
                write_events_json TEXT NOT NULL DEFAULT '[]',
                timestamp INTEGER NOT NULL
            )
        """)
        # Migrations for databases created before the columns
        # existed (same try/except ALTER pattern session_store
        # uses): backups written by older code carry NULL
        # attachments and restore with attachments absent; pending
        # rows written by older code were fully staged by the time
        # they existed, hence the 'complete' phase default. Backup
        # manifest rows written before write events (N01) restore
        # with no events, exactly as their source rows read.
        for statement in (
            "ALTER TABLE rewind_backup_messages ADD COLUMN attachments TEXT",
            "ALTER TABLE rewind_pending ADD COLUMN phase TEXT NOT NULL DEFAULT 'complete'",
            "ALTER TABLE rewind_pending ADD COLUMN checkpoint_plan TEXT NOT NULL DEFAULT '[]'",
            "ALTER TABLE rewind_backup_manifest ADD COLUMN write_events_json TEXT NOT NULL DEFAULT '[]'",
        ):
            try:
                conn.execute(statement)
            except sqlite3.OperationalError:
                pass
        conn.commit()
    _initialized_for = path
    # First touch in this process: converge any staged state a
    # previous process left interrupted or expired. A reconcile
    # failure must never break the service itself — the staged
    # rows stay put and the next touch retries.
    try:
        reconcile_pending()
    except Exception:
        logger.exception("Rewind reconciliation failed at init")


def _init_checkpoint_backup_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rewind_backup_checkpoints (
            rewind_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            thread_id TEXT,
            checkpoint_ns TEXT,
            checkpoint_id TEXT,
            parent_checkpoint_id TEXT,
            type TEXT,
            checkpoint BLOB,
            metadata BLOB
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rewind_backup_writes (
            rewind_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            thread_id TEXT,
            checkpoint_ns TEXT,
            checkpoint_id TEXT,
            task_id TEXT,
            task_path TEXT,
            idx INTEGER,
            channel TEXT,
            type TEXT,
            value BLOB
        )
    """)


def has_staged(session_id: str) -> bool:
    """True while a rewind for *session_id* sits in its undo window."""
    init_rewind_service_db()
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM rewind_pending WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    return row is not None


def _pending_row(session_id: str) -> sqlite3.Row | None:
    init_rewind_service_db()
    with _get_conn() as conn:
        return conn.execute(
            "SELECT * FROM rewind_pending WHERE session_id = ?",
            (session_id,),
        ).fetchone()


# ---------------------------------------------------------------------------
# Staged-state bookkeeping (shared by undo / commit / reconciliation)
# ---------------------------------------------------------------------------


def _delete_staged_rows(
    conn: sqlite3.Connection, session_id: str, rewind_id: str,
) -> None:
    """Drop a staged rewind's memory-side rows (backups + pending)."""
    conn.execute(
        "DELETE FROM rewind_backup_messages WHERE rewind_id = ?", (rewind_id,))
    conn.execute(
        "DELETE FROM rewind_backup_manifest WHERE rewind_id = ?", (rewind_id,))
    conn.execute(
        "DELETE FROM rewind_pending WHERE session_id = ?", (session_id,))


def _drop_checkpoint_backups(cp_db: Path, rewind_id: str) -> None:
    """Drop a staged rewind's checkpoint-side backup rows."""
    if not cp_db.exists():
        return
    conn = sqlite3.connect(cp_db)
    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "rewind_backup_checkpoints" in tables:
            conn.execute(
                "DELETE FROM rewind_backup_checkpoints WHERE rewind_id = ?",
                (rewind_id,))
            conn.execute(
                "DELETE FROM rewind_backup_writes WHERE rewind_id = ?",
                (rewind_id,))
            conn.commit()
    finally:
        conn.close()


def _set_pending_phase(rewind_id: str, phase: str) -> None:
    with _get_conn() as conn:
        conn.execute(
            "UPDATE rewind_pending SET phase = ? WHERE rewind_id = ?",
            (phase, rewind_id))
        conn.commit()


def reconcile_pending(*, checkpoint_db: Path | None = None) -> None:
    """Converge staged rewinds a previous process left behind.

    The persisted pending row is authoritative (see the module
    docstring): a ``staging`` row's memory transaction never
    committed, so nothing was staged and the intent is dropped; a
    ``memory`` row's checkpoint staging is completed from its
    persisted plan; any row past its persisted ``expires_at`` is
    pruned (the same drop commit performs). Rows still inside
    their window are left exactly as they are — undoable.

    Assumes the tables exist (callers initialise first). Written
    against raw connections so initialisation itself can call it
    without recursing.
    """
    cp_db = Path(checkpoint_db) if checkpoint_db else _checkpoint_db_path()
    with _get_conn() as conn:
        rows = [dict(row) for row in conn.execute(
            "SELECT * FROM rewind_pending").fetchall()]
    for row in rows:
        session_id = row["session_id"]
        rewind_id = row["rewind_id"]
        phase = row.get("phase") or "complete"
        if phase == "staging":
            with _get_conn() as conn:
                conn.execute(
                    "DELETE FROM rewind_pending WHERE session_id = ?",
                    (session_id,))
                conn.commit()
            logger.info(
                "TASK_REWIND dropped uncommitted staging intent session=%s",
                session_id)
            continue
        if phase == "memory":
            try:
                plan = {(ns, cp) for ns, cp in json.loads(
                    row.get("checkpoint_plan") or "[]")}
            except (ValueError, TypeError):
                plan = set()
            _stage_checkpoint_side(cp_db, session_id, rewind_id, plan)
            _set_pending_phase(rewind_id, "complete")
            logger.info(
                "TASK_REWIND completed interrupted staging session=%s",
                session_id)
        if float(row["expires_at"]) <= time.time():
            with _get_conn() as conn:
                _delete_staged_rows(conn, session_id, rewind_id)
                conn.commit()
            _drop_checkpoint_backups(cp_db, rewind_id)
            logger.info(
                "TASK_REWIND pruned expired staged rewind session=%s",
                session_id)


# ---------------------------------------------------------------------------
# Checkpoint pruning plan
# ---------------------------------------------------------------------------


def _read_checkpoint_rows(cp_db: Path, thread_id: str) -> list[tuple[str, str, str | None]]:
    """(checkpoint_ns, checkpoint_id, parent_checkpoint_id) for a
    thread; empty when the store or the thread does not exist."""
    if not cp_db.exists():
        return []
    conn = sqlite3.connect(f"file:{cp_db}?mode=ro", uri=True)
    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "checkpoints" not in tables:
            return []
        return conn.execute(
            "SELECT checkpoint_ns, checkpoint_id, parent_checkpoint_id "
            "FROM checkpoints WHERE thread_id = ?",
            (thread_id,),
        ).fetchall()
    finally:
        conn.close()


def _plan_checkpoint_prune(
    cp_db: Path, thread_id: str, is_graph_task: bool, target_cp: str,
) -> set[tuple[str, str]] | str:
    """The (checkpoint_ns, checkpoint_id) pairs a rewind discards —
    or an error string when the recorded target is gone."""
    if not is_graph_task:
        # Fast-chat points carry no checkpoints; an earlier graph
        # task's checkpoints in this session are not this task's
        # to prune.
        return set()
    rows = _read_checkpoint_rows(cp_db, thread_id)
    if not rows:
        if target_cp:
            return "The recorded checkpoint for this rewind point no longer exists."
        return set()
    if not target_cp:
        # Fresh-thread step 0: the thread was empty when this task
        # began, so every checkpoint in it is this task's.
        return {(ns, cp) for ns, cp, _parent in rows}

    main_parents = {cp: parent for ns, cp, parent in rows if ns == ""}
    if target_cp not in main_parents:
        return "The recorded checkpoint for this rewind point no longer exists."

    children: dict[str, list[str]] = {}
    for cp, parent in main_parents.items():
        if parent:
            children.setdefault(parent, []).append(cp)
    discarded: set[tuple[str, str]] = set()
    stack = list(children.get(target_cp, []))
    while stack:
        current = stack.pop()
        if ("", current) in discarded:
            continue
        discarded.add(("", current))
        stack.extend(children.get(current, []))

    earliest_by_ns: dict[str, str] = {}
    for ns, cp, _parent in rows:
        if ns and (ns not in earliest_by_ns or cp < earliest_by_ns[ns]):
            earliest_by_ns[ns] = cp
    for ns, cp, _parent in rows:
        if ns and earliest_by_ns[ns] > target_cp:
            discarded.add((ns, cp))
    return discarded


# ---------------------------------------------------------------------------
# Staging (the rewind itself)
# ---------------------------------------------------------------------------


def _message_cut_rowids(
    conn: sqlite3.Connection, session_id: str, target_step: int, final_step: int,
) -> list[int]:
    """Rowids of session_messages the rewind discards.

    Mirrors the UI's structural mapping (E8-1.1a): the task begins
    at the session's last user row. Step 0 discards from that row
    on; an intermediate point discards what follows it (the task's
    response — intermediate steps live in checkpoints, not rows);
    the final point discards nothing.
    """
    rows = conn.execute(
        "SELECT rowid, role FROM session_messages WHERE session_id = ? "
        "ORDER BY rowid ASC",
        (session_id,),
    ).fetchall()
    if not rows:
        return []
    start_rowid = None
    for row in rows:
        if row["role"] == "user":
            start_rowid = row["rowid"]
    if start_rowid is None:
        return []
    if target_step == 0:
        return [row["rowid"] for row in rows if row["rowid"] >= start_rowid]
    if target_step < final_step:
        return [row["rowid"] for row in rows if row["rowid"] > start_rowid]
    return []


def _record_staging_intent(
    session_id: str,
    task_id: str,
    target: dict[str, Any],
    rewind_id: str,
    expires_at: float,
    plan: set[tuple[str, str]],
) -> None:
    """Write the pending row FIRST, as a staging intent.

    The row carries everything reconciliation needs to converge an
    interrupted staging without guessing: the checkpoint prune
    plan and the undo deadline. Its phase starts at ``staging`` —
    until the memory transaction flips it, nothing has been staged
    and the intent can simply be dropped.
    """
    init_rewind_service_db()
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO rewind_pending "
            "(session_id, rewind_id, task_id, step_index, checkpoint_id, "
            "expires_at, phase, checkpoint_plan) "
            "VALUES (?, ?, ?, ?, ?, ?, 'staging', ?)",
            (session_id, rewind_id, task_id, target["step_index"],
             target["checkpoint_id"], expires_at,
             json.dumps(sorted(plan))),
        )
        conn.commit()


def _stage_memory_side(
    session_id: str,
    task_id: str,
    target: dict[str, Any],
    entries: list[dict[str, Any]],
    rewind_id: str,
) -> None:
    """One transaction: message/manifest backup+delete, then the
    pending row's phase flip to ``memory`` — atomic with the
    staging it records, so the phase never claims more (or less)
    than the store actually holds."""
    init_rewind_service_db()
    final_step = entries[-1]["step_index"]
    with _get_conn() as conn:
        cut = _message_cut_rowids(conn, session_id, target["step_index"], final_step)
        if cut:
            placeholders = ",".join("?" for _ in cut)
            conn.execute(
                "INSERT INTO rewind_backup_messages "
                "(rewind_id, session_id, orig_rowid, id, role, content, thoughts, "
                "attachments, timestamp) "
                f"SELECT ?, session_id, rowid, id, role, content, thoughts, "
                f"attachments, timestamp "
                f"FROM session_messages WHERE rowid IN ({placeholders})",
                (rewind_id, *cut),
            )
            conn.execute(
                f"DELETE FROM session_messages WHERE rowid IN ({placeholders})", cut)
        later_steps = [e["step_index"] for e in entries
                       if e["step_index"] > target["step_index"]]
        if later_steps:
            placeholders = ",".join("?" for _ in later_steps)
            conn.execute(
                "INSERT INTO rewind_backup_manifest "
                "(rewind_id, session_id, orig_rowid, id, task_id, step_index, "
                "checkpoint_id, label, file_writes_json, write_events_json, "
                "timestamp) "
                f"SELECT ?, session_id, rowid, id, task_id, step_index, "
                f"checkpoint_id, label, file_writes_json, write_events_json, "
                f"timestamp "
                f"FROM rewind_manifest WHERE session_id = ? AND task_id = ? "
                f"AND step_index IN ({placeholders})",
                (rewind_id, session_id, task_id, *later_steps),
            )
            conn.execute(
                "DELETE FROM rewind_manifest WHERE session_id = ? AND task_id = ? "
                f"AND step_index IN ({placeholders})",
                (session_id, task_id, *later_steps),
            )
        conn.execute(
            "UPDATE rewind_pending SET phase = 'memory' WHERE rewind_id = ?",
            (rewind_id,))
        conn.commit()


def _stage_checkpoint_side(
    cp_db: Path, session_id: str, rewind_id: str,
    discarded: set[tuple[str, str]],
) -> None:
    """Backup + delete the discarded checkpoint/write rows."""
    if not discarded or not cp_db.exists():
        return
    conn = sqlite3.connect(cp_db)
    try:
        _init_checkpoint_backup_db(conn)
        for ns, cp in sorted(discarded):
            conn.execute(
                "INSERT INTO rewind_backup_checkpoints "
                "(rewind_id, session_id, thread_id, checkpoint_ns, checkpoint_id, "
                "parent_checkpoint_id, type, checkpoint, metadata) "
                "SELECT ?, ?, thread_id, checkpoint_ns, checkpoint_id, "
                "parent_checkpoint_id, type, checkpoint, metadata "
                "FROM checkpoints WHERE thread_id = ? AND checkpoint_ns = ? "
                "AND checkpoint_id = ?",
                (rewind_id, session_id, session_id, ns, cp),
            )
            conn.execute(
                "INSERT INTO rewind_backup_writes "
                "(rewind_id, session_id, thread_id, checkpoint_ns, checkpoint_id, "
                "task_id, task_path, idx, channel, type, value) "
                "SELECT ?, ?, thread_id, checkpoint_ns, checkpoint_id, "
                "task_id, task_path, idx, channel, type, value "
                "FROM writes WHERE thread_id = ? AND checkpoint_ns = ? "
                "AND checkpoint_id = ?",
                (rewind_id, session_id, session_id, ns, cp),
            )
            conn.execute(
                "DELETE FROM writes WHERE thread_id = ? AND checkpoint_ns = ? "
                "AND checkpoint_id = ?",
                (session_id, ns, cp),
            )
            conn.execute(
                "DELETE FROM checkpoints WHERE thread_id = ? AND checkpoint_ns = ? "
                "AND checkpoint_id = ?",
                (session_id, ns, cp),
            )
        conn.commit()
    finally:
        conn.close()


def _files_not_reverted(
    entries: list[dict[str, Any]], target: dict[str, Any],
) -> list[str]:
    """The files a rewind to *target* will NOT revert: every path
    with at least one write after the target point, in first-write
    order, each listed once.

    Two sources, unioned:

    * WRITE EVENTS recorded after the target point (N01) — the only
      record that sees a REPEAT write: a file written before the
      point and again after it keeps its post-point content, so it
      belongs on this list, but the cumulative lists hold it on both
      sides of the boundary and a set difference cancels it out.
    * The cumulative difference (final minus target) — for points
      recorded before write events existed it is the only signal,
      and it never false-positives: a path in the final list but not
      the target's was first written after the target point.

    The desktop computes the same union for the confirm modal.
    """
    warned: list[str] = []
    seen: set[str] = set()

    def add(path: str) -> None:
        if path not in seen:
            seen.add(path)
            warned.append(path)

    for entry in entries:
        if entry["step_index"] > target["step_index"]:
            for path in entry.get("write_events") or []:
                add(path)
    target_files = set(target["files_written"])
    for path in entries[-1]["files_written"]:
        if path not in target_files:
            add(path)
    return warned


def stage_rewind(
    session_id: str,
    task_id: str,
    step_index: int,
    checkpoint_id: str,
    *,
    checkpoint_db: Path | None = None,
    undo_window_seconds: float = UNDO_WINDOW_SECONDS,
) -> dict[str, Any]:
    """Roll *session_id*'s last task back to a manifest point.

    Returns the TASK_REWIND_RESPONSE payload. Expected failures are
    refusals (``ok`` False with an error), raised before any state
    changes; they never raise.
    """
    def refusal(message: str) -> dict[str, Any]:
        return {
            "ok": False,
            "session_id": session_id if isinstance(session_id, str) else "",
            "rewound_to_step": (
                step_index if isinstance(step_index, int)
                and not isinstance(step_index, bool) else -1),
            "undo_expires_at": 0.0,
            "files_not_reverted": [],
            "messages": [],
            "error": message,
        }

    if not isinstance(session_id, str) or not session_id:
        return refusal("A session_id is required.")
    if not isinstance(task_id, str) or not task_id:
        return refusal("A task_id is required.")
    if isinstance(step_index, bool) or not isinstance(step_index, int):
        return refusal("A valid step_index is required.")
    if not isinstance(checkpoint_id, str):
        return refusal("A valid checkpoint_id is required.")

    init_rewind_service_db()
    # Converge anything a previous interruption left behind before
    # judging whether this session is free to stage: an expired or
    # half-staged pending row must not wedge new rewinds.
    reconcile_pending()
    if has_staged(session_id):
        return refusal(
            "A rewind is already waiting for undo in this session; "
            "undo it or let it expire first.")
    latest_task = rewind_manifest.get_latest_task_id(session_id)
    if latest_task is None:
        return refusal("No rewind points recorded for this session.")
    if task_id != latest_task:
        return refusal("Rewind is available for the current task only.")
    entries = rewind_manifest.get_entries(session_id, task_id)
    target = next(
        (e for e in entries if e["step_index"] == step_index), None)
    if target is None:
        return refusal("Rewind point not found for this task.")
    if target["checkpoint_id"] != checkpoint_id:
        return refusal("Rewind point does not match the recorded checkpoint.")

    files_not_reverted = _files_not_reverted(entries, target)
    is_graph_task = any(e["checkpoint_id"] for e in entries)
    cp_db = Path(checkpoint_db) if checkpoint_db else _checkpoint_db_path()
    plan = _plan_checkpoint_prune(cp_db, session_id, is_graph_task, checkpoint_id)
    if isinstance(plan, str):
        return refusal(plan)

    rewind_id = str(uuid.uuid4())
    expires_at = time.time() + float(undo_window_seconds)
    _record_staging_intent(
        session_id, task_id, target, rewind_id, expires_at, plan)
    _stage_memory_side(session_id, task_id, target, entries, rewind_id)
    _stage_checkpoint_side(cp_db, session_id, rewind_id, plan)
    _set_pending_phase(rewind_id, "complete")
    logger.info(
        "TASK_REWIND staged session=%s task=%s step=%s files_not_reverted=%s",
        session_id, task_id, step_index, files_not_reverted)
    # The retained history as the store now holds it (post-cut), so
    # the desktop renders the engine's own record — at the final
    # point that is the FULL history, response included; a local
    # reconstruction would remove it anyway and diverge on reload.
    stored = session_store.get_session(session_id)
    return {
        "ok": True,
        "session_id": session_id,
        "rewound_to_step": step_index,
        "undo_expires_at": expires_at,
        "files_not_reverted": files_not_reverted,
        "messages": stored["messages"] if stored else [],
        "error": None,
    }


# ---------------------------------------------------------------------------
# Undo / commit
# ---------------------------------------------------------------------------


def undo_staged(
    session_id: str, *, checkpoint_db: Path | None = None,
) -> dict[str, Any]:
    """Restore a staged rewind's discarded steps in full."""
    pending = _pending_row(session_id)
    if pending is None:
        return {
            "ok": False, "restored": False,
            "error": "No rewind is waiting for undo in this session.",
        }
    rewind_id = pending["rewind_id"]
    if time.time() > float(pending["expires_at"]):
        # The persisted deadline is authoritative — the in-process
        # expiry timer is only an optimisation, and a restart (or a
        # direct backend call) must not resurrect an expired undo.
        # The rewind commits instead: the steps are pruned.
        commit_staged(session_id, checkpoint_db=checkpoint_db)
        return {
            "ok": False, "restored": False,
            "error": "The undo window has expired; the rewound steps "
                     "have been pruned.",
        }
    if session_store.get_session(session_id) is None:
        # The session was deleted inside the window: nothing can be
        # restored into it — drop the staged state instead of
        # resurrecting rows under a dead foreign key.
        commit_staged(session_id, checkpoint_db=checkpoint_db)
        return {
            "ok": False, "restored": False,
            "error": "The session no longer exists.",
        }
    with _get_conn() as conn:
        # Fresh rowids, assigned in the backups' original order:
        # the rows' ids are their stable identity, and (timestamp,
        # rowid) readers see the original sequence because the
        # re-insertion order reproduces it. Reinserting the
        # ORIGINAL rowids instead would collide with any row
        # another session has since been assigned one of the freed
        # rowids (M10) and fail the whole restore.
        conn.execute(
            "INSERT INTO session_messages "
            "(id, session_id, role, content, thoughts, attachments, timestamp) "
            "SELECT id, session_id, role, content, thoughts, attachments, timestamp "
            "FROM rewind_backup_messages WHERE rewind_id = ? ORDER BY orig_rowid ASC",
            (rewind_id,),
        )
        conn.execute(
            "INSERT INTO rewind_manifest "
            "(id, session_id, task_id, step_index, checkpoint_id, label, "
            "file_writes_json, write_events_json, timestamp) "
            "SELECT id, session_id, task_id, step_index, checkpoint_id, "
            "label, file_writes_json, write_events_json, timestamp "
            "FROM rewind_backup_manifest WHERE rewind_id = ? ORDER BY orig_rowid ASC",
            (rewind_id,),
        )
        _delete_staged_rows(conn, session_id, rewind_id)
        conn.commit()
    cp_db = Path(checkpoint_db) if checkpoint_db else _checkpoint_db_path()
    if cp_db.exists():
        conn = sqlite3.connect(cp_db)
        try:
            tables = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
            if "rewind_backup_checkpoints" in tables:
                conn.execute(
                    "INSERT INTO checkpoints "
                    "(thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, "
                    "type, checkpoint, metadata) "
                    "SELECT thread_id, checkpoint_ns, checkpoint_id, "
                    "parent_checkpoint_id, type, checkpoint, metadata "
                    "FROM rewind_backup_checkpoints WHERE rewind_id = ?",
                    (rewind_id,),
                )
                conn.execute(
                    "INSERT INTO writes "
                    "(thread_id, checkpoint_ns, checkpoint_id, task_id, task_path, "
                    "idx, channel, type, value) "
                    "SELECT thread_id, checkpoint_ns, checkpoint_id, task_id, "
                    "task_path, idx, channel, type, value "
                    "FROM rewind_backup_writes WHERE rewind_id = ?",
                    (rewind_id,),
                )
                conn.execute(
                    "DELETE FROM rewind_backup_checkpoints WHERE rewind_id = ?",
                    (rewind_id,))
                conn.execute(
                    "DELETE FROM rewind_backup_writes WHERE rewind_id = ?",
                    (rewind_id,))
                conn.commit()
        finally:
            conn.close()
    logger.info("TASK_REWIND_UNDO restored session=%s", session_id)
    return {"ok": True, "restored": True, "error": None}


def commit_staged(
    session_id: str, *, checkpoint_db: Path | None = None,
) -> bool:
    """Prune a staged rewind's discarded steps for good.

    Returns True when a staged rewind was committed. Idempotent:
    with nothing staged it is a no-op, which is what lets the expiry
    timer, the undo path, and the new-message hook race safely —
    whoever commits first wins, the rest find nothing.
    """
    pending = _pending_row(session_id)
    if pending is None:
        return False
    rewind_id = pending["rewind_id"]
    with _get_conn() as conn:
        _delete_staged_rows(conn, session_id, rewind_id)
        conn.commit()
    cp_db = Path(checkpoint_db) if checkpoint_db else _checkpoint_db_path()
    _drop_checkpoint_backups(cp_db, rewind_id)
    logger.info("TASK_REWIND committed session=%s", session_id)
    return True


# ---------------------------------------------------------------------------
# Async shell: the 5-second undo window
# ---------------------------------------------------------------------------

_expiry_timers: dict[str, asyncio.Task] = {}


def _cancel_timer(session_id: str) -> None:
    task = _expiry_timers.pop(session_id, None)
    if task is not None and not task.done():
        task.cancel()


async def _expire_after(session_id: str, delay: float) -> None:
    try:
        await asyncio.sleep(delay)
        await asyncio.to_thread(commit_staged, session_id)
    except asyncio.CancelledError:
        raise
    except Exception:  # a failed prune must not kill the loop; the staged rows stay recoverable
        logger.exception("Rewind expiry prune failed for session %s", session_id)
    finally:
        _expiry_timers.pop(session_id, None)


async def rewind(
    session_id: str,
    task_id: str,
    step_index: int,
    checkpoint_id: str,
    *,
    undo_window_seconds: float = UNDO_WINDOW_SECONDS,
) -> dict[str, Any]:
    """Stage a rewind and arm its expiry timer (TASK_REWIND_REQUEST)."""
    result = await asyncio.to_thread(
        stage_rewind, session_id, task_id, step_index, checkpoint_id,
        undo_window_seconds=undo_window_seconds)
    if result["ok"]:
        _cancel_timer(session_id)
        _expiry_timers[session_id] = asyncio.create_task(
            _expire_after(session_id, float(undo_window_seconds)))
    return result


async def undo(session_id: str) -> dict[str, Any]:
    """Undo a staged rewind inside its window (TASK_REWIND_UNDO_REQUEST)."""
    result = await asyncio.to_thread(undo_staged, session_id)
    if result["ok"]:
        _cancel_timer(session_id)
    return result


async def commit(session_id: str) -> bool:
    """Commit a staged rewind now (a new message arrived for the
    session — the undo toast is dismissed and the steps pruned)."""
    committed = await asyncio.to_thread(commit_staged, session_id)
    if committed:
        _cancel_timer(session_id)
    return committed
