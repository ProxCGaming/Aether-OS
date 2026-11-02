"""E3-F03: manual MCP sessions lack a stable owner across WebSockets
and the shutdown hook.

The websocket Connect handler connects a server by calling
``MCPRegistry.connect_server`` from the socket's own handler task.
That used to open the session's AnyIO stack *in the handler task*
(``MCPClientManager.connect`` -> ``_open_and_register``), while only
auto-connect sessions got a dedicated owner task
(``connect_owned``). Consequences, reproduced by the full-product
review (``checks/epic3-ownership-integration-results.json`` and
``checks/epic3-socket_exit-integration-results.json``):

- a second socket disconnecting the server closed the stack from
  the wrong task: "Attempted to exit cancel scope in a different
  task than it was entered in", and the first socket's handler was
  cancelled even though disconnect reported success;
- an ordinary socket close left the session live and executable
  with no owner task, and the app shutdown hook — which only closes
  owner-task sessions (``stop_auto_connect`` -> ``shutdown_owned``)
  — never closed it, leaving a running stdio child behind.

The invariant these tests pin: every session registered with the
manager has exactly one owner — a dedicated task that opened its
stack and is the only task that ever closes it. Sockets, handler
tasks and shutdown merely *signal* the owner. Auto-connect sessions
already behaved this way (tests/test_mcp_auto_connect.py); manual
(Connect-button) sessions must behave identically.
"""
import asyncio
import contextlib
import logging
import subprocess
import sys
import time

import pytest

from aether_engine.mcp.client import MCPClientError


def _registry(tmp_path):
    from aether_engine.mcp.registry import MCPRegistry

    return MCPRegistry(config_path=tmp_path / "mcp_servers.json")


def _stdio_config(**overrides):
    cfg = {
        "transport": "stdio",
        "command": sys.executable,
        "args": ["tests/fake_stdio_server.py"],
        "timeout": 10,
    }
    cfg.update(overrides)
    return cfg


def _fixture_pids() -> set[int]:
    """PIDs of running fake-stdio-server children (leak detector)."""
    out = subprocess.run(
        ["pgrep", "-f", "fake_stdio_server.py"],
        capture_output=True, text=True, check=False,
    )
    if out.returncode != 0:
        return set()
    return {int(p) for p in out.stdout.split()}


