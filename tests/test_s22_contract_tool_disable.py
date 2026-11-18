"""S22 contract — tool Disable (SPEC-6B / Full Furnish D4-8).

A tool's Disable is a state of its own, distinct from its
approval policy (the D4-1 note "Disable ≠ policy" made real):

- storage: ``UserConfig.disabled_tools`` — a name list apart from
  ``tool_policies``, so disabling never rewrites the tool's
  approval setting and re-enabling restores it exactly; the
  project config layer may not set it (stripped with
  tool_policies);
- registry: ``build_tool_registry(disabled_tools=...)`` never
  registers a disabled tool — it is ABSENT from the registry the
  model sees, built-in and MCP alike — while a build without the
  set (the Settings display build) still contains it;
- policy: ``resolve_tool_policy`` / ``resolve_policy_by_name``
  resolve a disabled name to "deny" regardless of its approval
  setting — including "Always Allow" — and an enabled name is
  unaffected;
- wire: TOOL_ENABLED_SET_REQUEST updates the stored set (and
  only the set), TOOL_LIST_RESPONSE carries each tool's own
  ``enabled`` fact and its ``source`` ("builtin" / "mcp:<server>",
  the D4-2 badge data), and a disabled tool's listed
  ``effective_policy`` is "deny" while its stored ``policy`` is
  reported untouched.
"""

from __future__ import annotations

import asyncio

from aether_common.contracts import Event, EventType
from aether_engine.config import UserConfig
from aether_engine.tools.policy import (
    is_tool_disabled,
    resolve_policy_by_name,
    resolve_tool_policy,
)
from aether_engine.tools.registry import Tool, ToolRisk, build_tool_registry


def _mcp_tool(name: str = "ext_search") -> Tool:
    return Tool(
        name=name,
        description="An MCP-provided tool",
        risk=ToolRisk.READ_ONLY,
        source="mcp:extserver",
    )


# ---------------------------------------------------------------------------
# Storage: disabled_tools is its own config state
# ---------------------------------------------------------------------------


def test_disabled_tools_round_trip_without_touching_policies():
    cfg = UserConfig.from_dict({
        "tool_policies": {"read_file": "Always Allow"},
        "disabled_tools": ["read_file", "read_file", "wipe", 42, ""],
    })
    # De-duplicated, strings only, order kept.
    assert cfg.disabled_tools == ["read_file", "wipe"]
    assert cfg.tool_policies == {"read_file": "Always Allow"}
    assert cfg.to_dict()["disabled_tools"] == ["read_file", "wipe"]


def test_disabled_tools_defaults_to_none_disabled():
    assert UserConfig.from_dict({}).disabled_tools == []
    assert UserConfig.from_dict({"disabled_tools": "read_file"}).disabled_tools == []


def test_project_layer_cannot_set_disabled_tools():
    from aether_engine.config_layers import PROJECT_FORBIDDEN_KEYS

    assert "disabled_tools" in PROJECT_FORBIDDEN_KEYS


# ---------------------------------------------------------------------------
# Registry: a disabled tool is absent from the registry the model sees
# ---------------------------------------------------------------------------


def test_disabled_builtin_is_absent_from_model_registry():
    registry = build_tool_registry(disabled_tools=["read_file"])
    names = {t.name for t in registry.get_tools()}
    assert "read_file" not in names
    assert "get_current_time" in names  # siblings are untouched


def test_disabled_mcp_tool_is_absent_from_model_registry():
    registry = build_tool_registry(
        mcp_tools=[_mcp_tool()], disabled_tools=["ext_search"])
    assert all(t.name != "ext_search" for t in registry.get_tools())


def test_display_build_without_the_set_still_lists_the_tool():
    registry = build_tool_registry(mcp_tools=[_mcp_tool()])
    assert "ext_search" in {t.name for t in registry.get_tools()}
    assert "read_file" in {t.name for t in registry.get_tools()}


# ---------------------------------------------------------------------------
# Policy: disabled resolves to deny regardless of the approval setting
# ---------------------------------------------------------------------------


def test_disabled_tool_resolves_deny_despite_always_allow():
    tool = Tool(name="read_file", description="", risk=ToolRisk.READ_ONLY)
    policies = {"read_file": "Always Allow"}
    assert resolve_tool_policy(tool, policies) == "allow"  # control
    assert resolve_tool_policy(
        tool, policies, disabled_tools=["read_file"]) == "deny"
    assert resolve_policy_by_name(
        "read_file", policies, disabled_tools=["read_file"]) == "deny"


