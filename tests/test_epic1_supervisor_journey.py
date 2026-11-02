"""E1-F01 / E1-F02 regression tests (Astra full-product review, Epic 1).

Both defects live on the LIVE ``create_supervisor()`` route (the
adapter in ``aether_engine/langgraph/supervisor_model.py``), so every
test here drives the full executor graph with scripted providers —
never the legacy hand-written supervisor node API (removed in
S1-M4), whose passing tests are exactly what masked F02.

F01 — the adapter's completion guard ended the run on ANY specialist
text after the last user message, before the supervisor could route
again: a planner -> coder journey stopped after the planner. The
supervisor must be consulted after a specialist response; its next
decision (delegate on, or END) is what completes the chain.

F02 — the adapter converted provider memory calls via
``provider_call.arguments`` while the provider contract
(``providers/base.py`` ToolCall) exposes ``.args``: every native
supervisor memory call crashed with ``'ToolCall' object has no
attribute 'arguments'`` before retrieval. The conversion must work,
and the wrapper gates (Deny policy / role allow-list, enforced in
``nodes/supervisor._memory_tool_gate``) must still apply unchanged.
"""
from unittest.mock import AsyncMock

import pytest

from aether_common.contracts import EventType, TaskState
from aether_engine.config import UserConfig
from aether_engine.langgraph import executor as executor_module
from aether_engine.langgraph.nodes import coder as coder_module
from aether_engine.langgraph.nodes import planner as planner_module
from aether_engine.langgraph.nodes import supervisor as supervisor_module
from aether_engine.memory import episodic as episodic_module
from aether_engine.memory import knowledge_graph as kg_module
from aether_engine.providers.base import StreamChunk, ToolCall
from aether_engine.tools.registry import ToolRegistry

PLAN_TEXT = "Plan: 1) write the code, 2) ship it."
CODE_TEXT = "Implementation complete: the code is written."
FINAL_TEXT = "Specialist final answer."
MEMORY_SENTINEL = "MEMORY-SENTINEL: the user is called Arindam."


# --------------------------------------------------------------------------
# Scripted providers (house pattern: scripted call_stream, no network)
# --------------------------------------------------------------------------

class JourneyProvider:
    """Supervisor script: planner -> coder -> END (JSON decisions).

    Specialists answer with fixed final text on their first call.
    """

    def __init__(self):
        self.calls = []
        self._supervisor_calls = 0

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        self.calls.append({
            "node": node_name,
            "messages": [dict(m) for m in messages],
        })
        if node_name == "planner":
            yield StreamChunk(text=PLAN_TEXT, finish_reason="stop")
            return
        if node_name == "coder":
            yield StreamChunk(text=CODE_TEXT, finish_reason="stop")
            return
        self._supervisor_calls += 1
        if self._supervisor_calls == 1:
            text = '{"next": "planner", "reason": "plan it first", "briefing": ""}'
        elif self._supervisor_calls == 2:
            text = '{"next": "coder", "reason": "implement the plan", "briefing": ""}'
        else:
            text = '{"next": "END", "reason": "chain finished", "briefing": ""}'
        yield StreamChunk(text=text, finish_reason="stop")

    def nodes(self):
        return [c["node"] for c in self.calls]


class SingleSpecialistProvider:
    """Supervisor script: planner -> END. Control for F01: a
    single-specialist task must still complete, with no extra hops."""

    def __init__(self):
        self.calls = []
        self._supervisor_calls = 0

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        self.calls.append({
            "node": node_name,
            "messages": [dict(m) for m in messages],
        })
        if node_name == "planner":
            yield StreamChunk(text=FINAL_TEXT, finish_reason="stop")
            return
        self._supervisor_calls += 1
        if self._supervisor_calls == 1:
            text = '{"next": "planner", "reason": "handle it", "briefing": ""}'
        else:
            text = '{"next": "END", "reason": "done", "briefing": ""}'
        yield StreamChunk(text=text, finish_reason="stop")

    def nodes(self):
        return [c["node"] for c in self.calls]


