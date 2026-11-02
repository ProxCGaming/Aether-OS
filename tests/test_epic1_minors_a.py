"""Epic 1 minors, executor/supervisor cluster: E1-F08, F09, F10, F11,
F12, F16.

Each test pins one finding at its real seam:

  * F08 — a steer queued while a specialist's tool step runs must be
    in the specialist's NEXT model call (the tool-step boundary), not
    wait for the whole specialist subgraph to finish.
  * F09 — a native handoff whose stamped decision carries no briefing
    must still receive the memory-briefing reconstruction (the tool
    results since the last user message), like an unstamped handoff.
  * F10 — run_langgraph_task's max_turns is a real budget: the run
    stops at it instead of ignoring it.
  * F11 — plan mode disables supervisor spawns beyond the planner:
    a plan-mode delegation to the coder is denied, the planner still
    works.
  * F12 — spawn-denied specialists are absent from the native tool
    offers the provider receives, not just from the prompt text.
  * F16 — THINKING events published during an approval-resumed run
    reach the resume consumer (the resume path subscribes to the bus
    like the run path does).

Providers are scripted stubs (house pattern from test_epic1_steering /
test_epic1_spawn_policy): no real provider calls, no network.
"""
import asyncio
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool

from aether_common.contracts import EventType
from aether_engine.config import UserConfig
from aether_engine.langgraph import executor as executor_module
from aether_engine.langgraph.nodes import planner as planner_module
from aether_engine.langgraph.steering import STEERING_QUEUE, enqueue_steer
from aether_engine.langgraph.supervisor_model import (
    AetherSupervisorModel,
    _spawn_policy_denial,
    supervisor_post_model_hook,
)
from aether_engine.memory import episodic as episodic_module
from aether_engine.memory import knowledge_graph as kg_module
from aether_engine.providers.base import StreamChunk, ToolCall
from aether_engine.tools.registry import Tool, ToolRegistry, ToolRisk

TOOL_NAME = "minors_probe"
TOOL_ARGS = {"q": "x"}
STEER_TEXT = "STEER-SENTINEL: also check the second file"


@pytest.fixture
def graph_env(tmp_path, monkeypatch):
    monkeypatch.setattr(
        executor_module, "DEFAULT_CHECKPOINT_DB", tmp_path / "checkpoints.db")
    monkeypatch.setattr(
        episodic_module, "store_episode", AsyncMock(return_value=None))
    monkeypatch.setattr(
        kg_module, "extract_and_store_facts", AsyncMock(return_value=None))
    monkeypatch.setattr(
        planner_module, "save_artifact", lambda *a, **k: "minors/artifact.txt")
    STEERING_QUEUE.clear()
    yield tmp_path
    STEERING_QUEUE.clear()


async def _collect(agen):
    return [event async for event in agen]


def _probe_registry(executed):
    registry = ToolRegistry()

    def _fn(**kwargs):
        executed.append(kwargs)
        return "probe-result-42"

    registry.register(Tool(
        name=TOOL_NAME,
        description="Epic 1 minors probe tool",
        execute_fn=_fn,
        risk=ToolRisk.READ_ONLY,
    ))
    return registry


# ---------------------------------------------------------------------------
# E1-F08 — steering drains at the specialist's tool-step boundary
# ---------------------------------------------------------------------------

class _BoundaryProvider:
    """Supervisor routes to planner; the planner's first call is held
    open (so a test can enqueue a steer mid-call), then requests the
    probe tool; later planner calls return final text."""

    def __init__(self):
        self.calls = []
        self.planner_calls = 0
        self.first_planner_started = asyncio.Event()
        self.release_first_planner = asyncio.Event()

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        contents = [
            str(m.get("content", "")) if isinstance(m, dict)
            else str(getattr(m, "content", ""))
            for m in messages
        ]
        self.calls.append({"node": node_name, "contents": contents})
        if node_name == "planner":
            self.planner_calls += 1
            if self.planner_calls == 1:
                self.first_planner_started.set()
                await self.release_first_planner.wait()
                yield StreamChunk(
                    tool_calls=[ToolCall(name=TOOL_NAME, args=dict(TOOL_ARGS),
                                         call_id="call-minors-1")],
                    finish_reason="tool_calls",
                )
            else:
                yield StreamChunk(text="Specialist final answer.",
                                  finish_reason="stop")
            return
        yield StreamChunk(
            text='{"next": "planner", "reason": "minors routing", "briefing": ""}',
            finish_reason="stop",
        )


