"""Calendar event store (Calendar layer C1, 14 Nov 2026).

Aether's memory tiers own the past (episodes) and timeless facts
(knowledge graph); nothing owned the *future*. This module is that
layer: scheduled events with start/end times, conflict detection on
insert, and reminder claim state, in its own SQLite database at
``~/.aether/calendar.db`` — deliberately NOT wedged into episodic
memory or the KG, whose shapes (past-tense events, atemporal facts)
do not fit a schedule.

Design notes:
- Times are stored as epoch seconds (absolute); all day-boundary
  reasoning (itinerary, day-before reminders) happens in local time
  at read time.
- An event with no explicit end gets an effective window of
  ``DEFAULT_EVENT_SECONDS`` (1 hour) for conflict and "currently
  happening" purposes; the stored row keeps ``end_ts = NULL`` so the
  distinction is never lost.
- Reminder due-ness is claimed atomically (``UPDATE ... WHERE flag =
  0`` + rowcount), so the scheduler and a manual ``run now`` can race
  without ever double-firing a reminder.
- The WAL transition reuses the tolerant pattern proven by the
  knowledge-graph fix (``5e677a7e``): never a bare
  ``PRAGMA journal_mode = WAL`` on a fresh file.
"""

import logging
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from aether_engine.audit import AuditLogger

logger = logging.getLogger("aether_engine.calendar.store")
_audit_logger = AuditLogger()

DEFAULT_DB_PATH = Path.home() / ".aether" / "calendar.db"

# Effective window for an event stored without an end: one hour.
# Used ONLY for conflict overlap / in-progress checks; display uses
# the real (absent) end.
DEFAULT_EVENT_SECONDS = 3600


def _audit(event_type: str, details: dict[str, Any]) -> None:
    """Audit best-effort: a logging failure must never kill a write."""
    try:
        _audit_logger.log_event(event_type, details)
    except Exception:
        logger.debug("Calendar audit failed", exc_info=True)


def _ensure_wal_mode(conn: sqlite3.Connection) -> None:
    """Switch the DB file to WAL, tolerating the first-open race.

    Mirrors ``memory/knowledge_graph._ensure_wal_mode`` (the N-07
    fix): the journal-mode change needs exclusive access and SQLite
    does not apply busy_timeout to it, so a competing first
    connection can make the request fail with "database is locked".
    Skip when already WAL, retry briefly, and carry on without it if
    it still cannot land — this connection works correctly in the
    rollback journal and a later connection completes the transition.
    """
    try:
        row = conn.execute("PRAGMA journal_mode").fetchone()
    except sqlite3.Error:
        return
    if row is not None and str(row[0]).lower() == "wal":
        return
    for _ in range(10):
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.Error:
            time.sleep(0.05)
    logger.debug("Calendar DB: WAL transition deferred (file busy)")


@dataclass
class CalendarEvent:
    id: str
    title: str
    start_ts: float
    end_ts: float | None
    source: str  # "tool" | "chat" | "manual"
    source_text: str
    reminder_day_before_sent: bool
    reminder_lead_sent: bool
    created_ts: float

    @property
    def effective_end_ts(self) -> float:
        """End used for overlap / in-progress math (1h default)."""
        if self.end_ts is not None and self.end_ts > self.start_ts:
            return self.end_ts
        return self.start_ts + DEFAULT_EVENT_SECONDS

    @property
    def start_dt(self) -> datetime:
        return datetime.fromtimestamp(self.start_ts).astimezone()

    @property
    def end_dt(self) -> datetime | None:
        if self.end_ts is None:
            return None
        return datetime.fromtimestamp(self.end_ts).astimezone()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "start": self.start_dt.isoformat(),
            "end": self.end_dt.isoformat() if self.end_dt else None,
            "source": self.source,
            "source_text": self.source_text,
            "created_ts": self.created_ts,
        }


