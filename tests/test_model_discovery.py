"""Tests for dynamic API-based model discovery and registry updates.

Discovery returns a DiscoveryResult: the filtered chat-capable list, the
excluded list (with kind tags), and an optional DiscoveryError carrying the
HTTP status code. There are no hardcoded fallback model lists — failures
surface the real error.
"""
import os
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import httpx

from aether_engine.providers.discovery import (
    DiscoveryError,
    DiscoveryResult,
    fetch_available_models,
    _discover_gemini_models,
    _discover_openai_models,
    _discover_anthropic_models,
    _discover_deepseek_models,
    _discover_openrouter_models,
)
from aether_engine.routing.registry import ModelRegistry


@pytest.fixture(autouse=True)
def _no_proxy_env(monkeypatch):
    """This VM has malformed proxy env vars that break httpx client
    construction; strip them so the mocked HTTP layer is exercised."""
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy",
                "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(var, raising=False)


def _mock_get(fake_response, status_code=200):
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.json.return_value = fake_response
    mock_resp.text = "mock body"
    return mock_resp


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
                "name": "models/gemini-2.5-flash",
                "displayName": "Gemini 2.5 Flash",
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
            {
                "name": "models/gemini-3-pro-image",
                "displayName": "Gemini 3 Pro Image",
                "supportedGenerationMethods": ["generateContent"],
            },
            {
                "name": "models/gemini-3.5-transcribe",
                "displayName": "Gemini 3.5 Transcribe",
                "supportedGenerationMethods": ["generateContent"],
            },
            {
                "name": "models/lyria-3.5",
                "displayName": "Lyria 3.5",
                "supportedGenerationMethods": ["generateContent"],
            },
        ]
    }
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = _mock_get(fake_response)

        models, excluded = await _discover_gemini_models("fake-key")
        assert "gemini-2.0-flash" in models
        assert "gemini-2.5-flash" in models
        # Alias blocklist should filter out latest tags — but they stay visible
        assert "gemini-flash-latest" not in models
        excluded_ids = {e["id"]: e["kind"] for e in excluded}
        assert excluded_ids["gemini-flash-latest"] == "other"
        # Embedding model should be filtered out but reported
        assert "text-embedding-004" not in models
        assert excluded_ids["text-embedding-004"] == "embedding"
        # Image, audio, transcribe, and music models must be filtered out
        assert "gemini-3-pro-image" not in models
        assert "gemini-3.5-transcribe" not in models
        assert "lyria-3.5" not in models
        assert excluded_ids["gemini-3-pro-image"] == "image"
        assert excluded_ids["gemini-3.5-transcribe"] == "audio"
        assert excluded_ids["lyria-3.5"] == "audio"


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
        mock_get.return_value = _mock_get(fake_response)

        models, excluded = await _discover_openai_models("fake-key")
        assert "gpt-4o" in models
        assert "gpt-4o-mini" in models
        assert "dall-e-3" not in models
        assert "text-embedding-ada-002" not in models
        excluded_ids = {e["id"]: e["kind"] for e in excluded}
        assert excluded_ids["dall-e-3"] == "image"
        assert excluded_ids["text-embedding-ada-002"] == "embedding"


@pytest.mark.asyncio
async def test_fetch_anthropic_models_success():
    fake_response = {
        "data": [
            {"id": "claude-3-5-sonnet-20241022"},
            {"id": "claude-3-5-haiku-20241022"},
        ]
    }
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = _mock_get(fake_response)

        models, excluded = await _discover_anthropic_models("fake-key")
        assert "claude-3-5-sonnet-20241022" in models
        assert "claude-3-5-haiku-20241022" in models
        assert excluded == []


@pytest.mark.asyncio
async def test_fetch_available_models_error_handling():
    """Network failure surfaces a DiscoveryError with no HTTP code — and no
    hardcoded fallback models."""
    with patch("httpx.AsyncClient.get", side_effect=httpx.ConnectError("Network down")):
        result = await fetch_available_models("google_gemini", "fake-key")
        assert isinstance(result, DiscoveryResult)
        assert result.models == []
        assert result.excluded == []
        assert not result.ok
        assert result.error is not None
        assert result.error.code is None
        assert "Network down" in result.error.message


@pytest.mark.asyncio
async def test_fetch_available_models_http_error_carries_status_code():
    """A 401 from the provider is reported with its status code, not masked
    by a fallback list."""
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = _mock_get({"error": "invalid key"}, status_code=401)

        result = await fetch_available_models("openai", "bad-key")
        assert result.models == []
        assert not result.ok
        assert result.error.code == 401
        assert "401" in result.error.message
        err_dict = result.error.to_dict()
        assert err_dict == {"code": 401, "message": result.error.message}


@pytest.mark.asyncio
async def test_fetch_available_models_returns_excluded_list():
    fake_response = {
        "data": [
            {"id": "meta-llama/llama-3.3-70b-instruct"},
            {"id": "qwen/qwen-2.5-coder-32b-instruct"},
            {"id": "text-embedding-3-small"},
        ]
    }
    with patch("httpx.AsyncClient.get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = _mock_get(fake_response)

        result = await fetch_available_models(
            "custom_openai",
            "fake-key",
            base_url="https://api.groq.com/openai/v1",
        )
        assert result.ok
        assert "meta-llama/llama-3.3-70b-instruct" in result.models
        assert "qwen/qwen-2.5-coder-32b-instruct" in result.models
        assert "text-embedding-3-small" not in result.models
        excluded_ids = {e["id"]: e["kind"] for e in result.excluded}
        assert excluded_ids["text-embedding-3-small"] == "embedding"


@pytest.mark.asyncio
async def test_fetch_available_models_empty_key():
    result = await fetch_available_models("openai", "")
    assert result.ok
    assert result.models == []


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
