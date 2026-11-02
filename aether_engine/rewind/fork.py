"""Fork service (Epic 8, E8-1.2).

Fork copies a session up to a completed step boundary into a NEW
session (grill Q6) — "Fork of <title>", immediately continuable,
with the source left completely untouched. Unlike rewind, fork is
NOT restricted to the last task: any step in any task can be a
boundary. The boundary arrives in one of two forms:

  * ``step_index`` — a point in the LAST task's rewind manifest.
    The message cut mirrors the prefix a rewind to the same point
    would keep (see service.py): step 0 ends before the task's
    opening user message, an intermediate point ends at that prompt
    (the response belongs to later steps), the final point ends at
    the session's last message. When the point carries a real
    checkpoint id, the LangGraph thread is copied up to it — the
    main thread's ancestor chain (inclusive) plus subgraph rows at
    or before it (checkpoint ids are time-ordered) — under the
    fork's own thread id, so the fork continues from exactly that
    graph state and later writes on either side stay isolated.
    Fast-chat points carry "" and copy no checkpoints (the same
    doctrine as rewind: an earlier graph task's checkpoints are not
    this task's to copy).
  * ``message_id`` — a message boundary, the form older tasks use
    (the manifest holds no points for them). History ends at that
    message inclusive. No checkpoints are copied: the fork's thread
    starts empty and the executor re-seeds it from the copied
    messages, exactly like the edit-and-resend truncation flow.

With neither argument the whole session is duplicated, thread
included. The fork records no manifest of its own for the inherited
history — its points start with its next task, like a new session's,
so forking FROM a fork (or from an imported session) is again a
message-boundary fork; the message path below needs no manifest.
The response carries the fork's retained ``messages`` exactly as
stored, so the desktop renders the engine's own record of what the
fork kept rather than reconstructing it.

All expected failures are refusals (``ok`` False with an error),
validated before any state is created; a refused fork creates
nothing. Checkpoint rows are treated opaquely (copied by id +
parent chain), the same stance service.py takes.
"""
import logging
import sqlite3
from pathlib import Path
from typing import Any

from aether_engine.memory import session_store
from aether_engine.rewind import manifest as rewind_manifest
from aether_engine.rewind.service import _checkpoint_db_path, _read_checkpoint_rows

logger = logging.getLogger("aether_engine.rewind.fork")


def _refusal(source_session_id: Any, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "new_session_id": "",
        "source_session_id": (
            source_session_id if isinstance(source_session_id, str) else ""),
        "title": "",
        "messages_copied": 0,
        "messages": [],
        "error": message,
    }


def _message_rows(session_id: str) -> list[sqlite3.Row]:
    """(rowid, id, role) for the session's messages, in stored order."""
    path = Path(session_store.DB_PATH)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT rowid, id, role FROM session_messages "
            "WHERE session_id = ? ORDER BY rowid ASC",
            (session_id,),
        ).fetchall()
    finally:
        conn.close()


def _step_cut_message_id(
    session_id: str, step_index: int, final_step: int,
) -> tuple[str | None, bool] | str:
    """The message id a step fork's history ends at (inclusive).

    Returns (cut_message_id, is_empty): is_empty marks the step-0
    fork of a session whose last task is its only content (history
    ends before the first row). An error string is returned when
    the transcript cannot anchor the task. Mirrors the kept prefix
    of service._message_cut_rowids: the task begins at the session's
    last user row.
    """
    rows = _message_rows(session_id)
    if not rows:
        return "The session has no messages to fork."
    start_pos = None
    for pos, row in enumerate(rows):
        if row["role"] == "user":
            start_pos = pos
    if start_pos is None:
        return "The session has no task boundary to fork from."
    if step_index == 0:
        if start_pos == 0:
            return (None, True)
        return (rows[start_pos - 1]["id"], False)
    if step_index < final_step:
        return (rows[start_pos]["id"], False)
    return (rows[-1]["id"], False)


def _plan_checkpoint_copy(
    cp_db: Path, thread_id: str, target_cp: str,
) -> set[tuple[str, str]] | str:
    """The (checkpoint_ns, checkpoint_id) pairs copied for a fork at
    *target_cp* — or an error string when the recorded target is
    gone. Main thread: the target's ancestor chain, inclusive.
    Subgraph namespaces: rows at or before the target (checkpoint
    ids are time-ordered — the stance service.py's prune plan
    takes)."""
    rows = _read_checkpoint_rows(cp_db, thread_id)
    main_parents = {cp: parent for ns, cp, parent in rows if ns == ""}
    if target_cp not in main_parents:
        return "The recorded checkpoint for this fork point no longer exists."
    keep: set[tuple[str, str]] = set()
    current: str | None = target_cp
    while current:
        keep.add(("", current))
        current = main_parents.get(current)
    for ns, cp, _parent in rows:
        if ns and cp <= target_cp:
            keep.add((ns, cp))
    return keep


def _all_checkpoint_keys(cp_db: Path, thread_id: str) -> set[tuple[str, str]]:
    return {(ns, cp) for ns, cp, _parent in _read_checkpoint_rows(cp_db, thread_id)}


