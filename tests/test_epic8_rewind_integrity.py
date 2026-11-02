"""Epic 8 rewind integrity: attachments, rowid safety, crash safety.

Regression tests for the Astra review findings M09–M11:

  * M09 — rewind backup/restore and session fork silently dropped
    message attachments (the backup tables had no attachments
    column; fork's INSERT omitted it).
  * M10 — Undo reinserted discarded rows with their ORIGINAL global
    SQLite rowids; another session reusing a freed rowid made Undo
    fail with a UNIQUE constraint error.
  * M11 — staging was not crash-safe: memory committed before
    checkpoint staging (a crash between the stores split the
    manifest head from the checkpoint head), the 5-second expiry
    lived only in process-local timers (a restart wedged the
    pending rows), and ``undo_staged`` ignored the persisted
    ``expires_at`` (an expired backend Undo still succeeded).

The sync core is driven directly, mirroring
test_epic8_rewind_execution.py. Crashes are simulated by fault
injection at the store boundary plus a reset of the service's init
guard (the "restarted process"), never by killing processes.
"""
import sqlite3
import time

import pytest

from aether_engine.langgraph import executor as executor_module
from aether_engine.memory import session_store
from aether_engine.rewind import manifest as rewind_manifest
from aether_engine.rewind import service as rewind_service

SNIP = [{"name": "snip.png", "kind": "image", "path": "/snips/snip.png"}]
DIAGRAM = [{"name": "diagram.txt", "kind": "file", "path": "/docs/diagram.txt"}]


