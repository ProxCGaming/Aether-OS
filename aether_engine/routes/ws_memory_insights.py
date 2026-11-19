"""Memory stats + graph neighbourhood reads (Full Furnish Wave 6).

The two SPEC-6B reads that had no wire surface: C4's
``memory_stats()`` (the Settings → Memory controls row: counts +
decay read-out) and C5's ``graph_neighbourhood()`` (the local
graph view, edges derived at read time per D-4). Both are plain
facade reads answered off the receive loop with the canonical
failure shape, mirroring the memory handlers in
``ws_memory_media.py`` — an unavailable backend is an error
payload naming it, never a bare empty success.
"""
import asyncio
import logging

from aether_common.contracts import EventType
from aether_engine.state import engine_state

logger = logging.getLogger("aether_engine.routes.ws_memory_insights")


async def send_memory_stats_result(conn, req_id_):
    """Answer a MEMORY_STATS_REQUEST off the loop.

    The counts scan the facade's listings, which scale with
    memory size — same off-loop treatment as the episodes and
    graph reads in ws_memory_media.py. The live config rides
    along so the decay read-out names the configured fade
    period, not just the shipped default.
    """
    from aether_engine.routes import websocket as _websocket

    def _build_payload():
        backend = _websocket.backend_or_none(
            engine_state.user_config, context="memory stats")
        if backend is None:
            return {"success": False,
                    "error": "The configured memory backend is unavailable (see engine log)."}
        # The C4 fields ride at the payload root — the Memory
        # panel reads them there (integrator reconciliation,
        # Wave 6: no "stats" wrapper key).
        return {"success": True,
                **backend.memory_stats(config=engine_state.user_config)}

    async def _build():
        return await asyncio.to_thread(_build_payload)

    await conn.reply(
        req_id_, EventType.MEMORY_STATS_RESPONSE, _build,
        error_payload=lambda e: {"success": False, "error": str(e)},
        send_failure_message="Could not send memory stats response")


async def send_memory_graph_neighbourhood_result(
        conn, req_id_, entity_name_, depth_):
    """Answer a MEMORY_GRAPH_NEIGHBOURHOOD_REQUEST off the loop.

    The neighbourhood is derived from the full fact/episode
    listings (memory/graph_read.py), so it gets the same
    off-loop treatment as the other memory reads. An unknown
    entity is a successful honest-empty result (the contract's
    shape names the entity); a missing entity name is the only
    request error. Depth is 1 or 2 — anything else reads as 1.
    """
    from aether_engine.routes import websocket as _websocket

    if not str(entity_name_ or "").strip():
        await conn.send_event(
            req_id_, EventType.MEMORY_GRAPH_NEIGHBOURHOOD_RESPONSE,
            {"success": False, "error": "entity_name is required"},
            "Could not send graph neighbourhood response")
        return

    def _build_payload():
        backend = _websocket.backend_or_none(
            engine_state.user_config, context="graph neighbourhood")
        if backend is None:
            return {"success": False,
                    "error": "The configured memory backend is unavailable (see engine log)."}
        return {"success": True,
                "neighbourhood": backend.graph_neighbourhood(
                    str(entity_name_), depth=2 if depth_ == 2 else 1)}

    async def _build():
        return await asyncio.to_thread(_build_payload)

    await conn.reply(
        req_id_, EventType.MEMORY_GRAPH_NEIGHBOURHOOD_RESPONSE, _build,
        error_payload=lambda e: {"success": False, "error": str(e)},
        send_failure_message="Could not send graph neighbourhood response")
