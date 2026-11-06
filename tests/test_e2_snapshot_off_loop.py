"""E2-01 — the workspace snapshot must not run on the event loop.

``execute_tool_node`` snapshots the workspace before and after every
write-capable tool call so filesystem effects can be collected as
artifacts (N05). The walk is synchronous recursive I/O over the whole
workspace; run inline it blocks the event loop — every other task and
socket the engine is serving — for the whole walk, twice per tool
call. These tests pin the fix and the unchanged semantics:

* the async snapshot entry point delegates the walk to a worker
  thread (``asyncio.to_thread``) and leaves the loop responsive over
  a large tree;
* the before/after diff reports exactly what it reported before the
  move: created and content-modified files, sorted — never merely
  read or unchanged files, and never deletions (a deleted file
  leaves nothing to reference; see the N05 note in execute_tool.py).
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest import mock

from aether_engine.langgraph.nodes.execute_tool import (
    _changed_workspace_files,
    _snapshot_workspace_files,
    _snapshot_workspace_files_async,
)

# Enough files that a synchronous walk takes tens of milliseconds —
# unmissable next to the ticker's 2 ms beat, with wide margins.
_FILE_COUNT = 4000


def _build_tree(root: Path, count: int) -> None:
    for i in range(count):
        directory = root / f"pkg{i % 40}" / f"mod{i % 10}"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"file_{i}.txt").write_text("x", encoding="utf-8")


async def test_snapshot_async_delegates_to_worker_thread(tmp_path):
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")

    with mock.patch.object(
        asyncio, "to_thread", wraps=asyncio.to_thread
    ) as to_thread_spy:
        snapshot = await _snapshot_workspace_files_async([tmp_path])

    assert to_thread_spy.call_count == 1
    assert to_thread_spy.call_args.args[0] is _snapshot_workspace_files
    # The threaded result is the sync snapshot, unchanged.
    assert snapshot == _snapshot_workspace_files([tmp_path])


async def test_snapshot_async_leaves_event_loop_responsive(tmp_path):
    _build_tree(tmp_path, _FILE_COUNT)

    ticks = 0
    running = True

    async def _ticker() -> None:
        nonlocal ticks
        while running:
            ticks += 1
            await asyncio.sleep(0.002)

    ticker = asyncio.create_task(_ticker())
    try:
        await asyncio.sleep(0.05)
        ticks_before = ticks
        snapshot = await _snapshot_workspace_files_async([tmp_path])
        ticks_during = ticks - ticks_before
    finally:
        running = False
        await ticker

    assert len(snapshot) == _FILE_COUNT
    # Run inline, the walk advances the ticker by 0 (measured); off
    # the loop the ticker must keep beating throughout the walk.
    assert ticks_during >= 3


async def test_snapshot_diff_semantics_unchanged(tmp_path):
    keep = tmp_path / "keep.txt"
    keep.write_text("same", encoding="utf-8")
    modify = tmp_path / "modify.txt"
    modify.write_text("before", encoding="utf-8")
    delete = tmp_path / "delete.txt"
    delete.write_text("gone", encoding="utf-8")
    ignored = tmp_path / "node_modules" / "dep"
    ignored.mkdir(parents=True)
    (ignored / "index.js").write_text("dep", encoding="utf-8")

    before = await _snapshot_workspace_files_async([tmp_path])

    created = tmp_path / "created.txt"
    created.write_text("new", encoding="utf-8")
    with modify.open("a", encoding="utf-8") as fh:
        fh.write(" — extended")
    delete.unlink()
    (ignored / "late.js").write_text("dep", encoding="utf-8")

    after = await _snapshot_workspace_files_async([tmp_path])

    # Created + content-modified only, sorted; the unchanged file,
    # the deletion, and everything under a pruned directory are
    # absent — exactly the pre-fix semantics.
    assert _changed_workspace_files(before, after) == sorted(
        [str(created), str(modify)]
    )