def _copy_checkpoints(
    cp_db: Path,
    source_thread_id: str,
    target_thread_id: str,
    keep: set[tuple[str, str]],
) -> int:
    """Copy the kept checkpoint + writes rows under the fork's
    thread id. Returns the number of checkpoint rows copied."""
    if not keep or not cp_db.exists():
        return 0
    conn = sqlite3.connect(cp_db)
    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "checkpoints" not in tables:
            return 0
        copied = 0
        for ns, cp in sorted(keep):
            cur = conn.execute(
                "INSERT INTO checkpoints "
                "(thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, "
                "type, checkpoint, metadata) "
                "SELECT ?, checkpoint_ns, checkpoint_id, parent_checkpoint_id, "
                "type, checkpoint, metadata "
                "FROM checkpoints WHERE thread_id = ? AND checkpoint_ns = ? "
                "AND checkpoint_id = ?",
                (target_thread_id, source_thread_id, ns, cp),
            )
            copied += cur.rowcount
            if "writes" in tables:
                conn.execute(
                    "INSERT INTO writes "
                    "(thread_id, checkpoint_ns, checkpoint_id, task_id, task_path, "
                    "idx, channel, type, value) "
                    "SELECT ?, checkpoint_ns, checkpoint_id, task_id, task_path, "
                    "idx, channel, type, value "
                    "FROM writes WHERE thread_id = ? AND checkpoint_ns = ? "
                    "AND checkpoint_id = ?",
                    (target_thread_id, source_thread_id, ns, cp),
                )
        conn.commit()
        return copied
    finally:
        conn.close()


def fork_session_at(
    source_session_id: str,
    *,
    message_id: str | None = None,
    step_index: int | None = None,
    new_title: str | None = None,
    checkpoint_db: Path | None = None,
) -> dict[str, Any]:
    """Fork *source_session_id* at a step boundary into a new session.

    Returns the SESSION_FORK_RESPONSE payload. Expected failures are
    refusals (``ok`` False with an error), raised before any state
    is created; they never raise.
    """
    if not isinstance(source_session_id, str) or not source_session_id:
        return _refusal(source_session_id, "A session_id is required.")
    if step_index is not None and (
            isinstance(step_index, bool) or not isinstance(step_index, int)):
        return _refusal(source_session_id, "A valid step_index is required.")
    if message_id is not None and not isinstance(message_id, str):
        return _refusal(source_session_id, "A valid message_id is required.")
    if new_title is not None and not isinstance(new_title, str):
        return _refusal(source_session_id, "A valid new_title is required.")

    source = session_store.get_session(source_session_id)
    if source is None:
        return _refusal(source_session_id, "Session not found.")
    title = new_title or f"Fork of {source['title']}"
    cp_db = Path(checkpoint_db) if checkpoint_db else _checkpoint_db_path()

    keep: set[tuple[str, str]] | None = None
    if step_index is not None:
        latest_task = rewind_manifest.get_latest_task_id(source_session_id)
        if latest_task is None:
            return _refusal(
                source_session_id,
                "No recorded steps to fork from in this session.")
        entries = rewind_manifest.get_entries(source_session_id, latest_task)
        entry = next(
            (e for e in entries if e["step_index"] == step_index), None)
        if entry is None:
            return _refusal(
                source_session_id,
                "Fork point not found for this session's last task.")
        cut = _step_cut_message_id(
            source_session_id, step_index, entries[-1]["step_index"])
        if isinstance(cut, str):
            return _refusal(source_session_id, cut)
        cut_message_id, is_empty = cut
        target_cp = entry["checkpoint_id"]
        if target_cp:
            plan = _plan_checkpoint_copy(cp_db, source_session_id, target_cp)
            if isinstance(plan, str):
                return _refusal(source_session_id, plan)
            keep = plan
        if is_empty:
            # The store's fork_session inherits the binding on every
            # other path; this empty-fork branch creates directly, so
            # it must pass the source's binding explicitly or the
            # fork would silently land unbound.
            new_id = session_store.create_session(title, source.get("workspace"))
            copied = 0
            result = {"new_session_id": new_id, "title": title,
                      "messages_copied": 0}
        else:
            result = session_store.fork_session(
                source_session_id, cut_message_id, title)
            if result is None:
                return _refusal(
                    source_session_id, "Fork point could not be resolved.")
            copied = result["messages_copied"]
    elif message_id is not None:
        result = session_store.fork_session(
            source_session_id, message_id, title)
        if result is None:
            return _refusal(
                source_session_id, "Fork point not found in this session.")
        copied = result["messages_copied"]
    else:
        result = session_store.fork_session(source_session_id, None, title)
        if result is None:
            return _refusal(source_session_id, "Session could not be forked.")
        copied = result["messages_copied"]
        keep = _all_checkpoint_keys(cp_db, source_session_id)

    if keep:
        _copy_checkpoints(
            cp_db, source_session_id, result["new_session_id"], keep)
    # The retained history as the store now holds it — the same
    # record a later SESSION_GET serves and the executor re-seeds
    # from, returned so the caller renders the engine's truth.
    stored = session_store.get_session(result["new_session_id"])
    logger.info(
        "SESSION_FORK source=%s new=%s messages=%s step=%s message=%s",
        source_session_id, result["new_session_id"], copied,
        step_index, message_id)
    return {
        "ok": True,
        "new_session_id": result["new_session_id"],
        "source_session_id": source_session_id,
        "title": result["title"],
        # The fork inherits the source's binding (slice 1); the
        # response names it so the caller files the fork correctly.
        "workspace": stored.get("workspace") if stored else source.get("workspace"),
        "messages_copied": copied,
        "messages": stored["messages"] if stored else [],
        "error": None,
    }
