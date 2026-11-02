"""E5-F10: configured capability schedules must be honoured across reopen,
startup and cron registration.

Three mechanisms pinned here (audit shapes, controlled time — no real waits
beyond letting the scheduler fire its startup catch-up):

1. Reopen: a saved schedule/method (``none`` / ``Option A``) survives
   reconstruction — the active settings are the saved ones, not the
   weekly/Both construction defaults. Defaults apply only when nothing
   was ever saved.
2. Startup misfire recovery: with the last run 14 days old on a weekly
   schedule, startup executes exactly ONE catch-up run (never a storm of
   backdated runs), and the next periodic fire is aligned to the
   schedule's own occurrences (last_run + k * period), not pushed a full
   period away from now. A job not yet due runs nothing at startup.
3. Cron registration: ``0 2 * * *`` registers a real CronTrigger, not a
   12 h IntervalTrigger; an unparseable schedule registers no job and is
   surfaced in the job state (``schedule_error``) instead of silently
   degrading to 12 h.
"""
import asyncio
import sqlite3
import time

import pytest
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from aether_engine.scheduler.capability_jobs import CapabilitySchedulerManager

WEEK_S = 7 * 86400


def _seed_run(mgr: CapabilitySchedulerManager, started_at: float, method: str = "Option A"):
    mgr._record_run({
        "id": f"seed-{started_at}",
        "method": method,
        "started_at": started_at,
        "finished_at": started_at + 5.0,
        "models_tested": 1,
        "status": "SUCCESS",
        "cost_estimate": 0.0,
        "error_log": None,
    })


async def _wait_for_history_count(mgr, expected: int, timeout: float = 8.0) -> int:
    deadline = time.time() + timeout
    count = len(mgr.get_history(limit=50))
    while count < expected and time.time() < deadline:
        await asyncio.sleep(0.1)
        count = len(mgr.get_history(limit=50))
    return count


class TestReopenHonoursSavedConfig:
    def test_saved_none_option_a_survives_reopen(self, tmp_path):
        db = tmp_path / "caps.db"
        mgr = CapabilitySchedulerManager(db_path=db)
        mgr.set_schedule("none", "Option A")

        reopened = CapabilitySchedulerManager(db_path=db)
        assert reopened.current_schedule == "none"
        assert reopened.current_method == "Option A"

        # The values were in SQLite all along — the defect was that the
        # active settings ignored them.
        with sqlite3.connect(db) as conn:
            rows = dict(conn.execute("SELECT key, value FROM scheduler_config").fetchall())
        assert rows["schedule"] == "none"
        assert rows["method"] == "Option A"

    def test_fresh_install_gets_defaults(self, tmp_path):
        mgr = CapabilitySchedulerManager(db_path=tmp_path / "fresh.db")
        assert mgr.current_schedule == "weekly"
        assert mgr.current_method == "Both"

    def test_saved_cron_survives_reopen(self, tmp_path):
        db = tmp_path / "caps.db"
        mgr = CapabilitySchedulerManager(db_path=db)
        mgr.set_schedule("0 2 * * *", "Option C")

        reopened = CapabilitySchedulerManager(db_path=db)
        assert reopened.current_schedule == "0 2 * * *"
        assert reopened.current_method == "Option C"


