"""Tests for dynamic API-based model discovery and registry updates."""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import httpx

from aether_engine.providers.discovery import (
    fetch_available_models,
    _discover_gemini_models,
    _discover_openai_models,
    _discover_anthropic_models,
    _discover_deepseek_models,
    _discover_openrouter_models,
)
from aether_engine.routing.registry import ModelRegistry


@pytest.mark.asyncio
async def test_fetch_gemini_models_success():
    fake_response = {
        "models": [
            {
                "name": "models/gemini-2.0-flash",
                "displayName": "Gemini 2.0 Flash",
                "supportedGenerationMethods": ["generateContent"],
            },
            {
                "name": "models/gemini-flash-latest",
                "displayName": "Gemini Flash Latest",
                "supportedGenerationMethods": ["generateContent"],
            },
            {
                "name": "models/text-embedding-004",
                "displayName": "Embedding 004",
                "supportedGenerationMethods": ["embedContent"],
            },
        ]
    }
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = fake_response
        mock_get.return_value = mock_resp

        models = await _discover_gemini_models("fake-key")
        assert "gemini-2.0-flash" in models
        # Alias blocklist should filter out latest tags
        assert "gemini-flash-latest" not in models
        # Embedding model should be filtered out
        assert "text-embedding-004" not in models


@pytest.mark.asyncio
async def test_fetch_openai_models_success():
    fake_response = {
        "data": [
            {"id": "gpt-4o"},
            {"id": "gpt-4o-mini"},
            {"id": "text-embedding-ada-002"},
            {"id": "dall-e-3"},
        ]
    }
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = fake_response
        mock_get.return_value = mock_resp

        models = await _discover_openai_models("fake-key")
        assert "gpt-4o" in models
        assert "gpt-4o-mini" in models
        assert "dall-e-3" not in models
        assert "text-embedding-ada-002" not in models


@pytest.mark.asyncio
async def test_fetch_anthropic_models_success():
    fake_response = {
        "data": [
            {"id": "claude-3-5-sonnet-20241022"},
            {"id": "claude-3-5-haiku-20241022"},
        ]
    }
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = fake_response
        mock_get.return_value = mock_resp

        models = await _discover_anthropic_models("fake-key")
        assert "claude-3-5-sonnet-20241022" in models
        assert "claude-3-5-haiku-20241022" in models


@pytest.mark.asyncio
async def test_fetch_available_models_error_handling():
    with patch("httpx.AsyncClient.get", side_effect=httpx.ConnectError("Network down")):
        models = await fetch_available_models("google_gemini", "fake-key")
        # Should gracefully return empty list on network failure
        assert models == []


@pytest.mark.asyncio
async def test_registry_update_provider_models():
    registry = ModelRegistry()
    registry.update_provider_models("google_gemini", ["gemini-test-1", "gemini-test-2"])
    
    models = registry.get_models_for_provider("google_gemini")
    model_ids = [m.id for m in models]
    assert "gemini-test-1" in model_ids
    assert "gemini-test-2" in model_ids
    
    # Check that entry was registered with valid priority and capabilities
    entry = registry.get_model("gemini-test-1")
    assert entry is not None
    assert entry.priority >= 1
    assert len(entry.capabilities) > 0


def test_resolve_litellm_model_openrouter_and_providers():
    from aether_engine.providers.litellm_provider import resolve_litellm_model

    # OpenRouter models with author/model format
    assert resolve_litellm_model("moonshotai/kimi-k3", "openrouter") == "openrouter/moonshotai/kimi-k3"
    assert resolve_litellm_model("meta-llama/llama-3.3-70b-instruct", "openrouter") == "openrouter/meta-llama/llama-3.3-70b-instruct"
    assert resolve_litellm_model("openrouter/moonshotai/kimi-k3", "openrouter") == "openrouter/moonshotai/kimi-k3"
    # Unknown provider with slash should default to openrouter
    assert resolve_litellm_model("moonshotai/kimi-k3") == "openrouter/moonshotai/kimi-k3"

    # Gemini
    assert resolve_litellm_model("gemini-2.0-flash", "google_gemini") == "gemini/gemini-2.0-flash"
    assert resolve_litellm_model("models/gemini-2.0-flash", "google_gemini") == "gemini/gemini-2.0-flash"
    assert resolve_litellm_model("gemini/gemini-2.0-flash", "google_gemini") == "gemini/gemini-2.0-flash"

    # DeepSeek
    assert resolve_litellm_model("deepseek-chat", "deepseek") == "deepseek/deepseek-chat"

    # Anthropic
    assert resolve_litellm_model("claude-3-5-sonnet", "anthropic") == "anthropic/claude-3-5-sonnet-latest"

    # Custom OpenAI compatible
    assert resolve_litellm_model("llama-3.3-70b-versatile", "custom_openai") == "openai/llama-3.3-70b-versatile"
    assert resolve_litellm_model("my-custom-model", "custom") == "openai/my-custom-model"


@pytest.mark.asyncio
async def test_fetch_custom_openai_models_success():
    fake_response = {
        "data": [
            {"id": "meta-llama/llama-3.3-70b-instruct"},
            {"id": "qwen/qwen-2.5-coder-32b-instruct"},
            {"id": "text-embedding-3-small"},
        ]
    }
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = fake_response
        mock_get.return_value = mock_resp

        models = await fetch_available_models(
            "custom_openai",
            "fake-key",
            base_url="https://api.groq.com/openai/v1",
        )
        assert "meta-llama/llama-3.3-70b-instruct" in models
        assert "qwen/qwen-2.5-coder-32b-instruct" in models
        assert "text-embedding-3-small" not in models


