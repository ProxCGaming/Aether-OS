"""Chat event extraction (C2): table-driven, fixed clock.

The fixed ``now`` is Friday 13 Nov 2026, 10:00 IST-local (the test
uses an aware datetime; all expectations derive from it).
"""

from datetime import datetime, timedelta, timezone

import pytest

from aether_engine.calendar.extraction import (
    calendar_note_for_prompt,
    extract_event,
    process_user_message,
)
from aether_engine.calendar.store import EventStore

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 11, 13, 10, 0, tzinfo=IST)  # a Friday


def test_i_have_a_meeting_tomorrow():
    ev = extract_event("I have a meeting tomorrow at 3pm", now=NOW)
    assert ev is not None
    assert ev.title == "Meeting"
    assert ev.start_dt == datetime(2026, 11, 14, 15, 0, tzinfo=IST)
    assert ev.end_dt is None


def test_remind_me_ptm_saturday():
    ev = extract_event("Remind me about the PTM on Saturday at 4:30pm", now=NOW)
    assert ev is not None
    assert ev.start_dt == datetime(2026, 11, 14, 16, 30, tzinfo=IST)
    assert "PTM" in ev.title


def test_explicit_date_and_range():
    ev = extract_event("Schedule dentist on 20 November from 4pm to 6pm", now=NOW)
    assert ev is not None
    assert ev.start_dt == datetime(2026, 11, 20, 16, 0, tzinfo=IST)
    assert ev.end_dt == datetime(2026, 11, 20, 18, 0, tzinfo=IST)


def test_date_first_slashes_and_default_time():
    ev = extract_event("I have an appointment on 25/12/2026", now=NOW)
    assert ev is not None
    assert ev.start_dt == datetime(2026, 12, 25, 9, 0, tzinfo=IST)


def test_in_two_hours():
    ev = extract_event("Remind me to call Maa in 2 hours", now=NOW)
    assert ev is not None
    assert ev.start_dt == NOW + timedelta(hours=2)


def test_time_only_means_today_then_tomorrow():
    ev = extract_event("I have a call at 6pm", now=NOW)
    assert ev is not None
    assert ev.start_dt == datetime(2026, 11, 13, 18, 0, tzinfo=IST)
    past = extract_event("I have a call at 8am", now=NOW)
    assert past is not None
    assert past.start_dt == datetime(2026, 11, 14, 8, 0, tzinfo=IST)


def test_weekday_next():
    ev = extract_event("I have a class next Monday at 10am", now=NOW)
    assert ev is not None
    assert ev.start_dt == datetime(2026, 11, 23, 10, 0, tzinfo=IST)


@pytest.mark.parametrize(
    "text",
    [
        "What happened on 14 November 2024?",  # question
        "When is my meeting tomorrow?",  # question
        "I have a meeting yesterday at 3pm",  # no future date parses
        "The meeting was on 10 November at 3pm",  # past, no signal
        "Just chatting, nothing scheduled",  # no date/time at all
        "I have a question about tomorrow",  # signal-ish but no event shape
    ],
)
def test_no_extraction(text):
    assert extract_event(text, now=NOW) is None


def test_process_stores_and_dedupes(tmp_path):
    store = EventStore(tmp_path / "calendar.db")
    result = process_user_message("I have a meeting tomorrow at 3pm", store, now=NOW)
    assert result is not None
    event, conflicts = result
    assert event.title == "Meeting" and conflicts == []
    again = process_user_message("I have a meeting tomorrow at 3pm", store, now=NOW)
    assert again is not None and again[0].id == event.id
    assert len(store.upcoming(NOW.timestamp())) == 1


def test_note_mentions_undo_and_conflict(tmp_path):
    store = EventStore(tmp_path / "calendar.db")
    note = calendar_note_for_prompt("I have a meeting tomorrow at 3pm", store, now=NOW)
    assert note is not None and "Meeting" in note and "cancel that" in note
    clash = calendar_note_for_prompt(
        "I have a viva tomorrow at 3:30pm", store, now=NOW
    )
    assert clash is not None and "overlaps" in clash
