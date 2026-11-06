"""E4-03: a failed checkpoint copy must not orphan a half-made fork.

``fork_session_at``'s contract (module docstring): "a refused fork
creates nothing… they never raise." But the fork session (and its
copied messages) is created FIRST, and only then are the source's
checkpoint rows copied under the fork's thread id. The copy does
plain INSERT … SELECT with no guard: a constraint conflict, a locked
checkpoints.db, or a writes/checkpoints shape drift raised out of
``fork_session_at`` *after* the fork was committed, leaving an
orphan fork with no checkpoint thread and an exception instead of
the SESSION_FORK_RESPONSE refusal.

The fix compensates, the same pattern the session importer (M15)
already uses for placement failure: on copy failure the partial fork
is deleted (its messages go with it via ON DELETE CASCADE) along
with any checkpoint rows copied under its thread id, and the caller
gets the refusal payload.

The failure here is genuine, not mocked: the crafted checkpoints.db
has a drifted ``checkpoints`` table (no ``metadata`` column), which
the copy's INSERT … SELECT names — the planning reads (ns/id/parent
only) succeed, the copy raises ``sqlite3.OperationalError``.
"""
import sqlite3

import pytest

from aether_engine.memory import session_store
from aether_engine.rewind import fork as fork_service


@pytest.fixture
def store_env(tmp_path, monkeypatch):
    """Session store isolated to a tmp memory.db (the fork suite's
    recipe from test_session_fork.py)."""
    monkeypatch.setattr(session_store, "DB_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(session_store, "_initialized", False)
    return tmp_path


_CHAIN = ["cp-0001", "cp-0002"]


def _craft_checkpoints(db_path, thread_id, *, drifted: bool):
    """A checkpoints.db in the shape test_session_fork.py crafts —
    or, with ``drifted``, the same table missing the ``metadata``
    column the copy selects."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_col = "" if drifted else ", metadata BLOB"
    with sqlite3.connect(db_path) as conn:
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS checkpoints (
                thread_id TEXT NOT NULL,
                checkpoint_ns TEXT NOT NULL DEFAULT '',
                checkpoint_id TEXT NOT NULL,
                parent_checkpoint_id TEXT,
                type TEXT,
                checkpoint BLOB
                {metadata_col},
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
        for cp in _CHAIN:
            if drifted:
                conn.execute(
                    "INSERT INTO checkpoints VALUES (?, '', ?, ?, 'msgpack', ?)",
                    (thread_id, cp, parent, b"cp"))
            else:
                conn.execute(
                    "INSERT INTO checkpoints VALUES (?, '', ?, ?, 'msgpack', ?, ?)",
                    (thread_id, cp, parent, b"cp", b"md"))
            conn.execute(
                "INSERT INTO writes VALUES (?, '', ?, 'task-x', '', 0, 'messages', 'msgpack', ?)",
                (thread_id, cp, b"w"))
            parent = cp
        conn.commit()


def _checkpoint_threads(db_path):
    with sqlite3.connect(db_path) as conn:
        return {row[0] for row in conn.execute(
            "SELECT DISTINCT thread_id FROM checkpoints")}


def _make_source(store_env):
    sid = session_store.create_session("Fork source")
    session_store.add_message(sid, "user", "hello")
    session_store.add_message(sid, "assistant", "hi there")
    return sid


def test_fork_copy_failure_removes_the_partial_fork(store_env, tmp_path):
    sid = _make_source(store_env)
    cp_db = tmp_path / "checkpoints.db"
    _craft_checkpoints(cp_db, sid, drifted=True)

    result = fork_service.fork_session_at(sid, checkpoint_db=cp_db)

    # The refusal contract: ok False with an error, never a raise.
    assert result["ok"] is False
    assert result["error"]
    assert result["new_session_id"] == ""
    # Compensation: no orphan fork session (and no copied messages)
    # survives — the store holds exactly the source session.
    sessions = session_store.list_sessions()
    assert [s["id"] for s in sessions] == [sid]
    assert len(session_store.get_session(sid)["messages"]) == 2
    # And no checkpoint rows were left under any other thread id.
    assert _checkpoint_threads(cp_db) == {sid}


def test_fork_with_healthy_checkpoints_still_copies(store_env, tmp_path):
    """Control: the happy path is unchanged — a whole-session fork
    copies the checkpoint thread under the fork's own id."""
    sid = _make_source(store_env)
    cp_db = tmp_path / "checkpoints.db"
    _craft_checkpoints(cp_db, sid, drifted=False)

    result = fork_service.fork_session_at(sid, checkpoint_db=cp_db)

    assert result["ok"] is True
    new_id = result["new_session_id"]
    assert new_id and new_id != sid
    assert len(session_store.list_sessions()) == 2
    assert _checkpoint_threads(cp_db) == {sid, new_id}
    with sqlite3.connect(cp_db) as conn:
        copied = conn.execute(
            "SELECT COUNT(*) FROM checkpoints WHERE thread_id = ?",
            (new_id,)).fetchone()[0]
    assert copied == len(_CHAIN)
