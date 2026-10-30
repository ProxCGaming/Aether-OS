"""Epic 1 parity characterisation tests.

These tests pin the orchestration behaviour of the supervisor graph
(supervisor -> specialists -> tool pipeline) so that the migration to
LangGraph's prebuilt ``create_supervisor()`` — with Aether's Phase 9
approval pause, tool-policy gating and memory briefing kept as a
wrapper — is proven safe: the migrated implementation (E1-02, ADR 0027)
keeps every test here green, and any deliberate behaviour change must
show up as a diff in this file.

Behaviour pinned, all against today's code with scripted stub providers
(no real provider calls, no network):

1. Approval pause: a tool call whose policy is "Require Approval"
   interrupts the run (TOOL_APPROVAL_REQUEST + TASK_INTERRUPTED in
   PENDING_APPROVAL, tool NOT executed), and ``resume_langgraph_task``
   completes the task after approval.
2. Policy gate: a tool with a "Deny" policy never executes through the
   full graph; the denial surfaces as a tool message and the task still
   completes.
3. Memory briefing: when the supervisor calls a memory tool, the tool
   result is fed into the supervisor's follow-up model call, and the
   decision's briefing is stored in state as ``context_briefing``.
4. Per-agent models: a specialist node builds its provider from
   ``agent_models`` (``"provider:model"``) when an override is set, and
   uses the graph's default provider for ``"inherit"`` / no entry.
5. Checkpoint/resume: after an approval interrupt + resume, a FRESH
   checkpointer instance on the same DB sees the finished state with
   the tool result in the messages and no pending tool call.

Former gap, fixed by the E1-02 migration (30 Oct 2026, ADR 0027): the
supervisor stores the memory briefing in ``state["context_briefing"]``
and, since the migration, every specialist node injects it into its
system prompt. This suite originally pinned the gap as a strict xfail;
``test_specialist_prompt_includes_memory_briefing`` now passes and
guards the fix.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.runnables import RunnableConfig

from aether_common.contracts import EventType, TaskState
from aether_engine.config import UserConfig
from aether_engine.langgraph import executor as executor_module
from aether_engine.langgraph.nodes import planner as planner_module
from aether_engine.langgraph.nodes import supervisor as supervisor_module
from aether_engine.langgraph.nodes.planner import planner_node
from aether_engine.langgraph.nodes.supervisor import supervisor_node
from aether_engine.memory import episodic as episodic_module
from aether_engine.memory import knowledge_graph as kg_module
from aether_engine.orchestration import core as orchestration_core
from aether_engine.providers.base import StreamChunk, ToolCall
from aether_engine.tools.registry import Tool, ToolRegistry, ToolRisk

TOOL_NAME = "parity_probe"
TOOL_ARGS = {"q": "x"}
TOOL_RESULT = "probe-result-42"
FINAL_TEXT = "Specialist final answer."


# --------------------------------------------------------------------------
# Stub providers (house pattern: scripted call_stream, no network)
# --------------------------------------------------------------------------

class ScriptedGraphProvider:
    """Provider scripted for full-graph runs.

    Supervisor calls get a fixed JSON routing decision (delegate to
    planner). The planner's first call requests the parity tool; its
    later calls return final text. Every call is recorded.
    """

    def __init__(self):
        self.calls = []
        self._planner_calls = 0

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        self.calls.append({
            "node": node_name,
            "messages": [dict(m) for m in messages],
        })
        if node_name == "planner":
            self._planner_calls += 1
            if self._planner_calls == 1:
                yield StreamChunk(
                    tool_calls=[ToolCall(name=TOOL_NAME, args=dict(TOOL_ARGS),
                                         call_id="call-parity-1")],
                    finish_reason="tool_calls",
                )
            else:
                yield StreamChunk(text=FINAL_TEXT, finish_reason="stop")
            return
        # supervisor (and anything else): route to planner
        yield StreamChunk(
            text='{"next": "planner", "reason": "parity routing", "briefing": ""}',
            finish_reason="stop",
        )


class RecordingProvider:
    """Provider that just records its calls and returns fixed text."""

    def __init__(self, text="recorded-ok"):
        self.text = text
        self.calls = []

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        self.calls.append({
            "node": node_name,
            "messages": [dict(m) for m in messages],
        })
        yield StreamChunk(text=self.text, finish_reason="stop")


class MemoryToolProvider:
    """Supervisor provider: first call requests the KG memory tool,
    the follow-up call returns a decision carrying a briefing."""

    BRIEFING = "The user is called Arindam."

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
                text=('{"next": "planner", "reason": "uses memory", '
                      f'"briefing": "{self.BRIEFING}"}}'),
                finish_reason="stop",
            )


# --------------------------------------------------------------------------
# Helpers / fixtures
# --------------------------------------------------------------------------

def _probe_registry(executed):
    """A registry with one READ_ONLY probe tool (in-process execution,
    so no sandbox worker is involved)."""
    registry = ToolRegistry()

    def _fn(**kwargs):
        executed.append(kwargs)
        return TOOL_RESULT

    registry.register(Tool(
        name=TOOL_NAME,
        description="Epic 1 parity probe tool",
        execute_fn=_fn,
        risk=ToolRisk.READ_ONLY,
    ))
    return registry


@pytest.fixture
def graph_env(tmp_path, monkeypatch):
    """Isolate a full executor run: tmp checkpoint DB, no real memory
    writes on completion, no artifact files on disk."""
    monkeypatch.setattr(
        executor_module, "DEFAULT_CHECKPOINT_DB", tmp_path / "checkpoints.db")
    monkeypatch.setattr(
        episodic_module, "store_episode", AsyncMock(return_value=None))
    monkeypatch.setattr(
        kg_module, "extract_and_store_facts", AsyncMock(return_value=None))
    monkeypatch.setattr(
        planner_module, "save_artifact", lambda *a, **k: "parity/artifact.txt")
    return tmp_path


async def _collect(agen):
    return [event async for event in agen]


async def _run_task(provider, registry, user_config, session_id):
    return await _collect(executor_module.run_langgraph_task(
        "please run the parity probe",
        provider,
        tools=registry,
        task_id=f"task-{session_id}",
        request_id=f"req-{session_id}",
        session_id=session_id,
        user_config=user_config,
    ))


def _types(events):
    return [ev.type for ev in events]


# --------------------------------------------------------------------------
# 1. Approval pause + resume
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_approval_pause_then_resume_completes(graph_env):
    """A 'Require Approval' tool interrupts the run before executing;
    resume_langgraph_task(approved=True) then runs it and completes."""
    executed = []
    registry = _probe_registry(executed)
    provider = ScriptedGraphProvider()
    session_id = "parity-approve"
    user_config = UserConfig(tool_policies={TOOL_NAME: "Require Approval"})

    events = await _run_task(provider, registry, user_config, session_id)

    assert EventType.TOOL_APPROVAL_REQUEST in _types(events)
    approval = next(ev for ev in events
                    if ev.type == EventType.TOOL_APPROVAL_REQUEST)
    assert approval.payload["tool_name"] == TOOL_NAME
    assert approval.payload["args"] == TOOL_ARGS
    assert approval.payload["approval_key"] == session_id

    assert EventType.TASK_INTERRUPTED in _types(events)
    interrupted = next(ev for ev in events
                       if ev.type == EventType.TASK_INTERRUPTED)
    assert interrupted.payload["state"] == TaskState.PENDING_APPROVAL.value
    assert EventType.TASK_COMPLETED not in _types(events)
    assert executed == [], "tool must not execute before approval"

    resume_events = await _collect(executor_module.resume_langgraph_task(
        session_id, True, provider, tools=registry))

    assert EventType.TASK_COMPLETED in _types(resume_events)
    completed = next(ev for ev in resume_events
                     if ev.type == EventType.TASK_COMPLETED)
    assert completed.payload["state"] == TaskState.SUCCEEDED.value
    assert executed == [TOOL_ARGS], "approved tool must execute exactly once"


# --------------------------------------------------------------------------
# 2. Policy gate: deny never executes
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_deny_policy_tool_never_executes(graph_env):
    """A 'Deny' tool is blocked by the policy gate through the full
    graph: execute_fn is never called, no approval is requested, the
    denial surfaces in a tool message, and the task still completes."""
    executed = []
    registry = _probe_registry(executed)
    provider = ScriptedGraphProvider()
    user_config = UserConfig(tool_policies={TOOL_NAME: "Deny"})

    events = await _run_task(provider, registry, user_config, "parity-deny")

    assert executed == [], "denied tool's execute_fn must never be called"
    assert EventType.TOOL_APPROVAL_REQUEST not in _types(events)
    assert EventType.TASK_INTERRUPTED not in _types(events)
    assert EventType.TASK_COMPLETED in _types(events)

    tool_events = [ev for ev in events if ev.type == EventType.TOOL_ACTIVITY
                   and ev.payload.get("tool_name") == TOOL_NAME]
    assert tool_events, "expected a TOOL_ACTIVITY event for the denied tool"
    assert any("blocked by user policy" in str(ev.payload.get("result", ""))
               for ev in tool_events), (
        f"denial wording must surface in the tool result, got {tool_events}")


# --------------------------------------------------------------------------
# 5. Checkpoint/resume durability
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_checkpoint_resume_result_persisted_in_state(graph_env):
    """Interrupt mid-approval, resume, then read the thread back through
    a FRESH checkpointer on the same DB (simulating an engine restart):
    the tool result is in the persisted messages and nothing is pending."""
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    from aether_engine.langgraph.graph import compile_graph

    executed = []
    registry = _probe_registry(executed)
    provider = ScriptedGraphProvider()
    session_id = "parity-checkpoint"
    user_config = UserConfig(tool_policies={TOOL_NAME: "Require Approval"})

    events = await _run_task(provider, registry, user_config, session_id)
    assert EventType.TASK_INTERRUPTED in _types(events)
    assert executed == []

    resume_events = await _collect(executor_module.resume_langgraph_task(
        session_id, True, provider, tools=registry))
    assert EventType.TASK_COMPLETED in _types(resume_events)

    db_path = graph_env / "checkpoints.db"
    async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
        graph = compile_graph(checkpointer=saver)
        state = await graph.aget_state(
            {"configurable": {"thread_id": session_id}})

    messages = state.values.get("messages", [])
    tool_msgs = [m for m in messages
                 if m.get("role") == "tool" and m.get("name") == TOOL_NAME]
    assert tool_msgs, f"expected a persisted tool message, got {messages}"
    assert TOOL_RESULT in tool_msgs[-1]["content"]
    assert state.values.get("pending_tool_call") is None


# --------------------------------------------------------------------------
# 3. Memory briefing
# --------------------------------------------------------------------------

def _supervisor_state(**overrides):
    state = {
        "original_prompt": "what is my name?",
        "messages": [{"role": "user", "content": "what is my name?"}],
        "session_id": "mem-session",
        "task_id": "mem-task",
        "plan": "",
        "delegation_log": [],
        "pending_tool_call": None,
        "active_specialist": None,
    }
    state.update(overrides)
    return state


@pytest.fixture
def memory_env(monkeypatch):
    """Stub the memory layer + audit sink for supervisor-node tests."""
    fact = {"entity_name": "user", "entity_type": "Person",
            "fact_text": "The user is called Arindam."}
    monkeypatch.setattr(
        kg_module, "search_facts", lambda query, limit=5: [fact])
    monkeypatch.setattr(
        kg_module, "get_all_facts", lambda limit=5: [fact])
    monkeypatch.setattr(
        supervisor_module, "_audit_logger",
        SimpleNamespace(log_event=lambda *a, **k: None))


@pytest.mark.asyncio
async def test_supervisor_memory_tool_result_feeds_followup_call(memory_env):
    """When memory returns a fact, the supervisor's follow-up model
    call receives that fact as a tool message in its input."""
    provider = MemoryToolProvider()
    config = RunnableConfig(configurable={
        "provider": provider, "session_id": "mem-session"})

    result = await supervisor_node(_supervisor_state(), config)

    assert result["next"] == "planner"
    assert len(provider.calls) == 2, "expected initial + follow-up call"
    follow_up_messages = provider.calls[1]["messages"]
    tool_msgs = [m for m in follow_up_messages if m.get("role") == "tool"]
    assert tool_msgs, "follow-up call must carry the memory tool result"
    assert tool_msgs[0]["name"] == "search_knowledge_graph"
    assert "The user is called Arindam." in tool_msgs[0]["content"]


@pytest.mark.asyncio
async def test_supervisor_briefing_stored_in_state(memory_env):
    """The decision's briefing is stored in state as context_briefing
    (the channel specialists are supposed to receive it through)."""
    provider = MemoryToolProvider()
    config = RunnableConfig(configurable={
        "provider": provider, "session_id": "mem-session"})

    result = await supervisor_node(_supervisor_state(), config)

    assert result["context_briefing"] == MemoryToolProvider.BRIEFING


@pytest.mark.asyncio
async def test_specialist_prompt_includes_memory_briefing(monkeypatch):
    """Desired parity behaviour: a specialist's model input includes
    the memory briefing the supervisor retrieved."""
    monkeypatch.setattr(
        planner_module, "save_artifact", lambda *a, **k: "parity/artifact.txt")
    provider = RecordingProvider(text="a plan")
    state = {
        "original_prompt": "plan my day",
        "messages": [{"role": "user", "content": "plan my day"}],
        "plan": "No plan currently.",
        "task_id": "briefing-task",
        "active_skills": [],
        "context_briefing": "BRIEFING-SENTINEL: the user prefers mornings.",
    }
    config = RunnableConfig(configurable={"provider": provider})

    await planner_node(state, config)

    assert provider.calls, "planner must call its provider"
    all_content = "\n".join(
        str(m.get("content", ""))
        for call in provider.calls for m in call["messages"])
    assert "BRIEFING-SENTINEL" in all_content


# --------------------------------------------------------------------------
# 4. Per-agent models
# --------------------------------------------------------------------------

def _planner_state():
    return {
        "original_prompt": "make a plan",
        "messages": [{"role": "user", "content": "make a plan"}],
        "plan": "",
        "task_id": "models-task",
        "active_skills": [],
    }


@pytest.mark.asyncio
async def test_per_agent_model_override_used_for_specialist(monkeypatch):
    """With agent_models['planner'] = 'provX:modelY' and a secret store,
    the planner node constructs its provider via _create_provider_instance
    with exactly that split, and the override provider is the one called."""
    monkeypatch.setattr(
        planner_module, "save_artifact", lambda *a, **k: "parity/artifact.txt")
    create_calls = []
    override_provider = RecordingProvider(text="override plan")
    default_provider = RecordingProvider(text="default plan")

    def fake_create(provider_name, model, secret_store):
        create_calls.append((provider_name, model))
        return override_provider

    monkeypatch.setattr(
        orchestration_core, "_create_provider_instance", fake_create)

    config = RunnableConfig(configurable={
        "provider": default_provider,
        "agent_models": {"planner": "provX:modelY"},
        "secret_store": object(),
    })
    result = await planner_node(_planner_state(), config)

    assert create_calls == [("provX", "modelY")]
    assert len(override_provider.calls) == 1
    assert default_provider.calls == []
    assert result["messages"][0]["content"] == "override plan"


@pytest.mark.asyncio
async def test_per_agent_model_inherit_and_absent_use_default(monkeypatch):
    """'inherit' and a missing agent_models entry both keep the graph's
    default provider; _create_provider_instance is never called."""
    monkeypatch.setattr(
        planner_module, "save_artifact", lambda *a, **k: "parity/artifact.txt")
    create_calls = []

    def fake_create(provider_name, model, secret_store):
        create_calls.append((provider_name, model))
        return RecordingProvider(text="should not be used")

    monkeypatch.setattr(
        orchestration_core, "_create_provider_instance", fake_create)

    for agent_models in ({"planner": "inherit"}, {}, {"coder": "provX:modelY"}):
        default_provider = RecordingProvider(text="default plan")
        config = RunnableConfig(configurable={
            "provider": default_provider,
            "agent_models": agent_models,
            "secret_store": object(),
        })
        result = await planner_node(_planner_state(), config)
        assert len(default_provider.calls) == 1
        assert result["messages"][0]["content"] == "default plan"

    assert create_calls == []
