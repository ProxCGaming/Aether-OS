"""Epic 3 minors batch — E3-F06 (discovery pagination), E3-F07
(capability negotiation), E3-F08 (disabled server cannot connect),
E3-F09 (failed refresh / shutdown clear cached tools), E3-F10
(denied-handoff / memory-step dropped names persist).

Each test drives the real seam — MCPClientManager with a scripted
session, the real MCPRegistry, the real supervisor post-model hook,
and the real message normalization — against the behaviour the audit
promised. The scripted sessions below stand in for the transport: they
implement exactly the ClientSession surface the manager uses
(list_* with pagination params + get_server_capabilities).
"""
import pytest
from mcp import types
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData

from aether_engine.mcp.client import MCPClientError, MCPClientManager
from aether_engine.mcp.registry import MCPRegistry

# -- scripted session ---------------------------------------------------


class FakeStack:
    def __init__(self):
        self.closed = False

    async def aclose(self):
        self.closed = True


def _caps(tools=False, resources=False, prompts=False):
    return types.ServerCapabilities(
        tools=types.ToolsCapability() if tools else None,
        resources=types.ResourcesCapability() if resources else None,
        prompts=types.PromptsCapability() if prompts else None,
    )


def _tool_page(names, next_cursor=None):
    return types.ListToolsResult(
        tools=[
            types.Tool(name=n, description="", inputSchema={"type": "object"})
            for n in names
        ],
        nextCursor=next_cursor,
    )


def _resource_page(uris, next_cursor=None):
    return types.ListResourcesResult(
        resources=[types.Resource(uri=u, name=u) for u in uris],
        nextCursor=next_cursor,
    )


def _prompt_page(names, next_cursor=None):
    return types.ListPromptsResult(
        prompts=[types.Prompt(name=n, description="") for n in names],
        nextCursor=next_cursor,
    )


class FakeSession:
    """A paginated, capability-declaring stand-in for ClientSession."""

    def __init__(
        self,
        *,
        capabilities=None,
        tool_pages=None,
        resource_pages=None,
        prompt_pages=None,
        list_errors=None,
    ):
        self._capabilities = capabilities
        self._pages = {
            "tools": tool_pages or {},
            "resources": resource_pages or {},
            "prompts": prompt_pages or {},
        }
        self.list_errors = list_errors or {}
        self.calls: list[tuple[str, str | None]] = []

    def get_server_capabilities(self):
        return self._capabilities

    async def _list(self, kind, cursor, params):
        cur = params.cursor if params is not None else cursor
        self.calls.append((kind, cur))
        if kind in self.list_errors:
            raise self.list_errors[kind]
        return self._pages[kind][cur]

    async def list_tools(self, cursor=None, *, params=None):
        return await self._list("tools", cursor, params)

    async def list_resources(self, cursor=None, *, params=None):
        return await self._list("resources", cursor, params)

    async def list_prompts(self, cursor=None, *, params=None):
        return await self._list("prompts", cursor, params)


@pytest.fixture()
def manager():
    return MCPClientManager()


def _register(manager, session, name="srv"):
    manager._sessions[name] = session
    manager._set_status(name, "connected", tool_count=1)
    return session


def _patch_open(manager, session):
    stack = FakeStack()

    async def _open(config):
        return stack, session

    manager._open_session = _open
    return stack


# -- E3-F06: discovery follows pagination cursors ------------------------


async def test_f06_list_tools_follows_pagination(manager):
    session = _register(
        manager,
        FakeSession(
            capabilities=_caps(tools=True),
            tool_pages={None: _tool_page(["page_one"], "c2"), "c2": _tool_page(["page_two"])},
        ),
    )
    tools = await manager.list_tools("srv")
    assert [t.name for t in tools] == ["page_one", "page_two"]
    assert ("tools", "c2") in session.calls


async def test_f06_list_resources_follows_pagination(manager):
    session = _register(
        manager,
        FakeSession(
            capabilities=_caps(resources=True),
            resource_pages={
                None: _resource_page(["fake://one"], "c2"),
                "c2": _resource_page(["fake://two"]),
            },
        ),
    )
    resources = await manager.list_resources("srv")
    assert [r["uri"] for r in resources] == ["fake://one", "fake://two"]
    assert ("resources", "c2") in session.calls


async def test_f06_list_prompts_follows_pagination(manager):
    session = _register(
        manager,
        FakeSession(
            capabilities=_caps(prompts=True),
            prompt_pages={None: _prompt_page(["one"], "c2"), "c2": _prompt_page(["two"])},
        ),
    )
    prompts = await manager.list_prompts("srv")
    assert [p["name"] for p in prompts] == ["one", "two"]
    assert ("prompts", "c2") in session.calls


async def test_f06_connect_counts_tools_across_pages(manager):
    session = FakeSession(
        capabilities=_caps(tools=True),
        tool_pages={None: _tool_page(["a"], "c2"), "c2": _tool_page(["b"])},
    )
    _patch_open(manager, session)
    result = await manager.connect("srv", {"transport": "stdio", "command": "x"})
    assert result["ok"] is True, result
    assert manager.get_status("srv")["tool_count"] == 2
    await manager.disconnect("srv")


