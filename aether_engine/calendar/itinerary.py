"""Itinerary briefing (Calendar layer C3, 14 Nov 2026).

Friday's LiveContext idea in Aether's shape: the model should never
have to ask what today holds, and never plan as if a busy slot were
free. This module renders the schedule as a compact text section —
today, tomorrow, the next week, and any clashes — following the
``memory/learned.learned_briefing`` contract exactly: best-effort,
capped, and an empty string (never an exception) when there is
nothing to say, so a broken or empty calendar can never break a
prompt.
"""

import logging
from datetime import datetime, time, timedelta

from aether_engine.calendar.store import CalendarEvent, EventStore, get_calendar_store

logger = logging.getLogger("aether_engine.calendar.itinerary")

BRIEFING_MAX_CHARS = 1500


def _fmt_time(event: CalendarEvent) -> str:
    return event.start_dt.strftime("%I:%M %p").lstrip("0")


def _fmt_day_event(event: CalendarEvent) -> str:
    text = f"{event.title} — {_fmt_time(event)}"
    if event.end_dt is not None:
        text += "–" + event.end_dt.strftime("%I:%M %p").lstrip("0")
    return text


def _day_bounds(day: datetime) -> tuple[float, float]:
    start = datetime.combine(day.date(), time.min, tzinfo=day.tzinfo)
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def _conflict_lines(events: list[CalendarEvent], store: EventStore) -> list[str]:
    lines: list[str] = []
    seen: set[tuple[str, str]] = set()
    for event in events:
        for other in store.find_conflicts(
            event.start_ts, event.effective_end_ts, exclude_id=event.id
        ):
            pair = tuple(sorted((event.id, other.id)))
            if pair in seen:
                continue
            seen.add(pair)
            lines.append(
                f'Clash: "{event.title}" overlaps "{other.title}" '
                f"({_fmt_day_event(event)} / {_fmt_day_event(other)})."
            )
    return lines


def calendar_briefing(
    store: EventStore | None = None,
    *,
    now: datetime | None = None,
    config=None,
) -> str:
    """The supervisor's calendar section, or "" when there is none.

    Sections: today, tomorrow, the following week (max 5 entries),
    then any clashes among them. ``config``/``store``/``now`` are
    injectable for tests; production callers pass nothing.
    """
    try:
        if config is None:
            from aether_engine.config import load_config

            config = load_config()
        if not getattr(config, "calendar_enabled", True):
            return ""
        store = store if store is not None else get_calendar_store()
        now = now or datetime.now().astimezone()
        now_ts = now.timestamp()

        today_from, today_to = _day_bounds(now)
        tomorrow_from, tomorrow_to = today_to, today_to + 86400
        week_to = tomorrow_to + 6 * 86400

        today = store.events_between(today_from, today_to)
        today = [e for e in today if e.effective_end_ts > now_ts]
        tomorrow = store.events_between(tomorrow_from, tomorrow_to)
        week = [
            e
            for e in store.events_between(tomorrow_to, week_to)
        ][:5]

        if not (today or tomorrow or week):
            return ""

        lines = ["The user's calendar (local time):"]
        if today:
            lines.append(
                f"- Today ({now.strftime('%a %d %b')}): "
                + "; ".join(_fmt_day_event(e) for e in today)
            )
        if tomorrow:
            day_label = (now + timedelta(days=1)).strftime("%a %d %b")
            lines.append(
                f"- Tomorrow ({day_label}): "
                + "; ".join(_fmt_day_event(e) for e in tomorrow)
            )
        for event in week:
            lines.append(
                f"- {event.start_dt.strftime('%a %d %b')}: {_fmt_day_event(event)}"
            )
        lines.extend(_conflict_lines(today + tomorrow + week, store))
        text = "\n".join(lines)
        return text[:BRIEFING_MAX_CHARS]
    except Exception:
        logger.debug("Calendar briefing failed", exc_info=True)
        return ""


def fast_chat_itinerary_line(
    store: EventStore | None = None,
    *,
    now: datetime | None = None,
    config=None,
) -> str:
    """Fast chat's one-line version: today's remaining events plus
    the next event whenever it is. "" when the calendar is empty."""
    try:
        if config is None:
            from aether_engine.config import load_config

            config = load_config()
        if not getattr(config, "calendar_enabled", True):
            return ""
        store = store if store is not None else get_calendar_store()
        now = now or datetime.now().astimezone()
        upcoming = store.upcoming(now.timestamp(), limit=3)
        if not upcoming:
            return ""
        parts = []
        for event in upcoming:
            when = (
                "today " + _fmt_time(event)
                if event.start_dt.date() == now.date()
                else event.start_dt.strftime("%a %d %b ") + _fmt_time(event)
            )
            parts.append(f"{event.title} ({when})")
        return "Calendar — upcoming: " + "; ".join(parts) + "."
    except Exception:
        logger.debug("Fast-chat itinerary failed", exc_info=True)
        return ""
