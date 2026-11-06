"""Wave 2, N-09 [S2]: async tool calls get the tool's timeout too.

``tools/registry.py`` bounded only SYNC tools (``_run_sync_tool``'s
``wait_for(timeout_s)``). The MCP/browser branch and the READ_ONLY
coroutine branch awaited ``tool.execute_fn`` directly, so a hung
async tool hung the task forever — compounded by the browser driver
passing no ``timeout=`` to Playwright at all (Playwright's default
action timeout is 0 = disabled), so ``browser_click`` /
``browser_type`` on a missing or obscured selector never returned.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from aether_engine.browser.session import PlaywrightDriver
from aether_engine.tools.registry import (
    Tool,
    ToolExecutionError,
    ToolRegistry,
    ToolRisk,
)


async def _hang(**_kwargs):
    await asyncio.sleep(60)
    return "never"  # pragma: no cover


@pytest.mark.asyncio
async def test_read_only_coroutine_tool_times_out():
    registry = ToolRegistry()
    registry.register(Tool(
        name="hang_reader", description="", risk=ToolRisk.READ_ONLY,
        execute_fn=_hang, timeout_s=1))
    start = time.monotonic()
    with pytest.raises(ToolExecutionError, match="timed out"):
        await registry.execute("hang_reader", {})
    assert time.monotonic() - start < 10


@pytest.mark.asyncio
async def test_mcp_coroutine_tool_times_out():
    registry = ToolRegistry()
    registry.register(Tool(
        name="hang_mcp", description="", risk=ToolRisk.EXECUTE,
        source="mcp:fake", execute_fn=_hang, timeout_s=1))
    start = time.monotonic()
    with pytest.raises(ToolExecutionError, match="timed out"):
        # approved=True carries the call through the policy pipeline
        # (EXECUTE defaults to "ask") to the execution branch itself.
        await registry.execute("hang_mcp", {}, approved=True)
    assert time.monotonic() - start < 10


@pytest.mark.asyncio
async def test_fast_coroutine_tool_unaffected():
    async def _quick(**_kwargs):
        return "done"

    registry = ToolRegistry()
    registry.register(Tool(
        name="quick_reader", description="", risk=ToolRisk.READ_ONLY,
        execute_fn=_quick, timeout_s=5))
    assert await registry.execute("quick_reader", {}) == "done"


class _FakePage:
    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []

    async def goto(self, url, **kwargs):
        self.calls.append(("goto", (url,), kwargs))

    async def click(self, selector, **kwargs):
        self.calls.append(("click", (selector,), kwargs))

    async def fill(self, selector, text, **kwargs):
        self.calls.append(("fill", (selector, text), kwargs))

    async def inner_text(self, selector, **kwargs):
        self.calls.append(("inner_text", (selector,), kwargs))
        return "text"


@pytest.mark.asyncio
async def test_playwright_driver_passes_action_timeouts():
    page = _FakePage()
    driver = PlaywrightDriver(object(), object(), page)
    await driver.goto("https://example.test/")
    await driver.click("#go")
    await driver.fill("#q", "hello")
    await driver.extract_text("#main")
    timeouts = {name: kwargs.get("timeout") for name, _a, kwargs in page.calls}
    assert timeouts["goto"], "goto must carry an explicit timeout"
    assert timeouts["click"], "click must carry an explicit timeout"
    assert timeouts["fill"], "fill must carry an explicit timeout"
    assert timeouts["inner_text"], "extract_text must carry an explicit timeout"
    for value in timeouts.values():
        assert 0 < value <= 120_000
