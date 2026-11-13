"""Calendar tools: add_event / list_events / cancel_event.

One of the built-in tool families split out of ``tools/registry.py``
in the same style as ``tools/current_time.py``: the registry keeps
the assembly point; this module owns the factories. The store is
injectable for tests; production uses the shared store over
``~/.aether/calendar.db``.

Times cross the tool boundary as ISO-8601 strings in local time
(``2026-11-14T16:30`` or with an offset); naive values are read as
the user's local time, which is what a person means when they say
"4:30pm" to their own desktop assistant.
"""

import time
from datetime import datetime

from aether_engine.calendar.store import CalendarEvent, EventStore, get_calendar_store
from aether_engine.tools.registry import Tool, ToolRisk


def _parse_when(value: str) -> datetime:
    dt = datetime.fromisoformat(str(value).strip())
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt


def _format_event(event: CalendarEvent) -> str:
    start = event.start_dt
    when = start.strftime("%a %d %b %Y, %I:%M %p")
    if event.end_dt is not None:
        when += " – " + event.end_dt.strftime("%I:%M %p")
    return f"{event.title} — {when} (id: {event.id})"


def _add_event_fn(store: EventStore, **kwargs) -> str:
    title = str(kwargs.get("title") or "").strip()
    start_raw = kwargs.get("start")
    if not title:
        return "Error: add_event needs a title."
    if not start_raw:
        return "Error: add_event needs a start time (ISO-8601)."
    try:
        start_dt = _parse_when(str(start_raw))
        end_dt = _parse_when(str(kwargs["end"])) if kwargs.get("end") else None
    except ValueError:
        return (
            "Error: could not parse that date/time. Use ISO-8601, "
            "e.g. 2026-11-14T16:30."
        )
    try:
        event, conflicts = store.add_event(
            title,
            start_dt.timestamp(),
            end_dt.timestamp() if end_dt else None,
            source="tool",
        )
    except ValueError as exc:
        return f"Error: {exc}"
    reply = f"Added to calendar: {_format_event(event)}."
    if conflicts:
        names = "; ".join(_format_event(c) for c in conflicts)
        reply += f" Warning: this overlaps with {names}."
    return reply


def _list_events_fn(store: EventStore, **kwargs) -> str:
    days = kwargs.get("days")
    now = time.time()
    if days:
        try:
            horizon = now + float(days) * 86400
        except (TypeError, ValueError):
            return "Error: days must be a number."
        events = store.events_between(now - 3600, horizon)
        events = [e for e in events if e.effective_end_ts > now]
    else:
        events = store.upcoming(now, limit=int(kwargs.get("limit") or 10))
    if not events:
        return "No upcoming events on the calendar."
    lines = ["Upcoming events:"]
    lines.extend(f"- {_format_event(e)}" for e in events)
    return "\n".join(lines)


def _cancel_event_fn(store: EventStore, **kwargs) -> str:
    event_id = str(kwargs.get("event_id") or "").strip()
    title = str(kwargs.get("title") or "").strip()
    if event_id:
        removed = store.cancel_event(event_id)
        if removed is not None:
            return f"Cancelled: {_format_event(removed)}."
        if not title:
            return f"No calendar event with id {event_id}."
    if title:
        matches = store.search_upcoming_by_title(title, time.time())
        if len(matches) == 1:
            removed = store.cancel_event(matches[0].id)
            return f"Cancelled: {_format_event(removed)}."
        if len(matches) > 1:
            names = "; ".join(_format_event(m) for m in matches)
            return (
                "More than one upcoming event matches that title — "
                f"cancel by id instead: {names}."
            )
        return f"No upcoming event matching '{title}'."
    return "Error: cancel_event needs an event_id or a title."


def create_calendar_tools(store: EventStore | None = None) -> list[Tool]:
    store = store if store is not None else get_calendar_store()
    return [
        Tool(
            name="add_event",
            description=(
                "Add an event to the user's calendar. Use when the user "
                "asks to schedule, book or be reminded about something at "
                "a specific time. Reports scheduling conflicts."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "What the event is."},
                    "start": {
                        "type": "string",
                        "description": "Start as ISO-8601 local time, e.g. 2026-11-14T16:30.",
                    },
                    "end": {
                        "type": "string",
                        "description": "Optional end as ISO-8601 local time.",
                    },
                },
                "required": ["title", "start"],
            },
            execute_fn=lambda **kw: _add_event_fn(store, **kw),
            risk=ToolRisk.WRITE,
        ),
        Tool(
            name="list_events",
            description=(
                "List upcoming calendar events. Use when the user asks "
                "what's on, what's next, or whether a time is free."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "days": {
                        "type": "number",
                        "description": "Optional: only events within this many days.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum events to list (default 10).",
                    },
                },
            },
            execute_fn=lambda **kw: _list_events_fn(store, **kw),
            risk=ToolRisk.READ_ONLY,
        ),
        Tool(
            name="cancel_event",
            description=(
                "Cancel a calendar event by its id (from list_events) "
                "or by an unambiguous title. Use when the user asks to "
                "remove, cancel or undo a scheduled event."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "event_id": {"type": "string", "description": "The event id."},
                    "title": {
                        "type": "string",
                        "description": "Title to match when no id is known.",
                    },
                },
            },
            execute_fn=lambda **kw: _cancel_event_fn(store, **kw),
            risk=ToolRisk.WRITE,
        ),
    ]
