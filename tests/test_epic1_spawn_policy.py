"""Epic 1, E1-11: supervisor spawn policy + delegation cap.

The supervisor may only delegate to specialists on the configured
allow-list, and only up to the per-turn delegation cap. Enforcement
lives in the supervisor's post-model hook, at the last moment before a
handoff would execute: a denied handoff is stripped, logged as denied,
and explained as the run's final message — never silently dropped and
never executed. With no policy configured, routing is unchanged.
"""
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from aether_common.contracts import EventType
from aether_engine.config import UserConfig
from aether_engine.langgraph import executor as executor_module
from aether_engine.langgraph.nodes import planner as planner_module
from aether_engine.langgraph.supervisor_model import _spawn_policy_denial
from aether_engine.memory import episodic as episodic_module
from aether_engine.memory import knowledge_graph as kg_module
from aether_engine.providers.base import StreamChunk
from aether_engine.tools.registry import ToolRegistry


def _config(user_config):
    return {"configurable": {"user_config": user_config}}


def _handoff_message(target, content="routing"):
    return AIMessage(
        content=content,
        tool_calls=[{"name": f"transfer_to_{target}", "args": {}, "id": f"call-{target}"}],
    )


def _state(*messages):
    return {"messages": list(messages)}


# ── Config ───────────────────────────────────────────────────────────

def test_spawn_policy_defaults_and_round_trip():
    cfg = UserConfig()
    assert cfg.supervisor_spawn_allowlist is None
    assert cfg.supervisor_delegation_cap == 8

    custom = UserConfig(supervisor_spawn_allowlist=["planner"], supervisor_delegation_cap=3)
    restored = UserConfig.from_dict(custom.to_dict())
    assert restored.supervisor_spawn_allowlist == ["planner"]
    assert restored.supervisor_delegation_cap == 3

    corrupt = UserConfig.from_dict({"supervisor_spawn_allowlist": "planner",
                                    "supervisor_delegation_cap": "soon"})
    assert corrupt.supervisor_spawn_allowlist is None
    assert corrupt.supervisor_delegation_cap == 8


# ── Policy helper ────────────────────────────────────────────────────

def test_allowlist_denies_an_unlisted_specialist():
    state = _state(HumanMessage(content="build it"), _handoff_message("coder"))
    denial = _spawn_policy_denial(state, "coder", _config(UserConfig(supervisor_spawn_allowlist=["planner"])))
    assert denial is not None
    (stripped,) = denial["messages"]  # one replacement message, never a batch
    assert not stripped.tool_calls  # the transfer is gone
    assert "blocked by the spawn policy" in stripped.content
    assert denial["delegation_log"][0]["decision"]["denied"] is True
    assert denial["active_specialist"] is None


def test_allowlist_allows_a_listed_specialist():
    state = _state(HumanMessage(content="plan it"), _handoff_message("planner"))
    assert _spawn_policy_denial(state, "planner", _config(UserConfig(supervisor_spawn_allowlist=["planner"]))) is None


def test_no_policy_configured_allows_everything():
    state = _state(HumanMessage(content="build it"), _handoff_message("coder"))
    assert _spawn_policy_denial(state, "coder", _config(UserConfig())) is None
    assert _spawn_policy_denial(state, "coder", {"configurable": {}}) is None


def test_delegation_cap_counts_this_turns_handoffs():
    prior = _handoff_message("planner")
    pending = _handoff_message("coder")
    state = _state(HumanMessage(content="multi-step"), prior, AIMessage(content="planned"), pending)
    # One prior delegation + the pending one = 2 this turn.
    assert _spawn_policy_denial(state, "coder", _config(UserConfig(supervisor_delegation_cap=2))) is None
    denial = _spawn_policy_denial(state, "coder", _config(UserConfig(supervisor_delegation_cap=1)))
    assert denial is not None
    assert "delegation cap" in denial["delegation_log"][0]["decision"]["reason"]


def test_delegation_cap_resets_on_a_new_user_turn():
    msgs = [HumanMessage(content="turn one"), _handoff_message("planner"),
            AIMessage(content="done"), HumanMessage(content="turn two"),
            _handoff_message("coder")]
    state = _state(*msgs)
    assert _spawn_policy_denial(state, "coder", _config(UserConfig(supervisor_delegation_cap=1))) is None


# ── End-to-end through the graph ─────────────────────────────────────

class _RoutingProvider:
    """Supervisor always delegates to coder; coder answers if reached."""

    def __init__(self):
        self.nodes = []

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        self.nodes.append(node_name)
        if node_name == "coder":
            yield StreamChunk(text="Coder did the work.", finish_reason="stop")
            return
        yield StreamChunk(
            text='{"next": "coder", "reason": "needs code", "briefing": ""}',
            finish_reason="stop")


@pytest.fixture
def graph_env(tmp_path, monkeypatch):
    monkeypatch.setattr(
        executor_module, "DEFAULT_CHECKPOINT_DB", tmp_path / "checkpoints.db")
    monkeypatch.setattr(episodic_module, "store_episode", AsyncMock(return_value=None))
    monkeypatch.setattr(kg_module, "extract_and_store_facts", AsyncMock(return_value=None))
    monkeypatch.setattr(planner_module, "save_artifact", lambda *a, **k: "spawn/artifact.txt")
    return tmp_path


async def _run(provider, user_config, session_id):
    events = [ev async for ev in executor_module.run_langgraph_task(
        "please write some code",
        provider,
        tools=ToolRegistry(),
        task_id=f"task-{session_id}",
        request_id=f"req-{session_id}",
        session_id=session_id,
        user_config=user_config,
    )]
    return events


@pytest.mark.asyncio
async def test_denied_delegation_never_reaches_the_specialist(graph_env):
    provider = _RoutingProvider()
    events = await _run(provider, UserConfig(supervisor_spawn_allowlist=["planner"]), "spawn-deny")
    assert "coder" not in provider.nodes
    completed = [ev for ev in events if ev.type == EventType.TASK_COMPLETED]
    assert completed, "the task must still complete, with the denial as its answer"
    assert "spawn policy" in completed[-1].payload.get("response", "")


@pytest.mark.asyncio
async def test_allowed_delegation_runs_as_before(graph_env):
    provider = _RoutingProvider()
    events = await _run(provider, UserConfig(), "spawn-allow")
    assert "coder" in provider.nodes
    completed = [ev for ev in events if ev.type == EventType.TASK_COMPLETED]
    assert completed
    assert "Coder did the work." in completed[-1].payload.get("response", "")