async def test_f06_test_connection_lists_all_pages(manager):
    session = FakeSession(
        capabilities=_caps(tools=True),
        tool_pages={None: _tool_page(["a"], "c2"), "c2": _tool_page(["b"])},
    )
    _patch_open(manager, session)
    result = await manager.test_connection("srv", {"transport": "stdio", "command": "x"})
    assert result["ok"] is True, result
    assert result["tools"] == ["a", "b"]


# -- E3-F07: capability negotiation is honoured ---------------------------


async def test_f07_resources_only_server_connects(manager):
    """A server that never advertised tools must connect: tools/list is
    not a condition of a healthy connection."""
    session = FakeSession(capabilities=_caps(resources=True))
    _patch_open(manager, session)
    result = await manager.connect("srv", {"transport": "stdio", "command": "x"})
    assert result["ok"] is True, result
    assert manager.get_status("srv")["status"] == "connected"
    assert manager.get_status("srv")["tool_count"] == 0
    assert not any(kind == "tools" for kind, _ in session.calls)
    await manager.disconnect("srv")


async def test_f07_resources_only_server_passes_test_connection(manager):
    session = FakeSession(capabilities=_caps(resources=True))
    _patch_open(manager, session)
    result = await manager.test_connection("srv", {"transport": "stdio", "command": "x"})
    assert result["ok"] is True, result
    assert result["tools"] == []


async def test_f07_unadvertised_resources_list_is_empty_not_failure(manager):
    session = _register(manager, FakeSession(capabilities=_caps(tools=True)))
    assert await manager.list_resources("srv") == []
    # The server is healthy; an unadvertised optional capability must
    # not mark it failed, and the request must not even be sent.
    assert manager.get_status("srv")["status"] == "connected"
    assert not any(kind == "resources" for kind, _ in session.calls)


async def test_f07_unadvertised_prompts_list_is_empty_not_failure(manager):
    session = _register(manager, FakeSession(capabilities=_caps(tools=True)))
    assert await manager.list_prompts("srv") == []
    assert manager.get_status("srv")["status"] == "connected"
    assert not any(kind == "prompts" for kind, _ in session.calls)


async def test_f07_method_not_found_list_is_empty_not_failure(manager):
    """A server that declares resources but answers method-not-found
    (subset implementations do) gets an empty list, not a failure."""
    err = McpError(ErrorData(code=types.METHOD_NOT_FOUND, message="Method not found"))
    _register(
        manager,
        FakeSession(
            capabilities=_caps(tools=True, resources=True),
            list_errors={"resources": err},
        ),
    )
    assert await manager.list_resources("srv") == []
    assert manager.get_status("srv")["status"] == "connected"


async def test_f07_method_not_found_wrapped_in_group_is_still_tolerated(manager):
    err = McpError(ErrorData(code=types.METHOD_NOT_FOUND, message="Method not found"))
    _register(
        manager,
        FakeSession(
            capabilities=_caps(tools=True, prompts=True),
            list_errors={"prompts": BaseExceptionGroup("sdk", [err])},
        ),
    )
    assert await manager.list_prompts("srv") == []
    assert manager.get_status("srv")["status"] == "connected"


async def test_f07_genuine_list_error_still_fails_the_server(manager):
    """Control: a real list failure (not a missing capability) keeps
    the existing behaviour — raise, mark failed."""
    _register(
        manager,
        FakeSession(
            capabilities=_caps(tools=True, resources=True),
            list_errors={"resources": RuntimeError("boom")},
        ),
    )
    with pytest.raises(MCPClientError):
        await manager.list_resources("srv")
    assert manager.get_status("srv")["status"] == "failed"


async def test_f07_server_declaring_nothing_keeps_legacy_behaviour(manager):
    """Control: a raw-protocol server that declares NO capabilities at
    all (the stall/limits fixtures advertise ``{}``) is not gated —
    its lists are still exercised, so a stall still fails loudly."""
    session = _register(
        manager,
        FakeSession(
            capabilities=_caps(),  # declares nothing
            resource_pages={None: _resource_page(["fake://one"])},
        ),
    )
    resources = await manager.list_resources("srv")
    assert [r["uri"] for r in resources] == ["fake://one"]
    assert ("resources", None) in session.calls


# -- E3-F08 / F09: registry seams ------------------------------------------

_STDIO = {"command": "python", "args": ["-m", "fake"]}


@pytest.fixture()
def registry(tmp_path):
    return MCPRegistry(
        config_path=tmp_path / "mcp_servers.json", client=MCPClientManager()
    )


def _registry_session(registry, session):
    return _patch_open(registry._client, session)