@pytest.fixture
def store_env(tmp_path, monkeypatch):
    """Session store (and therefore the manifest + rewind backups,
    which share its DB) isolated to a tmp memory.db."""
    monkeypatch.setattr(session_store, "DB_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(session_store, "_initialized", False)
    return tmp_path


@pytest.fixture
def cp_env(store_env, monkeypatch):
    """store_env plus the checkpoint DB the service resolves."""
    monkeypatch.setattr(
        executor_module, "DEFAULT_CHECKPOINT_DB", store_env / "checkpoints.db")
    return store_env


def _messages(sid):
    sess = session_store.get_session(sid)
    return sess["messages"]


def _raw(db_path, sql, params=()):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(sql, params).fetchall()


def _simulate_restart(monkeypatch):
    """A restarted process re-runs the service's init (and with it
    reconciliation) on its first touch of a store."""
    monkeypatch.setattr(rewind_service, "_initialized_for", False)


def _fast_chat_session(store_env, title="Chat"):
    sid = session_store.create_session(title)
    session_store.add_message(sid, "user", "look at this", attachments=SNIP)
    session_store.add_message(sid, "assistant", "I see it", attachments=DIAGRAM)
    rewind_manifest.record_point(sid, "task-1", 0, "", "Before this task began", [])
    rewind_manifest.record_point(sid, "task-1", 1, "", "Reply complete", [])
    return sid


# ---------------------------------------------------------------------------
# M09 — attachments survive fork and rewind/Undo
# ---------------------------------------------------------------------------


def test_fork_preserves_message_attachments(store_env):
    sid = session_store.create_session("Source")
    session_store.add_message(sid, "user", "look at this", attachments=SNIP)
    session_store.add_message(sid, "assistant", "I see it", attachments=DIAGRAM)

    result = session_store.fork_session(sid, None, "Fork of Source")
    assert result is not None
    forked = _messages(result["new_session_id"])
    assert [(m["role"], m["content"]) for m in forked] == [
        ("user", "look at this"), ("assistant", "I see it")]
    assert forked[0]["attachments"] == SNIP
    assert forked[1]["attachments"] == DIAGRAM


def test_rewind_undo_preserves_message_attachments(store_env):
    sid = _fast_chat_session(store_env)

    result = rewind_service.stage_rewind(sid, "task-1", 0, "")
    assert result["ok"] is True
    assert _messages(sid) == []

    undo = rewind_service.undo_staged(sid)
    assert undo == {"ok": True, "restored": True, "error": None}
    restored = _messages(sid)
    assert [(m["role"], m["content"]) for m in restored] == [
        ("user", "look at this"), ("assistant", "I see it")]
    assert restored[0]["attachments"] == SNIP
    assert restored[1]["attachments"] == DIAGRAM


# ---------------------------------------------------------------------------
# M10 — Undo must not depend on freed global rowids being free
# ---------------------------------------------------------------------------


def test_undo_survives_rowid_reuse_by_another_session(store_env):
    db = store_env / "memory.db"
    sid = _fast_chat_session(store_env, "Rewound")
    orig_msg_rowids = {r[0] for r in _raw(
        db, "SELECT rowid FROM session_messages WHERE session_id = ?", (sid,))}
    orig_manifest_rowids = {r[0] for r in _raw(
        db, "SELECT rowid FROM rewind_manifest WHERE session_id = ?", (sid,))}
    assert orig_msg_rowids and orig_manifest_rowids

    result = rewind_service.stage_rewind(sid, "task-1", 0, "")
    assert result["ok"] is True
    assert _messages(sid) == []

    # A concurrent session's activity reuses the freed rowids (the
    # exact collision the review reproduced).
    other = session_store.create_session("Concurrent")
    session_store.add_message(other, "user", "meanwhile")
    session_store.add_message(other, "assistant", "indeed")
    rewind_manifest.record_point(other, "task-b", 0, "", "Before this task began", [])
    rewind_manifest.record_point(other, "task-b", 1, "", "Reply complete", [])
    reused_msg = {r[0] for r in _raw(
        db, "SELECT rowid FROM session_messages WHERE session_id = ?", (other,))}
    reused_manifest = {r[0] for r in _raw(
        db, "SELECT rowid FROM rewind_manifest WHERE session_id = ?", (other,))}
    assert reused_msg & orig_msg_rowids, "precondition: a message rowid was reused"
    assert reused_manifest & orig_manifest_rowids, \
        "precondition: a manifest rowid was reused"

    undo = rewind_service.undo_staged(sid)
    assert undo == {"ok": True, "restored": True, "error": None}
    restored = _messages(sid)
    assert [(m["role"], m["content"]) for m in restored] == [
        ("user", "look at this"), ("assistant", "I see it")]
    assert [e["step_index"] for e in rewind_manifest.get_entries(sid, "task-1")] == [0, 1]
    # The other session is untouched by the restore.
    assert [(m["role"], m["content"]) for m in _messages(other)] == [
        ("user", "meanwhile"), ("assistant", "indeed")]


# ---------------------------------------------------------------------------
# M11 — the persisted deadline is authoritative
# ---------------------------------------------------------------------------


def test_expired_undo_is_refused_and_the_rewind_commits(store_env):
    db = store_env / "memory.db"
    sid = _fast_chat_session(store_env)
    result = rewind_service.stage_rewind(sid, "task-1", 0, "")
    assert result["ok"] is True

    # The window elapses with no timer to fire (e.g. the process
    # restarted): only the persisted expires_at knows.
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE rewind_pending SET expires_at = ? WHERE session_id = ?",
            (time.time() - 1.0, sid))
        conn.commit()

    undo = rewind_service.undo_staged(sid)
    assert undo["ok"] is False
    assert undo["restored"] is False
    assert "expir" in (undo["error"] or "").lower()
    # The expired rewind is committed, not left pending: the steps
    # stay pruned and nothing blocks the session any more.
    assert rewind_service.has_staged(sid) is False
    assert _messages(sid) == []


def test_restart_prunes_an_expired_pending_rewind(store_env, monkeypatch):
    db = store_env / "memory.db"
    sid = _fast_chat_session(store_env)
    result = rewind_service.stage_rewind(sid, "task-1", 0, "")
    assert result["ok"] is True
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE rewind_pending SET expires_at = ? WHERE session_id = ?",
            (time.time() - 1.0, sid))
        conn.commit()

    _simulate_restart(monkeypatch)
    # First touch after the restart reconciles the wedged pending
    # row instead of reporting it as a live undo window forever.
    assert rewind_service.has_staged(sid) is False
    assert _raw(db, "SELECT COUNT(*) FROM rewind_backup_messages") == [(0,)]
    assert _raw(db, "SELECT COUNT(*) FROM rewind_backup_manifest") == [(0,)]
    assert _messages(sid) == []

    # And the session is not wedged: a later task can rewind.
    session_store.add_message(sid, "user", "again")
    session_store.add_message(sid, "assistant", "ok")
    rewind_manifest.record_point(sid, "task-2", 0, "", "Before this task began", [])
    rewind_manifest.record_point(sid, "task-2", 1, "", "Reply complete", [])
    assert rewind_service.stage_rewind(sid, "task-2", 0, "")["ok"] is True


# ---------------------------------------------------------------------------
# M11 — cross-store staging is recoverable
# ---------------------------------------------------------------------------

_CP_CHAIN = ["cp-0001", "cp-0002", "cp-0003"]