def test_disabled_tool_resolves_deny_despite_path_rule_allow(tmp_path):
    tool = Tool(name="write_file", description="", risk=ToolRisk.WRITE)
    rules = [{"prefix": str(tmp_path), "verdict": "Always Allow"}]
    args = {"path": str(tmp_path / "note.txt")}
    assert resolve_tool_policy(
        tool, {}, args=args, path_rules=rules,
        disabled_tools=["write_file"]) == "deny"


def test_enabled_neighbours_are_unaffected():
    assert is_tool_disabled("read_file", ["read_file"]) is True
    assert is_tool_disabled("read_file", ["wipe"]) is False
    assert is_tool_disabled("read_file", []) is False
    assert is_tool_disabled("read_file", None) is False
    assert resolve_policy_by_name(
        "get_current_time", {"get_current_time": "Always Allow"},
        disabled_tools=["read_file"]) == "allow"


# ---------------------------------------------------------------------------
# Wire: the enabled-set handler and the tool list payload
# ---------------------------------------------------------------------------


class _FakeWs:
    def __init__(self):
        self.sent: list[str] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(text)


class _FakeConn:
    def __init__(self):
        self.ws = _FakeWs()


def _run_enabled_set(monkeypatch, config, payload) -> dict:
    from aether_engine.routes import websocket as ws_routes
    from aether_engine.state import engine_state

    monkeypatch.setattr(engine_state, "user_config", config)
    monkeypatch.setattr(ws_routes, "_try_save_config", lambda: None)
    conn = _FakeConn()
    msg = Event(type=EventType.TOOL_ENABLED_SET_REQUEST,
                request_id="ena-1", payload=payload)
    asyncio.run(ws_routes._handle_tool_enabled_set_request(conn, msg))
    assert len(conn.ws.sent) == 1
    reply = Event.from_json(conn.ws.sent[0])
    assert reply.type == EventType.TOOL_ENABLED_SET_RESPONSE
    return reply.payload


def test_enabled_set_disable_then_enable_round_trip(monkeypatch):
    cfg = UserConfig.from_dict({"tool_policies": {"read_file": "Always Allow"}})

    reply = _run_enabled_set(
        monkeypatch, cfg, {"tool_name": "read_file", "enabled": False})
    assert reply == {"success": True, "tool_name": "read_file", "enabled": False}
    assert cfg.disabled_tools == ["read_file"]
    # The approval policy is NOT rewritten by disabling.
    assert cfg.tool_policies == {"read_file": "Always Allow"}

    reply = _run_enabled_set(
        monkeypatch, cfg, {"tool_name": "read_file", "enabled": True})
    assert reply == {"success": True, "tool_name": "read_file", "enabled": True}
    assert cfg.disabled_tools == []
    assert cfg.tool_policies == {"read_file": "Always Allow"}


def test_enabled_set_refuses_a_malformed_request(monkeypatch):
    cfg = UserConfig.from_dict({})
    reply = _run_enabled_set(monkeypatch, cfg, {"tool_name": "read_file"})
    assert reply["success"] is False
    assert cfg.disabled_tools == []
    reply = _run_enabled_set(
        monkeypatch, cfg, {"tool_name": "read_file", "enabled": "no"})
    assert reply["success"] is False
    assert cfg.disabled_tools == []


def _run_tool_list(monkeypatch, config) -> dict:
    from aether_engine.routes import websocket as ws_routes
    from aether_engine.state import engine_state

    monkeypatch.setattr(engine_state, "user_config", config)
    conn = _FakeConn()
    msg = Event(type=EventType.TOOL_LIST_REQUEST,
                request_id="list-1", payload={})
    asyncio.run(ws_routes._handle_tool_list_request(conn, msg))
    assert len(conn.ws.sent) == 1
    reply = Event.from_json(conn.ws.sent[0])
    assert reply.type == EventType.TOOL_LIST_RESPONSE
    return reply.payload


def test_tool_list_carries_enabled_and_source(monkeypatch):
    cfg = UserConfig.from_dict({
        "tool_policies": {"read_file": "Always Allow"},
        "disabled_tools": ["read_file"],
    })
    payload = _run_tool_list(monkeypatch, cfg)
    by_name = {d["function"]["name"]: d for d in payload["tools"]}

    # A disabled tool stays LISTED (Settings must be able to
    # re-enable it), carrying its own enabled fact, a "deny"
    # effective policy — and its stored policy untouched.
    read_file = by_name["read_file"]
    assert read_file["enabled"] is False
    assert read_file["effective_policy"] == "deny"
    assert read_file["policy"] == "Always Allow"
    assert read_file["source"] == "builtin"

    current_time = by_name["get_current_time"]
    assert current_time["enabled"] is True
    assert current_time["source"] == "builtin"
