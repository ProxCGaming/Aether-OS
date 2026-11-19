"""Calendar websocket handlers (Calendar layer C5, 14 Nov 2026).

The Settings → Calendar surface's read path, plus the panel's
direct create / cancel (Full Furnish SPEC-6B C3 — the panel calls
the store itself, not via chat extraction). Split out of
routes/websocket.py in the Wave 5 style: the dispatch table there
maps CALENDAR_LIST_REQUEST / CALENDAR_CREATE_REQUEST /
CALENDAR_CANCEL_REQUEST here. Failures answer honestly
(success=False + error), in the E4-F14 vocabulary the renderer
already renders for memory surfaces — never a silent empty list.
"""

import asyncio
import logging
import time
from datetime import datetime

from aether_common.contracts import Event, EventType
from aether_engine.calendar.store import DEFAULT_EVENT_SECONDS, get_calendar_store
from aether_engine.routes.ws_connection import WsConnection

logger = logging.getLogger("aether_engine.routes.ws_calendar")


async def _reply(conn: WsConnection, req_id, event_type: str, payload: dict) -> None:
    await conn.ws.send_text(
        Event(type=event_type, request_id=req_id, payload=payload).to_json()
    )


async def handle_calendar_list_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    try:
        limit = int((msg.payload or {}).get("limit") or 20)
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 100))
    try:
        events = await asyncio.to_thread(
            get_calendar_store().upcoming, time.time(), limit=limit
        )
        payload = {
            "success": True,
            "events": [e.to_dict() for e in events],
        }
    except Exception as exc:
        logger.exception("Calendar list failed")
        payload = {"success": False, "events": [], "error": str(exc)}
    await _reply(conn, req_id, EventType.CALENDAR_LIST_RESPONSE, payload)


def _conflict_dict(event) -> dict:
    """One conflicting event in the SPEC-6B C3 reply shape."""
    return {
        "event_id": event.id,
        "title": event.title,
        "start": event.start_dt.isoformat(),
        "end": event.end_dt.isoformat() if event.end_dt else None,
    }


def _payload_start_ts(payload: dict) -> float:
    """The create payload's start as epoch seconds: ``start_ts``
    (a number) or ``start`` (an ISO 8601 string; a naive value is
    read as local time). Raises ValueError/TypeError otherwise."""
    raw = payload.get("start_ts")
    if raw is None:
        raw = payload.get("start")
    if isinstance(raw, str):
        return datetime.fromisoformat(raw).timestamp()
    return float(raw)


async def handle_calendar_create_request(conn: WsConnection, msg: Event) -> None:
    """SPEC-6B C3: create one event directly from the panel.

    A non-empty conflicts list is a WARNING reply, not an error:
    without ``accept_conflicts`` nothing is stored (saved=False)
    and the reply names the conflicts, so the board's "Save
    anyway" is this same call with accept_conflicts=True — never
    a second, duplicate insert.
    """
    req_id = msg.request_id
    payload_in = msg.payload or {}
    title = str(payload_in.get("title") or "").strip()
    accept_conflicts = bool(payload_in.get("accept_conflicts") or False)
    try:
        start_ts = _payload_start_ts(payload_in)
        raw_duration = payload_in.get("duration_s")
        duration_s = float(raw_duration) if raw_duration is not None else None
        if duration_s is not None and duration_s <= 0:
            raise ValueError("duration_s must be positive")
    except (TypeError, ValueError) as exc:
        await _reply(conn, req_id, EventType.CALENDAR_CREATE_RESPONSE, {
            "success": False, "saved": False, "event": None,
            "conflicts": [], "error": str(exc),
        })
        return
    if not title:
        await _reply(conn, req_id, EventType.CALENDAR_CREATE_RESPONSE, {
            "success": False, "saved": False, "event": None,
            "conflicts": [], "error": "An event title is required.",
        })
        return
    try:
        store = get_calendar_store()
        end_ts = start_ts + duration_s if duration_s is not None else None
        effective_end = (
            end_ts if end_ts is not None
            else start_ts + DEFAULT_EVENT_SECONDS
        )
        conflicts = await asyncio.to_thread(
            store.find_conflicts, start_ts, effective_end, exclude_id=None
        )
        if conflicts and not accept_conflicts:
            payload = {
                "success": True,
                "saved": False,
                "event": None,
                "conflicts": [_conflict_dict(c) for c in conflicts],
            }
        else:
            event, conflicts = await asyncio.to_thread(
                store.add_event, title, start_ts, end_ts, source="manual"
            )
            payload = {
                "success": True,
                "saved": True,
                "event": event.to_dict(),
                "conflicts": [_conflict_dict(c) for c in conflicts],
            }
    except Exception as exc:
        logger.exception("Calendar create failed")
        payload = {
            "success": False, "saved": False, "event": None,
            "conflicts": [], "error": str(exc),
        }
    await _reply(conn, req_id, EventType.CALENDAR_CREATE_RESPONSE, payload)


async def handle_calendar_cancel_request(conn: WsConnection, msg: Event) -> None:
    """SPEC-6B C3: cancel one event by id. The cancel is the
    store's own delete, so it is durable across restart; a missing
    id answers an honest not-found (success=False) — never a fake
    success."""
    req_id = msg.request_id
    event_id = str((msg.payload or {}).get("event_id") or "")
    try:
        removed = (
            await asyncio.to_thread(get_calendar_store().cancel_event, event_id)
            if event_id else None
        )
        if removed is None:
            payload = {
                "success": False,
                "cancelled": None,
                "error": (
                    f"No calendar event with id {event_id!r} exists "
                    "— nothing was cancelled."
                ),
            }
        else:
            payload = {"success": True, "cancelled": removed.id}
    except Exception as exc:
        logger.exception("Calendar cancel failed")
        payload = {"success": False, "cancelled": None, "error": str(exc)}
    await _reply(conn, req_id, EventType.CALENDAR_CANCEL_RESPONSE, payload)
