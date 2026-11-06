"""Wave 2, N-15 + N-16 [S2]: MCP pagination and connect_owned bounds.

N-15: ``_list_all_pages`` followed ``nextCursor`` in a bare
``while True`` — no page cap, no repeated-cursor detection. A buggy
or malicious server returning fresh (or cycling) cursors loops
connect/tool-list forever.

N-16: ``connect_owned`` returned ``{"ok": True}`` to a concurrent
second caller while the first connect was still in progress (no
session existed yet), and the creating caller's ``await ready.wait()``
had no timeout — an owner dying without setting ``ready`` hung the
caller indefinitely.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from aether_engine.mcp import client as client_mod
from aether_engine.mcp.client import MCPClientManager, _list_all_pages


class _Page:
    def __init__(self, items, next_cursor):
        self.tools = items
        self.nextCursor = next_cursor


class _FreshCursorSession:
    """Every page invents a brand-new cursor — the loop never ends."""

    def __init__(self):
        self.calls = 0

    async def list_tools(self, params=None):
        await asyncio.sleep(0)  # a real session suspends on IO per page
        self.calls += 1
        return _Page([f"tool-{self.calls}"], f"cursor-{self.calls}")


class _CyclingCursorSession:
    """Cursors cycle A -> B -> A -> ... — no forward progress."""

    def __init__(self):
        self.calls = 0

    async def list_tools(self, params=None):
        await asyncio.sleep(0)
        self.calls += 1
        cursor = None if params is None else params.cursor
        nxt = {"A": "B", "B": "A"}.get(cursor, "A")
        return _Page(["tool"], nxt)


class _FiniteSession:
    def __init__(self):
        self.calls = 0

    async def list_tools(self, params=None):
        self.calls += 1
        cursor = None if params is None else params.cursor
        if cursor is None:
            return _Page(["a"], "c1")
        if cursor == "c1":
            return _Page(["b"], "c2")
        return _Page(["c"], None)


@pytest.mark.asyncio
async def test_pagination_fresh_cursors_are_bounded():
    session = _FreshCursorSession()
    with pytest.raises(ValueError):
        await asyncio.wait_for(
            _list_all_pages(session, "list_tools", "tools"), timeout=10)
    # The bound is the page cap (plus at most the page that trips it),
    # not an unbounded spin.
    assert session.calls <= client_mod.MCP_LIST_MAX_PAGES + 1


@pytest.mark.asyncio
async def test_pagination_cycling_cursor_is_detected():
    session = _CyclingCursorSession()
    with pytest.raises(ValueError):
        await asyncio.wait_for(
            _list_all_pages(session, "list_tools", "tools"), timeout=10)
    assert session.calls <= 4


@pytest.mark.asyncio
async def test_pagination_finite_list_still_complete():
    session = _FiniteSession()
    items = await _list_all_pages(session, "list_tools", "tools")
    assert items == ["a", "b", "c"]
    assert session.calls == 3


def _manager_with_fake_open(monkeypatch, open_impl):
    manager = MCPClientManager()
    monkeypatch.setattr(manager, "_open_and_register", open_impl)
    return manager


@pytest.mark.asyncio
async def test_connect_owned_concurrent_caller_sees_real_failure(
        monkeypatch):
    """A second connect during an in-flight connect must wait for the
    actual outcome — not report success for a session that does not
    exist yet (and never will, here)."""
    gate = asyncio.Event()

    async def fake_open(name, config):
        await gate.wait()
        return {"ok": False, "error": "boom"}

    manager = _manager_with_fake_open(monkeypatch, fake_open)
    first = asyncio.create_task(manager.connect_owned("srv", {}))
    await asyncio.sleep(0.1)  # first connect is in flight
    second = asyncio.create_task(manager.connect_owned("srv", {}))
    await asyncio.sleep(0.2)
    assert not second.done(), (
        "concurrent connect_owned returned before the in-flight "
        "connect settled — it reported a result for a session that "
        "does not exist yet")
    gate.set()
    first_result, second_result = await asyncio.gather(first, second)
    assert first_result == {"ok": False, "error": "boom"}
    assert second_result == {"ok": False, "error": "boom"}


@pytest.mark.asyncio
async def test_connect_owned_ready_wait_is_bounded(monkeypatch):
    """An owner whose connect never settles must not hang the caller."""
    monkeypatch.setattr(
        client_mod, "MCP_CONNECT_READY_TIMEOUT_SECONDS", 0.3)
    never = asyncio.Event()

    async def fake_open(name, config):
        await never.wait()
        return {"ok": True, "error": None}  # pragma: no cover

    manager = _manager_with_fake_open(monkeypatch, fake_open)
    result = await asyncio.wait_for(
        manager.connect_owned("srv", {}), timeout=10)
    assert result["ok"] is False
    assert result["error"]


@pytest.mark.asyncio
async def test_connect_owned_success_path_unchanged(monkeypatch):
    """The ordinary connect still reports ok once the session is up,
    and a later connect to the live session still short-circuits."""

    async def fake_open(name, config):
        manager._sessions[name] = SimpleNamespace()
        manager._set_status(name, "connected", error=None, tool_count=1)
        return {"ok": True, "error": None}

    manager = _manager_with_fake_open(monkeypatch, fake_open)
    result = await manager.connect_owned("srv", {})
    assert result == {"ok": True, "error": None}
    again = await manager.connect_owned("srv", {})
    assert again == {"ok": True, "error": None}
    await manager.disconnect("srv")