class TestStartupMisfireRecovery:
    @pytest.mark.asyncio
    async def test_overdue_weekly_runs_exactly_one_catchup_and_aligns_next(self, tmp_path):
        db = tmp_path / "caps.db"
        mgr = CapabilitySchedulerManager(db_path=db)
        mgr.set_schedule("weekly", "Option A")
        seeded_at = time.time() - 14 * 86400
        _seed_run(mgr, seeded_at)

        mgr.start()
        try:
            count = await _wait_for_history_count(mgr, 2)
            assert count == 2, "startup must execute the missed run as one catch-up"
            # Settle: no storm of backdated runs may follow.
            await asyncio.sleep(1.0)
            assert len(mgr.get_history(limit=50)) == 2

            job = mgr.scheduler.get_job("capability_check_periodic")
            assert job is not None and job.next_run_time is not None
            next_ts = job.next_run_time.timestamp()
            now = time.time()
            assert 0 < next_ts - now <= WEEK_S
            # Aligned to the schedule's own occurrences from the last run,
            # not "now + one period".
            offset = (next_ts - seeded_at) % WEEK_S
            assert min(offset, WEEK_S - offset) < 5.0
        finally:
            mgr.stop()

    @pytest.mark.asyncio
    async def test_many_periods_overdue_still_exactly_one_catchup(self, tmp_path):
        db = tmp_path / "caps.db"
        mgr = CapabilitySchedulerManager(db_path=db)
        mgr.set_schedule("weekly", "Option A")
        _seed_run(mgr, time.time() - 35 * 86400)

        mgr.start()
        try:
            count = await _wait_for_history_count(mgr, 2)
            assert count == 2
            await asyncio.sleep(1.0)
            assert len(mgr.get_history(limit=50)) == 2
        finally:
            mgr.stop()

    @pytest.mark.asyncio
    async def test_not_yet_due_runs_nothing_at_startup(self, tmp_path):
        db = tmp_path / "caps.db"
        mgr = CapabilitySchedulerManager(db_path=db)
        mgr.set_schedule("weekly", "Option A")
        seeded_at = time.time() - 3 * 86400
        _seed_run(mgr, seeded_at)

        mgr.start()
        try:
            await asyncio.sleep(1.2)
            assert len(mgr.get_history(limit=50)) == 1

            job = mgr.scheduler.get_job("capability_check_periodic")
            assert job is not None and job.next_run_time is not None
            next_ts = job.next_run_time.timestamp()
            # Next fire is the occurrence 7 days after the last run.
            assert abs(next_ts - (seeded_at + WEEK_S)) < 5.0
        finally:
            mgr.stop()

    @pytest.mark.asyncio
    async def test_no_history_no_catchup(self, tmp_path):
        mgr = CapabilitySchedulerManager(db_path=tmp_path / "caps.db")
        mgr.start()
        try:
            await asyncio.sleep(1.2)
            assert mgr.get_history(limit=50) == []
        finally:
            mgr.stop()

    @pytest.mark.asyncio
    async def test_schedule_none_never_recovers(self, tmp_path):
        db = tmp_path / "caps.db"
        mgr = CapabilitySchedulerManager(db_path=db)
        mgr.set_schedule("none", "Option A")
        _seed_run(mgr, time.time() - 30 * 86400)

        mgr.start()
        try:
            await asyncio.sleep(1.2)
            assert len(mgr.get_history(limit=50)) == 1
            assert mgr.scheduler.get_job("capability_check_periodic") is None
        finally:
            mgr.stop()


class TestScheduleRegistration:
    @pytest.mark.asyncio
    async def test_cron_string_registers_cron_trigger(self, tmp_path):
        mgr = CapabilitySchedulerManager(db_path=tmp_path / "caps.db")
        mgr.start()
        try:
            mgr.set_schedule("0 2 * * *", "Both")
            job = mgr.scheduler.get_job("capability_check_periodic")
            assert job is not None
            assert isinstance(job.trigger, CronTrigger)
            assert job.next_run_time is not None
            assert job.next_run_time.hour == 2
            assert job.next_run_time.minute == 0
            assert mgr.get_status_summary()["schedule_error"] is None
        finally:
            mgr.stop()

    @pytest.mark.asyncio
    async def test_named_intervals_still_register_as_intervals(self, tmp_path):
        mgr = CapabilitySchedulerManager(db_path=tmp_path / "caps.db")
        mgr.start()
        try:
            mgr.set_schedule("daily", "Both")
            job = mgr.scheduler.get_job("capability_check_periodic")
            assert isinstance(job.trigger, IntervalTrigger)
            assert job.trigger.interval_length == 86400

            mgr.set_schedule("weekly", "Both")
            job = mgr.scheduler.get_job("capability_check_periodic")
            assert isinstance(job.trigger, IntervalTrigger)
            assert job.trigger.interval_length == WEEK_S
        finally:
            mgr.stop()

    @pytest.mark.asyncio
    async def test_unparseable_schedule_refused_loudly(self, tmp_path):
        mgr = CapabilitySchedulerManager(db_path=tmp_path / "caps.db")
        mgr.start()
        try:
            mgr.set_schedule("whenever it feels like it", "Both")
            # No job silently running at 12 h under a false name.
            assert mgr.scheduler.get_job("capability_check_periodic") is None
            summary = mgr.get_status_summary()
            assert summary["schedule_error"]
            assert "whenever it feels like it" in summary["schedule_error"]

            # A later valid schedule clears the error and registers.
            mgr.set_schedule("daily", "Both")
            assert mgr.scheduler.get_job("capability_check_periodic") is not None
            assert mgr.get_status_summary()["schedule_error"] is None
        finally:
            mgr.stop()

    @pytest.mark.asyncio
    async def test_invalid_cron_fields_refused(self, tmp_path):
        mgr = CapabilitySchedulerManager(db_path=tmp_path / "caps.db")
        mgr.start()
        try:
            mgr.set_schedule("99 25 * * *", "Both")
            assert mgr.scheduler.get_job("capability_check_periodic") is None
            assert mgr.get_status_summary()["schedule_error"]
        finally:
            mgr.stop()
