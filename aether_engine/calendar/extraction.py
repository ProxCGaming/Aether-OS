"""Chat event extraction (Calendar layer C2, 14 Nov 2026).

Friday's EventEngine insight, kept: the hot path is pure regex +
SQLite — no LLM call — so noticing "I have a meeting tomorrow at
3pm" costs milliseconds and works offline. The extractor is
deliberately conservative:

- Questions never extract ("what happened on 14 Nov?" is not an
  event being scheduled).
- Past dates never extract.
- A scheduling signal is required: an explicit verb ("remind me",
  "schedule", "add") or a statement of a future event ("I have a
  meeting...", "my PTM is on...").
- A miss is acceptable (the user can still say "add that" and the
  add_event tool fires); a false positive is merely annoying, and
  every extraction is announced in the reply with an undo path
  (cancel_event), never silently stored.

Dates are read day-first (14/11 = 14 November), matching the user's
locale. A date with no time defaults to 09:00 local; a time with no
date means today if it is still ahead, else tomorrow.
"""

import logging
import re
from dataclasses import dataclass
from datetime import date as dt_date
from datetime import datetime, timedelta
from datetime import time as dt_time

from aether_engine.calendar.store import CalendarEvent, EventStore

logger = logging.getLogger("aether_engine.calendar.extraction")

DEFAULT_HOUR = 9  # a date with no time starts at 09:00 local

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

_SCHEDULING_VERBS = re.compile(
    r"\b(remind me|schedule|add to (my )?calendar|book|set a reminder)\b",
    re.IGNORECASE,
)
_EVENT_STATEMENTS = re.compile(
    r"\b(i have|i've got|there is|there's|my .{1,40}? is (on|at|tomorrow|today)|"
    r"is scheduled|is booked)\b",
    re.IGNORECASE,
)
_EVENT_NOUNS = re.compile(
    r"\b(meeting|appointment|class|lecture|ptm|call|viva|exam|test|deadline|"
    r"event|session|interview|visit|flight|train|birthday|anniversary|"
    r"party|workshop|seminar|webinar|review|demo|presentation|shift|"
    r"dentist|doctor|flight|trip|game|match)\b",
    re.IGNORECASE,
)
_PAST_MARKERS = re.compile(
    r"\b(yesterday|ago|last\s+(week|month|year|night|monday|tuesday|"
    r"wednesday|thursday|friday|saturday|sunday))\b",
    re.IGNORECASE,
)
_QUESTION_START = re.compile(
    r"^\s*(what|when|where|who|why|how|is|are|do|does|did|can|could|"
    r"would|should|will)\b",
    re.IGNORECASE,
)

_TIME_RE = re.compile(
    r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b|\b([01]?\d|2[0-3]):([0-5]\d)\b",
    re.IGNORECASE,
)
_IN_RE = re.compile(r"\bin\s+(\d+)\s*(minutes?|mins?|hours?|hrs?|days?)\b", re.IGNORECASE)
_DATE_ISO_RE = re.compile(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b")
_DATE_DMY_RE = re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](20\d{2})\b")
_DATE_MONTH_NAME_RE = re.compile(
    r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(" + "|".join(_MONTHS) + r")\b"
    r"(?:\s+(20\d{2}))?|\b(" + "|".join(_MONTHS) + r")\s+(\d{1,2})(?:st|nd|rd|th)?"
    r"(?:,?\s+(20\d{2}))?\b",
    re.IGNORECASE,
)
_WEEKDAY_RE = re.compile(
    r"\b(this|next)?\s*(" + "|".join(_WEEKDAYS) + r")\b", re.IGNORECASE
)
_RANGE_TO_RE = re.compile(r"\bto\b", re.IGNORECASE)


@dataclass
class ExtractedEvent:
    title: str
    start_dt: datetime
    end_dt: datetime | None


def _parse_time_at(text: str, pos: int = 0) -> tuple[dt_time | None, tuple[int, int] | None]:
    """First clock time at/after ``pos``; returns (time, span)."""
    m = _TIME_RE.search(text, pos)
    if not m:
        return None, None
    if m.group(3) is not None:  # 12-hour with am/pm
        hour = int(m.group(1)) % 12
        if m.group(3).lower() == "pm":
            hour += 12
        minute = int(m.group(2) or 0)
    else:  # 24-hour HH:MM
        hour, minute = int(m.group(4)), int(m.group(5))
    return dt_time(hour, minute), (m.start(), m.end())


