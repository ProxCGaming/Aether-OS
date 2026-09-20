import pytest
import os
from unittest.mock import patch
from aether_engine.memory.episodic import store_episode, query_episodes

@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    test_db = tmp_path / "test_episodic.db"
    with patch("aether_engine.memory.episodic.DB_PATH", test_db):
        # Disable AuditLogger for testing by mocking it
        with patch("aether_engine.memory.episodic._audit_logger.log_event") as mock_log:
            yield test_db, mock_log

def test_episodic_memory_store_and_query(setup_test_db):
    test_db, mock_log = setup_test_db
    
    task_id = "task-123"
    prompt = "Create a python script"
    summary = "Generated a simple python script"
    tags = ["python", "script"]
    outcome = "SUCCESS"
    
    store_episode(task_id, prompt, summary, tags, outcome)
    
    # Ensure audit log was called
    mock_log.assert_called_with("EPISODIC_MEMORY_STORED", {"task_id": task_id, "tags": tags})
    
    # Query with exact word
    results = query_episodes("python")
    assert len(results) == 1
    assert results[0]["task_id"] == task_id
    assert results[0]["user_prompt"] == prompt
    
    # Ensure audit log for retrieval
    mock_log.assert_called_with("EPISODIC_MEMORY_RETRIEVED", {"query": "python", "results_count": 1})
    
    # Query with no matches
    results_empty = query_episodes("java")
    assert len(results_empty) == 0
