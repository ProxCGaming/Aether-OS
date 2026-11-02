"""E5-F06: a public custom model catalog must never convert a failed
authentication into "connected / healthy".

The validation verdict is governed by the chat ping — the only step
that actually exercises the key. A custom endpoint's /models listing
is often public (no auth), so its success proves nothing about the
key; it legitimately contributes only model-availability info to the
result payload (``models`` / ``excluded_models``), and the persisted
model list when the ping genuinely connects.

Case matrix pinned here:
- ping connected (any catalog state)        -> connected, health success
- all pings invalid_key (explicit 401)      -> invalid_key, health failure
  (non-retriable) — regardless of catalog
- all pings network_error                   -> network_error, health
  failure (retriable) — regardless of catalog
- discovery fetch itself failed             -> the real fetch error is
  surfaced as network_error, as before
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aether_engine.orchestration.core import _validate_provider_key
from aether_engine.providers.discovery import DiscoveryError, DiscoveryResult
from aether_engine.state import engine_state

CORE = "aether_engine.orchestration.core"


def _run_validation(provider_name, *, discovered, ping, persist_models=False):
    """Drive the real _validate_provider_key with synthetic discovery/ping.

    Returns (result, record_success, record_failure, update_models).
    """
    async def fake_fetch(name, key, base_url=None, provider_flavor=None):
        return discovered

    async def fake_validate(name, key, model="", base_url=None, provider_flavor=None):
        return ping(model)

    record_success = MagicMock()
    record_failure = MagicMock()
    update_models = MagicMock()
    return record_success, record_failure, update_models, fake_fetch, fake_validate


async def _validate(provider_name, *, discovered, ping, persist_models=False):
    record_success, record_failure, update_models, fake_fetch, fake_validate = (
        _run_validation(provider_name, discovered=discovered, ping=ping)
    )
    with patch(f"{CORE}.fetch_available_models", new=AsyncMock(side_effect=fake_fetch)), \
         patch(f"{CORE}.validate_api_key", new=AsyncMock(side_effect=fake_validate)), \
         patch(f"{CORE}.save_config"), \
         patch.object(engine_state.health_manager, "record_success", record_success), \
         patch.object(engine_state.health_manager, "record_failure", record_failure), \
         patch.object(engine_state.model_registry, "update_provider_models", update_models):
        result = await _validate_provider_key(
            provider_name, "sk-bad", base_url="https://example.com/v1",
            persist_models=persist_models,
        )
    return result, record_success, record_failure, update_models


def _invalid_key(model):
    return {"status": "invalid_key", "message": "Invalid API key.", "latency_ms": 3.0}


def _network_error(model):
    return {"status": "network_error", "message": "quota exhausted", "latency_ms": 3.0}


@pytest.mark.asyncio
async def test_catalog_never_converts_invalid_key_to_connected():
    """The audit's exact probe: explicit invalid_key/401 on every sampled
    ping + a nonempty public catalog. Pre-fix this returned connected /
    'API key is valid (3 models available)' and recorded health success."""
    result, record_success, record_failure, update_models = await _validate(
        "custom_e5f06_repro",
        discovered=DiscoveryResult(models=["m1", "m2", "m3"]),
        ping=_invalid_key,
    )

    assert result["status"] == "invalid_key"
    assert "API key is valid" not in result["message"]
    # Health must follow the true verdict: a non-retriable failure, and
    # never a success.
    record_success.assert_not_called()
    record_failure.assert_called_once()
    assert record_failure.call_args.kwargs["is_retriable"] is False
    # The catalog still contributes availability info to the payload...
    assert result["models"] == ["m1", "m2", "m3"]
    # ...but nothing is persisted under a fabricated success.
    update_models.assert_not_called()


@pytest.mark.asyncio
async def test_catalog_never_converts_network_error_to_connected():
    """Unreachable/quota pings are an error, not a validation — a public
    catalog cannot upgrade them to 'valid' either."""
    result, record_success, record_failure, _ = await _validate(
        "custom_e5f06_unreachable",
        discovered=DiscoveryResult(models=["m1", "m2"]),
        ping=_network_error,
    )

    assert result["status"] == "network_error"
    record_success.assert_not_called()
    record_failure.assert_called_once()
    assert record_failure.call_args.kwargs["is_retriable"] is True


@pytest.mark.asyncio
async def test_ping_success_governs_with_catalog():
    """Control: a model-specific invalid_key followed by a connecting
    ping still validates (the scan keeps its existing semantics), and
    the catalog rides along as availability info."""
    def ping(model):
        if model == "working-model":
            return {"status": "connected", "message": "ok", "latency_ms": 9.0}
        return _invalid_key(model)

    result, record_success, record_failure, _ = await _validate(
        "custom_e5f06_ok",
        discovered=DiscoveryResult(models=["401-model", "working-model"]),
        ping=ping,
    )

    assert result["status"] == "connected"
    assert "working-model" in result["message"]
    record_success.assert_called_once()
    record_failure.assert_not_called()


@pytest.mark.asyncio
async def test_ping_success_without_catalog():
    """Control: with no discovered models the default-model ping alone
    decides, exactly as before."""
    def ping(model):
        assert model == ""
        return {"status": "connected", "message": "ok", "latency_ms": 5.0}

    result, record_success, record_failure, _ = await _validate(
        "custom_e5f06_nocat",
        discovered=DiscoveryResult(models=[]),
        ping=ping,
    )

    assert result["status"] == "connected"
    record_success.assert_called_once()
    record_failure.assert_not_called()


@pytest.mark.asyncio
async def test_builtin_provider_was_never_catalog_overridden():
    """Control: built-in providers never had the catalog override; their
    invalid-key verdict stands before and after the fix."""
    result, record_success, record_failure, _ = await _validate(
        "openai",
        discovered=DiscoveryResult(models=["gpt-x"]),
        ping=_invalid_key,
    )

    assert result["status"] == "invalid_key"
    record_success.assert_not_called()
    record_failure.assert_called_once()
    assert record_failure.call_args.kwargs["is_retriable"] is False


@pytest.mark.asyncio
async def test_discovery_error_still_surfaced():
    """Control: when the model-list fetch itself fails and no ping
    connects, the real fetch error (with HTTP code) is surfaced."""
    result, record_success, _, _ = await _validate(
        "custom_e5f06_fetchfail",
        discovered=DiscoveryResult(
            models=[], error=DiscoveryError(code=503, message="upstream down")),
        ping=_network_error,
    )

    assert result["status"] == "network_error"
    assert "Model list fetch failed" in result["message"]
    assert "HTTP 503" in result["message"]
    record_success.assert_not_called()