@pytest.mark.asyncio
async def test_steer_applies_at_the_tool_step_boundary(graph_env):
    executed = []
    registry = _probe_registry(executed)
    provider = _BoundaryProvider()
    session_id = "minors-steer-boundary"

    task = asyncio.create_task(_collect(executor_module.run_langgraph_task(
        "please run the probe",
        provider,
        tools=registry,
        task_id="task-minors-steer",
        request_id="req-minors-steer",
        session_id=session_id,
        user_config=UserConfig(),
    )))
    await asyncio.wait_for(provider.first_planner_started.wait(), timeout=10)
    enqueue_steer(session_id, STEER_TEXT, "steer-minors-1")
    provider.release_first_planner.set()
    events = await asyncio.wait_for(task, timeout=30)

    types = [ev.type for ev in events]
    assert EventType.TASK_COMPLETED in types
    assert EventType.TASK_FAILED not in types
    applied = [ev for ev in events if ev.type == EventType.STEERING_APPLIED]
    assert len(applied) == 1
    assert applied[0].payload["message_id"] == "steer-minors-1"
    assert executed == [TOOL_ARGS]

    planner_contents = [
        "\n".join(call["contents"]) for call in provider.calls
        if call["node"] == "planner"
    ]
    assert len(planner_contents) >= 2
    assert STEER_TEXT not in planner_contents[0]
    # THE F08 assertion: the call immediately after the tool step —
    # the specialist's next step — already sees the steer. Before the
    # fix it appeared only in a later call, after the whole subgraph
    # finished and the executor restarted the segment.
    assert STEER_TEXT in planner_contents[1]


# ---------------------------------------------------------------------------
# E1-F09 — native handoffs receive the briefing reconstruction
# ---------------------------------------------------------------------------

MEMORY_SENTINEL = "MEMORY-SENTINEL: the user prefers terse answers"


def _hook_state(final_message):
    return {
        "messages": [
            HumanMessage(content="build the thing"),
            AIMessage(
                content="",
                tool_calls=[{
                    "name": "search_knowledge_graph",
                    "args": {"query": "preferences"},
                    "id": "mem-1",
                }],
            ),
            ToolMessage(content=MEMORY_SENTINEL, tool_call_id="mem-1"),
            final_message,
        ]
    }


def _native_handoff(decision):
    kwargs = {"additional_kwargs": {"aether_decision": decision}} if decision is not None else {}
    return AIMessage(
        content="routing to coder",
        tool_calls=[{"name": "transfer_to_coder", "args": {}, "id": "handoff-1"}],
        **kwargs,
    )


def test_native_handoff_with_empty_briefing_gets_reconstruction():
    state = _hook_state(_native_handoff(
        {"next": "coder", "reason": "needs code", "briefing": ""}))
    update = supervisor_post_model_hook(state)
    assert update["context_briefing"] == MEMORY_SENTINEL
    assert update["active_specialist"] == "coder"


def test_native_handoff_without_decision_still_reconstructs():
    # The pre-existing unstamped path: reconstruction already ran here.
    state = _hook_state(_native_handoff(None))
    update = supervisor_post_model_hook(state)
    assert update["context_briefing"] == MEMORY_SENTINEL


def test_handoff_with_its_own_briefing_keeps_it():
    state = _hook_state(_native_handoff(
        {"next": "coder", "reason": "needs code", "briefing": "MODEL-BRIEF"}))
    update = supervisor_post_model_hook(state)
    assert update["context_briefing"] == "MODEL-BRIEF"


# ---------------------------------------------------------------------------
# E1-F10 — max_turns is enforced
# ---------------------------------------------------------------------------

class _LoopingProvider:
    """Supervisor always routes to planner; the planner always asks
    for the probe tool — an unbounded tool loop without a budget."""

    def __init__(self):
        self.planner_calls = 0

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        if node_name == "planner":
            self.planner_calls += 1
            yield StreamChunk(
                tool_calls=[ToolCall(name=TOOL_NAME, args=dict(TOOL_ARGS),
                                     call_id=f"loop-{self.planner_calls}")],
                finish_reason="tool_calls",
            )
            return
        yield StreamChunk(
            text='{"next": "planner", "reason": "loop", "briefing": ""}',
            finish_reason="stop",
        )