def _find_date(text: str, now: datetime) -> tuple[dt_date | None, tuple[int, int] | None, bool]:
    """First explicit/relative date; returns (date, span, is_relative_now).

    ``is_relative_now`` marks the "in N hours" family, where the time
    comes from the offset itself, not a clock time.
    """
    m = _IN_RE.search(text)
    if m:
        amount = int(m.group(1))
        unit = m.group(2).lower()
        delta = (
            timedelta(minutes=amount)
            if unit.startswith("min")
            else timedelta(hours=amount)
            if unit.startswith("h")
            else timedelta(days=amount)
        )
        target = now + delta
        return target.date(), (m.start(), m.end()), True
    lowered = text.lower()
    for phrase, offset in (("day after tomorrow", 2), ("tomorrow", 1), ("today", 0)):
        idx = lowered.find(phrase)
        if idx >= 0:
            return (now + timedelta(days=offset)).date(), (idx, idx + len(phrase)), False
    m = _DATE_ISO_RE.search(text)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date(), (m.start(), m.end()), False
        except ValueError:
            return None, None, False
    m = _DATE_DMY_RE.search(text)
    if m:
        try:
            return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1))).date(), (m.start(), m.end()), False
        except ValueError:
            return None, None, False
    m = _DATE_MONTH_NAME_RE.search(text)
    if m:
        if m.group(1) is not None:
            day, month, year = int(m.group(1)), _MONTHS[m.group(2).lower()], m.group(3)
        else:
            day, month, year = int(m.group(5)), _MONTHS[m.group(4).lower()], m.group(6)
        year = int(year) if year else now.year
        try:
            candidate = datetime(year, month, day).date()
        except ValueError:
            return None, None, False
        # No roll-forward: a yearless date in the past stays in the
        # past (a statement about last month is not next year's
        # booking); the caller's future check rejects it.
        return candidate, (m.start(), m.end()), False
    m = _WEEKDAY_RE.search(text)
    if m:
        target = _WEEKDAYS[m.group(2).lower()]
        days_ahead = (target - now.weekday()) % 7
        if m.group(1) and m.group(1).lower() == "next":
            # "next Friday" = the Friday of next week, never today.
            days_ahead = days_ahead + 7 if days_ahead > 0 else 7
        return (now + timedelta(days=days_ahead)).date(), (m.start(), m.end()), False
    return None, None, False


_TITLE_LEADERS = re.compile(
    r"^\s*(please\s+)?(remind me\s+(to|about|of)?\s*|add\s+|schedule\s+|"
    r"i have\s+(a\s+|an\s+|the\s+)?|i've got\s+(a\s+|an\s+)?|"
    r"there is\s+(a\s+|an\s+)?|there's\s+(a\s+|an\s+)?|set a reminder\s+(for\s+)?)",
    re.IGNORECASE,
)
_TITLE_NOISE = re.compile(
    r"(?:\s+(on|at|for|from))+\s*$|[.\s]+$", re.IGNORECASE
)


def _clean_title(text: str, spans: list[tuple[int, int]]) -> str:
    kept = []
    last = 0
    for start, end in sorted(spans):
        kept.append(text[last:start])
        last = max(last, end)
    kept.append(text[last:])
    title = " ".join(" ".join(kept).split())
    title = _TITLE_LEADERS.sub("", title).strip()
    title = _TITLE_NOISE.sub("", title).strip(" ,.")
    # A range's leftover "to" between two removed times can strand a
    # leading/trailing "to" — drop it too.
    title = re.sub(r"^(to|from)\s+|\s+(to|from)$", "", title, flags=re.IGNORECASE).strip(" ,.")
    return title[:1].upper() + title[1:] if title else ""


