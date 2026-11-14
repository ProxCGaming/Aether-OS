"""Calendar store (C1): CRUD, conflicts, upcoming, reminder claims.

All stores live in tmp_path — the real ~/.aether/calendar.db is
never touched.
"""

import time
from datetime import datetime, timedelta

import pytest

from aether_engine.calendar.store import EventStore


@pytest.fixture()
def store(tmp_path):
    return EventStore(tmp_path / "calendar.db")


def _ts(dt: datetime) -> float:
    return dt.timestamp()


def _in(**kwargs) -> datetime:
    return datetime.now().astimezone() + timedelta(**kwargs)


def test_add_and_get_roundtrip(store):
    start = _in(days=1)
    event, conflicts = store.add_event("Dentist", _ts(start))
    assert conflicts == []
    fetched = store.get_event(event.id)
    assert fetched is not None
    assert fetched.title == "Dentist"
    assert fetched.start_ts == pytest.approx(_ts(start))
    assert fetched.end_ts is None
    assert fetched.reminder_lead_sent is False


def test_add_rejects_empty_title_and_bad_range(store):
    with pytest.raises(ValueError):
        store.add_event("  ", _ts(_in(days=1)))
    start = _in(days=1)
    with pytest.raises(ValueError):
        store.add_event("Backwards", _ts(start), _ts(start - timedelta(hours=1)))


def test_conflict_overlap_and_adjacency(store):
    base = _in(days=1).replace(minute=0, second=0, microsecond=0)
    store.add_event("A", _ts(base), _ts(base + timedelta(hours=1)))
    # Overlapping B conflicts with A.
    _, conflicts = store.add_event(
        "B", _ts(base + timedelta(minutes=30)), _ts(base + timedelta(hours=2))
    )
    assert [c.title for c in conflicts] == ["A"]
    # Adjacent C (starts exactly when A ends, B still running) conflicts with B only.
    _, conflicts_c = store.add_event(
        "C", _ts(base + timedelta(hours=1)), _ts(base + timedelta(hours=2))
    )
    assert [c.title for c in conflicts_c] == ["B"]
    # Far-away D conflicts with nothing.
    _, conflicts_d = store.add_event("D", _ts(base + timedelta(hours=5)))
    assert conflicts_d == []


def test_conflict_uses_one_hour_default_window(store):
    base = _in(days=2).replace(minute=0, second=0, microsecond=0)
    store.add_event("NoEnd", _ts(base))  # effective window: base .. base+1h
    _, conflicts = store.add_event("Inside", _ts(base + timedelta(minutes=45)))
    assert [c.title for c in conflicts] == ["NoEnd"]
    _, conflicts2 = store.add_event("After", _ts(base + timedelta(hours=2)))
    assert conflicts2 == []


def test_upcoming_orders_and_skips_finished(store):
    now = time.time()
    store.add_event("Past", now - 7200, now - 3600)
    store.add_event("Later", now + 7200)
    store.add_event("Sooner", now + 1800)
    titles = [e.title for e in store.upcoming(now)]
    assert titles == ["Sooner", "Later"]


def test_events_between(store):
    base = _in(days=1).replace(minute=0, second=0, microsecond=0)
    store.add_event("Morning", _ts(base.replace(hour=9)))
    store.add_event("Evening", _ts(base.replace(hour=18)))
    day = store.events_between(
        _ts(base.replace(hour=0)), _ts(base.replace(hour=23, minute=59))
    )
    assert [e.title for e in day] == ["Morning", "Evening"]
    morning = store.events_between(
        _ts(base.replace(hour=8)), _ts(base.replace(hour=12))
    )
    assert [e.title for e in morning] == ["Morning"]


def test_cancel_event(store):
    event, _ = store.add_event("Gone", _ts(_in(days=1)))
    removed = store.cancel_event(event.id)
    assert removed is not None and removed.title == "Gone"
    assert store.get_event(event.id) is None
    assert store.cancel_event(event.id) is None


def test_search_upcoming_by_title(store):
    now = time.time()
    store.add_event("College PTM", now + 3600)
    store.add_event("Gym session", now + 7200)
    hits = store.search_upcoming_by_title("ptm", now)
    assert [e.title for e in hits] == ["College PTM"]


def test_lead_reminder_claimed_once(store):
    # Day-before disabled for the same reason as the test below: near
    # midnight the +20 min event lands tomorrow and would also earn a
    # day-before claim in the same pass — correct engine behaviour,
    # not what this lead-only assertion is about.
    now = time.time()
    store.add_event("Soon", now + 20 * 60)  # starts in 20 min, lead = 30
    due = store.due_reminders(now, day_before_enabled=False, lead_minutes=30)
    assert [(d["event"].title, d["kind"]) for d in due] == [("Soon", "lead")]
    # Second pass at the same instant: already claimed, nothing due.
    assert store.due_reminders(now, day_before_enabled=False, lead_minutes=30) == []


def test_lead_reminder_not_due_yet_and_disabled(store):
    # This test is about the LEAD kind only, so the day-before kind is
    # disabled: with it enabled, "Later" (+90 min) is legitimately due
    # as a day-before reminder whenever the run happens late enough in
    # the evening that the event lands tomorrow (the engine is right;
    # the assertion was wall-clock-sensitive — it failed at 23:38 IST
    # on 14 Nov after passing all day). The day-before kind has its
    # own test below (test_day_before_reminder).
    now = time.time()
    store.add_event("Later", now + 90 * 60)
    assert store.due_reminders(now, day_before_enabled=False, lead_minutes=30) == []
    store.add_event("Soon", now + 10 * 60)
    assert store.due_reminders(now, day_before_enabled=False, lead_minutes=0) == []


def test_day_before_reminder(store):
    now = time.time()
    tomorrow = datetime.now().astimezone() + timedelta(days=1)
    store.add_event("Tomorrow thing", _ts(tomorrow))
    due = store.due_reminders(now, day_before_enabled=True, lead_minutes=30)
    assert [(d["event"].title, d["kind"]) for d in due] == [
        ("Tomorrow thing", "day_before")
    ]
    assert store.due_reminders(now, day_before_enabled=True, lead_minutes=30) == []
    # Disabled flag: a fresh store/event stays silent.
    store2_event, _ = store.add_event("Other", _ts(tomorrow + timedelta(hours=2)))
    assert store2_event is not None
    assert store.due_reminders(now, day_before_enabled=False, lead_minutes=30) == []