def _row_to_event(row: sqlite3.Row) -> CalendarEvent:
    return CalendarEvent(
        id=row["id"],
        title=row["title"],
        start_ts=float(row["start_ts"]),
        end_ts=float(row["end_ts"]) if row["end_ts"] is not None else None,
        source=row["source"],
        source_text=row["source_text"] or "",
        reminder_day_before_sent=bool(row["reminder_day_before_sent"]),
        reminder_lead_sent=bool(row["reminder_lead_sent"]),
        created_ts=float(row["created_ts"]),
    )


class EventStore:
    """SQLite-backed calendar store. One connection per operation,
    so the scheduler, tools and extraction threads never share a
    connection object."""

    def __init__(self, db_path: Path | None = None):
        self.db_path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    start_ts REAL NOT NULL,
                    end_ts REAL,
                    source TEXT NOT NULL DEFAULT 'tool',
                    source_text TEXT NOT NULL DEFAULT '',
                    reminder_day_before_sent INTEGER NOT NULL DEFAULT 0,
                    reminder_lead_sent INTEGER NOT NULL DEFAULT 0,
                    created_ts REAL NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_start ON events(start_ts)"
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        _ensure_wal_mode(conn)
        return conn

    # ── Writes ────────────────────────────────────────────────────

    def add_event(
        self,
        title: str,
        start_ts: float,
        end_ts: float | None = None,
        *,
        source: str = "tool",
        source_text: str = "",
    ) -> tuple[CalendarEvent, list[CalendarEvent]]:
        """Insert an event; returns it plus any conflicting events.

        Conflicts are reported, never blocking: the caller decides
        how to surface them (the tool reply names them; the user can
        cancel either side).
        """
        title = (title or "").strip()
        if not title:
            raise ValueError("event title must not be empty")
        if end_ts is not None and end_ts <= start_ts:
            raise ValueError("event end must be after its start")
        event = CalendarEvent(
            id=uuid.uuid4().hex,
            title=title,
            start_ts=float(start_ts),
            end_ts=float(end_ts) if end_ts is not None else None,
            source=source,
            source_text=source_text or "",
            reminder_day_before_sent=False,
            reminder_lead_sent=False,
            created_ts=time.time(),
        )
        conflicts = self.find_conflicts(
            event.start_ts, event.effective_end_ts, exclude_id=None
        )
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO events (id, title, start_ts, end_ts, source,"
                " source_text, reminder_day_before_sent, reminder_lead_sent,"
                " created_ts) VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?)",
                (
                    event.id,
                    event.title,
                    event.start_ts,
                    event.end_ts,
                    event.source,
                    event.source_text,
                    event.created_ts,
                ),
            )
        _audit(
            "CALENDAR_EVENT_ADDED",
            {
                "event_id": event.id,
                "title": event.title,
                "start_ts": event.start_ts,
                "source": event.source,
                "conflicts": len(conflicts),
            },
        )
        return event, conflicts

    def cancel_event(self, event_id: str) -> CalendarEvent | None:
        """Delete an event by id; returns the removed event or None."""
        existing = self.get_event(event_id)
        if existing is None:
            return None
        with self._connect() as conn:
            conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
        _audit(
            "CALENDAR_EVENT_CANCELLED",
            {"event_id": existing.id, "title": existing.title},
        )
        return existing

    # ── Reads ─────────────────────────────────────────────────────

    def get_event(self, event_id: str) -> CalendarEvent | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM events WHERE id = ?", (event_id,)
            ).fetchone()
        return _row_to_event(row) if row is not None else None

    def find_conflicts(
        self, start_ts: float, end_ts: float, *, exclude_id: str | None
    ) -> list[CalendarEvent]:
        """Events whose effective window overlaps [start_ts, end_ts)."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE start_ts < ? AND start_ts > ?"
                " ORDER BY start_ts",
                (end_ts, start_ts - DEFAULT_EVENT_SECONDS - 1),
            ).fetchall()
        conflicts = []
        for row in rows:
            other = _row_to_event(row)
            if exclude_id is not None and other.id == exclude_id:
                continue
            if start_ts < other.effective_end_ts and other.start_ts < end_ts:
                conflicts.append(other)
        return conflicts

    def upcoming(self, now_ts: float, *, limit: int = 20) -> list[CalendarEvent]:
        """Events not yet finished (in-progress included), soonest first."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE start_ts > ? ORDER BY start_ts LIMIT ?",
                (now_ts - DEFAULT_EVENT_SECONDS - 1, max(limit * 4, limit)),
            ).fetchall()
        events = [
            e for e in (_row_to_event(r) for r in rows)
            if e.effective_end_ts > now_ts
        ]
        return events[:limit]

    def events_between(
        self, from_ts: float, to_ts: float
    ) -> list[CalendarEvent]:
        """Events overlapping the [from_ts, to_ts) window, by start."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE start_ts < ? AND start_ts > ?"
                " ORDER BY start_ts",
                (to_ts, from_ts - DEFAULT_EVENT_SECONDS - 1),
            ).fetchall()
        return [
            e for e in (_row_to_event(r) for r in rows)
            if e.start_ts < to_ts and e.effective_end_ts > from_ts
        ]

    def search_upcoming_by_title(
        self, text: str, now_ts: float, *, limit: int = 10
    ) -> list[CalendarEvent]:
        """Upcoming events whose title contains ``text`` (case-insensitive)."""
        needle = (text or "").strip().lower()
        if not needle:
            return []
        return [
            e for e in self.upcoming(now_ts, limit=100)
            if needle in e.title.lower()
        ][:limit]

    # ── Reminders ─────────────────────────────────────────────────

    def due_reminders(
        self,
        now_ts: float,
        *,
        day_before_enabled: bool,
        lead_minutes: int,
    ) -> list[dict[str, Any]]:
        """Claim and return the reminders due at ``now_ts``.

        Each due reminder is claimed (its sent-flag flipped) in the
        same pass, so it can never fire twice — a second call at the
        same instant returns nothing for it. Kinds: ``day_before``
        (the event starts tomorrow, local time) and ``lead`` (the
        event starts within ``lead_minutes`` and has not started).
        Claiming an event whose lead reminder is due also settles
        its day-before flag when the event is today, so a same-day
        event never gets a redundant day-before claim tomorrow.
        """
        due: list[dict[str, Any]] = []
        now_local = datetime.fromtimestamp(now_ts).astimezone()
        tomorrow = (now_local + timedelta(days=1)).date()
        candidates = self.upcoming(now_ts, limit=200)
        for event in candidates:
            start_local = event.start_dt
            if (
                lead_minutes > 0
                and not event.reminder_lead_sent
                and now_ts < event.start_ts <= now_ts + lead_minutes * 60
            ):
                if self._claim(event.id, "reminder_lead_sent"):
                    due.append({"event": event, "kind": "lead"})
                continue
            if (
                day_before_enabled
                and not event.reminder_day_before_sent
                and start_local.date() == tomorrow
                and self._claim(event.id, "reminder_day_before_sent")
            ):
                due.append({"event": event, "kind": "day_before"})
        return due

    def _claim(self, event_id: str, column: str) -> bool:
        """Flip a reminder flag iff it is still unset. Returns True
        when this caller won the claim."""
        assert column in ("reminder_lead_sent", "reminder_day_before_sent")
        with self._connect() as conn:
            cur = conn.execute(
                f"UPDATE events SET {column} = 1 WHERE id = ? AND {column} = 0",
                (event_id,),
            )
            return cur.rowcount == 1


_default_store: EventStore | None = None


def get_calendar_store(db_path: Path | None = None) -> EventStore:
    """The shared store over ``~/.aether/calendar.db`` (or ``db_path``
    once — an explicit path always builds a fresh store)."""
    global _default_store
    if db_path is not None:
        return EventStore(db_path)
    if _default_store is None:
        _default_store = EventStore()
    return _default_store