async def _wait_for(predicate, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return predicate()


async def _shutdown_quietly(registry):
    # Test cleanup must never mask the result under test.
    with contextlib.suppress(Exception):
        await registry.disconnect_server("local")
    with contextlib.suppress(Exception):
        await registry.stop_auto_connect()


async def test_disconnect_from_another_task_does_not_harm_the_connecting_task(
    tmp_path, caplog
):
    """Review manifestation 1: socket 1 connects, socket 2 disconnects.

    Pre-fix the disconnect closed the session stack from the wrong
    task: the cross-task cancel-scope error was logged, the close
    aborted part-way (child left running), and in the real WS probe
    the first handler task was cancelled — all while the disconnect
    reported success.
    """
    registry = _registry(tmp_path)
    registry.add_server("local", _stdio_config())
    baseline_pids = _fixture_pids()
    park = asyncio.Event()
    state: dict = {}

    async def socket_one_handler():
        # Exactly what the WS Connect handler does, then the handler
        # parks the way a live socket handler parks on receive.
        state["connect"] = await registry.connect_server("local")
        try:
            await park.wait()
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise

    socket_task = asyncio.create_task(socket_one_handler())
    try:
        # Wait for the connect CALL to return, not just for the
        # session to register: the owner task registers the session
        # before the caller task resumes and records the result, so
        # is_connected alone races the state write under load.
        assert await _wait_for(lambda: "connect" in state)
        assert state["connect"]["ok"] is True, state["connect"]
        tools = {t.name: t for t in registry.get_mcp_tools()}
        assert await tools["local__add"].execute_fn(a=3, b=5) == "8"
        assert _fixture_pids() - baseline_pids, "stdio child did not start"

        with caplog.at_level(logging.WARNING, logger="aether_engine.mcp.client"):
            # A *different* task, like a second socket's handler.
            result = await asyncio.create_task(registry.disconnect_server("local"))

        assert result["ok"] is True, result
        assert not registry._client.is_connected("local")
        assert registry._client.get_status("local")["status"] == "disconnected"
        # The harm signals: no cross-task cancel-scope close was
        # attempted, the connecting task was not cancelled, and the
        # stdio child is actually gone (not leaked by a half-close).
        assert "different task" not in caplog.text, caplog.text
        assert not socket_task.done(), "connecting task was torn down by disconnect"
        assert state.get("cancelled") is not True
        assert await _wait_for(lambda: not (_fixture_pids() - baseline_pids)), (
            "stdio child leaked after disconnect"
        )
    finally:
        park.set()
        try:
            await asyncio.wait_for(socket_task, timeout=5)
        except (TimeoutError, asyncio.CancelledError):
            socket_task.cancel()
        await _shutdown_quietly(registry)


async def test_shutdown_hook_closes_manual_sessions(tmp_path):
    """Review manifestation 2: the app shutdown hook must close a
    manually connected session too.

    ``app.py``'s lifespan exit calls ``stop_auto_connect()`` and
    nothing else for MCP. Pre-fix that only signalled owner-task
    (auto-connect) sessions, so a manual session stayed live,
    executable, and its stdio child kept running past shutdown.
    """
    registry = _registry(tmp_path)
    registry.add_server("local", _stdio_config())
    baseline_pids = _fixture_pids()
    try:
        result = await registry.connect_server("local")
        assert result["ok"] is True, result
        assert registry._client.is_connected("local")
        out = await registry._client.call_tool("local", "add", {"a": 3, "b": 5})
        assert out.strip() == "8"

        # The exact call the app shutdown hook makes.
        await registry.stop_auto_connect()

        assert not registry._client.is_connected("local"), (
            "manual session survived the shutdown hook"
        )
        assert registry._client.get_status("local")["status"] == "disconnected"
        with pytest.raises(MCPClientError):
            await registry._client.call_tool("local", "add", {"a": 1, "b": 2})
        assert await _wait_for(lambda: not (_fixture_pids() - baseline_pids)), (
            "stdio child leaked past shutdown"
        )
    finally:
        await _shutdown_quietly(registry)


async def test_session_survives_connecting_task_death_and_disconnects_cleanly(
    tmp_path, caplog
):
    """Review manifestation 2 (socket-exit variant): when the socket
    that connected a server goes away, its handler task is torn
    down. The session must not be orphaned with that task — it
    belongs to the registry and stays usable until an explicit
    disconnect, which must then close it cleanly from any task.
    """
    registry = _registry(tmp_path)
    registry.add_server("local", _stdio_config())
    baseline_pids = _fixture_pids()

    async def socket_handler():
        result = await registry.connect_server("local")
        assert result["ok"] is True, result
        await asyncio.sleep(3600)  # parked on receive; socket dies below

    socket_task = asyncio.create_task(socket_handler())
    try:
        assert await _wait_for(lambda: registry._client.is_connected("local"))
        # The socket drops: its handler task is cancelled by the
        # server framework, with no MCP-specific teardown (there is
        # none in the WebSocketDisconnect path).
        socket_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await socket_task

        # Not orphaned: still registered, still owned, still usable.
        assert registry._client.is_connected("local")
        out = await registry._client.call_tool("local", "add", {"a": 3, "b": 5})
        assert out.strip() == "8"

        with caplog.at_level(logging.WARNING, logger="aether_engine.mcp.client"):
            result = await registry.disconnect_server("local")
        assert result["ok"] is True, result
        assert not registry._client.is_connected("local")
        assert "different task" not in caplog.text, caplog.text
        assert await _wait_for(lambda: not (_fixture_pids() - baseline_pids)), (
            "stdio child leaked after disconnect"
        )
    finally:
        if not socket_task.done():
            socket_task.cancel()
        await _shutdown_quietly(registry)


async def test_manual_connect_registers_exactly_one_owner(tmp_path):
    """Ownership is explicit: a manual connect leaves exactly one
    live owner task for the session (the same shape auto-connect
    always had), a repeat connect creates no second owner, and a
    disconnect retires the owner completely."""
    registry = _registry(tmp_path)
    registry.add_server("local", _stdio_config())
    manager = registry._client
    try:
        result = await registry.connect_server("local")
        assert result["ok"] is True, result
        owner = manager._owner_tasks.get("local")
        assert owner is not None and not owner.done(), (
            "manual session has no owner task"
        )

        again = await registry.connect_server("local")
        assert again["ok"] is True, again
        assert manager._owner_tasks.get("local") is owner, (
            "repeat connect replaced the session's owner"
        )

        await registry.disconnect_server("local")
        assert manager._owner_tasks.get("local") is None
        assert manager._close_events.get("local") is None
        assert not manager.is_connected("local")
    finally:
        await _shutdown_quietly(registry)
