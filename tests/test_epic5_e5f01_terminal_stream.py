"""E5-F01 — terminal-stream consumption loses the sole tool call and
the successful-fallback audit.

The defect is a producer/consumer termination mismatch with two
facets of one cause:

1. ``LiteLLMProvider.call_stream`` stamps the raw stream's
   ``finish_reason`` onto the trailing TEXT chunk it yields, and only
   afterwards yields the assembled tool calls as a separate chunk.
   Every consumer in the codebase (the supervisor model, the
   specialist nodes, the advisor, ...) accumulates chunks and breaks
   on the first ``finish_reason`` — so a response whose terminal raw
   chunk carries text + ``finish_reason="tool_calls"`` loses its
   tool calls entirely: the consumer stops one chunk too early.

2. The per-call fallback success audit ``NODE_FALLBACK_TRIGGERED``
   (ADR 0033) is emitted only after the provider generator is fully
   exhausted. A consumer that stops at the terminal chunk closes the
   generator early (GeneratorExit at the terminal yield), so a
   successful fallback consumed that way leaves no audit event.

These tests drive the real seams: the real LiteLLMProvider (with a
fake ``litellm.acompletion``) and the real supervisor consumer
(``AetherSupervisorModel._call_provider``).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import litellm
import pytest

from aether_engine.langgraph.supervisor_model import AetherSupervisorModel
from aether_engine.providers.litellm_provider import FallbackModel, LiteLLMProvider

# ---------------------------------------------------------------------------
# Fakes (mirroring tests/test_epic5_providers.py's harness)
# ---------------------------------------------------------------------------


def _chunk(text=None, finish=None, tool_calls=None):
    delta = SimpleNamespace(
        content=text, reasoning_content=None, thinking=None, tool_calls=tool_calls
    )
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish)])


def _tool_delta(index, call_id=None, name=None, arguments=None):
    return SimpleNamespace(
        index=index,
        id=call_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


class _Stream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for c in self._chunks:
            yield c


def _provider():
    return LiteLLMProvider(
        api_key="sk-test",
        model="gemini-3.5-flash",
        provider_name="google_gemini",
        fallback_models=[FallbackModel(model="gpt-4o-mini", api_key="sk-test2", provider_name="openai")],
    )


def _rate_limit():
    return litellm.RateLimitError("rate limited", "openai", "gpt-4o-mini")


@pytest.fixture
def audit_spy(monkeypatch):
    events = []

    class SpyLogger:
        def log_event(self, name, payload):
            events.append((name, payload))

    monkeypatch.setattr("aether_engine.audit.AuditLogger", SpyLogger)
    return events


def _fallback_events(events):
    return [payload for name, payload in events if name == "NODE_FALLBACK_TRIGGERED"]


_MESSAGES = [{"role": "user", "content": "read my notes"}]


def _sole_tool_stream():
    """The audited response shape: tool-call deltas accumulate, then
    the terminal raw chunk carries trailing text AND
    finish_reason="tool_calls"."""
    return _Stream([
        _chunk(tool_calls=[_tool_delta(0, call_id="call_1", name="read_file")]),
        _chunk(tool_calls=[_tool_delta(0, arguments='{"path": "notes.txt"}')]),
        _chunk(text="Let me read that file.", finish="tool_calls"),
    ])


def _two_tool_stream():
    return _Stream([
        _chunk(tool_calls=[
            _tool_delta(0, call_id="call_1", name="read_file"),
            _tool_delta(1, call_id="call_2", name="search_text"),
        ]),
        _chunk(tool_calls=[
            _tool_delta(0, arguments='{"path": "a.txt"}'),
            _tool_delta(1, arguments='{"query": "needle"}'),
        ]),
        _chunk(text="Working on it.", finish="tool_calls"),
    ])


def _text_stream():
    return _Stream([
        _chunk(text="one "),
        _chunk(text="two "),
        _chunk(text="three.", finish="stop"),
    ])


async def _supervisor_round(provider):
    """One real supervisor consumer round-trip: (text, [ToolCall])."""
    model = AetherSupervisorModel()
    return await model._call_provider(provider, _MESSAGES, tools=None)


# ---------------------------------------------------------------------------
# Facet 1 — the tool call must survive terminal-stream consumption
# ---------------------------------------------------------------------------


def test_supervisor_keeps_sole_tool_call_from_terminal_stream(monkeypatch):
    async def fake_acompletion(**kwargs):
        return _sole_tool_stream()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    text, calls = asyncio.run(_supervisor_round(_provider()))
    assert text == "Let me read that file."
    assert [(tc.name, tc.args, tc.call_id) for tc in calls] == [
        ("read_file", {"path": "notes.txt"}, "call_1")
    ]


def test_supervisor_keeps_all_tool_calls_from_terminal_stream(monkeypatch):
    async def fake_acompletion(**kwargs):
        return _two_tool_stream()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    text, calls = asyncio.run(_supervisor_round(_provider()))
    assert text == "Working on it."
    assert [(tc.name, tc.args, tc.call_id) for tc in calls] == [
        ("read_file", {"path": "a.txt"}, "call_1"),
        ("search_text", {"query": "needle"}, "call_2"),
    ]


def test_text_only_terminal_stream_completes_normally(monkeypatch):
    """Control: a plain text stream with a finish chunk is consumed
    exactly as before — full text, no tool calls."""
    async def fake_acompletion(**kwargs):
        return _text_stream()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    text, calls = asyncio.run(_supervisor_round(_provider()))
    assert text == "one two three."
    assert calls == []


def test_producer_marks_finish_only_on_the_terminal_chunk(monkeypatch):
    """Producer contract every break-on-finish consumer relies on:
    finish_reason appears on the LAST chunk only, and that chunk is
    the one carrying the assembled tool calls."""
    async def fake_acompletion(**kwargs):
        return _sole_tool_stream()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    async def collect():
        return [c async for c in _provider().call_stream(_MESSAGES, node_name="supervisor")]

    chunks = asyncio.run(collect())
    assert len(chunks) >= 2
    assert all(c.finish_reason is None for c in chunks[:-1])
    assert chunks[-1].finish_reason == "tool_calls"
    assert [(tc.name, tc.call_id) for tc in chunks[-1].tool_calls or []] == [
        ("read_file", "call_1")
    ]
    assert "".join(c.text or "" for c in chunks) == "Let me read that file."


# ---------------------------------------------------------------------------
# Facet 2 — a successful fallback must audit, however consumption ends
# ---------------------------------------------------------------------------


def test_fallback_audits_when_supervisor_stops_at_terminal_chunk(monkeypatch, audit_spy):
    """The audited repro: primary rate-limited, the fallback answers
    with the sole-tool-call stream, the supervisor consumes it (and
    stops at the terminal chunk, closing the provider generator).
    The tool call must arrive AND the fallback must be audited."""
    calls = []

    async def fake_acompletion(**kwargs):
        calls.append(kwargs["model"])
        if len(calls) == 1:
            raise _rate_limit()
        return _sole_tool_stream()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    async def round_then_settle():
        result = await _supervisor_round(_provider())
        # The consumer stopped at the terminal chunk; the provider
        # generator's close — which carries the fallback audit on
        # its GeneratorExit path (E5-F01), exactly like M19's usage
        # recording — is delivered by the loop's async-generator
        # finalizer. Give the loop the turns it needs, as the
        # production engine loop does continuously.
        for _ in range(3):
            await asyncio.sleep(0)
        return result

    text, tool_calls = asyncio.run(round_then_settle())
    assert text == "Let me read that file."
    assert [(tc.name, tc.call_id) for tc in tool_calls] == [("read_file", "call_1")]
    events = _fallback_events(audit_spy)
    assert len(events) == 1
    assert events[0]["fallback_model"] == "gpt-4o-mini"
    assert events[0]["primary_model"]


def test_fallback_audits_when_consumer_closes_stream_early(monkeypatch, audit_spy):
    """The intent-classifier pattern: the consumer takes the first
    chunk it needs and closes the stream. The fallback produced that
    chunk — it succeeded — so the audit must still fire, once."""
    calls = []

    async def fake_acompletion(**kwargs):
        calls.append(kwargs["model"])
        if len(calls) == 1:
            raise _rate_limit()
        return _text_stream()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    async def take_one_and_close():
        stream = _provider().call_stream(_MESSAGES, node_name="intent_classifier")
        first = await stream.__anext__()
        await stream.aclose()
        return first

    first = asyncio.run(take_one_and_close())
    assert first.text == "one "
    assert len(_fallback_events(audit_spy)) == 1


def test_fallback_full_consumption_audits_exactly_once(monkeypatch, audit_spy):
    """Control: full exhaustion of a successful fallback stream
    audits once — the early-close path must not double-fire."""
    calls = []

    async def fake_acompletion(**kwargs):
        calls.append(kwargs["model"])
        if len(calls) == 1:
            raise _rate_limit()
        return _text_stream()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    async def collect():
        return [c async for c in _provider().call_stream(_MESSAGES, node_name="planner")]

    chunks = asyncio.run(collect())
    assert "".join(c.text or "" for c in chunks) == "one two three."
    assert len(_fallback_events(audit_spy)) == 1


def test_primary_stream_early_close_emits_no_fallback_audit(monkeypatch, audit_spy):
    """Control: closing a PRIMARY stream early is not a fallback and
    must never produce NODE_FALLBACK_TRIGGERED."""
    async def fake_acompletion(**kwargs):
        return _text_stream()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    async def take_one_and_close():
        stream = _provider().call_stream(_MESSAGES, node_name="intent_classifier")
        first = await stream.__anext__()
        await stream.aclose()
        return first

    first = asyncio.run(take_one_and_close())
    assert first.text == "one "
    assert _fallback_events(audit_spy) == []