def _craft_checkpoints(db_path, thread_id):
    """A crafted checkpoint chain — the service treats checkpoint
    rows opaquely (same pattern as the websocket contract test)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS checkpoints (
                thread_id TEXT NOT NULL,
                checkpoint_ns TEXT NOT NULL DEFAULT '',
                checkpoint_id TEXT NOT NULL,
                parent_checkpoint_id TEXT,
                type TEXT,
                checkpoint BLOB,
                metadata BLOB,
                PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
            )""")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS writes (
                thread_id TEXT NOT NULL,
                checkpoint_ns TEXT NOT NULL DEFAULT '',
                checkpoint_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                task_path TEXT NOT NULL DEFAULT '',
                idx INTEGER NOT NULL,
                channel TEXT NOT NULL,
                type TEXT,
                value BLOB,
                PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
            )""")
        parent = None
        for cp in _CP_CHAIN:
            conn.execute(
                "INSERT INTO checkpoints VALUES (?, '', ?, ?, 'msgpack', ?, ?)",
                (thread_id, cp, parent, b"cp", b"md"))
            conn.execute(
                "INSERT INTO writes VALUES (?, '', ?, 'task-x', '', 0, 'messages', 'msgpack', ?)",
                (thread_id, cp, b"w"))
            parent = cp
        conn.commit()


def _stored_checkpoint_ids(db_path, thread_id):
    if not db_path.exists():
        return set()
    with sqlite3.connect(db_path) as conn:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        if "checkpoints" not in tables:
            return set()
        return {row[0] for row in conn.execute(
            "SELECT checkpoint_id FROM checkpoints WHERE thread_id = ?",
            (thread_id,))}


def _graph_session(cp_env):
    sid = session_store.create_session("Graph")
    session_store.add_message(sid, "user", "please write a file")
    session_store.add_message(sid, "assistant", "Wrote the file.")
    _craft_checkpoints(cp_env / "checkpoints.db", sid)
    rewind_manifest.record_point(sid, "task-g", 0, "", "Before this task began", [])
    rewind_manifest.record_point(sid, "task-g", 1, "cp-0002", "After planner", [])
    rewind_manifest.record_point(sid, "task-g", 2, "cp-0003", "Task complete", [])
    return sid


def test_crash_between_stores_is_reconciled_on_restart(cp_env, monkeypatch):
    sid = _graph_session(cp_env)
    cp_db = cp_env / "checkpoints.db"

    def _crash(*args, **kwargs):
        raise RuntimeError("simulated crash during checkpoint staging")

    original = rewind_service._stage_checkpoint_side
    monkeypatch.setattr(rewind_service, "_stage_checkpoint_side", _crash)
    try:
        with pytest.raises(RuntimeError):
            rewind_service.stage_rewind(sid, "task-g", 1, "cp-0002")
    finally:
        monkeypatch.setattr(rewind_service, "_stage_checkpoint_side", original)

    # The split state the review reproduced: the manifest has
    # rolled back to step 1 while the checkpoint head is still the
    # final checkpoint.
    assert [e["step_index"] for e in rewind_manifest.get_entries(sid, "task-g")] == [0, 1]
    assert _stored_checkpoint_ids(cp_db, sid) == set(_CP_CHAIN)

    _simulate_restart(monkeypatch)
    # Reconciliation completes the interrupted staging from the
    # persisted plan: the stores converge on the rewound state.
    assert rewind_service.has_staged(sid) is True
    assert _stored_checkpoint_ids(cp_db, sid) == {"cp-0001", "cp-0002"}

    # The reconciled rewind is fully undoable.
    undo = rewind_service.undo_staged(sid)
    assert undo == {"ok": True, "restored": True, "error": None}
    assert _stored_checkpoint_ids(cp_db, sid) == set(_CP_CHAIN)
    assert [(m["role"], m["content"]) for m in _messages(sid)] == [
        ("user", "please write a file"), ("assistant", "Wrote the file.")]
    assert [e["step_index"] for e in rewind_manifest.get_entries(sid, "task-g")] == [0, 1, 2]


def test_aborted_staging_leaves_no_pending_rewind(store_env, monkeypatch):
    """Protocol invariant: a failure before the memory side commits
    (here, inside the memory staging step) leaves the session
    exactly as it was — no pending row, no lost rows — once the
    next process reconciles."""
    sid = _fast_chat_session(store_env)

    def _crash(*args, **kwargs):
        raise RuntimeError("simulated crash during memory staging")

    original = rewind_service._stage_memory_side
    monkeypatch.setattr(rewind_service, "_stage_memory_side", _crash)
    try:
        with pytest.raises(RuntimeError):
            rewind_service.stage_rewind(sid, "task-1", 0, "")
    finally:
        monkeypatch.setattr(rewind_service, "_stage_memory_side", original)

    _simulate_restart(monkeypatch)
    assert rewind_service.has_staged(sid) is False
    assert [(m["role"], m["content"]) for m in _messages(sid)] == [
        ("user", "look at this"), ("assistant", "I see it")]
    undo = rewind_service.undo_staged(sid)
    assert undo["ok"] is False and undo["restored"] is False


# ---------------------------------------------------------------------------
# Backup-schema migration: pre-fix backups still restore
# ---------------------------------------------------------------------------

_OLD_PENDING_DDL = """
    CREATE TABLE rewind_pending (
        session_id TEXT PRIMARY KEY,
        rewind_id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        step_index INTEGER NOT NULL,
        checkpoint_id TEXT NOT NULL,
        expires_at REAL NOT NULL
    )"""

_OLD_BACKUP_MESSAGES_DDL = """
    CREATE TABLE rewind_backup_messages (
        rewind_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        orig_rowid INTEGER NOT NULL,
        id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        thoughts TEXT,
        timestamp INTEGER NOT NULL
    )"""

_OLD_BACKUP_MANIFEST_DDL = """
    CREATE TABLE rewind_backup_manifest (
        rewind_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        orig_rowid INTEGER NOT NULL,
        id TEXT NOT NULL,
        task_id TEXT NOT NULL,
        step_index INTEGER NOT NULL,
        checkpoint_id TEXT NOT NULL,
        label TEXT NOT NULL,
        file_writes_json TEXT NOT NULL DEFAULT '[]',
        timestamp INTEGER NOT NULL
    )"""


def test_legacy_backup_restores_after_schema_migration(store_env):
    """A backup written by the pre-fix code (no attachments column,
    no staging phase/plan columns) must still undo cleanly after
    the schema migration — attachments were never recorded in it,
    so they restore as absent rather than crashing the restore."""
    db = store_env / "memory.db"
    sid = session_store.create_session("Legacy")
    session_store.add_message(sid, "user", "look at this", attachments=SNIP)
    session_store.add_message(sid, "assistant", "I see it")
    rewind_manifest.record_point(sid, "task-1", 0, "", "Before this task began", [])
    rewind_manifest.record_point(sid, "task-1", 1, "", "Reply complete", [])

    # Recreate the staged state exactly as the old code left it.
    with sqlite3.connect(db) as conn:
        conn.execute(_OLD_PENDING_DDL)
        conn.execute(_OLD_BACKUP_MESSAGES_DDL)
        conn.execute(_OLD_BACKUP_MANIFEST_DDL)
        conn.execute(
            "INSERT INTO rewind_pending VALUES (?, 'legacy-rw', 'task-1', 0, '', ?)",
            (sid, time.time() + 60.0))
        conn.execute(
            "INSERT INTO rewind_backup_messages "
            "(rewind_id, session_id, orig_rowid, id, role, content, thoughts, timestamp) "
            "SELECT 'legacy-rw', session_id, rowid, id, role, content, thoughts, timestamp "
            "FROM session_messages WHERE session_id = ?", (sid,))
        conn.execute("DELETE FROM session_messages WHERE session_id = ?", (sid,))
        conn.execute(
            "INSERT INTO rewind_backup_manifest "
            "(rewind_id, session_id, orig_rowid, id, task_id, step_index, "
            "checkpoint_id, label, file_writes_json, timestamp) "
            "SELECT 'legacy-rw', session_id, rowid, id, task_id, step_index, "
            "checkpoint_id, label, file_writes_json, timestamp "
            "FROM rewind_manifest WHERE session_id = ? AND step_index > 0", (sid,))
        conn.execute(
            "DELETE FROM rewind_manifest WHERE session_id = ? AND step_index > 0",
            (sid,))
        conn.commit()
    assert _messages(sid) == []

    undo = rewind_service.undo_staged(sid)
    assert undo == {"ok": True, "restored": True, "error": None}
    restored = _messages(sid)
    assert [(m["role"], m["content"]) for m in restored] == [
        ("user", "look at this"), ("assistant", "I see it")]
    assert restored[0]["attachments"] == []
    assert [e["step_index"] for e in rewind_manifest.get_entries(sid, "task-1")] == [0, 1]
