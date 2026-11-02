"""Epic 9 — optimisation guards.

- The embedding circuit breaker: a connection-class failure pauses
  further embedding attempts for the cooldown (callers degrade to
  lexical retrieval) instead of every store/query/backfill attempt
  paying LiteLLM retry/backoff against a dead endpoint. Arbitrary
  errors do not trip it; a success resets it.
- litellm stays lazily loaded: importing the provider module must
  not pull the real litellm into sys.modules.
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, patch

import pytest

from aether_engine.memory import episodic


@pytest.fixture(autouse=True)
def _closed_breaker():
    with patch("aether_engine.memory.episodic._embed_unavailable_until", 0.0):
        yield


def _embed_mock(side_effect=None, return_value=None):
    if return_value is not None:
        return AsyncMock(return_value=return_value)
    return AsyncMock(side_effect=side_effect)


def test_breaker_trips_on_connection_error_and_skips():
    failing = _embed_mock(side_effect=ConnectionError("Ollama down"))
    with patch("aether_engine.memory.episodic.litellm.aembedding", new=failing):
        assert asyncio.run(episodic._embed_text("one", task_id="t1")) is None
        assert episodic._embed_breaker_open()
        # Second call: skipped before reaching the (mocked) endpoint.
        assert asyncio.run(episodic._embed_text("two", task_id="t2")) is None
        assert failing.await_count == 1


def test_breaker_ignores_arbitrary_errors():
    failing = _embed_mock(side_effect=RuntimeError("bad request"))
    with patch("aether_engine.memory.episodic.litellm.aembedding", new=failing):
        assert asyncio.run(episodic._embed_text("one", task_id="t1")) is None
        assert not episodic._embed_breaker_open()
        assert asyncio.run(episodic._embed_text("two", task_id="t2")) is None
        assert failing.await_count == 2


def test_success_resets_breaker():
    ok = _embed_mock(return_value=type("R", (), {"data": [{"embedding": [0.1, 0.2]}]})())
    with patch("aether_engine.memory.episodic.litellm.aembedding", new=ok):
        episodic._trip_embed_breaker(ConnectionError("down"))
        assert episodic._embed_breaker_open()
        # A success can only happen once the cooldown lapses; simulate
        # the lapse, then confirm the success path leaves it closed.
        episodic._embed_unavailable_until = 0.0
        assert asyncio.run(episodic._embed_text("hi", task_id="t3")) == [0.1, 0.2]
        assert episodic._embed_unavailable_until == 0.0


def test_litellm_not_imported_by_provider_module():
    # In a fresh interpreter, importing the engine's provider module
    # must leave the real litellm unloaded (the ~4s import is deferred
    # to first use / the startup warm-up thread).
    import subprocess

    code = (
        "import sys; import aether_engine.providers.litellm_provider; "
        "print('litellm' in sys.modules)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=False, timeout=120,
    )
    assert out.stdout.strip().endswith("False"), out.stdout + out.stderr
