"""llama.cpp router surface: status, models, load/unload and
router control.

The read/control family for the local llama.cpp router backend
(``aether_engine/models/llama_router.py``), in the
``ws_memory_media`` idiom: module-level handlers handed the
connection's :class:`~aether_engine.routes.ws_connection.WsConnection`,
each spawning its sender as a background task so a slow router
read never stalls the message loop, and every request answered
through ``WsConnection.reply`` — an error payload in the family's
own shape, never silence.

The manager is built per request from the live config (it is a
thin value over the config + secret store; the supervised child
process it may have spawned lives in the manager module's
registry, so per-request construction loses nothing).
"""
import logging

from aether_common.contracts import EventType
from aether_engine.models.llama_presets import preset_rows
from aether_engine.models.llama_router import LlamaRouterManager
from aether_engine.state import engine_state

logger = logging.getLogger("aether_engine.routes.ws_llamacpp")


def _manager(conn) -> LlamaRouterManager:
    """The router manager for the live config + this
    connection's secret store."""
    return LlamaRouterManager.from_config(
        engine_state.user_config, secret_store=conn.secret_store)


async def send_llamacpp_status_result(conn, req_id_):
    """Answer a LLAMACPP_STATUS_REQUEST from a background task."""
    manager = _manager(conn)
    enabled = bool(getattr(engine_state.user_config,
                           "llamacpp_enabled", False))

    async def _build_payload():
        return await manager.status_payload(enabled)

    await conn.reply(
        req_id_, EventType.LLAMACPP_STATUS_RESPONSE, _build_payload,
        error_payload=lambda e: {
            "enabled": enabled,
            "running": False,
            "url": manager.router_url,
            "version": None,
            "models_loaded": None,
            "supervised": manager.supervised_running(),
            "error": str(e),
        },
        send_failure_message="Could not send llama.cpp status response",
        warn=lambda e: f"llama.cpp status raised: {e}")


async def send_llamacpp_models_result(conn, req_id_):
    """Answer a LLAMACPP_MODELS_REQUEST from a background task."""
    manager = _manager(conn)

    async def _build_payload():
        return await manager.models_payload()

    await conn.reply(
        req_id_, EventType.LLAMACPP_MODELS_RESPONSE, _build_payload,
        error_payload=lambda e: {
            "models": [],
            "presets": preset_rows([]),
            "error": str(e),
        },
        send_failure_message="Could not send llama.cpp models response",
        warn=lambda e: f"llama.cpp model list raised: {e}")


async def send_llamacpp_model_load_result(conn, req_id_, model_id_):
    """Answer a LLAMACPP_MODEL_LOAD_REQUEST from a background task."""
    manager = _manager(conn)

    async def _build_payload():
        return await manager.load_model(model_id_)

    await conn.reply(
        req_id_, EventType.LLAMACPP_MODEL_LOAD_RESPONSE, _build_payload,
        error_payload=lambda e: {"success": False, "error": str(e)},
        send_failure_message="Could not send llama.cpp model load response",
        warn=lambda e: f"llama.cpp model load raised: {e}")


async def send_llamacpp_model_unload_result(conn, req_id_, model_id_):
    """Answer a LLAMACPP_MODEL_UNLOAD_REQUEST from a background task."""
    manager = _manager(conn)

    async def _build_payload():
        return await manager.unload_model(model_id_)

    await conn.reply(
        req_id_, EventType.LLAMACPP_MODEL_UNLOAD_RESPONSE, _build_payload,
        error_payload=lambda e: {"success": False, "error": str(e)},
        send_failure_message="Could not send llama.cpp model unload response",
        warn=lambda e: f"llama.cpp model unload raised: {e}")


async def send_llamacpp_router_control_result(conn, req_id_, action_):
    """Answer a LLAMACPP_ROUTER_CONTROL_REQUEST from a background task."""
    manager = _manager(conn)

    async def _build_payload():
        return await manager.router_control(action_)

    await conn.reply(
        req_id_, EventType.LLAMACPP_ROUTER_CONTROL_RESPONSE, _build_payload,
        error_payload=lambda e: {
            "success": False, "running": False, "error": str(e)},
        send_failure_message="Could not send llama.cpp router control response",
        warn=lambda e: f"llama.cpp router control raised: {e}")


# ------------------------------------------------------------------
# Dispatch handlers (wired into the websocket dispatch table by
# the integrator; each hands its work to a background sender, in
# the local-model handlers' idiom).
# ------------------------------------------------------------------


async def handle_llamacpp_status_request(conn, msg) -> None:
    req_id = msg.request_id
    conn.spawn_background(send_llamacpp_status_result(conn, req_id))


async def handle_llamacpp_models_request(conn, msg) -> None:
    req_id = msg.request_id
    conn.spawn_background(send_llamacpp_models_result(conn, req_id))


async def handle_llamacpp_model_load_request(conn, msg) -> None:
    req_id = msg.request_id
    conn.spawn_background(send_llamacpp_model_load_result(
        conn, req_id, msg.payload.get("model_id", "") or ""))


async def handle_llamacpp_model_unload_request(conn, msg) -> None:
    req_id = msg.request_id
    conn.spawn_background(send_llamacpp_model_unload_result(
        conn, req_id, msg.payload.get("model_id", "") or ""))


async def handle_llamacpp_router_control_request(conn, msg) -> None:
    req_id = msg.request_id
    conn.spawn_background(send_llamacpp_router_control_result(
        conn, req_id, msg.payload.get("action", "") or ""))
