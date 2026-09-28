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


