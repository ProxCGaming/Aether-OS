"""Phase 10.1, B1: provider validation ping must have a bounded timeout.

Before the fix, ``validate_api_key`` called ``litellm.acompletion`` with no
``timeout`` and no ``asyncio.wait_for`` wrapper, so a stalled provider blocked
key validation (and the provider-list UI) indefinitely. These tests mock a
provider that never responds and assert validation returns quickly with a
real error — never a hang, never a silent success, never an empty success.

Each test here fails on the pre-fix code:
- the hanging-provider test: pre-fix there is no timeout bound at all, so the
  outer ``wait_for`` in the test fires (or the ``timeout`` kwarg is rejected);
- the timeout-exception tests: pre-fix the generic ``except Exception`` path
  returned a ``network_error`` with no ``code`` and a message that never said
  the ping timed out;
- the kwarg-forwarding test: pre-fix no ``timeout`` was passed to LiteLLM.
"""
import asyncio
import time

import litellm
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from aether_engine.providers.discovery import DISCOVERY_TIMEOUT
from aether_engine.providers.litellm_provider import (
    VALIDATION_TIMEOUT,
    validate_api_key,
)


@pytest.mark.asyncio
async def test_hanging_provider_returns_timeout_error_quickly():
    """A provider that never responds must not block validation indefinitely.

    The mocked provider sleeps 30s (i.e. hangs). Validation must return well
    inside that window with a real error. The outer wait_for is a guard: on
    pre-fix code it fires and the test fails instead of hanging the suite.
    """
    async def _hang_forever(**kwargs):
        await asyncio.sleep(30)
        return MagicMock()

    start = time.monotonic()
    with patch(
        "aether_engine.providers.litellm_provider.litellm.acompletion",
        new=_hang_forever,
    ):
        result = await asyncio.wait_for(
            validate_api_key("openai", "sk-test", model="gpt-4o-mini", timeout=0.05),
            timeout=5.0,
        )
    elapsed = time.monotonic() - start

    assert elapsed < 5.0
    assert result["status"] == "network_error"
    assert result["status"] != "connected"
    # DiscoveryResult-style error shape: a real error with a code. None means
    # a transport-level failure (no HTTP response received), same convention
    # as DiscoveryError.
    assert "code" in result
    assert result["code"] is None
    assert "timed out" in result["message"]


@pytest.mark.asyncio
async def test_asyncio_timeout_error_returns_coded_error():
    """A transport-level timeout surfaces as a coded error, not a bare message."""
    with patch(
        "aether_engine.providers.litellm_provider.litellm.acompletion",
        new_callable=AsyncMock,
    ) as mock_acompletion:
        mock_acompletion.side_effect = asyncio.TimeoutError()
        result = await validate_api_key("openai", "sk-test", model="gpt-4o-mini", timeout=0.05)

    assert result["status"] == "network_error"
    assert result["code"] is None
    assert "timed out" in result["message"]
    assert "0.05" in result["message"]


@pytest.mark.asyncio
async def test_litellm_timeout_exception_returns_coded_error():
    """LiteLLM's own Timeout exception takes the same error path."""
    with patch(
        "aether_engine.providers.litellm_provider.litellm.acompletion",
        new_callable=AsyncMock,
    ) as mock_acompletion:
        mock_acompletion.side_effect = litellm.Timeout(
            "Request timed out.", "gpt-4o-mini", "openai"
        )
        result = await validate_api_key("openai", "sk-test", model="gpt-4o-mini", timeout=0.05)

    assert result["status"] == "network_error"
    assert result["code"] is None
    assert "timed out" in result["message"]


@pytest.mark.asyncio
async def test_timeout_is_passed_to_litellm_acompletion():
    """The bound is enforced by LiteLLM's HTTP client as well as wait_for."""
    with patch(
        "aether_engine.providers.litellm_provider.litellm.acompletion",
        new_callable=AsyncMock,
    ) as mock_acompletion:
        mock_acompletion.return_value = MagicMock()
        result = await validate_api_key("openai", "sk-test", model="gpt-4o-mini")

    assert result["status"] == "connected"
    assert mock_acompletion.call_args.kwargs["timeout"] == VALIDATION_TIMEOUT

    with patch(
        "aether_engine.providers.litellm_provider.litellm.acompletion",
        new_callable=AsyncMock,
    ) as mock_acompletion:
        mock_acompletion.return_value = MagicMock()
        await validate_api_key("openai", "sk-test", model="gpt-4o-mini", timeout=3.0)

    assert mock_acompletion.call_args.kwargs["timeout"] == 3.0


def test_validation_timeout_matches_discovery_timeout():
    """The ping bound is 10s, matching DISCOVERY_TIMEOUT (prompt requirement)."""
    assert VALIDATION_TIMEOUT == 10.0
    assert VALIDATION_TIMEOUT == DISCOVERY_TIMEOUT
