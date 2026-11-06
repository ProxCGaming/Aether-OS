"""Wave 2 remainder — audit + app lifecycle: N-33, N-35.

- N-33: audit rotation's 10 MB gzip must not run on the caller's
  stack (async handlers stall for the whole compression, and every
  other writer waits on the append lock behind it). Rotation should
  cost the caller a rename; compression finishes in the background,
  in order.
- N-35: the startup episodic backfill task must be referenced and
  shutdown-managed like the discovery task next to it, and the media
  analyzer's detached backfill tasks must be cancellable at shutdown
  instead of running on against a closing engine.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from aether_engine import audit as audit_module
from aether_engine.audit import AuditLogger

# ---------------------------------------------------------------------------
# N-33 — rotation compression off the caller's stack
# ---------------------------------------------------------------------------

def test_rotation_compression_does_not_block_the_caller(tmp_path, monkeypatch):
    log = tmp_path / "audit.log"
    logger = AuditLogger(log, max_bytes=600, backup_count=2)
    for i in range(5):
        logger.log_event("FILL", {"i": i, "pad": "x" * 80})

    release = threading.Event()
    real_gzip_open = gzip.open

    def blocking_gzip_open(*args, **kwargs):
        release.wait(timeout=3)
        return real_gzip_open(*args, **kwargs)

    monkeypatch.setattr(gzip, "open", blocking_gzip_open)
    start = time.monotonic()
    logger.log_event("TRIGGER", {"pad": "y" * 200})  # crosses the cap
    elapsed = time.monotonic() - start
    release.set()

    assert elapsed < 1.5, (
        f"log_event stalled {elapsed:.2f}s behind rotation compression")
    # The write itself was never lost, and the rotation is recorded.
    current = [json.loads(line) for line in log.read_text().splitlines()
               if line.strip()]
    actions = [e.get("action") for e in current]
    assert "TRIGGER" in actions
    assert "AUDIT_LOG_ROTATED" in actions

    # Compression completes in the background, in order.
    assert audit_module.flush_pending_rotations(timeout=5)
    archive = tmp_path / "audit.log.1.gz"
    assert archive.exists()
    with gzip.open(archive, "rt") as f:
        archived = [json.loads(line) for line in f if line.strip()]
    # The archive holds real audit events from the rotated file —
    # FILL entries (plus the AUDIT_LOG_ROTATED marker a later
    # rotation swept up), never partial/garbled lines.
    assert archived and all(e.get("type") == "ENGINE_EVENT" for e in archived)
    assert any(e.get("action") == "FILL" for e in archived)


# ---------------------------------------------------------------------------
# N-35 — startup backfill tasks are shutdown-managed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_lifespan_manages_episodic_backfill_task(monkeypatch):
    import importlib

    # NB: aether_engine/__init__ re-exports the FastAPI instance as
    # ``app``; importlib gets the module itself.
    app_module = importlib.import_module("aether_engine.app")
    from aether_engine.mcp.registry import GLOBAL_MCP_REGISTRY
    from aether_engine.media import analyzer as analyzer_module

    monkeypatch.setattr(app_module.engine_state, "initialize", lambda: None)
    monkeypatch.setattr(app_module.engine_state, "shutdown", lambda: None)
    # The shared engine_state singleton's audit_logger may have been
    # bound to an earlier test's (now deleted) tmp dir by a real
    # initialize() elsewhere in the suite; the lifespan's shutdown
    # path logs ENGINE_STOPPED through it. Stub it so this test is
    # hermetic regardless of suite order.
    monkeypatch.setattr(
        app_module.engine_state, "audit_logger",
        SimpleNamespace(log_event=lambda *a, **k: None))
    monkeypatch.setattr(
        "aether_engine.diagnostics.install_crash_logging", lambda: None)
    monkeypatch.setattr(
        GLOBAL_MCP_REGISTRY, "start_auto_connect", lambda: None)
    monkeypatch.setattr(
        GLOBAL_MCP_REGISTRY, "stop_auto_connect", AsyncMock())

    async def _quick_discovery():
        return None

    monkeypatch.setattr(app_module, "_discover_startup_models", _quick_discovery)
    monkeypatch.setattr(analyzer_module, "schedule_media_backfill", lambda: None)
    cancel_spy = AsyncMock()
    monkeypatch.setattr(
        analyzer_module, "cancel_inflight_tasks", cancel_spy, raising=False)

    gate = asyncio.Event()

    async def _blocking_backfill():
        await gate.wait()

    monkeypatch.setattr(
        app_module, "_ensure_embedder_then_backfill", _blocking_backfill)

    agen = app_module.lifespan(app_module.app)
    await agen.__anext__()
    try:
        task = app_module._episodic_backfill_task
        assert task is not None, "episodic backfill task has no handle"
        assert not task.done()
    finally:
        with pytest.raises(StopAsyncIteration):
            await agen.__anext__()
    assert task.cancelled() or task.done()
    assert app_module._episodic_backfill_task is None
    cancel_spy.assert_awaited_once()


@pytest.mark.asyncio
async def test_analyzer_cancel_inflight_tasks_stops_backfills(monkeypatch):
    from aether_engine.media import analyzer as analyzer_module

    started = asyncio.Event()
    release = asyncio.Event()

    class _StubAnalyzer:
        async def backfill(self):
            started.set()
            await release.wait()

    monkeypatch.setattr(
        analyzer_module, "get_media_analyzer", lambda: _StubAnalyzer())
    analyzer_module.schedule_media_backfill()
    await asyncio.wait_for(started.wait(), timeout=5)
    assert analyzer_module._inflight_tasks, "backfill task was not tracked"

    await analyzer_module.cancel_inflight_tasks()
    assert all(t.done() for t in analyzer_module._inflight_tasks) or \
        not analyzer_module._inflight_tasks
    release.set()
