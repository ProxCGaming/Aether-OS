import pytest
import sqlite3
import time
from pathlib import Path
from langchain_core.runnables import RunnableConfig

from aether_engine.langgraph.nodes.supervisor import supervisor_node
from aether_engine.memory.episodic import init_episodic_db, store_episode
from aether_engine.providers.litellm_provider import LiteLLMProvider

class InspectProvider(LiteLLMProvider):
    def __init__(self):
        super().__init__(api_key="test", model="mock", provider_name="mock")
        self.received_messages = []
        
    async def call_stream(self, messages, **kwargs):
        self.received_messages.extend(messages)
        class MockChunk:
            def __init__(self, text, finish_reason=None):
                self.text = text
                self.tool_calls = []
                self.finish_reason = finish_reason
        yield MockChunk('{"next": "planner", "reason": "test"}', finish_reason="stop")

@pytest.mark.asyncio
async def test_supervisor_uses_episodic_memory(monkeypatch, tmp_path):
    # Redirect DB path to temp
    monkeypatch.setattr("aether_engine.memory.episodic.DB_PATH", tmp_path / "memory.db")
    init_episodic_db()
    
    # Store a test episode
    store_episode("test_task_1", "make a pizza", "I made a pepperoni pizza", ["food"], "Success")
    
    provider = InspectProvider()
    config = RunnableConfig(configurable={"provider": provider})
    state = {
        "original_prompt": "make a pizza",
        "messages": [{"role": "user", "content": "make a pizza"}],
        "task_id": "test_task_2"
    }
    
    result = await supervisor_node(state, config)
    
    assert result["next"] == "planner"
    
    # Verify the system prompt included the episodic memory
    assert len(provider.received_messages) > 0
    system_msg = provider.received_messages[0]["content"]
    assert "Relevant Past Context" in system_msg
    assert "Past User Request: 'make a pizza'" in system_msg
    assert "Outcome: 'Success" in system_msg
