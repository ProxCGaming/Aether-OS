from unittest.mock import patch

import pytest

from aether_engine.memory.session_store import (
    add_message,
    create_session,
    delete_session,
    get_chat_history,
    get_session,
    list_sessions,
    truncate_session,
    truncate_session_by_content,
)


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    test_db = tmp_path / "test_memory.db"
    with patch("aether_engine.memory.session_store.DB_PATH", test_db):
        yield test_db

def test_session_lifecycle():
    # Create
    session_id = create_session("Test Chat")
    assert session_id is not None
    
    # List
    sessions = list_sessions()
    assert len(sessions) == 1
    assert sessions[0]["title"] == "Test Chat"
    
    # Add message
    add_message(session_id, "user", "Hello World")
    add_message(session_id, "assistant", "Hi there!")
    
    # Get
    session_data = get_session(session_id)
    assert session_data is not None
    assert len(session_data["messages"]) == 2
    assert session_data["messages"][0]["role"] == "user"
    assert session_data["messages"][0]["content"] == "Hello World"
    
    # Delete
    deleted = delete_session(session_id)
    assert deleted is True
    assert len(list_sessions()) == 0

def test_session_thoughts():
    session_id = create_session("Thoughts Chat")
    sample_thoughts = [
        {"type": "NODE_ACTIVITY", "payload": {"from_node": "supervisor", "to_node": "planner", "action": "delegation"}},
        {"type": "TOOL_ACTIVITY", "payload": {"tool_name": "get_current_time", "status": "completed", "result": "2026-09-24"}}
    ]
    add_message(session_id, "user", "What time is it?")
    add_message(session_id, "assistant", "It is 2026-09-24.", thoughts=sample_thoughts)
    
    session_data = get_session(session_id)
    assert session_data is not None
    assert len(session_data["messages"]) == 2
    assistant_msg = session_data["messages"][1]
    assert assistant_msg["role"] == "assistant"
    assert assistant_msg["content"] == "It is 2026-09-24."
    assert isinstance(assistant_msg["thoughts"], list)
    assert len(assistant_msg["thoughts"]) == 2
    assert assistant_msg["thoughts"][0]["type"] == "NODE_ACTIVITY"
    assert assistant_msg["thoughts"][1]["payload"]["tool_name"] == "get_current_time"


# ---------------------------------------------------------------------------
# Edit-and-resend hardening: shared message identity, session-scoped and
# occurrence-aware truncation, stable ordering, shared chat history.
# ---------------------------------------------------------------------------

def _seed_duplicate_session():
    session_id = create_session("Duplicates")
    add_message(session_id, "user", "same text")
    add_message(session_id, "assistant", "first answer")
    add_message(session_id, "user", "same text")
    add_message(session_id, "assistant", "second answer")
    return session_id


def _contents(session_id):
    return [m["content"] for m in get_session(session_id)["messages"]]


def test_add_message_honours_a_valid_client_id():
    session_id = create_session("Client ids")
    returned = add_message(session_id, "user", "hello", message_id="msg-client-123")
    assert returned == "msg-client-123"
    assert get_session(session_id)["messages"][0]["id"] == "msg-client-123"


def test_add_message_rejects_a_malformed_client_id():
    session_id = create_session("Bad ids")
    returned = add_message(session_id, "user", "hello", message_id="bad id with spaces!")
    assert returned != "bad id with spaces!"
    assert get_session(session_id)["messages"][0]["id"] == returned


def test_messages_come_back_in_insertion_order_within_the_same_second():
    session_id = create_session("Ordering")
    for text in ("one", "two", "three", "four"):
        add_message(session_id, "user", text)
    assert _contents(session_id) == ["one", "two", "three", "four"]


def test_truncate_by_id_is_scoped_to_its_own_session():
    session_a = _seed_duplicate_session()
    session_b = _seed_duplicate_session()
    foreign_id = get_session(session_b)["messages"][0]["id"]
    # Session A's truncate addressed with session B's message id must
    # not cut anything in A.
    assert truncate_session(session_a, foreign_id) is False
    assert len(_contents(session_a)) == 4


def test_truncate_by_content_occurrence_cuts_at_the_clicked_duplicate():
    first = _seed_duplicate_session()
    assert truncate_session_by_content(first, "user", "same text", occurrence=0) is True
    assert _contents(first) == []

    second = _seed_duplicate_session()
    assert truncate_session_by_content(second, "user", "same text", occurrence=1) is True
    assert _contents(second) == ["same text", "first answer"]


def test_truncate_by_content_occurrence_out_of_range_is_a_safe_noop():
    session_id = _seed_duplicate_session()
    assert truncate_session_by_content(session_id, "user", "same text", occurrence=5) is False
    assert len(_contents(session_id)) == 4


def test_truncate_by_content_without_occurrence_keeps_legacy_behaviour():
    session_id = _seed_duplicate_session()
    assert truncate_session_by_content(session_id, "user", "same text") is True
    assert _contents(session_id) == ["same text", "first answer"]


def test_get_chat_history_excludes_system_and_the_current_prompt():
    session_id = create_session("History")
    add_message(session_id, "user", "first question")
    add_message(session_id, "assistant", "first answer")
    add_message(session_id, "system", "a loaded resource")
    add_message(session_id, "user", "second question")
    history = get_chat_history(session_id, "second question")
    assert history == [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
    ]
