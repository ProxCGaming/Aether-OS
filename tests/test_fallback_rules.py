"""Tests for error classification, automatic fallback rules, and tool loop safety."""
import pytest
from unittest.mock import AsyncMock, MagicMock

from aether_common.contracts import EventType
from aether_engine.orchestration.simple import run_task
from aether_engine.providers.base import (
    AuthenticationError,
    BaseProvider,
    NetworkError,
    RateLimitError,
    StreamChunk,
    TimeoutError,
    ToolCall,
)
from aether_engine.routing.fallback import (
    classify_provider_error,
    get_fallback_candidates,
    is_retriable_error,
)
from aether_engine.routing.health import ProviderHealthManager
from aether_engine.routing.registry import ModelEntry, ModelRegistry
from aether_engine.tools.registry import Tool, ToolRegistry


class TestFallbackRules:
    def test_classify_retriable_errors(self):
        assert is_retriable_error(TimeoutError("Request timed out"))
        assert is_retriable_error(RateLimitError("Rate limit 429"))
        assert is_retriable_error(NetworkError("Connection reset by peer"))
        assert is_retriable_error(Exception("503 Service Unavailable"))

    def test_classify_non_retriable_errors(self):
        assert not is_retriable_error(AuthenticationError("401 Unauthorized"))
        assert not is_retriable_error(Exception("API_KEY_INVALID"))
        assert not is_retriable_error(Exception("403 Forbidden"))

    def test_get_fallback_candidates(self):
        health_mgr = ProviderHealthManager()
        models = [
            ModelEntry(provider="google_gemini", id="gemini-2.5-flash", label="Gemini", priority=1),
            ModelEntry(provider="openai", id="gpt-4o-mini", label="OpenAI Mini", priority=1),
            ModelEntry(provider="anthropic", id="claude-3-5-haiku", label="Claude Haiku", priority=1),
        ]
        registry = ModelRegistry(models)

        # Mark anthropic offline
        health_mgr.record_failure("anthropic", "Auth failed", is_retriable=False)

        candidates = get_fallback_candidates(
            failed_provider="google_gemini",
            configured_providers=["google_gemini", "openai", "anthropic"],
            health_manager=health_mgr,
            registry=registry,
        )

        assert len(candidates) == 1
        assert candidates[0].provider == "openai"
        assert candidates[0].id == "gpt-4o-mini"

    @pytest.mark.asyncio
    async def test_mid_tool_loop_failure_is_strictly_non_retriable(self):
        """Verify ADR 0005 trade-off: never silently switch providers mid-turn."""
        mock_provider = MagicMock(spec=BaseProvider)

        # Turn 0: emit tool call
        async def mock_stream(messages, tool_defs):
            if len(messages) == 1:
                # Turn 0
                yield StreamChunk(tool_calls=[ToolCall(name="get_time", args={})])
            else:
                # Turn 1 (mid tool-loop) -> simulate network/provider error
                raise NetworkError("Connection dropped during second turn")

        mock_provider.call_stream = mock_stream

        tools = ToolRegistry()
        tools.register(Tool(
            name="get_time",
            description="Get time",
            input_schema={"type": "object", "properties": {}},
            execute_fn=lambda **kw: "12:00 PM",
        ))

        events = []
        async for ev in run_task(prompt="What time is it?", provider=mock_provider, tools=tools):
            events.append(ev)

        failed_events = [e for e in events if e.type == EventType.TASK_FAILED]
        assert len(failed_events) == 1
        failed_payload = failed_events[0].payload
        assert failed_payload["retriable"] is False
        assert failed_payload["tool_loop"] is True
        assert "Tool loop failed" in failed_payload["error"]
