"""N-02 (S1, blow-up engine lane): the MCP tool-call timeout path must
return even when the SDK task ignores cancellation.

``MCPClientManager.call_tool`` bounds the call with
``wait_for(shield(inner))``, but on ``TimeoutError`` it then awaited the
cancelled inner task with NO bound. A dead server whose SDK / anyio
task swallows ``CancelledError`` — the exact case the timeout exists
for — wedged the tool call forever: the wrapper never raised, and the
server was never marked failed.

These tests pin the bounded post-cancel grace: the wrapper raises its
timeout outcome within ``timeout + grace``, marks the server failed,
and does not wait for the stubborn task. The grace constant itself is
pinned small so it cannot quietly grow into a second timeout.
"""
import asyncio
import logging
import time

import pytest

from aether_engine.mcp.client import MCPClientError
from tests.fake_mcp_http import start_fake_http_server

# The grace is a backstop for a task that ignores cancellation, not a
# second timeout: it must stay in single-digit seconds.
EXPECTED_GRACE_MAX = 5.0


@pytest.fixture(scope="module")
def fake_url():
    url, stop = start_fake_http_server()
    yield url
    stop()


@pytest.fixture()
def manager():
    from aether_engine.mcp.client import MCPClientManager

    return MCPClientManager()


async def _connect(fake_url, manager):
    config = {"transport": "streamable_http", "url": fake_url, "timeout": 10}
    assert (await manager.connect("fake", config))["ok"] is True
    return manager._sessions["fake"]


def test_cancel_grace_constant_is_small():
    from aether_engine.mcp.client import MCP_TOOL_CALL_CANCEL_GRACE_SECONDS

    assert 0 < MCP_TOOL_CALL_CANCEL_GRACE_SECONDS <= EXPECTED_GRACE_MAX


async def test_call_tool_timeout_returns_when_inner_ignores_cancellation(
    fake_url, manager
):
    """The N-02 regression: inner swallows every CancelledError and
    keeps waiting. call_tool must still raise its timeout outcome
    within timeout + grace and mark the server failed — without
    waiting for the stubborn task to finish."""
    session = await _connect(fake_url, manager)

    release = asyncio.Event()
    inner_tasks: list[asyncio.Task] = []

    async def _stubborn(*args, **kwargs):
        inner_tasks.append(asyncio.current_task())
        # Dead-server stand-in: cancellation is delivered and ignored,
        # repeatedly, until the test releases the task.
        while not release.is_set():
            try:
                await asyncio.wait_for(release.wait(), timeout=0.05)
            except asyncio.CancelledError:
                continue  # the N-02 case: cancel never lands
            except TimeoutError:
                pass
        from mcp.types import CallToolResult, TextContent

        return CallToolResult(
            content=[TextContent(type="text", text="too late")], isError=False
        )

    session.call_tool = _stubborn
    start = time.monotonic()
    try:
        with pytest.raises(MCPClientError, match="timed out after 1"):
            await manager.call_tool("fake", "add", {"a": 1, "b": 2}, timeout=1)
        elapsed = time.monotonic() - start
        # The real timeout elapsed first...
        assert elapsed >= 0.9, f"call returned after {elapsed:.2f}s — timeout not honoured"
        # ...but the wrapper did NOT hang on the stubborn task.
        assert elapsed < 1 + EXPECTED_GRACE_MAX + 5, (
            f"call_tool took {elapsed:.1f}s for a 1s timeout — "
            "the post-cancel await is not bounded"
        )
        assert manager.get_status("fake")["status"] == "failed"
        # Proof the wrapper returned without waiting: the stubborn
        # task is still running.
        assert inner_tasks and not inner_tasks[0].done()
    finally:
        release.set()
        if inner_tasks:
            try:
                await asyncio.wait_for(asyncio.shield(inner_tasks[0]), timeout=2)
            except BaseException:  # cleanup only; the assertions above already decided the outcome
                logging.getLogger(__name__).debug("stubborn inner task cleanup abandoned", exc_info=True)
        await manager.disconnect("fake")


async def test_call_tool_slow_but_within_timeout_succeeds(fake_url, manager):
    """Normal path: a call slower than nothing but inside its timeout
    succeeds and leaves the server connected (not marked failed)."""
    from mcp.types import CallToolResult, TextContent

    session = await _connect(fake_url, manager)

    async def _slow_ok(*args, **kwargs):
        await asyncio.sleep(0.3)
        return CallToolResult(
            content=[TextContent(type="text", text="8")], isError=False
        )

    session.call_tool = _slow_ok
    out = await manager.call_tool("fake", "add", {"a": 3, "b": 5}, timeout=5)
    assert out == "8"
    assert manager.get_status("fake")["status"] == "connected"
    await manager.disconnect("fake")


async def test_call_tool_timeout_cancels_a_cancellable_inner_cleanly(
    fake_url, manager
):
    """Normal path: a well-behaved inner task takes its cancellation
    at once — the grace is a backstop, never an added delay — and the
    timeout outcome (MCPClientError + server failed) is unchanged."""
    session = await _connect(fake_url, manager)
    observed: dict[str, bool] = {}

    async def _cancellable(*args, **kwargs):
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            observed["cancelled"] = True
            raise

    session.call_tool = _cancellable
    start = time.monotonic()
    with pytest.raises(MCPClientError, match="timed out after 1"):
        await manager.call_tool("fake", "add", {"a": 1, "b": 2}, timeout=1)
    elapsed = time.monotonic() - start
    assert elapsed < 1 + 2, (
        f"cancellable inner took {elapsed:.1f}s to time out — "
        "the grace is being paid even when cancellation lands"
    )
    assert observed.get("cancelled") is True
    assert manager.get_status("fake")["status"] == "failed"
    await manager.disconnect("fake")
