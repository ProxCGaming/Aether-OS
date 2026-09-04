"""Tests for error classification, automatic fallback rules, and tool loop safety.

Phase 5.1 Fix 6: Rewritten to test the *live* fallback paths:
  - classify_provider_error / is_retriable_error  (from routing.fallback — used by live app.py)
  - get_fallback_candidates                        (from routing.fallback — used by live app.py)
  - Per-node fallback via LiteLLMProvider          (Fix 4)

Previously this file imported run_task from the dead orchestration.simple module.
That import and the test depending on it have been replaced with tests that exercise
the live code paths.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

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

    def test_classify_error_message_extraction(self):
        """Verify classify_provider_error returns user-friendly messages."""
        _, msg = classify_provider_error(TimeoutError("deadline exceeded"))
        assert "timed out" in msg.lower()

        _, msg = classify_provider_error(AuthenticationError("bad key"))
        assert "authentication" in msg.lower()

    def test_fallback_candidates_excludes_failed_and_unhealthy(self):
        """Confirm failed provider and unhealthy providers are both excluded."""
        health_mgr = ProviderHealthManager()
        models = [
            ModelEntry(provider="a", id="model-a", label="A", priority=1),
            ModelEntry(provider="b", id="model-b", label="B", priority=1),
            ModelEntry(provider="c", id="model-c", label="C", priority=1),
        ]
        registry = ModelRegistry(models)

        # 'b' is down
        health_mgr.record_failure("b", "down", is_retriable=False)

        candidates = get_fallback_candidates(
            failed_provider="a",
            configured_providers=["a", "b", "c"],
            health_manager=health_mgr,
            registry=registry,
        )
        provider_names = [c.provider for c in candidates]
        assert "a" not in provider_names  # failed
        assert "b" not in provider_names  # unhealthy
        assert "c" in provider_names


class TestPerNodeFallback:
    """Tests for the per-node fallback mechanism in LiteLLMProvider (Fix 4)."""

    @pytest.mark.asyncio
    async def test_litellm_provider_stores_fallback_models(self):
        """Verify that fallback_models are stored on the provider instance."""
        from aether_engine.providers.litellm_provider import LiteLLMProvider
        provider = LiteLLMProvider(
            api_key="test-key",
            model="gemini-2.5-flash",
            provider_name="google_gemini",
            fallback_models=["openai/gpt-4o-mini", "anthropic/claude-3-5-haiku-latest"],
        )
        assert len(provider.fallback_models) == 2
        assert provider.fallback_models[0] == "openai/gpt-4o-mini"

    @pytest.mark.asyncio
    async def test_litellm_provider_no_fallback_by_default(self):
        """When no fallback_models are provided, the list is empty."""
        from aether_engine.providers.litellm_provider import LiteLLMProvider
        provider = LiteLLMProvider(
            api_key="test-key",
            model="gpt-4o-mini",
            provider_name="openai",
        )
        assert provider.fallback_models == []
