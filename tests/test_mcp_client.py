"""Issue 001: raw MCP SDK client — Streamable HTTP only.

TDD red first: these tests fail until aether_engine/mcp/client.py exists.
The fake HTTP server (tests/fake_mcp_http.py) exposes one tool, ``add``.
"""
import os

import pytest

from tests.fake_mcp_http import start_fake_http_server


@pytest.fixture(scope="module")
def fake_url():
    url, stop = start_fake_http_server()
    yield url
    stop()


@pytest.fixture()
def manager():
    from aether_engine.mcp.client import MCPClientManager

    return MCPClientManager()


async def test_connect_lists_add_tool_and_calls_it(fake_url, manager):
    config = {"transport": "streamable_http", "url": fake_url, "timeout": 10}
    assert manager.get_status("fake")["status"] == "disconnected"
    result = await manager.connect("fake", config)
    assert result["ok"] is True, result
    assert manager.get_status("fake")["status"] == "connected"

    tools = await manager.list_tools("fake")
    assert [t.name for t in tools] == ["add"]

    out = await manager.call_tool("fake", "add", {"a": 3, "b": 5})
    assert out.strip() == "8"

    await manager.disconnect("fake")
    assert manager.get_status("fake")["status"] == "disconnected"


async def test_test_connection_returns_tools_without_leaving_session(fake_url, manager):
    config = {"transport": "streamable_http", "url": fake_url, "timeout": 10}
    result = await manager.test_connection("fake", config)
    assert result["ok"] is True
    assert result["tools"] == ["add"]
    assert manager.get_status("fake")["status"] == "disconnected"


async def test_call_on_disconnected_server_fails_gracefully(manager):
    from aether_engine.mcp.client import MCPClientError

    with pytest.raises(MCPClientError) as ctx:
        await manager.call_tool("ghost", "add", {"a": 1, "b": 2})
    assert "ghost" in str(ctx.value)


async def test_dead_server_blackhole_fails_in_one_timeout(manager):
    """One attempt only: a black-hole server (accepts TCP, never answers)
    with a 3s timeout must fail in ~3s, not ~9s (3 retries)."""
    import socket
    import threading
    import time

    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    port = listener.getsockname()[1]
    stop = threading.Event()

    def _accept_forever():
        while not stop.is_set():
            try:
                listener.settimeout(0.2)
                conn, _ = listener.accept()
                # Hold the connection open, never answer: one attempt burns
                # the whole timeout, a retry would burn it again.
                conn.settimeout(5)
                try:
                    conn.recv(4096)
                except OSError:
                    pass
            except OSError:
                pass

    thread = threading.Thread(target=_accept_forever, daemon=True)
    thread.start()
    try:
        config = {
            "transport": "streamable_http",
            "url": f"http://127.0.0.1:{port}/mcp",
            "timeout": 3,
        }
        start = time.monotonic()
        result = await manager.connect("blackhole", config)
        elapsed = time.monotonic() - start
        assert result["ok"] is False
        assert elapsed < 7.0, f"connect took {elapsed:.1f}s — looks like retries"
    finally:
        stop.set()
        listener.close()
        thread.join(timeout=2)


async def test_dead_server_fails_immediately_no_retry(manager):
    config = {
        "transport": "streamable_http",
        "url": "http://127.0.0.1:9/mcp",  # nothing listens here
        "timeout": 2,
    }
    result = await manager.connect("dead", config)
    assert result["ok"] is False
    assert result["error"]
    assert manager.get_status("dead")["status"] == "failed"


async def test_genuine_cancellation_still_propagates(fake_url, manager):
    """A bare task.cancel() during a tool call must still cancel the call —
    the SDK-cancel discriminator must not swallow real cancellations."""
    import asyncio

    config = {"transport": "streamable_http", "url": fake_url, "timeout": 10}
    assert (await manager.connect("fake", config))["ok"] is True
    session = manager._sessions["fake"]
    original_call_tool = session.call_tool

    async def _slow(*args, **kwargs):
        await asyncio.sleep(30)
        return await original_call_tool(*args, **kwargs)

    session.call_tool = _slow
    task = asyncio.create_task(manager.call_tool("fake", "add", {"a": 1, "b": 2}))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    try:
        await manager.disconnect("fake")
    except BaseException:  # noqa: BLE001, S110 — best-effort cleanup after the cancellation assertion; disconnect may raise CancelledError, which must not escape
        pass


def test_header_env_var_expansion():
    from aether_engine.mcp.client import expand_env_vars

    os.environ["AETHER_TEST_KEY"] = "secret-123"
    assert expand_env_vars("Bearer ${AETHER_TEST_KEY}") == "Bearer secret-123"
    assert expand_env_vars("${MISSING_VAR:-fallback}") == "fallback"
    assert expand_env_vars("plain") == "plain"
