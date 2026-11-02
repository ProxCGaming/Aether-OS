"""E7-F07 — TTSR rules must vet streamed reasoning text, not just content.

The full-product review found that LiteLLMProvider.call_stream applied
StreamRules only to the answer `content` of each chunk while the
reasoning (`reasoning_content` / `thinking` delta fields) rode along
unvetted on StreamChunk.thinking, and the thinking forwarder published
it as THINKING.text_delta. Enabled redact/block rules never saw it.

These tests drive the real provider streaming path with synthetic
LiteLLM chunks (the audit's four whole-pattern cases: redact/block x
reasoning_content/thinking), plus the forwarder end to end, plus the
controls (rules off = byte-identical, non-matching rule = untouched,
content parity).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import ClassVar

import litellm
import pytest

import aether_engine.audit as audit_module
from aether_engine.providers.base import ProviderError
from aether_engine.providers.litellm_provider import LiteLLMProvider

SECRET = "SYNTHETIC_SECRET_THINKING_7"
REDACT_RULE = {"name": "secret", "pattern": SECRET, "action": "redact"}
BLOCK_RULE = {"name": "secret", "pattern": SECRET, "action": "block"}


class _AuditSpy:
    events: ClassVar[list[str]] = []

    def log_event(self, name, payload):
        type(self).events.append(name)


@pytest.fixture(autouse=True)
def _spy_audit(monkeypatch):
    _AuditSpy.events = []
    monkeypatch.setattr(audit_module, "AuditLogger", _AuditSpy)
    yield


def _chunk(content=None, reasoning_content=None, thinking=None, finish=None):
    delta = SimpleNamespace(
        content=content,
        reasoning_content=reasoning_content,
        thinking=thinking,
        tool_calls=None,
    )
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish)])


class _Stream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for c in self._chunks:
            yield c


def _provider(monkeypatch, chunks, rules):
    async def fake_acompletion(**kwargs):
        return _Stream(chunks)

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    return LiteLLMProvider(
        api_key="test-key",
        model="gemini-3.5-flash",
        provider_name="google_gemini",
        ttsr_rules=rules,
    )


def _collect(provider):
    async def collect():
        return [c async for c in provider.call_stream([{"role": "user", "content": "hi"}])]

    return asyncio.run(collect())


def _thinking_of(chunks):
    return "".join(c.thinking or "" for c in chunks)


# ---------------------------------------------------------------------------
# The audit's four cases
# ---------------------------------------------------------------------------


def test_ttsr_redact_applies_to_reasoning_content(monkeypatch):
    provider = _provider(
        monkeypatch,
        [_chunk(reasoning_content=f"pondering {SECRET} quietly"),
         _chunk(content="answer", finish="stop")],
        [REDACT_RULE],
    )
    chunks = _collect(provider)
    thinking = _thinking_of(chunks)
    assert SECRET not in thinking
    assert "«redacted:secret»" in thinking


def test_ttsr_redact_applies_to_thinking_field(monkeypatch):
    provider = _provider(
        monkeypatch,
        [_chunk(thinking=f"pondering {SECRET} quietly"),
         _chunk(content="answer", finish="stop")],
        [REDACT_RULE],
    )
    chunks = _collect(provider)
    thinking = _thinking_of(chunks)
    assert SECRET not in thinking
    assert "«redacted:secret»" in thinking


def test_ttsr_block_applies_to_reasoning_content(monkeypatch):
    provider = _provider(
        monkeypatch,
        [_chunk(reasoning_content=f"pondering {SECRET} quietly"),
         _chunk(content="answer", finish="stop")],
        [BLOCK_RULE],
    )
    with pytest.raises(ProviderError):
        _collect(provider)
    assert "TTSR_BLOCKED" in _AuditSpy.events


def test_ttsr_block_applies_to_thinking_field(monkeypatch):
    provider = _provider(
        monkeypatch,
        [_chunk(thinking=f"pondering {SECRET} quietly"),
         _chunk(content="answer", finish="stop")],
        [BLOCK_RULE],
    )
    with pytest.raises(ProviderError):
        _collect(provider)
    assert "TTSR_BLOCKED" in _AuditSpy.events


# ---------------------------------------------------------------------------
# Seam details
# ---------------------------------------------------------------------------


def test_ttsr_redact_applies_to_thinking_only_chunk(monkeypatch):
    """A chunk carrying thinking but no text must still be vetted."""
    provider = _provider(
        monkeypatch,
        [_chunk(thinking=f"only reasoning here: {SECRET}"),
         _chunk(finish="stop")],
        [REDACT_RULE],
    )
    chunks = _collect(provider)
    thinking = _thinking_of(chunks)
    assert SECRET not in thinking
    assert "«redacted:secret»" in thinking


def test_ttsr_block_on_thinking_only_chunk(monkeypatch):
    provider = _provider(
        monkeypatch,
        [_chunk(reasoning_content=f"only reasoning here: {SECRET}"),
         _chunk(finish="stop")],
        [BLOCK_RULE],
    )
    with pytest.raises(ProviderError):
        _collect(provider)
    assert "TTSR_BLOCKED" in _AuditSpy.events


def test_ttsr_redacts_text_and_thinking_in_the_same_chunk(monkeypatch):
    provider = _provider(
        monkeypatch,
        [_chunk(content=f"text {SECRET}", reasoning_content=f"reasoning {SECRET}"),
         _chunk(finish="stop")],
        [REDACT_RULE],
    )
    chunks = _collect(provider)
    joined_text = "".join(c.text or "" for c in chunks)
    thinking = _thinking_of(chunks)
    assert SECRET not in joined_text and "«redacted:secret»" in joined_text
    assert SECRET not in thinking and "«redacted:secret»" in thinking


def test_forward_thinking_publishes_only_vetted_reasoning(monkeypatch):
    """End to end: provider yield -> forward_thinking -> THINKING event."""
    from aether_common.contracts import EventType
    from aether_engine.event_bus import event_bus
    from aether_engine.langgraph.thinking import forward_thinking

    provider = _provider(
        monkeypatch,
        [_chunk(reasoning_content=f"pondering {SECRET} quietly"),
         _chunk(content="answer", finish="stop")],
        [REDACT_RULE],
    )
    chunks = _collect(provider)

    queue: asyncio.Queue = asyncio.Queue()
    event_bus.subscribe("task-e7f07", queue)
    try:
        state = {"task_id": "task-e7f07"}
        for chunk in chunks:
            forward_thinking(state, "planner", chunk)
    finally:
        event_bus.unsubscribe("task-e7f07", queue)

    published = []
    while not queue.empty():
        published.append(queue.get_nowait())
    thinking_events = [e for e in published if e.type == EventType.THINKING]
    assert thinking_events, "expected the reasoning to be forwarded"
    for event in thinking_events:
        assert SECRET not in event.payload["text_delta"]
    assert any("«redacted:secret»" in e.payload["text_delta"] for e in thinking_events)


# ---------------------------------------------------------------------------
# Controls
# ---------------------------------------------------------------------------


def test_reasoning_untouched_when_rules_off(monkeypatch):
    raw = f"pondering {SECRET} quietly"
    provider = _provider(
        monkeypatch,
        [_chunk(reasoning_content=raw), _chunk(content="answer", finish="stop")],
        None,
    )
    chunks = _collect(provider)
    assert _thinking_of(chunks) == raw


def test_reasoning_untouched_when_no_rule_matches(monkeypatch):
    raw = "perfectly ordinary reasoning, nothing to hide"
    provider = _provider(
        monkeypatch,
        [_chunk(thinking=raw), _chunk(content="answer", finish="stop")],
        [REDACT_RULE, BLOCK_RULE],
    )
    chunks = _collect(provider)
    assert _thinking_of(chunks) == raw
    assert "TTSR_BLOCKED" not in _AuditSpy.events