@pytest.mark.asyncio
async def test_max_turns_bounds_the_run(graph_env):
    executed = []
    provider = _LoopingProvider()
    events = await _collect(executor_module.run_langgraph_task(
        "loop forever please",
        provider,
        tools=_probe_registry(executed),
        task_id="task-minors-max-turns",
        request_id="req-minors-max-turns",
        session_id="minors-max-turns",
        max_turns=3,
        user_config=UserConfig(),
    ))
    failed = [ev for ev in events if ev.type == EventType.TASK_FAILED]
    assert failed, "a run that exceeds its turn budget must stop, not complete"
    assert "Recursion limit of 3" in failed[-1].payload["error"]
    assert EventType.TASK_COMPLETED not in [ev.type for ev in events]
    # The unbounded repro made four planner calls before anything
    # stopped it; a budget of 3 steps leaves room for one.
    assert provider.planner_calls <= 2


@pytest.mark.asyncio
async def test_max_turns_generous_budget_still_completes(graph_env):
    provider = _BoundaryProvider()
    provider.release_first_planner.set()  # never actually held here
    events = await _collect(executor_module.run_langgraph_task(
        "please run the probe",
        provider,
        tools=_probe_registry([]),
        task_id="task-minors-max-turns-ok",
        request_id="req-minors-max-turns-ok",
        session_id="minors-max-turns-ok",
        max_turns=25,
        user_config=UserConfig(),
    ))
    assert EventType.TASK_COMPLETED in [ev.type for ev in events]


# ---------------------------------------------------------------------------
# E1-F11 — plan mode disables supervisor spawns beyond the planner
# ---------------------------------------------------------------------------

def _denial_state(target, *, plan_mode):
    return {
        "messages": [
            HumanMessage(content="do the thing"),
            AIMessage(
                content="routing",
                tool_calls=[{"name": f"transfer_to_{target}", "args": {},
                             "id": f"call-{target}"}],
            ),
        ],
        "plan_mode": plan_mode,
    }


def test_plan_mode_denies_coder_spawn():
    denial = _spawn_policy_denial(
        _denial_state("coder", plan_mode=True), "coder",
        {"configurable": {"user_config": UserConfig()}})
    assert denial is not None
    assert "plan mode" in denial["delegation_log"][0]["decision"]["reason"]
    assert denial["delegation_log"][0]["decision"]["denied"] is True
    assert denial["active_specialist"] is None


def test_plan_mode_still_allows_the_planner():
    assert _spawn_policy_denial(
        _denial_state("planner", plan_mode=True), "planner",
        {"configurable": {"user_config": UserConfig()}}) is None


def test_no_plan_mode_no_spawn_denial():
    assert _spawn_policy_denial(
        _denial_state("coder", plan_mode=False), "coder",
        {"configurable": {"user_config": UserConfig()}}) is None


class _JsonRoutingProvider:
    """Supervisor answers with a JSON decision for a fixed target;
    specialists answer with plain text if ever reached."""

    def __init__(self, target):
        self.target = target
        self.nodes = []

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        self.nodes.append(node_name)
        if node_name in ("planner", "coder", "researcher"):
            yield StreamChunk(text=f"{node_name} did the work.",
                              finish_reason="stop")
            return
        yield StreamChunk(
            text=f'{{"next": "{self.target}", "reason": "routing", "briefing": ""}}',
            finish_reason="stop",
        )


@pytest.mark.asyncio
async def test_plan_mode_graph_never_reaches_the_coder(graph_env):
    provider = _JsonRoutingProvider("coder")
    events = await _collect(executor_module.run_langgraph_task(
        "write some code",
        provider,
        tools=ToolRegistry(),
        task_id="task-minors-plan-coder",
        request_id="req-minors-plan-coder",
        session_id="minors-plan-coder",
        plan_mode=True,
        user_config=UserConfig(),
    ))
    assert "coder" not in provider.nodes
    completed = [ev for ev in events if ev.type == EventType.TASK_COMPLETED]
    assert completed, "the task must still complete, with the denial as its answer"
    assert "plan mode" in completed[-1].payload.get("response", "")


