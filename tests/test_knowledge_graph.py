import pytest
import os
import json
from unittest.mock import patch, MagicMock
from aether_engine.memory.knowledge_graph import extract_and_store_facts, get_entity_facts, get_all_facts

class MockStreamChunk:
    def __init__(self, text):
        self.text = text

class MockProvider:
    async def call_stream(self, messages):
        # Yield a mock JSON array response
        yield MockStreamChunk('[')
        yield MockStreamChunk('{"entity_name": "Test User", "entity_type": "Person", "fact": "Likes Python", "confidence": 0.9}')
        yield MockStreamChunk(']')

@pytest.fixture(autouse=True)
def setup_test_db(tmp_path):
    test_db = tmp_path / "test_knowledge_graph.db"
    with patch("aether_engine.memory.knowledge_graph.DB_PATH", test_db):
        with patch("aether_engine.memory.knowledge_graph._audit_logger.log_event") as mock_log:
            yield test_db, mock_log

@pytest.mark.asyncio
async def test_knowledge_graph_extraction(setup_test_db):
    test_db, mock_log = setup_test_db
    provider = MockProvider()
    
    await extract_and_store_facts(
        provider=provider,
        user_prompt="I really like coding in Python.",
        assistant_response="That's great! Python is very versatile.",
        task_id="task-456"
    )
    
    # Check if facts were extracted
    facts = get_all_facts(limit=10)
    assert len(facts) == 1
    assert facts[0]["entity_name"] == "Test User"
    assert facts[0]["fact_text"] == "Likes Python"
    
    # Check entity specific facts
    entity_facts = get_entity_facts("Test User")
    assert len(entity_facts) == 1
    assert entity_facts[0]["fact_text"] == "Likes Python"
    
    # Check audit log
    mock_log.assert_called_with("KNOWLEDGE_FACT_STORED", {
        "entity_name": "Test User",
        "fact": "Likes Python",
        "task_id": "task-456"
    })