class AlwaysPlannerProvider:
    """Supervisor that answers EVERY routing call with 'planner' —
    the degenerate rut the no-progress breaker exists for."""

    def __init__(self):
        self.calls = []

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        self.calls.append({
            "node": node_name,
            "messages": [dict(m) for m in messages],
        })
        if node_name == "planner":
            yield StreamChunk(text=FINAL_TEXT, finish_reason="stop")
            return
        yield StreamChunk(
            text='{"next": "planner", "reason": "always planner", "briefing": ""}',
            finish_reason="stop",
        )

    def nodes(self):
        return [c["node"] for c in self.calls]


class NativeMemoryProvider:
    """Supervisor's first call emits a NATIVE memory tool call (the
    provider ToolCall contract), its follow-up decides END."""

    def __init__(self):
        self.calls = []

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        self.calls.append({
            "node": node_name,
            "messages": [dict(m) for m in messages],
        })
        if len(self.calls) == 1:
            yield StreamChunk(
                tool_calls=[ToolCall(name="search_knowledge_graph",
                                     args={"query": "user name"},
                                     call_id="mem-call-1")],
                finish_reason="tool_calls",
            )
        else:
            yield StreamChunk(
                text='{"next": "END", "reason": "answered from memory", "briefing": ""}',
                finish_reason="stop",
            )


class FakeMemoryBackend:
    """Facade-seam double: records fact searches, returns a sentinel."""

    def __init__(self):
        self.search_calls = []

    def search_facts(self, query, limit=10, include_history=False):
        self.search_calls.append(query)
        return [{"entity_name": "user", "entity_type": "Person",
                 "fact_text": MEMORY_SENTINEL}]

    def list_facts(self, limit=10):
        return [{"entity_name": "user", "entity_type": "Person",
                 "fact_text": MEMORY_SENTINEL}]


# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------

@pytest.fixture
def graph_env(tmp_path, monkeypatch):
    """Isolate a full executor run (mirrors test_epic1_parity)."""
    monkeypatch.setattr(
        executor_module, "DEFAULT_CHECKPOINT_DB", tmp_path / "checkpoints.db")
    monkeypatch.setattr(
        episodic_module, "store_episode", AsyncMock(return_value=None))
    monkeypatch.setattr(
        kg_module, "extract_and_store_facts", AsyncMock(return_value=None))
    monkeypatch.setattr(
        planner_module, "save_artifact", lambda *a, **k: "journey/plan.txt")
    monkeypatch.setattr(
        coder_module, "save_artifact", lambda *a, **k: "journey/code.txt")
    return tmp_path


async def _run(provider, session_id, user_config=None, prompt="do the work"):
    events = [event async for event in executor_module.run_langgraph_task(
        prompt,
        provider,
        tools=ToolRegistry(),
        task_id=f"task-{session_id}",
        request_id=f"req-{session_id}",
        session_id=session_id,
        user_config=user_config or UserConfig(),
    )]
    return events


def _types(events):
    return [ev.type for ev in events]


# --------------------------------------------------------------------------
# E1-F01 — planner -> coder journey
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_planner_then_coder_journey_completes(graph_env):
    """A supervisor scripted planner -> coder -> END must traverse
    BOTH specialists: the planner's text answer is an intermediate
    product, not task completion."""
    provider = JourneyProvider()

    events = await _run(provider, "journey-chain")

    assert EventType.TASK_FAILED not in _types(events)
    assert EventType.TASK_COMPLETED in _types(events)
    completed = next(ev for ev in events if ev.type == EventType.TASK_COMPLETED)
    assert completed.payload["state"] == TaskState.SUCCEEDED.value

    nodes = provider.nodes()
    assert nodes.count("planner") == 1, f"planner must run once, got {nodes}"
    assert nodes.count("coder") == 1, (
        f"coder must run after the planner (F01: it never ran), got {nodes}")
    assert nodes.index("planner") < nodes.index("coder")
    # The supervisor was consulted after the planner responded (its
    # second decision routed to the coder) and again to end the chain.
    assert nodes.count("supervisor") == 3, f"got supervisor calls in {nodes}"
    # The user-visible result is the implementation, not just the plan.
    assert CODE_TEXT in completed.payload["response"]


