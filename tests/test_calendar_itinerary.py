"""Itinerary briefing (C3): supervisor section + fast-chat line."""

from datetime import datetime, timedelta, timezone

import pytest

from aether_engine.calendar import itinerary
from aether_engine.calendar.store import EventStore
from aether_engine.config import UserConfig

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 11, 13, 10, 0, tzinfo=IST)  # Friday


@pytest.fixture()
def store(tmp_path):
    return EventStore(tmp_path / "calendar.db")


def _add(store, title, dt, end=None):
    return store.add_event(
        title, dt.timestamp(), end.timestamp() if end else None
    )[0]


def test_briefing_today_tomorrow_week(store):
    _add(store, "Standup", NOW.replace(hour=12))
    _add(store, "PTM", NOW + timedelta(days=1, hours=6, minutes=30))
    _add(store, "Dentist", NOW + timedelta(days=5, hours=2))
    text = itinerary.calendar_briefing(store, now=NOW, config=UserConfig())
    assert "Today (Fri 13 Nov)" in text and "Standup" in text
    assert "Tomorrow (Sat 14 Nov)" in text and "PTM" in text
    assert "Dentist" in text


def test_briefing_skips_finished_today(store):
    _add(store, "Early call", NOW.replace(hour=7))
    text = itinerary.calendar_briefing(store, now=NOW, config=UserConfig())
    assert text == ""


def test_briefing_reports_clash(store):
    _add(store, "Meeting", NOW + timedelta(days=1, hours=5))
    _add(store, "Viva", NOW + timedelta(days=1, hours=5, minutes=30))
    text = itinerary.calendar_briefing(store, now=NOW, config=UserConfig())
    assert "Clash" in text and "Meeting" in text and "Viva" in text


def test_briefing_empty_and_disabled(store):
    cfg = UserConfig()
    assert itinerary.calendar_briefing(store, now=NOW, config=cfg) == ""
    _add(store, "PTM", NOW + timedelta(days=1))
    cfg.calendar_enabled = False
    assert itinerary.calendar_briefing(store, now=NOW, config=cfg) == ""
    assert itinerary.fast_chat_itinerary_line(store, now=NOW, config=cfg) == ""


def test_fast_chat_line(store):
    _add(store, "Lunch with Maa", NOW.replace(hour=13))
    _add(store, "PTM", NOW + timedelta(days=1, hours=6, minutes=30))
    line = itinerary.fast_chat_itinerary_line(store, now=NOW, config=UserConfig())
    assert line.startswith("Calendar — upcoming:")
    assert "Lunch with Maa (today 1:00 PM)" in line
    assert "PTM (Sat 14 Nov" in line


def test_supervisor_prompt_carries_itinerary(store, monkeypatch):
    import aether_engine.calendar.store as store_module

    # The production prompt path reads the real clock — the event
    # must be ahead of it, not of the fixed NOW used above.
    real_now = datetime.now().astimezone()
    _add(store, "Board review", real_now + timedelta(hours=3))
    monkeypatch.setattr(store_module, "_default_store", store)
    from aether_engine.langgraph.nodes.supervisor import _supervisor_system_prompt

    prompt = _supervisor_system_prompt("hello", "No plan currently.", ["planner"])
    assert "Board review" in prompt
    assert "The user's calendar" in prompt
