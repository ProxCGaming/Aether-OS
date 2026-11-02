"""Per-workspace sessions, slice 1: session-store schema + API.

Sessions gain a nullable ``workspace`` binding (the canonicalised
workspace root path, or NULL = unbound / "No workspace"). These tests
pin the store-level contract only: schema version 3, create/list/move
semantics, fork inheritance, and the upgrade of a version-2 database
with rows intact and NULL bindings (no backfill).
"""

import sqlite3
from unittest.mock import patch

import pytest

from aether_engine.memory import session_store
from aether_engine.memory.session_store import (
    ALL_WORKSPACES,
    create_session,
    fork_session,
    get_session,
    list_sessions,
    search_sessions,
    set_session_workspace,
)

ALPHA = "/projects/alpha"
BETA = "/projects/beta"


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    test_db = tmp_path / "test_memory.db"
    with patch("aether_engine.memory.session_store.DB_PATH", test_db):
        yield test_db


def _by_id(sessions):
    return {s["id"]: s for s in sessions}


def test_schema_records_version_3_and_workspace_column(setup_test_db):
    create_session("Fresh")
    with sqlite3.connect(setup_test_db) as conn:
        columns = {row[1]: row[2] for row in conn.execute("PRAGMA table_info(sessions)")}
        assert columns.get("workspace") == "TEXT"
        row = conn.execute(
            "SELECT version FROM aether_schema_meta WHERE store = 'sessions'"
        ).fetchone()
        assert row[0] == 3
        assert session_store.SCHEMA_VERSION == 3
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3


def test_create_session_stores_binding_and_defaults_unbound():
    bound = create_session("Bound", workspace=ALPHA)
    unbound = create_session("Unbound")
    sessions = _by_id(list_sessions())
    assert sessions[bound]["workspace"] == ALPHA
    assert sessions[unbound]["workspace"] is None
    assert get_session(bound)["workspace"] == ALPHA
    assert get_session(unbound)["workspace"] is None


def test_list_sessions_filter_has_three_unambiguous_states():
    alpha = create_session("Alpha", workspace=ALPHA)
    beta = create_session("Beta", workspace=BETA)
    unbound = create_session("Unbound")

    # No argument (and the explicit sentinel) means: no filter.
    assert {s["id"] for s in list_sessions()} == {alpha, beta, unbound}
    assert {s["id"] for s in list_sessions(workspace=ALL_WORKSPACES)} == {alpha, beta, unbound}
    # A path value means: only that workspace.
    assert {s["id"] for s in list_sessions(workspace=ALPHA)} == {alpha}
    assert {s["id"] for s in list_sessions(workspace=BETA)} == {beta}
    # Explicit None means: unbound only (NULL bindings).
    assert {s["id"] for s in list_sessions(workspace=None)} == {unbound}


def test_set_session_workspace_moves_both_ways(setup_test_db):
    session_id = create_session("Mover")
    # Age the row: a move is filing, not activity, so it must not
    # refresh updated_at (pruning judges age on updated_at).
    with sqlite3.connect(setup_test_db) as conn:
        conn.execute("UPDATE sessions SET updated_at = 12345 WHERE id = ?", (session_id,))
        conn.commit()

    assert set_session_workspace(session_id, ALPHA) is True
    moved = get_session(session_id)
    assert moved["workspace"] == ALPHA
    assert moved["updated_at"] == 12345
    assert {s["id"] for s in list_sessions(workspace=ALPHA)} == {session_id}
    assert list_sessions(workspace=None) == []

    assert set_session_workspace(session_id, None) is True
    assert get_session(session_id)["workspace"] is None
    assert {s["id"] for s in list_sessions(workspace=None)} == {session_id}

    assert set_session_workspace("no-such-session", ALPHA) is False


def test_fork_session_inherits_binding():
    source = create_session("Source", workspace=ALPHA)
    session_store.add_message(source, "user", "hello")
    result = fork_session(source, None, "Fork")
    assert result is not None
    forked = get_session(result["new_session_id"])
    assert forked["workspace"] == ALPHA
    assert result["workspace"] == ALPHA

    unbound = create_session("Plain")
    result = fork_session(unbound, None, "Plain fork")
    assert get_session(result["new_session_id"])["workspace"] is None


def test_search_sessions_carry_the_workspace_binding():
    bound = create_session("Needle bound", workspace=BETA)
    create_session("Needle unbound")
    results = _by_id(search_sessions("needle"))
    assert results[bound]["workspace"] == BETA
    assert all("workspace" in s for s in results.values())


def _create_v2_database(path):
    """A schema-version-2 memory.db, exactly as the old build left it."""
    with sqlite3.connect(path) as conn:
        conn.execute("""
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE session_messages (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                thoughts TEXT,
                attachments TEXT,
                timestamp INTEGER NOT NULL,
                FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
            )
        """)
        conn.execute(
            "INSERT INTO sessions (id, title, created_at, updated_at) VALUES ('legacy-1', 'Legacy', 100, 200)"
        )
        conn.execute(
            "INSERT INTO session_messages (id, session_id, role, content, timestamp) "
            "VALUES ('legacy-msg-1', 'legacy-1', 'user', 'old words', 150)"
        )
        conn.execute("""
            CREATE TABLE aether_schema_meta (
                store TEXT PRIMARY KEY,
                version INTEGER NOT NULL
            )
        """)
        conn.execute("INSERT INTO aether_schema_meta (store, version) VALUES ('sessions', 2)")
        conn.execute("PRAGMA user_version = 2")
        conn.commit()


def test_v2_database_upgrades_with_rows_intact_and_null_bindings(setup_test_db):
    _create_v2_database(setup_test_db)

    sessions = list_sessions()
    assert len(sessions) == 1
    assert sessions[0]["id"] == "legacy-1"
    assert sessions[0]["title"] == "Legacy"
    assert sessions[0]["workspace"] is None

    legacy = get_session("legacy-1")
    assert [m["content"] for m in legacy["messages"]] == ["old words"]
    assert {s["id"] for s in list_sessions(workspace=None)} == {"legacy-1"}

    with sqlite3.connect(setup_test_db) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
        assert "workspace" in columns
        row = conn.execute(
            "SELECT version FROM aether_schema_meta WHERE store = 'sessions'"
        ).fetchone()
        assert row[0] == 3
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