async def test_f08_disabled_server_cannot_connect(registry):
    registry.add_server("srv", {**_STDIO, "enabled": False})
    opened = []

    async def _open(config):
        opened.append(config)
        return FakeStack(), FakeSession(
            capabilities=_caps(tools=True), tool_pages={None: _tool_page(["probe"])}
        )

    registry._client._open_session = _open

    result = await registry.connect_server("srv")
    assert result["ok"] is False
    assert "disabled" in result["error"]
    result_owned = await registry.connect_server_owned("srv")
    assert result_owned["ok"] is False
    assert "disabled" in result_owned["error"]
    # Never attempted, still displayed disabled, nothing to execute.
    assert opened == []
    assert registry.get_server_status("srv")["status"] == "disabled"
    assert registry.get_mcp_tools() == []


async def test_f08_enabled_server_still_connects(registry):
    registry.add_server("srv", _STDIO)
    _registry_session(
        registry,
        FakeSession(
            capabilities=_caps(tools=True), tool_pages={None: _tool_page(["probe"])}
        ),
    )
    result = await registry.connect_server("srv")
    assert result["ok"] is True, result
    assert [t.name for t in registry.get_mcp_tools()] == ["srv__probe"]
    await registry.disconnect_server("srv")


async def test_f09_failed_refresh_clears_cached_tools(registry):
    registry.add_server("flaky", _STDIO)
    session = FakeSession(
        capabilities=_caps(tools=True), tool_pages={None: _tool_page(["probe"])}
    )
    _registry_session(registry, session)
    result = await registry.connect_server("flaky")
    assert result["ok"] is True, result
    assert [t.name for t in registry.get_mcp_tools()] == ["flaky__probe"]

    # The audit's repro: a later refresh fails after a good connection.
    session.list_errors["tools"] = RuntimeError("list blew up")
    result = await registry.connect_server("flaky")
    assert result["ok"] is False
    assert registry.get_mcp_tools() == []


async def test_f09_shutdown_clears_cached_tools(registry):
    registry.add_server("autostop", _STDIO)
    _registry_session(
        registry,
        FakeSession(
            capabilities=_caps(tools=True), tool_pages={None: _tool_page(["probe"])}
        ),
    )
    result = await registry.connect_server("autostop")
    assert result["ok"] is True, result
    assert [t.name for t in registry.get_mcp_tools()] == ["autostop__probe"]

    await registry.stop_auto_connect()
    assert registry.get_mcp_tools() == []
    assert registry._client.get_status("autostop")["status"] == "disconnected"


# -- E3-F10: dropped names persist through denial / memory steps ----------


def _memory_message(dropped=None):
    from langchain_core.messages import AIMessage

    kwargs = {}
    if dropped is not None:
        kwargs["aether_dropped_tool_calls"] = dropped
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": "get_current_chat_history",
                "args": {"limit": 3},
                "id": "m1",
                "type": "tool_call",
            }
        ],
        additional_kwargs=kwargs,
    )


def test_f10_memory_step_dropped_names_reach_delegation_log():
    from aether_engine.langgraph.supervisor_model import supervisor_post_model_hook

    update = supervisor_post_model_hook(
        {"messages": [_memory_message(dropped=["write_file"])]}
    )
    entries = update.get("delegation_log") or []
    assert entries, "a memory step that dropped calls must record them in the log"
    assert entries[0]["decision"]["dropped_tool_calls"] == ["write_file"]


def test_f10_memory_step_without_drops_still_changes_nothing():
    from aether_engine.langgraph.supervisor_model import supervisor_post_model_hook

    assert supervisor_post_model_hook({"messages": [_memory_message()]}) == {}


def test_f10_denied_handoff_keeps_dropped_names_in_log():
    from langchain_core.messages import AIMessage

    from aether_engine.langgraph.supervisor_model import supervisor_post_model_hook

    msg = AIMessage(
        content="",
        tool_calls=[
            {
                "name": "transfer_to_coder",
                "args": {},
                "id": "h1",
                "type": "tool_call",
            }
        ],
        additional_kwargs={
            "aether_decision": {
                "next": "coder",
                "reason": "go",
                "briefing": "",
                "dropped_tool_calls": ["execute_shell"],
            }
        },
    )
    # plan_mode denies every non-planner handoff — the deterministic
    # denial path (no config needed).
    update = supervisor_post_model_hook({"messages": [msg], "plan_mode": True})
    entries = update.get("delegation_log") or []
    assert entries and entries[0]["decision"].get("denied") is True
    assert entries[0]["decision"]["dropped_tool_calls"] == ["execute_shell"]


def test_f10_message_normalization_keeps_dropped_names():
    from aether_engine.langgraph.state import to_aether_message

    converted = to_aether_message(_memory_message(dropped=["write_file"]))
    assert converted["aether_dropped_tool_calls"] == ["write_file"]


def test_f10_message_normalization_without_drops_adds_no_key():
    from langchain_core.messages import AIMessage

    from aether_engine.langgraph.state import to_aether_message

    converted = to_aether_message(AIMessage(content="hello"))
    assert "aether_dropped_tool_calls" not in converted