@pytest.mark.asyncio
async def test_single_specialist_task_still_completes(graph_env):
    """Control: a one-specialist task completes with exactly that
    specialist invoked — no phantom hops, no coder."""
    provider = SingleSpecialistProvider()

    events = await _run(provider, "journey-single")

    assert EventType.TASK_FAILED not in _types(events)
    assert EventType.TASK_COMPLETED in _types(events)
    nodes = provider.nodes()
    assert nodes.count("planner") == 1, f"got {nodes}"
    assert "coder" not in nodes, f"got {nodes}"
    assert "researcher" not in nodes, f"got {nodes}"


@pytest.mark.asyncio
async def test_same_specialist_redelegation_ends_instead_of_looping(graph_env):
    """A supervisor stuck delegating to the specialist that just
    answered must end with that answer standing — never ping-pong
    until the graph recursion limit fails the task. The breaker
    trips when the specialist's answer repeats verbatim, so the
    planner runs exactly twice: its first answer, one re-run the
    supervisor asked for, then the run ends instead of a third."""
    provider = AlwaysPlannerProvider()

    events = await _run(provider, "journey-rut")

    assert EventType.TASK_FAILED not in _types(events), (
        "the rut must end gracefully, not in GraphRecursionError")
    assert EventType.TASK_COMPLETED in _types(events)
    nodes = provider.nodes()
    assert nodes.count("planner") == 2, (
        f"breaker must trip on the verbatim repeat, got {nodes}")


# --------------------------------------------------------------------------
# E1-F02 — native supervisor memory calls
# --------------------------------------------------------------------------

@pytest.fixture
def memory_backend(monkeypatch):
    backend = FakeMemoryBackend()
    monkeypatch.setattr(
        supervisor_module, "backend_or_none", lambda *a, **k: backend)
    return backend


@pytest.mark.asyncio
async def test_native_memory_call_retrieves_through_live_graph(
        graph_env, memory_backend):
    """A native search_knowledge_graph call on the live graph must
    execute (no 'arguments' AttributeError) and its result must reach
    the supervisor's follow-up call as a tool message."""
    provider = NativeMemoryProvider()

    events = await _run(provider, "journey-memory",
                        prompt="what is my name?")

    assert EventType.TASK_FAILED not in _types(events), (
        f"native memory call must not fail the task, got "
        f"{[ev.payload for ev in events if ev.type == EventType.TASK_FAILED]}")
    assert EventType.TASK_COMPLETED in _types(events)
    assert memory_backend.search_calls == ["user name"], (
        f"retrieval must run once with the call's query, "
        f"got {memory_backend.search_calls}")

    assert len(provider.calls) == 2, "expected memory call + follow-up"
    follow_up = provider.calls[1]["messages"]
    tool_msgs = [m for m in follow_up if m.get("role") == "tool"]
    assert tool_msgs, "follow-up call must carry the memory tool result"
    assert MEMORY_SENTINEL in tool_msgs[-1]["content"]


@pytest.mark.asyncio
async def test_native_memory_call_denied_by_policy_retrieves_nothing(
        graph_env, memory_backend):
    """The Deny gate survives the F02 fix: a policy-denied memory call
    is answered by the wrapper gate and retrieval never runs."""
    provider = NativeMemoryProvider()
    user_config = UserConfig(
        tool_policies={"search_knowledge_graph": "Deny"})

    events = await _run(provider, "journey-memory-deny",
                        user_config=user_config, prompt="what is my name?")

    assert EventType.TASK_FAILED not in _types(events)
    assert EventType.TASK_COMPLETED in _types(events)
    assert memory_backend.search_calls == [], (
        "denied memory tool must never reach retrieval")

    assert len(provider.calls) == 2, "expected memory call + follow-up"
    follow_up = provider.calls[1]["messages"]
    tool_msgs = [m for m in follow_up if m.get("role") == "tool"]
    assert tool_msgs, "the gate's answer must come back as a tool message"
    assert "blocked by user policy" in tool_msgs[-1]["content"]
    assert MEMORY_SENTINEL not in tool_msgs[-1]["content"]
