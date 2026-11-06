"""Finding N-01 [S1]: the main provider stream must be time-bounded.

Before the fix, ``LiteLLMProvider._build_call_kwargs`` set no ``timeout``
and ``call_stream`` awaited ``litellm.acompletion`` and iterated the
response with no ``wait_for`` anywhere, so a provider that connected and
then stalled — before the first chunk, or mid-stream — hung the task
indefinitely.

The bound is an IDLE bound (STREAM_TIMEOUT, 120s in production): every
phase of a call that produces nothing — the dispatch and the wait for
each next chunk — may last at most that long. A slow but progressing
stream is never cut; there is no total deadline. On expiry the call
fails through the provider's existing timeout path (per-call fallback
when §6.4 allows it, then the typed domain ``TimeoutError`` the graph
layer already treats as a provider failure).

These tests patch STREAM_TIMEOUT down to fractions of a second. Each
stall test wraps the collect in an outer ``wait_for`` guard: on pre-fix
code the guard fires and the test FAILS instead of hanging the suite.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import litellm
import pytest

from aether_engine.providers import litellm_provider
from aether_engine.providers.base import TimeoutError
from aether_engine.providers.litellm_provider import (
    STREAM_TIMEOUT,
    FallbackModel,
    LiteLLMProvider,
)

_HANG_S = 30  # stands in for "forever": far beyond every bound in play
_GUARD_S = 5.0  # test-level hang guard; fires only on pre-fix code


# ---------------------------------------------------------------------------
# Fakes (mirroring tests/test_epic5_providers.py)
# ---------------------------------------------------------------------------


def _chunk(text=None, finish=None):
    delta = SimpleNamespace(content=text, reasoning_content=None, thinking=None, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish)])


class _StallStream:
    """Yields the given chunks, then goes silent forever."""

    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for c in self._chunks:
            yield c
        await asyncio.sleep(_HANG_S)
        yield _chunk("too-late")  # pragma: no cover — the bound fires first


class _DripStream:
    """Healthy but slow: a pause before every chunk, the stream completes."""

    def __init__(self, chunks, delay):
        self._chunks = chunks
        self._delay = delay

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for c in self._chunks:
            await asyncio.sleep(self._delay)
            yield c


def _provider(with_fallback=True):
    fallbacks = (
        [FallbackModel(model="gpt-4o-mini", api_key="sk-test2", provider_name="openai")]
        if with_fallback
        else []
    )
    return LiteLLMProvider(
        api_key="sk-test",
        model="gemini-3.5-flash",
        provider_name="google_gemini",
        fallback_models=fallbacks,
    )


async def _collect(provider, messages):
    return [c async for c in provider.call_stream(messages, node_name="planner")]


@pytest.fixture
def short_bound(monkeypatch):
    """Shrink the production idle bound so stall tests run in milliseconds."""
    monkeypatch.setattr(litellm_provider, "STREAM_TIMEOUT", 0.2)
    return 0.2


# ---------------------------------------------------------------------------
# The bound itself
# ---------------------------------------------------------------------------


def test_stream_timeout_default_is_a_long_operation_bound():
    """120s: generation is not a ping — a cold local model or a reasoning
    model legitimately takes far longer than VALIDATION_TIMEOUT (10s) to
    produce a first token, so the stream bound sits at the codebase's
    long-operation magnitude while still being finite."""
    assert STREAM_TIMEOUT == 120.0
    assert STREAM_TIMEOUT > litellm_provider.VALIDATION_TIMEOUT


def test_timeout_kwarg_is_passed_to_acompletion(monkeypatch):
    """The bound is forwarded to LiteLLM so its HTTP client enforces it
    too (the validation ping's belt-and-braces pattern)."""
    seen_kwargs = []

    async def fake_acompletion(**kwargs):
        seen_kwargs.append(kwargs)
        return _DripStream([_chunk("ok", finish="stop")], delay=0)

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    chunks = asyncio.run(_collect(_provider(), [{"role": "user", "content": "hi"}]))
    assert [c.text for c in chunks if c.text] == ["ok"]
    assert seen_kwargs[0]["timeout"] == STREAM_TIMEOUT


# ---------------------------------------------------------------------------
# Stalls: never-yield, mid-stream, and at dispatch
# ---------------------------------------------------------------------------


def test_stream_that_never_yields_times_out(monkeypatch, short_bound):
    """A provider that connects then stalls before the first chunk must
    fail with the typed domain TimeoutError, not hang. The stall is
    retriable per model, so the fallback is attempted and bounded too."""
    calls = []

    async def fake_acompletion(**kwargs):
        calls.append(kwargs["model"])
        return _StallStream([])

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        asyncio.run(
            asyncio.wait_for(
                _collect(_provider(), [{"role": "user", "content": "hi"}]),
                timeout=_GUARD_S,
            )
        )
    elapsed = time.monotonic() - start
    assert elapsed < _GUARD_S
    assert len(calls) == 2  # primary + fallback, each bounded on its own


def test_mid_stream_stall_times_out_after_partial_output(monkeypatch, short_bound):
    """A stall after output started: the consumer keeps the partial chunk
    and then gets the typed TimeoutError — no silent hang, no restarted
    stream (§6.4: output already yielded blocks fallback)."""
    calls = []

    async def fake_acompletion(**kwargs):
        calls.append(kwargs["model"])
        return _StallStream([_chunk("part-1")])

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    seen = []

    async def run():
        async for c in _provider().call_stream(
            [{"role": "user", "content": "hi"}], node_name="planner"
        ):
            seen.append(c)

    start = time.monotonic()
    with pytest.raises(TimeoutError):
        asyncio.run(asyncio.wait_for(run(), timeout=_GUARD_S))
    elapsed = time.monotonic() - start
    assert elapsed < _GUARD_S
    assert [c.text for c in seen if c.text] == ["part-1"]
    assert len(calls) == 1  # no fallback once output has streamed


def test_dispatch_stall_falls_back_to_next_model(monkeypatch, short_bound):
    """A provider that stalls before even returning a stream: the
    dispatch bound fires and the existing retriable-timeout path falls
    back to the next model, which answers normally."""

    async def fake_acompletion(**kwargs):
        if kwargs["model"] != "gpt-4o-mini":  # the primary stalls; the fallback answers
            await asyncio.sleep(_HANG_S)
        return _DripStream([_chunk("from-fallback", finish="stop")], delay=0)

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    start = time.monotonic()
    chunks = asyncio.run(
        asyncio.wait_for(
            _collect(_provider(), [{"role": "user", "content": "hi"}]),
            timeout=_GUARD_S,
        )
    )
    elapsed = time.monotonic() - start
    assert elapsed < _GUARD_S
    assert [c.text for c in chunks if c.text] == ["from-fallback"]


# ---------------------------------------------------------------------------
# Healthy streams are untouched
# ---------------------------------------------------------------------------


def test_slow_but_progressing_stream_is_not_cut(monkeypatch):
    """The bound is per-chunk idle time, NOT a total deadline: a stream
    whose total duration exceeds STREAM_TIMEOUT completes as long as no
    single gap between chunks does."""
    monkeypatch.setattr(litellm_provider, "STREAM_TIMEOUT", 0.3)

    async def fake_acompletion(**kwargs):
        return _DripStream(
            [_chunk("a"), _chunk("b"), _chunk("c", finish="stop")], delay=0.15
        )

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    start = time.monotonic()
    chunks = asyncio.run(_collect(_provider(), [{"role": "user", "content": "hi"}]))
    elapsed = time.monotonic() - start
    assert elapsed > 0.3  # total ran past the bound — and still finished
    assert [c.text for c in chunks if c.text] == ["a", "b", "c"]


def test_healthy_stream_flows_unchanged(monkeypatch):
    """Baseline: an immediate, well-formed stream yields its chunks and
    terminal finish chunk exactly as before the fix."""

    async def fake_acompletion(**kwargs):
        return _DripStream([_chunk("hello"), _chunk(" world", finish="stop")], delay=0)

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    chunks = asyncio.run(_collect(_provider(), [{"role": "user", "content": "hi"}]))
    assert [c.text for c in chunks if c.text] == ["hello", " world"]
    assert chunks[-1].finish_reason == "stop"
