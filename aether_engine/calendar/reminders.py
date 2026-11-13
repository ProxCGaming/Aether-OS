"""Reminder dispatch + push fan-out (Calendar layer C4, 14 Nov 2026).

The scheduler claims due reminders from the store (atomically — a
reminder can never fire twice) and this module turns each claim into
a ``REMINDER_DUE`` event for the desktop. Delivery mirrors the media
analyzer's N07 pattern: reminder firing is triggered from the
scheduler, outside any task, so it cannot ride the per-task
EventBus — interested parties (one websocket handler per
connection) subscribe here and the dispatcher fans out to them.
Sync and async callbacks are both supported; a failing subscriber
never starves the others or fails the dispatch.
"""

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from aether_common.contracts import Event, EventType
from aether_engine.audit import AuditLogger
from aether_engine.calendar.store import EventStore

logger = logging.getLogger("aether_engine.calendar.reminders")
_audit_logger = AuditLogger()

_subscribers: set[Callable[[Event], Any]] = set()


def subscribe_reminder_events(callback: Callable[[Event], Any]) -> Callable[[], None]:
    """Register for reminder events; returns an idempotent
    unsubscribe callable."""
    _subscribers.add(callback)

    def _unsubscribe() -> None:
        _subscribers.discard(callback)

    return _unsubscribe


async def _broadcast(event: Event) -> None:
    for callback in list(_subscribers):
        try:
            result = callback(event)
            if asyncio.iscoroutine(result):
                await result
        except Exception:  # one bad subscriber must not starve the rest
            logger.debug("Reminder event subscriber failed", exc_info=True)


def build_reminder_event(item: dict[str, Any]) -> Event:
    event = item["event"]
    return Event(
        type=EventType.REMINDER_DUE,
        payload={
            "event_id": event.id,
            "title": event.title,
            "start_ts": event.start_ts,
            "start_text": event.start_dt.strftime("%a %d %b, %I:%M %p"),
            "kind": item["kind"],
        },
    )


async def dispatch_due_reminders(
    store: EventStore,
    *,
    day_before_enabled: bool,
    lead_minutes: int,
    now_ts: float,
) -> list[Event]:
    """Claim every reminder due at ``now_ts``, audit it, fan it out
    to subscribers, and return the events built. The store claim is
    the duplicate guard; this function adds no state of its own."""
    due = await asyncio.to_thread(
        store.due_reminders,
        now_ts,
        day_before_enabled=day_before_enabled,
        lead_minutes=lead_minutes,
    )
    events: list[Event] = []
    for item in due:
        event = build_reminder_event(item)
        try:
            _audit_logger.log_event("CALENDAR_REMINDER_DUE", event.payload)
        except Exception:  # audit must never block a reminder
            logger.debug("Reminder audit failed", exc_info=True)
        await _broadcast(event)
        events.append(event)
    return events
