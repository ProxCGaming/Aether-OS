"""Epic 1, E1-09: plan mode is enforced by absence, not by refusal.

In plan mode a specialist's tool list contains only READ_ONLY tools —
write/execute/destructive tools (and MCP tools, EXECUTE by default) are
never offered to the model. As a backstop, execute_tool refuses a risky
pending call if one ever appears in plan-mode state. Outside plan mode
nothing changes.
"""
from types import SimpleNamespace

import pytest

from aether_engine.langgraph.nodes import planner as planner_module
from aether_engine.langgraph.nodes.execute_tool import execute_tool_node
from aether_engine.tools.registry import build_tool_registry


class StubProvider:
    def __init__(self, text="ok"):
        self.text = text
        self.seen_tools = None

    async def call_stream(self, messages, tools=None, node_name=None):
        self.seen_tools = tools
        yield SimpleNamespace(text=self.text, tool_calls=None, finish_reason="stop")


def _state(**overrides):
    state = {
        "original_prompt": "Investigate the repo",
        "messages": [{"role": "user", "content": "Investigate the repo"}],
        "plan": "",
        "current_phase": "",
        "artifact_paths": {},
        "tool_results_summary": {},
        "pending_tool_call": None,
        "active_specialist": None,
        "tool_call_signature": None,
        "tool_call_repeats": 0,
        "delegation_log": [],
        "task_id": "pm-test",
        "session_id": "pm-session",
        "context_briefing": None,
        "active_skills": [],
        "blackboard": {},
        "plan_mode": False,
    }
    state.update(overrides)
    return state


def _config(provider, registry):
    return {"configurable": {"provider": provider, "tool_registry": registry, "agent_tools": {}}}


@pytest.mark.asyncio
async def test_plan_mode_offers_only_read_only_tools(monkeypatch):
    monkeypatch.setattr(planner_module, "save_artifact", lambda *a, **k: "/tmp/p.txt")
    registry = build_tool_registry()  # builtins: time(RO), file write/delete/shell, web search(RO), scratchpad(RO)
    provider = StubProvider("a plan")
    await planner_module.planner_node(_state(plan_mode=True), _config(provider, registry))
    names = {t["function"]["name"] for t in provider.seen_tools}
    assert names == {"get_current_time", "search_web", "scratchpad_render"}


@pytest.mark.asyncio
async def test_normal_mode_offers_everything(monkeypatch):
    monkeypatch.setattr(planner_module, "save_artifact", lambda *a, **k: "/tmp/p.txt")
    registry = build_tool_registry()
    provider = StubProvider("a plan")
    await planner_module.planner_node(_state(plan_mode=False), _config(provider, registry))
    names = {t["function"]["name"] for t in provider.seen_tools}
    assert {"write_file", "delete_file", "execute_shell"} <= names


class CountingRegistry:
    def __init__(self, tool):
        self._tool = tool
        self.calls = 0

    def get_tool(self, name):
        return self._tool

    async def execute(self, name, args, **kwargs):
        self.calls += 1
        return "executed"


@pytest.mark.asyncio
async def test_execute_tool_backstop_refuses_write_in_plan_mode():
    from aether_engine.tools.registry import Tool, ToolRisk

    registry = CountingRegistry(Tool(name="write_file", description="", risk=ToolRisk.WRITE))
    state = _state(
        plan_mode=True,
        pending_tool_call={"name": "write_file", "args": {"path": "x"}, "call_id": "c1"},
    )
    config = {"configurable": {"tool_registry": registry, "agent_tools": {}, "user_config": None}}
    result = await execute_tool_node(state, config)
    assert registry.calls == 0
    assert "plan mode" in result["messages"][0]["content"]


@pytest.mark.asyncio
async def test_execute_tool_backstop_allows_read_only_in_plan_mode():
    from aether_engine.tools.registry import Tool, ToolRisk

    registry = CountingRegistry(Tool(name="search_web", description="", risk=ToolRisk.READ_ONLY))
    state = _state(
        plan_mode=True,
        pending_tool_call={"name": "search_web", "args": {"query": "q"}, "call_id": "c2"},
    )
    config = {"configurable": {"tool_registry": registry, "agent_tools": {}, "user_config": None}}
    result = await execute_tool_node(state, config)
    assert registry.calls == 1
    assert result["messages"][0]["content"] == "executed"