def extract_event(text: str, *, now: datetime | None = None) -> ExtractedEvent | None:
    """Pull one future event out of a user message, or None.

    Pure function — no store, no clock reads beyond ``now`` — so the
    whole behaviour is table-testable.
    """
    if not text or len(text) > 1000:
        return None
    now = now or datetime.now().astimezone()
    stripped = text.strip()
    if stripped.endswith("?") or _QUESTION_START.match(stripped):
        return None
    if _PAST_MARKERS.search(text):
        return None
    # Signal gate, three strengths:
    #   1. an explicit scheduling verb ("remind me", "schedule") —
    #      always enough on its own;
    #   2. a statement of a future event ("I have...", "there's...",
    #      "my X is on...") — needs an event noun too, so "I have a
    #      question about tomorrow" stays a question, not an event;
    #   3. shorthand ("PTM tomorrow at 4:30pm") — an event noun plus
    #      the full date+time shape found below, checked after parsing.
    has_verb = bool(_SCHEDULING_VERBS.search(text))
    has_statement = bool(_EVENT_STATEMENTS.search(text)) and bool(
        _EVENT_NOUNS.search(text)
    )
    # Path 3 (shorthand) is decided after the date/time parse; with
    # no verb, no statement and no event noun there is nothing here.
    if not (has_verb or has_statement) and not _EVENT_NOUNS.search(text):
        return None

    date, date_span, is_relative = _find_date(text, now)
    spans: list[tuple[int, int]] = []
    if date_span:
        spans.append(date_span)

    start_time, time_span = _parse_time_at(text)
    if time_span:
        spans.append(time_span)

    if is_relative:
        # "in 2 hours" / "in 3 days": the offset fixes the moment.
        # Minutes/hours keep the exact offset; days land on the
        # clock time when one was given, else the 09:00 default.
        amount_match = _IN_RE.search(text)
        amount = int(amount_match.group(1))
        unit = amount_match.group(2).lower()
        if unit.startswith("min"):
            start_dt = now + timedelta(minutes=amount)
        elif unit.startswith("h"):
            start_dt = now + timedelta(hours=amount)
        else:
            day = (now + timedelta(days=amount)).date()
            start_dt = datetime.combine(
                day,
                start_time or dt_time(DEFAULT_HOUR, 0),
                tzinfo=now.tzinfo,
            )
        end_dt = None
    else:
        if date is None and start_time is None:
            return None
        if date is None:
            candidate = now.date()
            start_dt = datetime.combine(candidate, start_time, tzinfo=now.tzinfo)
            if start_dt <= now:
                start_dt = start_dt + timedelta(days=1)
        else:
            start_dt = datetime.combine(
                date,
                start_time or datetime.min.time().replace(hour=DEFAULT_HOUR),
                tzinfo=now.tzinfo,
            )
        end_dt = None
        # Optional range: "... 4pm to 6pm" — a second time after a "to".
        if time_span:
            rest = text[time_span[1]:]
            to_match = _RANGE_TO_RE.search(rest)
            if to_match:
                end_time, end_span = _parse_time_at(rest, to_match.end())
                if end_time is not None and end_span is not None:
                    end_dt = datetime.combine(start_dt.date(), end_time, tzinfo=now.tzinfo)
                    if end_dt <= start_dt:
                        end_dt = None
                    else:
                        spans.append((time_span[1] + end_span[0], time_span[1] + end_span[1]))

    # Shorthand path: noun only — demand the full date+time shape.
    if (
        not (has_verb or has_statement)
        and not is_relative
        and (date is None or start_time is None)
    ):
        return None
    if start_dt <= now:
        return None
    title = _clean_title(text, spans)
    if not title:
        return None
    return ExtractedEvent(title=title, start_dt=start_dt, end_dt=end_dt)


def process_user_message(
    text: str,
    store: EventStore,
    *,
    now: datetime | None = None,
) -> tuple[CalendarEvent, list[CalendarEvent]] | None:
    """Extract + store one event from a chat message.

    Returns (stored event, conflicts) or None when nothing was
    extracted. An identical title at the identical start returns the
    existing event instead of double-adding (a user repeating
    themselves in fast chat and the graph must not fork the diary).
    """
    extracted = extract_event(text, now=now)
    if extracted is None:
        return None
    start_ts = extracted.start_dt.timestamp()
    end_ts = extracted.end_dt.timestamp() if extracted.end_dt else None
    window = store.events_between(start_ts - 1, start_ts + 1)
    for existing in window:
        if existing.title.lower() == extracted.title.lower() and abs(existing.start_ts - start_ts) < 1:
            return existing, []
    event, conflicts = store.add_event(
        extracted.title,
        start_ts,
        end_ts,
        source="chat",
        source_text=text,
    )
    return event, conflicts


def calendar_note_for_prompt(
    text: str,
    store: EventStore,
    *,
    now: datetime | None = None,
) -> str | None:
    """The chat-entry hook shared by fast chat and the graph runner.

    Stores any extracted event and returns the system note that makes
    the reply announce it (the propose-in-reply rule), or None.
    Never raises to the caller beyond what the caller guards — but
    keeps its own failures silent-but-logged for the same reason:
    calendar must never break a chat turn.
    """
    try:
        result = process_user_message(text, store, now=now)
    except Exception:
        logger.debug("Calendar extraction failed", exc_info=True)
        return None
    if result is None:
        return None
    event, conflicts = result
    when = event.start_dt.strftime("%A %d %B, %I:%M %p")
    note = (
        f"[Calendar] An event was just added from the user's message: "
        f"\"{event.title}\" on {when} (id: {event.id}). Mention it "
        f"briefly and naturally in your reply, and let the user know "
        f"they can say \"cancel that\" to undo it."
    )
    if conflicts:
        names = ", ".join(f"\"{c.title}\"" for c in conflicts)
        note += f" It overlaps with {names} — mention the clash too."
    return note


def extraction_enabled(config) -> bool:
    return bool(getattr(config, "calendar_enabled", True))


def note_for_message_if_enabled(text: str, *, now_ts: float | None = None) -> str | None:
    """Full entry point for the chat paths: load config, check the
    switch, use the shared store. Mirrors the best-effort pattern of
    the skill auto-match hook — any failure means no note, never a
    broken turn.
    """
    try:
        from aether_engine.config import load_config

        config = load_config()
        if not extraction_enabled(config):
            return None
        from aether_engine.calendar.store import get_calendar_store

        now = (
            datetime.fromtimestamp(now_ts).astimezone()
            if now_ts is not None
            else None
        )
        return calendar_note_for_prompt(text, get_calendar_store(), now=now)
    except Exception:
        logger.debug("Calendar note hook failed", exc_info=True)
        return None

