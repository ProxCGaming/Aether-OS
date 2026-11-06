from unittest.mock import patch

import pytest

from aether_engine.memory.episodic import query_episodes, store_episode


@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    test_db = tmp_path / "test_episodic.db"
    # Disable AuditLogger for testing by mocking it
    with (
        patch("aether_engine.memory.episodic.DB_PATH", test_db),
        patch("aether_engine.memory.episodic._audit_logger.log_event") as mock_log,
    ):
        yield test_db, mock_log

@pytest.mark.asyncio
async def test_episodic_memory_store_and_query(setup_test_db):
    _test_db, mock_log = setup_test_db
    
    task_id = "task-123"
    prompt = "Create a python script"
    summary = "Generated a simple python script"
    tags = ["python", "script"]
    outcome = "SUCCESS"
    
    await store_episode(task_id, prompt, summary, tags, outcome)
    
    # Ensure audit log was called. Superset match: the store payload
    # also carries "embedded" since embeddings landed; every original
    # field must still be present with its exact value.
    stored_event, stored_payload = mock_log.call_args.args
    assert stored_event == "EPISODIC_MEMORY_STORED"
    assert {"task_id": task_id, "tags": tags}.items() <= stored_payload.items()
    
    # Query with exact word
    results = await query_episodes("python")
    assert len(results) == 1
    assert results[0]["task_id"] == task_id
    assert results[0]["user_prompt"] == prompt
    
    # Ensure audit log for retrieval. Superset match: the retrieve
    # payload also carries "hybrid" / "similarity_threshold" since
    # embeddings landed; the original fields must match exactly.
    retrieved_event, retrieved_payload = mock_log.call_args.args
    assert retrieved_event == "EPISODIC_MEMORY_RETRIEVED"
    assert {"query": "python", "results_count": 1}.items() <= retrieved_payload.items()
    
    # Query with no matches
    results_empty = await query_episodes("java")
    assert len(results_empty) == 0
