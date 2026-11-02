"""Epic 5 — providers: the §6.4 fallback gates, routing truth, and
the bounded key-validation regression pins.

E5-01: per-call fallback inside LiteLLMProvider.call_stream is
allowed only on an initial failure — never when the conversation
already carries tool results, and never after the call has streamed
output.

E5-02: the task-level gate (should_attempt_task_fallback) — first
attempt only, no tool activity, factory required.

E5-03: routing stamps/decisions carry the real reason; the stale
gemini-2.5-flash default argument is gone.

E5-04: validate_api_key is time-bounded and pings gemini-3.5-flash
for google_gemini.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from types import SimpleNamespace

import litellm
import pytest

from aether_engine.providers.base import NetworkError, RateLimitError
from aether_engine.providers.litellm_provider import FallbackModel, LiteLLMProvider

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _chunk(text=None, finish=None):
    delta = SimpleNamespace(content=text, reasoning_content=None, thinking=None, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish)])


class _Stream:
    def __init__(self, chunks, error=None, error_after=None):
        self._chunks = chunks
        self._error = error
        self._error_after = error_after  # raise after N chunks yielded

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        yielded = 0
        for c in self._chunks:
            if self._error is not None and self._error_after is not None and yielded == self._error_after:
                raise self._error
            yield c
            yielded += 1
        if self._error is not None and (self._error_after is None or self._error_after >= yielded):
            raise self._error


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


async def _collect(provider, messages):
    return [c async for c in provider.call_stream(messages, node_name="planner")]


# ---------------------------------------------------------------------------
# E5-01 — per-call §6.4 gate
# ---------------------------------------------------------------------------


def test_initial_failure_falls_back_and_audits(monkeypatch, audit_spy):
    calls = []

    async def fake_acompletion(**kwargs):
        calls.append(kwargs["model"])
        if len(calls) == 1:
            raise _rate_limit()
        return _Stream([_chunk("hello", finish="stop")])

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    chunks = asyncio.run(_collect(_provider(), [{"role": "user", "content": "hi"}]))
    assert [c.text for c in chunks if c.text] == ["hello"]
    assert len(calls) == 2  # primary + fallback
    assert any(name == "NODE_FALLBACK_TRIGGERED" for name, _ in audit_spy)


def test_tool_results_in_history_block_fallback(monkeypatch):
    calls = []

    async def fake_acompletion(**kwargs):
        calls.append(kwargs["model"])
        raise _rate_limit()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    messages = [
        {"role": "user", "content": "run it"},
        {"role": "assistant", "content": ""},
        {"role": "tool", "content": "tool output"},
    ]
    with pytest.raises(RateLimitError):
        asyncio.run(_collect(_provider(), messages))
    assert len(calls) == 1  # the fallback model was never attempted


def test_partial_stream_failure_does_not_restart(monkeypatch):
    calls = []

    async def fake_acompletion(**kwargs):
        calls.append(kwargs["model"])
        return _Stream([_chunk("part-1")], error=_rate_limit(), error_after=1)

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    seen = []

    async def run():
        async for c in _provider().call_stream([{"role": "user", "content": "hi"}], node_name="planner"):
            seen.append(c)

    with pytest.raises((RateLimitError, NetworkError)):
        asyncio.run(run())
    assert [c.text for c in seen if c.text] == ["part-1"]  # streamed once, never duplicated
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# E5-02 — task-level gate truth table
# ---------------------------------------------------------------------------


def test_task_fallback_gate_truth_table():
    from aether_engine.routes.websocket import should_attempt_task_fallback

    assert should_attempt_task_fallback(attempt=1, saw_tool_activity=False, has_factory=True) is True
    assert should_attempt_task_fallback(attempt=1, saw_tool_activity=True, has_factory=True) is False
    assert should_attempt_task_fallback(attempt=2, saw_tool_activity=False, has_factory=True) is False
    assert should_attempt_task_fallback(attempt=1, saw_tool_activity=False, has_factory=False) is False


# ---------------------------------------------------------------------------
# E5-03 — routing truth
# ---------------------------------------------------------------------------


def test_select_route_default_arg_is_current_model():
    from aether_engine.routing.policy import RoutingPolicy

    sig = inspect.signature(RoutingPolicy.select_route)
    assert sig.parameters["user_default_model"].default == "gemini-3.5-flash"


def _route(prompt="hello there", **kw):
    from aether_engine.routing.health import ProviderHealthManager
    from aether_engine.routing.policy import RoutingPolicy

    health = kw.pop("health", ProviderHealthManager())
    return RoutingPolicy().select_route(
        prompt=prompt,
        configured_providers=kw.pop("providers", ["google_gemini", "openai"]),
        health_manager=health,
        user_default_provider="google_gemini",
        user_default_model="gemini-3.5-flash",
        **kw,
    )


def test_requested_model_wins_when_healthy():
    d = _route(requested_model="gpt-4o-mini")
    assert d.provider == "openai"
    assert d.model == "gpt-4o-mini"
    assert d.reason  # a real reason is stamped, never blank


def test_user_default_wins_for_generic_prompt():
    d = _route()
    assert (d.provider, d.model) == ("google_gemini", "gemini-3.5-flash")


def test_unhealthy_default_falls_back_with_honest_reason():
    from aether_engine.routing.health import ProviderHealthManager

    health = ProviderHealthManager()
    for _ in range(5):
        health.record_failure("google_gemini", "boom", is_retriable=True)
    d = _route(health=health)
    assert d.provider == "openai"
    assert "google_gemini" in d.reason  # the reason names what failed over


# ---------------------------------------------------------------------------
# E5-04 — validation: bounded + current ping model
# ---------------------------------------------------------------------------


def test_validate_api_key_success_and_gemini_ping_model(monkeypatch):
    from aether_engine.providers.litellm_provider import validate_api_key

    seen = {}

    async def fake_acompletion(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    result = asyncio.run(validate_api_key("google_gemini", "sk-test"))
    assert result["status"] == "connected"
    assert "gemini-3.5-flash" in str(seen.get("model"))


def test_validate_api_key_is_time_bounded(monkeypatch):
    from aether_engine.providers.litellm_provider import validate_api_key

    async def hang(**kwargs):
        await asyncio.sleep(60)

    monkeypatch.setattr(litellm, "acompletion", hang)
    start = time.monotonic()
    result = asyncio.run(validate_api_key("google_gemini", "sk-test", timeout=2))
    elapsed = time.monotonic() - start
    assert result["status"] == "network_error"  # a timeout is never a success
    assert elapsed < 20  # bounded by the 2s timeout (+ generous load slack), not the 60s hang
    assert result["message"]  # an honest failure message, not silence
