"""Epic 1, E1-13: prewalk.

A prewalk-enabled specialist plans on the run's (strong) provider and
switches to its per-agent model — the execution model from
agent_models — at the first write/execute tool that actually runs.
Plans that never write stay on the planning model. execute_tool sets
the write_tool_used state flag only on a successful non-READ_ONLY
execution (a denied or read-only call never flips the switch).
Agents not listed in prewalk_agents keep the E1-05 behaviour exactly.
"""
from types import SimpleNamespace

import pytest

from aether_engine.config import UserConfig
from aether_engine.langgraph.nodes import coder as coder_module
from aether_engine.langgraph.nodes.coder import coder_node
from aether_engine.langgraph.nodes.execute_tool import execute_tool_node
from aether_engine.orchestration import core as orchestration_core
from aether_engine.providers.base import StreamChunk
from aether_engine.tools.registry import ToolExecutionError, ToolRisk

# ── execute_tool flips the switch ────────────────────────────────────

class _FakeRegistry:
    def __init__(self, risk, fail=None):
        self._risk = risk
        self._fail = fail

    def get_tool(self, name):
        return SimpleNamespace(name=name, risk=self._risk)

    async def execute(self, name, args, **kwargs):
        if self._fail:
            raise self._fail
        return "done"


async def _run_tool(risk, fail=None):
    state = {
        "pending_tool_call": {"name": "probe", "args": {}, "call_id": "c1"},
        "active_specialist": "coder",
        "messages": [],
    }
    config = {"configurable": {"tool_registry": _FakeRegistry(risk, fail)}}
    return await execute_tool_node(state, config)


@pytest.mark.asyncio
async def test_write_execution_flips_the_switch():
    result = await _run_tool(ToolRisk.WRITE)
    assert result["write_tool_used"] is True


@pytest.mark.asyncio
async def test_execute_and_destructive_also_flip_it():
    assert (await _run_tool(ToolRisk.EXECUTE))["write_tool_used"] is True
    assert (await _run_tool(ToolRisk.DESTRUCTIVE))["write_tool_used"] is True


@pytest.mark.asyncio
async def test_read_only_execution_does_not_flip_it():
    result = await _run_tool(ToolRisk.READ_ONLY)
    assert "write_tool_used" not in result


@pytest.mark.asyncio
async def test_denied_write_does_not_flip_it():
    result = await _run_tool(ToolRisk.WRITE, fail=ToolExecutionError("blocked by policy (deny)"))
    assert "write_tool_used" not in result
    assert "blocked by user policy" in result["messages"][0]["content"]


# ── Specialist provider selection ────────────────────────────────────

class _NamedProvider:
    def __init__(self, label):
        self.label = label
        self.calls = 0

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        self.calls += 1
        yield StreamChunk(text=f"answer from {self.label}", finish_reason="stop")


@pytest.fixture
def providers(monkeypatch):
    strong = _NamedProvider("strong")
    fast = _NamedProvider("fast")
    factory_calls = []

    def _factory(p_name, m_name, secret_store, **_kwargs):
        # **_kwargs: the factory contract gained keyword-only
        # constraint params (local_only / require_vision, M17).
        factory_calls.append((p_name, m_name))
        return fast

    monkeypatch.setattr(orchestration_core, "_create_provider_instance", _factory)
    monkeypatch.setattr(coder_module, "save_artifact", lambda *a, **k: "prewalk/code.txt")
    return strong, fast, factory_calls


def _coder_config(strong, user_config):
    return {"configurable": {
        "provider": strong,
        "tool_registry": None,
        "agent_models": {"coder": "fastprov:fast-model"},
        "agent_tools": {},
        "secret_store": object(),
        "user_config": user_config,
    }}


def _coder_state(**overrides):
    state = {"messages": [{"role": "user", "content": "build it"}],
             "original_prompt": "build it", "task_id": "prewalk-test"}
    state.update(overrides)
    return state


@pytest.mark.asyncio
async def test_prewalk_plans_on_strong_then_switches_at_the_write(providers):
    strong, fast, factory_calls = providers
    cfg = _coder_config(strong, UserConfig(prewalk_agents=["coder"]))

    planning = await coder_node(_coder_state(), cfg)
    assert strong.calls == 1 and fast.calls == 0 and factory_calls == []
    assert planning["messages"][0]["content"] == "answer from strong"

    executing = await coder_node(_coder_state(write_tool_used=True), cfg)
    assert factory_calls == [("fastprov", "fast-model")]
    assert fast.calls == 1
    assert executing["messages"][0]["content"] == "answer from fast"


@pytest.mark.asyncio
async def test_without_prewalk_the_override_applies_immediately(providers):
    strong, _fast, factory_calls = providers
    cfg = _coder_config(strong, UserConfig())  # prewalk off
    result = await coder_node(_coder_state(), cfg)
    assert factory_calls == [("fastprov", "fast-model")]
    assert result["messages"][0]["content"] == "answer from fast"


def test_prewalk_agents_round_trip_through_config():
    cfg = UserConfig(prewalk_agents=["coder"])
    assert UserConfig.from_dict(cfg.to_dict()).prewalk_agents == ["coder"]
    assert UserConfig.from_dict({"prewalk_agents": "coder"}).prewalk_agents == []
