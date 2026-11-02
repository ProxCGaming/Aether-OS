"""PF-5: a storage-broken approval cache must degrade to the ask.

The graph router (``langgraph/graph.py``, ``tool_router``) guards its
``covering_grant`` check: on any cache error it logs and falls through
to the normal approval pause — a cache failure must never break
routing. ``ToolRegistry.execute`` re-checks the same cache before
running (E2-09) but called it UNGUARDED, so a storage-level failure
at execution time (dead/locked/corrupt grants DB) escaped
``execute()`` as a raw ``sqlite3`` error and killed the tool call
instead of degrading to the ordinary "requires approval" refusal.

These tests drive the REAL cache object into failing — its SQLite
connection is closed, so ``covering_grant`` raises from inside the
cache's own storage layer — rather than mocking ``covering_grant``
away. The controls pin that healthy-cache behaviour is unchanged:
a covering grant still skips the ask, no grant still asks, and an
explicitly approved call never consults the cache at all.
"""

from __future__ import annotations

from unittest import mock

import pytest

from aether_engine.tools.approval_cache import ApprovalCache
from aether_engine.tools.registry import ToolExecutionError, build_tool_registry
from aether_engine.workers import sandbox
from aether_worker.__main__ import run_worker


async def _in_process(tool_name, args, task_class="standard", audit_logger=None, task_classes=None):
    """In-process worker substitute, as in test_epic2_approval_cache:
    the grant path is what matters here, and the sandbox needs pywin32."""
    return await run_worker({"tool_name": tool_name, "args": args})


@pytest.fixture()
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return root


@pytest.fixture()
def cache(tmp_path):
    return ApprovalCache(tmp_path / "grants.db")


def _break_storage(cache: ApprovalCache) -> None:
    """Drive the real cache's storage layer into failing: with its
    connection closed, every read raises sqlite3.ProgrammingError
    from inside the cache — the execution-time storage failure PF-5
    is about (a locked/corrupt DB fails the same way, one layer in)."""
    cache._conn.close()


@pytest.mark.asyncio
async def test_broken_cache_degrades_to_approval_ask(ws, cache):
    target = ws / "f.txt"
    registry = build_tool_registry()
    # A covering grant EXISTS: a healthy cache would skip the ask and
    # let this write run. The broken cache must not turn that into a
    # raw escape — the safe degradation is the normal approval ask.
    cache.grant(str(ws), "write", tool_name="write_file")
    _break_storage(cache)

    with pytest.raises(ToolExecutionError, match="requires approval"):
        await registry.execute(
            "write_file", {"path": str(target), "content": "x"},
            workspace_roots=[str(ws)], policies={}, approval_cache=cache,
        )
    # Fail-closed: the unapproved call never executed.
    assert not target.exists()


@pytest.mark.asyncio
async def test_healthy_cache_with_grant_still_skips_ask(ws, cache):
    """Control: grant semantics unchanged — a covering grant turns
    the ask into an allow and the tool runs."""
    target = ws / "f.txt"
    registry = build_tool_registry()
    cache.grant(str(ws), "write", tool_name="write_file")

    with mock.patch.object(sandbox, "execute_tool_in_worker", _in_process):
        result = await registry.execute(
            "write_file", {"path": str(target), "content": "x"},
            workspace_roots=[str(ws)], policies={}, approval_cache=cache,
        )
    assert result["written"] is True
    assert target.read_text(encoding="utf-8") == "x"


@pytest.mark.asyncio
async def test_healthy_cache_without_grant_still_asks(ws, cache):
    """Control: no grant, no approval — the ordinary refusal."""
    target = ws / "f.txt"
    registry = build_tool_registry()

    with pytest.raises(ToolExecutionError, match="requires approval"):
        await registry.execute(
            "write_file", {"path": str(target), "content": "x"},
            workspace_roots=[str(ws)], policies={}, approval_cache=cache,
        )
    assert not target.exists()


@pytest.mark.asyncio
async def test_broken_cache_never_blocks_an_approved_call(ws, cache):
    """approved=True short-circuits before the cache is consulted,
    so a broken cache must not block an explicitly approved call."""
    target = ws / "f.txt"
    registry = build_tool_registry()
    _break_storage(cache)

    with mock.patch.object(sandbox, "execute_tool_in_worker", _in_process):
        result = await registry.execute(
            "write_file", {"path": str(target), "content": "x"},
            workspace_roots=[str(ws)], policies={}, approval_cache=cache,
            approved=True,
        )
    assert result["written"] is True
    assert target.read_text(encoding="utf-8") == "x"