@pytest.mark.asyncio
async def test_plan_mode_graph_still_reaches_the_planner(graph_env):
    provider = _JsonRoutingProvider("planner")
    events = await _collect(executor_module.run_langgraph_task(
        "plan the work",
        provider,
        tools=ToolRegistry(),
        task_id="task-minors-plan-planner",
        request_id="req-minors-plan-planner",
        session_id="minors-plan-planner",
        plan_mode=True,
        user_config=UserConfig(),
    ))
    assert "planner" in provider.nodes
    completed = [ev for ev in events if ev.type == EventType.TASK_COMPLETED]
    assert completed
    assert "planner did the work." in completed[-1].payload.get("response", "")


# ---------------------------------------------------------------------------
# E1-F12 — spawn-denied specialists are absent from native tool offers
# ---------------------------------------------------------------------------

def _dummy_tool(name):
    return StructuredTool.from_function(
        lambda: None, name=name, description=f"{name} tool")


def _offered_names(user_config):
    model = AetherSupervisorModel().bind_tools([
        _dummy_tool("transfer_to_planner"),
        _dummy_tool("transfer_to_coder"),
        _dummy_tool("transfer_to_researcher"),
        _dummy_tool("search_knowledge_graph"),
    ])
    config = {"configurable": {"user_config": user_config, "agent_tools": {}}}
    offered = model._offered_tools(config, [], ["researcher", "planner", "coder"])
    return {t["function"]["name"] for t in offered}


def test_denied_specialists_are_not_offered():
    names = _offered_names(UserConfig(supervisor_spawn_allowlist=["planner"]))
    assert names == {"transfer_to_planner", "search_knowledge_graph"}


def test_no_allowlist_offers_every_specialist():
    names = _offered_names(UserConfig())
    assert names == {
        "transfer_to_planner", "transfer_to_coder",
        "transfer_to_researcher", "search_knowledge_graph",
    }


# ---------------------------------------------------------------------------
# E1-F16 — THINKING events survive the approval-resume path
# ---------------------------------------------------------------------------

RUN_THINK = "RUN-THINK-SENTINEL"
RESUME_THINK = "RESUME-THINK-SENTINEL"


class _ResumeThinkingProvider:
    """Planner's first call requests the approval-gated probe tool
    (thinking as it goes); its post-resume call thinks a distinct
    sentinel before answering."""

    def __init__(self):
        self.planner_calls = 0

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        if node_name == "planner":
            self.planner_calls += 1
            if self.planner_calls == 1:
                yield StreamChunk(thinking=RUN_THINK, finish_reason=None)
                yield StreamChunk(
                    tool_calls=[ToolCall(name=TOOL_NAME, args=dict(TOOL_ARGS),
                                         call_id="call-resume-1")],
                    finish_reason="tool_calls",
                )
            else:
                yield StreamChunk(thinking=RESUME_THINK, finish_reason=None)
                yield StreamChunk(text="Resumed specialist answer.",
                                  finish_reason="stop")
            return
        yield StreamChunk(
            text='{"next": "planner", "reason": "resume routing", "briefing": ""}',
            finish_reason="stop",
        )


@pytest.mark.asyncio
async def test_thinking_events_survive_approval_resume(graph_env):
    executed = []
    registry = _probe_registry(executed)
    provider = _ResumeThinkingProvider()
    session_id = "minors-resume-thinking"
    user_config = UserConfig(tool_policies={TOOL_NAME: "Require Approval"})

    events = await _collect(executor_module.run_langgraph_task(
        "please run the probe",
        provider,
        tools=registry,
        task_id="task-minors-resume-think",
        request_id="req-minors-resume-think",
        session_id=session_id,
        user_config=user_config,
    ))
    assert EventType.TASK_INTERRUPTED in [ev.type for ev in events]
    # Control: the run path already forwards thinking.
    run_thinks = [ev for ev in events if ev.type == EventType.THINKING]
    assert any(ev.payload.get("text_delta") == RUN_THINK for ev in run_thinks)

    resume_events = await _collect(executor_module.resume_langgraph_task(
        session_id, True, provider, tools=registry))

    assert EventType.TASK_COMPLETED in [ev.type for ev in resume_events]
    assert executed == [TOOL_ARGS]
    resume_thinks = [ev for ev in resume_events if ev.type == EventType.THINKING]
    assert any(
        ev.payload.get("text_delta") == RESUME_THINK
        and ev.payload.get("node") == "planner"
        for ev in resume_thinks
    ), "the resumed specialist's thinking must reach the resume consumer"
