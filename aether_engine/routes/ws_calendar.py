"""Calendar websocket handlers (Calendar layer C5, 14 Nov 2026).

The Settings → Calendar surface's read path. Split out of
routes/websocket.py in the Wave 5 style: the dispatch table there
maps CALENDAR_LIST_REQUEST here. Failures answer honestly
(success=False + error), in the E4-F14 vocabulary the renderer
already renders for memory surfaces — never a silent empty list.
"""

import asyncio
import logging
import time

from aether_common.contracts import Event, EventType
from aether_engine.calendar.store import get_calendar_store
from aether_engine.routes.ws_connection import WsConnection

logger = logging.getLogger("aether_engine.routes.ws_calendar")


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
    await conn.ws.send_text(
        Event(
            type=EventType.CALENDAR_LIST_RESPONSE,
            request_id=req_id,
            payload=payload,
        ).to_json()
    )
