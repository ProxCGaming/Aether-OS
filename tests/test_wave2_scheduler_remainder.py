"""Wave 2 remainder — scheduler: N-26, N-38, N-12 (capability jobs).

- N-26: a failed/deferred ``start()`` must not permanently disable
  the scheduler — the half-built scheduler object has to be cleared
  so a later ``start()`` retries, and ``stop()`` must clear a
  not-running scheduler too.
- N-38: the module-level duplicate ``BENCHMARK_CACHE`` in
  capability_jobs is dead (the live table is
  ``capability.benchmark_cache``'s) and "monthly" must be a calendar
  month, not ``IntervalTrigger(weeks=4)`` (13 fires a year, drifting).
- N-12: ``_execute_check_job`` must not run the synchronous
  Option A pass on the event loop.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from datetime import UTC, datetime

import pytest
from apscheduler.triggers.cron import CronTrigger

from aether_engine.scheduler import capability_jobs
from aether_engine.scheduler.capability_jobs import CapabilitySchedulerManager


@pytest.fixture
def manager(tmp_path):
    return CapabilitySchedulerManager(db_path=tmp_path / "capabilities.db")


async def _probe_delay(coro, probe_s=0.05):
    task = asyncio.create_task(coro)
    start = time.monotonic()
    await asyncio.sleep(probe_s)
    delay = time.monotonic() - start
    result = await task
    return delay, result


# ---------------------------------------------------------------------------
# N-38 — dead duplicate benchmark table + "monthly" that isn't
# ---------------------------------------------------------------------------

def test_no_dead_duplicate_benchmark_cache(manager):
    assert not hasattr(capability_jobs, "BENCHMARK_CACHE"), (
        "capability_jobs still carries its dead duplicate BENCHMARK_CACHE; "
        "the live table is aether_engine.capability.benchmark_cache's")
    # Option A still loads the LIVE cache into model_capabilities.
    from aether_engine.capability.benchmark_cache import BENCHMARK_CACHE

    count = manager._run_option_a()
    assert count == len(BENCHMARK_CACHE)
    with sqlite3.connect(manager.db_path) as conn:
        rows = conn.execute(
            "SELECT COUNT(*) FROM model_capabilities").fetchone()[0]
    assert rows == len(BENCHMARK_CACHE)


def test_monthly_trigger_is_a_calendar_month(manager):
    anchor = datetime(2026, 1, 15, 10, 30, tzinfo=UTC)
    manager._last_run_started_at = lambda: anchor.timestamp()
    trigger = manager._build_trigger("monthly")
    assert isinstance(trigger, CronTrigger), (
        f"monthly must be a calendar-month trigger, got {type(trigger).__name__}")
    fires = []
    prev = anchor
    for _ in range(14):
        nxt = trigger.get_next_fire_time(prev, prev)
        assert nxt is not None
        fires.append(nxt)
        prev = nxt
    # Same day-of-month every month — no drift — and 12 fires a year.
    assert all(f.day == 15 for f in fires), fires
    within_year = [f for f in fires if (f - anchor).days < 370]
    assert len(within_year) == 12, within_year


# ---------------------------------------------------------------------------
# N-26 — start() can never retry after a deferred/failed start
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_start_retries_after_failed_start(tmp_path, monkeypatch):
    manager = CapabilitySchedulerManager(db_path=tmp_path / "capabilities.db")
    real_scheduler_cls = capability_jobs.AsyncIOScheduler

    class _BoomScheduler:
        def __init__(self, event_loop=None):
            self.running = False

        def start(self):
            raise RuntimeError("no running event loop yet")

    monkeypatch.setattr(capability_jobs, "AsyncIOScheduler", _BoomScheduler)
    manager.start()
    assert manager.scheduler is None, (
        "failed start left a half-built scheduler behind; "
        "every later start() is a no-op")

    monkeypatch.setattr(capability_jobs, "AsyncIOScheduler", real_scheduler_cls)
    manager.start()
    try:
        assert manager.scheduler is not None
        assert manager.scheduler.running
    finally:
        manager.stop()
    assert manager.scheduler is None


@pytest.mark.asyncio
async def test_stop_clears_not_running_scheduler(tmp_path):
    manager = CapabilitySchedulerManager(db_path=tmp_path / "capabilities.db")
    manager.scheduler = capability_jobs.AsyncIOScheduler(
        event_loop=asyncio.get_running_loop())
    assert not manager.scheduler.running
    manager.stop()
    assert manager.scheduler is None, (
        "stop() left a not-running scheduler in place; "
        "start() would stay a no-op")


# ---------------------------------------------------------------------------
# N-12 — _execute_check_job runs sync Option A on the event loop
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_option_a_runs_off_event_loop(manager):
    def slow_option_a():
        time.sleep(0.4)
        return 7

    manager._run_option_a = slow_option_a
    delay, record = await _probe_delay(
        manager._execute_check_job(method="Option A"))
    assert record["models_tested"] == 7
    assert record["status"] == "SUCCESS"
    assert delay < 0.25, f"event loop stalled {delay:.2f}s by Option A"
