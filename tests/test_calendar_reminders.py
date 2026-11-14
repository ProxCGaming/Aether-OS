"""Reminder dispatch + scheduler registration (C4)."""

import time
from datetime import datetime, timedelta

import pytest

from aether_common.contracts import EventType
from aether_engine.calendar import reminders
from aether_engine.calendar.store import EventStore


@pytest.fixture()
def store(tmp_path):
    return EventStore(tmp_path / "calendar.db")


@pytest.mark.asyncio
async def test_dispatch_lead_reminder_to_subscribers(store):
    # Day-before disabled: near midnight the +15 min event lands
    # tomorrow and would also earn a day-before dispatch in the same
    # pass — correct engine behaviour (see the note in
    # test_calendar_store.py), not what this lead-only test asserts.
    now = time.time()
    store.add_event("PTM", now + 15 * 60)
    received = []

    async def collector(event):
        received.append(event)

    unsubscribe = reminders.subscribe_reminder_events(collector)
    try:
        events = await reminders.dispatch_due_reminders(
            store, day_before_enabled=False, lead_minutes=30, now_ts=now
        )
    finally:
        unsubscribe()
    assert len(events) == 1
    assert events[0].type == EventType.REMINDER_DUE
    assert events[0].payload["title"] == "PTM"
    assert events[0].payload["kind"] == "lead"
    assert received == events
    # Claimed: a second dispatch at the same instant sends nothing.
    assert (
        await reminders.dispatch_due_reminders(
            store, day_before_enabled=False, lead_minutes=30, now_ts=now
        )
        == []
    )


@pytest.mark.asyncio
async def test_dispatch_day_before(store):
    now = time.time()
    tomorrow = datetime.now().astimezone() + timedelta(days=1)
    store.add_event("Dentist", tomorrow.timestamp())
    events = await reminders.dispatch_due_reminders(
        store, day_before_enabled=True, lead_minutes=30, now_ts=now
    )
    assert [e.payload["kind"] for e in events] == ["day_before"]
    # With day-before disabled, nothing fires for a fresh event.
    store.add_event("Other", (tomorrow + timedelta(hours=3)).timestamp())
    assert (
        await reminders.dispatch_due_reminders(
            store, day_before_enabled=False, lead_minutes=30, now_ts=now
        )
        == []
    )


@pytest.mark.asyncio
async def test_failing_subscriber_does_not_starve_others(store):
    now = time.time()
    store.add_event("Soon", now + 5 * 60)
    received = []

    def bad(_event):
        raise RuntimeError("boom")

    unsub_bad = reminders.subscribe_reminder_events(bad)
    unsub_good = reminders.subscribe_reminder_events(received.append)
    try:
        # Day-before disabled — same midnight sensitivity as above:
        # this test counts dispatches for one lead reminder only.
        events = await reminders.dispatch_due_reminders(
            store, day_before_enabled=False, lead_minutes=30, now_ts=now
        )
    finally:
        unsub_bad()
        unsub_good()
    assert len(events) == 1 and received == events


@pytest.mark.asyncio
async def test_scheduler_registers_reminder_job(tmp_path):
    from aether_engine.scheduler.capability_jobs import CapabilitySchedulerManager

    mgr = CapabilitySchedulerManager(db_path=tmp_path / "caps.db")
    mgr.start()
    try:
        assert mgr.scheduler is not None
        job = mgr.scheduler.get_job("calendar_reminders")
        assert job is not None
    finally:
        mgr.stop()


@pytest.mark.asyncio
async def test_reminder_job_noop_without_due_events(tmp_path, monkeypatch):
    from aether_engine.scheduler.capability_jobs import CapabilitySchedulerManager

    store = EventStore(tmp_path / "calendar.db")
    monkeypatch.setattr(
        "aether_engine.calendar.store._default_store", store
    )
    mgr = CapabilitySchedulerManager(db_path=tmp_path / "caps.db")
    assert await mgr._run_reminder_job() == []
