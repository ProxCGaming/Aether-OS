"""S1-M11: bounded history fetch + WAL/busy_timeout on memory stores.

Parity tests encode the EXACT current (pre-fix) output semantics of
``get_chat_history`` with hand-computed expectations — they passed
against the old full-scan implementation and must keep passing
against the bounded SQL implementation. The WAL tests fail pre-fix.
"""
import sqlite3
import uuid
from unittest.mock import patch

import pytest

from aether_engine.memory import knowledge_graph, session_store
from aether_engine.memory.session_store import (
    create_session,
    get_chat_history,
    get_session,
)
from aether_engine.rag.store import WorkspaceRagStore


@pytest.fixture()
def session_db(tmp_path):
    test_db = tmp_path / "test_memory.db"
    with patch("aether_engine.memory.session_store.DB_PATH", test_db):
        yield test_db


def _insert(db_path, session_id, role, content, timestamp):
    """Insert a row directly so timestamps (incl. ties) are controlled."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO session_messages (id, session_id, role, content, timestamp)"
            " VALUES (?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), session_id, role, content, timestamp),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Parity: limit + same-second (rowid) tiebreak
# ---------------------------------------------------------------------------

def test_history_limit_keeps_most_recent_with_rowid_tiebreak(session_db):
    sid = create_session("Tiebreak")
    # All eight share one timestamp: order is insertion (rowid) order.
    turns = [
        ("user", "q1"), ("assistant", "a1"),
        ("user", "q2"), ("assistant", "a2"),
        ("user", "q3"), ("assistant", "a3"),
        ("user", "q4"), ("assistant", "a4"),
    ]
    for role, content in turns:
        _insert(session_db, sid, role, content, 1000)
    assert get_chat_history(sid, limit=3) == [
        {"role": "assistant", "content": "a3"},
        {"role": "user", "content": "q4"},
        {"role": "assistant", "content": "a4"},
    ]


def test_history_orders_by_timestamp_then_rowid(session_db):
    sid = create_session("Ordering")
    _insert(session_db, sid, "user", "later-ts", 2000)
    _insert(session_db, sid, "user", "earlier-ts", 1000)
    _insert(session_db, sid, "assistant", "same-ts-second", 2000)
    assert get_chat_history(sid) == [
        {"role": "user", "content": "earlier-ts"},
        {"role": "user", "content": "later-ts"},
        {"role": "assistant", "content": "same-ts-second"},
    ]


# ---------------------------------------------------------------------------
# Parity: role / whitespace filtering + content stripping
# ---------------------------------------------------------------------------

def test_history_filters_system_tool_and_whitespace_and_strips(session_db):
    sid = create_session("Filtering")
    rows = [
        ("user", "  first question  "),
        ("system", "a loaded resource"),
        ("assistant", "first answer"),
        ("tool", "tool output"),
        ("user", "   "),
        ("assistant", "\t \n"),
        ("user", "second question"),
        ("assistant", "  second answer  "),
    ]
    for i, (role, content) in enumerate(rows):
        _insert(session_db, sid, role, content, 1000 + i)
    assert get_chat_history(sid) == [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "second question"},
        {"role": "assistant", "content": "second answer"},
    ]


# ---------------------------------------------------------------------------
# Parity: trailing current-prompt exclusion (incl. the limit+1 window)
# ---------------------------------------------------------------------------

def test_history_excludes_trailing_user_message_equal_to_prompt(session_db):
    sid = create_session("Current prompt")
    _insert(session_db, sid, "user", "first question", 1000)
    _insert(session_db, sid, "assistant", "first answer", 1001)
    _insert(session_db, sid, "user", "  second question  ", 1002)
    assert get_chat_history(sid, " second question ") == [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
    ]


def test_history_exclusion_still_fills_the_limit_window(session_db):
    # Filtered history before exclusion: u1 a1 u2 a2 prompt — after
    # popping the prompt, the most recent 3 are a1 u2 a2. Fetching
    # only `limit` rows would wrongly return just u2 a2.
    sid = create_session("Window")
    rows = [
        ("user", "u1"), ("assistant", "a1"),
        ("user", "u2"), ("assistant", "a2"),
        ("user", "current"),
    ]
    for i, (role, content) in enumerate(rows):
        _insert(session_db, sid, role, content, 1000 + i)
    assert get_chat_history(sid, "current", limit=3) == [
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
    ]


def test_history_non_trailing_duplicate_of_prompt_is_kept(session_db):
    sid = create_session("Non-trailing")
    _insert(session_db, sid, "user", "repeat", 1000)
    _insert(session_db, sid, "assistant", "answer", 1001)
    assert get_chat_history(sid, "repeat") == [
        {"role": "user", "content": "repeat"},
        {"role": "assistant", "content": "answer"},
    ]


# ---------------------------------------------------------------------------
# Parity: char-cap trimming (front-drop, at least one message kept)
# ---------------------------------------------------------------------------

def test_history_char_cap_drops_oldest_until_within_cap(session_db):
    sid = create_session("Char cap")
    for i in range(4):
        role = "user" if i % 2 == 0 else "assistant"
        _insert(session_db, sid, role, "x" * 10, 1000 + i)
    # Total 40 > 25: drop two oldest -> 20 chars remain.
    assert get_chat_history(sid, limit=20, max_chars=25) == [
        {"role": "user", "content": "x" * 10},
        {"role": "assistant", "content": "x" * 10},
    ]


def test_history_char_cap_never_drops_the_last_message(session_db):
    sid = create_session("Char cap floor")
    for i in range(3):
        role = "user" if i % 2 == 0 else "assistant"
        _insert(session_db, sid, role, "y" * 10, 1000 + i)
    assert get_chat_history(sid, limit=20, max_chars=5) == [
        {"role": "user", "content": "y" * 10},
    ]


# ---------------------------------------------------------------------------
# Large session: exactly the latest window; get_session keeps full fidelity
# ---------------------------------------------------------------------------

def test_history_large_session_returns_exactly_latest_window(session_db):
    sid = create_session("Large")
    for i in range(300):
        role = "user" if i % 2 == 0 else "assistant"
        _insert(session_db, sid, role, f"msg-{i:03d}", 1000 + i)
    history = get_chat_history(sid, limit=20)
    assert history == [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"msg-{i:03d}"}
        for i in range(280, 300)
    ]
    # get_session is NOT truncated: the UI load path needs everything.
    assert len(get_session(sid)["messages"]) == 300


def test_history_unknown_session_is_empty(session_db):
    assert get_chat_history("no-such-session") == []


# ---------------------------------------------------------------------------
# Part 2: WAL + busy_timeout on each store's own connect path
# ---------------------------------------------------------------------------

def _assert_wal_and_busy_timeout(conn):
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        conn.close()


def test_session_store_conn_uses_wal_and_busy_timeout(tmp_path):
    with patch("aether_engine.memory.session_store.DB_PATH", tmp_path / "m.db"):
        _assert_wal_and_busy_timeout(session_store._get_conn())


def test_knowledge_graph_conn_uses_wal_and_busy_timeout(tmp_path):
    with patch("aether_engine.memory.knowledge_graph.DB_PATH", tmp_path / "m.db"):
        _assert_wal_and_busy_timeout(knowledge_graph._get_conn())


def test_rag_store_conn_uses_wal_and_busy_timeout(tmp_path):
    store = WorkspaceRagStore(db_path=tmp_path / "rag.db")
    _assert_wal_and_busy_timeout(store._get_conn())
