"""E2-F05 + E2-F06: worker resource profiles and event-loop dispatch.

E2-F05 — configured worker resource profiles never reach the live tool
pipeline. ``UserConfig.task_classes`` carries the user's time/memory
caps (ADR 0010), and the sandbox (``execute_tool_in_worker``) honours a
``task_classes`` override when one is supplied — but the live path
(graph node → ``ToolRegistry.execute`` → sandbox) never supplies it, so
every worker run silently uses the sandbox's separate built-in
defaults. The WebSocket wrapper that DOES supply it
(``routes/websocket.py::_execute_tool_in_worker``) has no callers.
Reproduction in the review: configured 7 s / 123 MB, the real chain
constructed the job wrapper with 1,800 s / 4,096 MB.

E2-F06 — synchronous native tools run inline on the engine event loop
and ``Tool.timeout_s`` is consumed nowhere. A slow or regex-heavy
READ_ONLY call (the review's probe: real ``search_text`` with
``^(a+)+$`` on a 27-byte file) stalls every other connection/task on
the loop and returns successfully past its timeout metadata.

These tests fail on the pre-fix code and pass after:

* F05: the configured caps reach the Job Object through the real
  node → registry → sandbox chain; ``registry.execute`` forwards a
  supplied ``task_classes`` (and ``None`` — built-in defaults — when
  not supplied); an unknown task class is refused loudly instead of
  silently downgraded to another profile.
* F06: a slow synchronous tool no longer stalls a concurrent heartbeat
  on the same loop, and a tool that overruns its ``timeout_s`` fails
  with a timeout error instead of returning late success — for the
  native READ_ONLY branch, the MCP-source branch, and (deadline half)
  the real ``search_text`` from the review's probe; see that test for
  the measured GIL limit on the responsiveness half.

The worker harness mirrors tests/test_phase10_2_worker_timeout.py:
real worker subprocess, cwd in a tmp dir (token + audit resolve
there), PYTHONPATH at the repo, Windows Job Object faked (it cannot
attach on Linux).
"""
import asyncio
import dataclasses
import itertools
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch

import pytest

