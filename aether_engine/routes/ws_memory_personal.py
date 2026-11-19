"""Profile + Forget-centre websocket senders (SPEC-6B C1/C2).

Split out in the Wave 5 style of ws_memory_media.py: module-level
senders handed the connection's
:class:`~aether_engine.routes.ws_connection.WsConnection`,
payloads built off the receive loop on worker threads, and the C5
reply discipline — a failure is an error payload in the family's
own shape, never silence. ``backend_or_none`` resolves through
the websocket module at call time, exactly as in ws_memory_media
(the suites patch it there).

Dispatch (wired by the integrator in routes/websocket.py): each
MEMORY_PROFILE_* / MEMORY_FORGET_* request maps to a thin wrapper
that spawns the matching sender here in the background, the
``_handle_memory_episodes_request`` pattern.
"""
import asyncio
import logging

from aether_common.contracts import EventType
from aether_engine.state import engine_state

logger = logging.getLogger("aether_engine.routes.ws_memory_personal")

_UNAVAILABLE = "The configured memory backend is unavailable (see engine log)."


async def send_memory_profile_result(conn, req_id_, limit_, offset_):
    """Answer a MEMORY_PROFILE_REQUEST off the loop (SPEC-6B C1).

    The profile view re-derives from the backend's facts on every
    call — a synchronous read that scales with memory size, so it
    runs on a worker thread like the other memory reads. An
    unavailable backend answers profile=None + the error: the
    explicit unavailable state, never an empty profile dressed as
    success (SPEC-6B Rule 1).
    """
    from aether_engine.routes import websocket as _websocket

    def _build_payload():
        backend = _websocket.backend_or_none(engine_state.user_config, context="memory profile")
        if backend is None:
            return {"profile": None, "error": _UNAVAILABLE}
        return {
            "profile": backend.user_profile(limit=limit_, offset=offset_),
            "backend": backend.name,
        }

    async def _build():
        return await asyncio.to_thread(_build_payload)

    await conn.reply(
        req_id_, EventType.MEMORY_PROFILE_RESPONSE, _build,
        error_payload=lambda e: {"profile": None, "error": str(e)},
        send_failure_message="Could not send memory profile response")


async def send_memory_forget_candidates_result(conn, req_id_):
    """Answer a MEMORY_FORGET_CANDIDATES_REQUEST off the loop
    (SPEC-6B C2): topic discovery — distinct topics ranked by
    item count, counted by the forget finder itself."""
    from aether_engine.routes import websocket as _websocket

    def _build_payload():
        backend = _websocket.backend_or_none(engine_state.user_config, context="memory forget candidates")
        if backend is None:
            return {"candidates": [], "error": _UNAVAILABLE}
        return {
            "candidates": backend.forget_candidates(),
            "backend": backend.name,
        }

    async def _build():
        return await asyncio.to_thread(_build_payload)

    await conn.reply(
        req_id_, EventType.MEMORY_FORGET_CANDIDATES_RESPONSE, _build,
        error_payload=lambda e: {"candidates": [], "error": str(e)},
        send_failure_message="Could not send memory forget candidates response")


async def send_memory_forget_execute_result(conn, req_id_, topic_):
    """Answer a MEMORY_FORGET_EXECUTE_REQUEST off the loop
    (SPEC-6B C2): the cascading forget of one topic, answered
    with the EXACT removed list + the tombstone id undo would
    consume. A blank topic is refused honestly — a topic-scoped
    forget only ever deletes what its topic matched."""
    from aether_engine.routes import websocket as _websocket

    def _build_payload():
        backend = _websocket.backend_or_none(engine_state.user_config, context="memory forget execute")
        if backend is None:
            return {"success": False, "removed": [], "error": _UNAVAILABLE}
        if not str(topic_ or "").strip():
            return {"success": False, "removed": [], "error": "A topic is required."}
        result = backend.forget_execute(
            str(topic_), config=engine_state.user_config)
        return {"success": True, **result}

    async def _build():
        return await asyncio.to_thread(_build_payload)

    await conn.reply(
        req_id_, EventType.MEMORY_FORGET_EXECUTE_RESPONSE, _build,
        error_payload=lambda e: {"success": False, "removed": [], "error": str(e)},
        send_failure_message="Could not send memory forget execute response")


async def send_memory_forget_undo_result(conn, req_id_):
    """Answer a MEMORY_FORGET_UNDO_REQUEST off the loop (SPEC-6B
    C2): restore the latest tombstone. Nothing to undo is
    undone=False with success=True — an honest empty result, not
    an error; a refused restore (the tombstone belongs to another
    backend) is success=False with the reason."""
    from aether_engine.routes import websocket as _websocket

    def _build_payload():
        backend = _websocket.backend_or_none(engine_state.user_config, context="memory forget undo")
        if backend is None:
            return {"success": False, "undone": False, "error": _UNAVAILABLE}
        result = backend.forget_undo(config=engine_state.user_config)
        return {"success": "error" not in result, **result}

    async def _build():
        return await asyncio.to_thread(_build_payload)

    await conn.reply(
        req_id_, EventType.MEMORY_FORGET_UNDO_RESPONSE, _build,
        error_payload=lambda e: {"success": False, "undone": False, "error": str(e)},
        send_failure_message="Could not send memory forget undo response")


async def send_memory_forget_history_result(conn, req_id_, limit_, offset_):
    """Answer a MEMORY_FORGET_HISTORY_REQUEST off the loop
    (SPEC-6B C2): the tombstone store as Forgotten history,
    newest first, paginated with a total."""
    from aether_engine.routes import websocket as _websocket

    def _build_payload():
        backend = _websocket.backend_or_none(engine_state.user_config, context="memory forget history")
        if backend is None:
            return {"history": [], "total": 0, "error": _UNAVAILABLE}
        history = backend.forget_history(limit=limit_, offset=offset_)
        return {
            "history": history["entries"],
            "total": history["total"],
            "backend": backend.name,
        }

    async def _build():
        return await asyncio.to_thread(_build_payload)

    await conn.reply(
        req_id_, EventType.MEMORY_FORGET_HISTORY_RESPONSE, _build,
        error_payload=lambda e: {"history": [], "total": 0, "error": str(e)},
        send_failure_message="Could not send memory forget history response")
