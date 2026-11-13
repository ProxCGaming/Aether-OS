"""Aether calendar layer: future events, conflicts, reminders.

Owns what no memory tier does — the user's schedule. See
``store.py`` for the storage design, ``extraction.py`` for pulling
events out of chat, ``itinerary.py`` for the prompt-facing briefing,
and ``reminders.py`` for the push fan-out.
"""

from aether_engine.calendar.store import (
    CalendarEvent,
    EventStore,
    get_calendar_store,
)

__all__ = ["CalendarEvent", "EventStore", "get_calendar_store"]