from aether_engine.audit import AuditLogger
from aether_engine.config import UserConfig
from aether_engine.langgraph.nodes.execute_tool import execute_tool_node
from aether_engine.tools.registry import (
    Tool,
    ToolExecutionError,
    ToolRegistry,
    ToolRisk,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
# Assembled rather than a bare literal: the write path scrubs
# credential-shaped assignments (same quirk noted in the vault tests).
TOKEN = "e2-worker-profiles" + "-test-token"


def _audit_events(log_path):
    log = Path(log_path)
    if not log.is_file():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


@contextmanager
def _engine_call_env(tmp_path, job_cls):
    """chdir to tmp_path (token file + audit log resolve relative to
    cwd), point PYTHONPATH at the repo for the worker child, and fake
    the Windows Job Object. Restores everything on exit."""
    token_dir = tmp_path / ".run"
    token_dir.mkdir(parents=True, exist_ok=True)
    (token_dir / "engine.token").write_text(TOKEN, encoding="utf-8")
    audit = AuditLogger(log_path=tmp_path / "logs" / "audit.log")

    old_cwd = os.getcwd()
    old_pythonpath = os.environ.get("PYTHONPATH")
    try:
        os.chdir(tmp_path)
        os.environ["PYTHONPATH"] = str(REPO_ROOT)
        with patch("aether_engine.workers.sandbox.WorkerJobObject", job_cls):
            yield audit
    finally:
        os.chdir(old_cwd)
        if old_pythonpath is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = old_pythonpath


class FakeJobObject:
    """Job Object stand-in: construction + assignment succeed (no-op)."""

    def __init__(self, *args, **kwargs):
        pass

    def assign_process(self, pid):
        pass


class RecordingJobObject(FakeJobObject):
    """Job Object stand-in that records the caps it was built with —
    the worker-side observation point for the selected profile."""

    constructed: ClassVar[list[tuple[int, int]]] = []

    def __init__(self, time_cap_seconds, memory_cap_mb):
        RecordingJobObject.constructed.append((time_cap_seconds, memory_cap_mb))


# ─── E2-F05: configured profiles reach the worker ────────────────────


@pytest.mark.asyncio
async def test_configured_profile_reaches_job_object_through_node(tmp_path):
    """Full live chain: node → ToolRegistry.execute → sandbox → worker.

    The user's config selects 7 s / 123 MB for the task's class; the
    Job Object (which enforces the profile) must be constructed with
    exactly those caps. Pre-fix the registry drops ``task_classes``
    and the sandbox default 1,800 s / 4,096 MB is used instead."""
    from aether_engine.tools.registry import create_file_tools

    RecordingJobObject.constructed = []
    registry = ToolRegistry()
    for tool in create_file_tools():
        registry.register(tool)
    target = tmp_path / "profile-wrote.txt"
    state = {"pending_tool_call": {"name": "write_file", "call_id": "f05-1",
                                   "args": {"path": str(target), "content": "profiled"},
                                   "approved": True}}
    custom_profiles = {"standard": {"time_cap_seconds": 7, "memory_cap_mb": 123}}
    with _engine_call_env(tmp_path, RecordingJobObject) as audit:
        config = {"configurable": {
            "tool_registry": registry,
            "user_config": UserConfig(task_classes=custom_profiles),
            "workspace_roots": [str(tmp_path)],
            "task_class": "standard",
            "audit_logger": audit,
        }}
        result = await execute_tool_node(state, config)

    assert target.read_text(encoding="utf-8") == "profiled"
    assert "profile-wrote.txt" in result["messages"][0]["content"]
    assert RecordingJobObject.constructed == [(7, 123)], (
        f"job wrapper built with {RecordingJobObject.constructed}, "
        "expected the configured [(7, 123)]"
    )
    events = _audit_events(tmp_path / "logs" / "audit.log")
    assert any(e.get("action") == "WORKER_EXECUTION_COMPLETED" for e in events), events


@pytest.mark.asyncio
async def test_registry_execute_forwards_task_classes_to_worker():
    """The registry seam itself: a supplied ``task_classes`` reaches
    ``execute_tool_in_worker`` verbatim; omitting it forwards ``None``
    (the sandbox's built-in defaults), preserving prior behaviour for
    callers without a config."""
    from aether_engine.tools.registry import create_file_tools

    calls = []

    async def fake_worker(**kwargs):
        calls.append(kwargs)
        return "worker-result"

    registry = ToolRegistry()
    for tool in create_file_tools():
        registry.register(tool)
    custom = {"standard": {"time_cap_seconds": 7, "memory_cap_mb": 123}}
    with patch("aether_engine.workers.sandbox.execute_tool_in_worker", new=fake_worker):
        result = await registry.execute(
            "write_file", {"path": "/tmp/e2f05_write.txt", "content": "x"},
            approved=True, workspace_roots=["/tmp"], task_classes=custom,
        )
        assert result == "worker-result"
        await registry.execute(
            "write_file", {"path": "/tmp/e2f05_write.txt", "content": "x"},
            approved=True, workspace_roots=["/tmp"],
        )
    assert len(calls) == 2
    assert calls[0].get("task_classes") == custom, (
        f"configured profiles dropped at the registry: {calls[0]}"
    )
    assert calls[1].get("task_classes") is None


@pytest.mark.asyncio
async def test_unknown_task_class_refused_not_silently_downgraded(tmp_path):
    """A task class with no configured profile must fail loudly
    (WORKER_EXECUTION_REFUSED, nothing runs) — never silently run
    under a different profile's caps."""
    from aether_engine.workers.sandbox import execute_tool_in_worker

    target = tmp_path / "must-not-exist.txt"
    profiles = {"standard": {"time_cap_seconds": 30, "memory_cap_mb": 256}}
    with _engine_call_env(tmp_path, FakeJobObject) as audit, \
            pytest.raises(RuntimeError, match="Refusing to execute"):
        await execute_tool_in_worker(
            tool_name="write_file",
            args={"path": str(target), "content": "downgraded"},
            task_class="no-such-class",
            audit_logger=audit,
            task_classes=profiles,
        )
    assert not target.exists(), "a refused execution must not run the tool"
    events = _audit_events(tmp_path / "logs" / "audit.log")
    refused = [e for e in events if e.get("action") == "WORKER_EXECUTION_REFUSED"]
    assert refused, f"expected WORKER_EXECUTION_REFUSED, got {events}"
    assert "no-such-class" in refused[0]["details"]["reason"]


# ─── E2-F06: synchronous native tools off the event loop ─────────────


async def _heartbeat_gaps(coro_factory, interval=0.05):
    """Run ``coro_factory()`` on this loop with a concurrent heartbeat;
    return (result, max in-window tick gap, in-window tick count). The
    heartbeat stands in for every other connection/task the engine
    loop serves: if the tool blocks the loop, the gaps show it.

    The heartbeat is given a baseline rhythm BEFORE the measured call
    starts — otherwise the call (which runs inline until its first
    await) can block before the heartbeat task is ever scheduled, and
    the stall goes unmeasured. The measured window starts at the last
    tick before the call, so a stall shows up as one long gap."""
    ticks: list[float] = []
    stop = asyncio.Event()

    async def heartbeat():
        while not stop.is_set():
            ticks.append(time.monotonic())
            await asyncio.sleep(interval)

    hb = asyncio.create_task(heartbeat())
    try:
        await asyncio.sleep(4 * interval)
        boundary = len(ticks) - 1
        result = await coro_factory()
    finally:
        stop.set()
        await hb
    window = ticks[boundary:]
    gaps = [b - a for a, b in itertools.pairwise(window)]
    return result, (max(gaps) if gaps else 0.0), len(window) - 1


def _slow_tool(name, seconds, **tool_kwargs):
    def _slow(**_kwargs):
        time.sleep(seconds)
        return f"{name}-done"

    return Tool(
        name=name,
        description="controllably slow synchronous tool",
        execute_fn=_slow,
        risk=ToolRisk.READ_ONLY,
        **tool_kwargs,
    )


@pytest.mark.asyncio
async def test_slow_sync_read_only_tool_does_not_stall_event_loop():
    registry = ToolRegistry()
    registry.register(_slow_tool("slow_read", 0.6))
    result, max_gap, ticks_in_window = await _heartbeat_gaps(
        lambda: registry.execute("slow_read", {}))
    assert result == "slow_read-done"
    assert ticks_in_window >= 4, (
        f"heartbeat ticked only {ticks_in_window} times during a 0.6s "
        "tool call — the loop was not free while it ran"
    )
    assert max_gap < 0.35, (
        f"event loop stalled {max_gap:.2f}s behind a synchronous tool "
        "(heartbeat interval is 0.05s)"
    )


@pytest.mark.asyncio
async def test_slow_sync_mcp_source_tool_does_not_stall_event_loop():
    """Sibling dispatch branch: MCP-source tools execute in-process
    too; a synchronous one must not stall the loop either (in practice
    MCP execute_fns are coroutines — this pins the sync fallback)."""
    registry = ToolRegistry()
    registry.register(_slow_tool("mcp_slow", 0.6, source="mcp:fake"))
    result, max_gap, ticks_in_window = await _heartbeat_gaps(
        lambda: registry.execute("mcp_slow", {}))
    assert result == "mcp_slow-done"
    assert ticks_in_window >= 4, (
        f"heartbeat ticked only {ticks_in_window} times during a 0.6s "
        "tool call — the loop was not free while it ran"
    )
    assert max_gap < 0.35, (
        f"event loop stalled {max_gap:.2f}s behind a synchronous MCP tool"
    )


@pytest.mark.asyncio
async def test_sync_tool_timeout_s_is_enforced():
    """A synchronous tool that overruns its ``timeout_s`` fails at the
    deadline with a timeout error instead of returning a late success.
    (The worker thread cannot be killed; the loop is freed and the
    late result discarded, like every ``asyncio.to_thread`` user in
    the codebase.)"""
    registry = ToolRegistry()
    registry.register(_slow_tool("slow_read", 3, timeout_s=1))
    start = time.monotonic()
    with pytest.raises(ToolExecutionError, match="timed out"):
        await registry.execute("slow_read", {})
    elapsed = time.monotonic() - start
    assert elapsed < 2.5, f"timeout enforced late: caller blocked {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_real_search_text_pathological_regex_times_out_not_late_success(tmp_path):
    """The review's own probe, at its own scale: the real ``search_text``
    against a 27-byte file whose line makes ``^(a+)+$`` backtrack for
    seconds, with the tool's ``timeout_s`` set to 1. Pre-fix the call
    returns SUCCESSFULLY whenever the scan eventually finishes;
    post-fix it fails with a timeout error — the deadline is enforced
    and the late result is never delivered as a success.

    Scope note (measured, not assumed): this test deliberately does
    NOT assert loop responsiveness. A single ``re`` match never
    releases the CPython GIL — measured on this codebase's runtime,
    a ticker thread got 1 tick in 3.46 s while the pattern ran in
    another thread — so no in-process placement can keep the loop
    responsive during one pathological match; only a subprocess
    could, and READ_ONLY native tools are in-process by design
    (ADR 0010). The deadline therefore lands when the match returns
    rather than at exactly 1 s. Loop responsiveness for the general
    blocking cases (scans, I/O, sleeps — many GIL-yielding steps) is
    pinned by the heartbeat tests above."""
    from aether_engine.tools.filesystem import create_search_text_tool

    target = tmp_path / "trap.txt"
    target.write_text("a" * 26 + "b", encoding="utf-8")
    tool = dataclasses.replace(create_search_text_tool(), timeout_s=1)
    registry = ToolRegistry()
    registry.register(tool)

    with pytest.raises(ToolExecutionError, match="timed out"):
        await registry.execute(
            "search_text",
            {"query": "^(a+)+$", "path": str(target)},
            workspace_roots=[str(tmp_path)],
        )
