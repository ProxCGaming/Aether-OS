import pytest
import os
from unittest.mock import patch, MagicMock
from pathlib import Path
from aether_engine.memory.session_store import create_session, get_session, list_sessions, delete_session, add_message, DB_PATH, init_db

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
