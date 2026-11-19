import asyncio
import base64
import copy
import logging
import time
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from aether_common.auth import (
    verify_token,
)
from aether_common.contracts import (
    Event,
    EventType,
    InvalidEventPayloadError,
    SchemaVersionMismatchError,
    TaskState,
)
from aether_engine.config import (
    AUXILIARY_TASK_CLASSES,
    UserConfig,
    canonical_workspace_root,
    find_workspace_root,
    remove_workspace_root,
    save_config,
)
from aether_engine.langgraph.checkpointer import delete_thread_checkpoints
from aether_engine.langgraph.executor import (
    get_pending_approvals,
    get_persisted_run_config,
    resume_langgraph_task,
    run_langgraph_task,  # noqa: F401 — patch target: tests patch it here; ws_task_exec/ws_voice resolve it through this module at call time
)
from aether_engine.langgraph.fast_chat import run_fast_chat  # noqa: F401 — patch target: tests patch it here; ws_task_exec resolves it through this module at call time
from aether_engine.langgraph.steering import enqueue_steer
from aether_engine.memory import session_store
from aether_engine.memory.facade import backend_or_none
from aether_engine.orchestration.core import (
    _build_provider_list_async,
    _create_provider_instance,
)
from aether_engine.providers.discovery import fetch_available_models  # noqa: F401 — patch target: tests patch it here; ws_providers resolves it through this module at call time
from aether_engine.rewind import fork as fork_service
from aether_engine.rewind import manifest as rewind_manifest
from aether_engine.rewind import service as rewind_service
from aether_engine.routes.ws_calendar import handle_calendar_list_request
from aether_engine.routes.ws_connection import WsConnection
from aether_engine.routes.ws_mcp_plugins import (
    PLUGIN_INSTALL_TTL_SECONDS,  # noqa: F401 — re-exported: tests patch the TTL on this module
    pending_plugin_install_created,
    pending_plugin_installs,
    prune_expired_plugin_installs,
    send_mcp_prompt_list_result,
    send_mcp_prompt_run_result,
    send_mcp_resource_list_result,
    send_mcp_resource_read_result,
    send_mcp_server_connect_result,
    send_mcp_server_disconnect_result,
    send_mcp_server_remove_result,
    send_mcp_server_test_result,
    send_mcp_server_update_result,
    send_plugin_install_result,
    send_plugin_toggle_result,
    send_plugin_uninstall_result,
)
from aether_engine.routes.ws_memory_media import (
    send_diagnostics_export_result,
    send_local_model_delete_result,
    send_local_model_list_result,
    send_media_search_result,
    send_memory_episodes_result,
    send_memory_export_result,
    send_memory_graph_result,
    send_model_hub_delete_result,
    send_model_hub_detail_result,
    send_model_hub_search_result,
    send_version_check_result,
)
from aether_engine.routes.ws_providers import (
    complete_provider_save_discovery,
    send_capability_check_result,
    send_provider_list_update,
    send_refresh_models_result,
    send_validation_result,
)
from aether_engine.routes.ws_sessions import (
    _ARTIFACT_TRUNCATION_MARKER,  # noqa: F401 — re-export, see ws_sessions
    ARTIFACT_CONTENT_CAP_BYTES,  # noqa: F401 — re-export, see ws_sessions
    _create_bound_session,  # noqa: F401 — re-export, see ws_sessions
    _inspect_artifact_on_disk,  # noqa: F401 — re-export, see ws_sessions
    _read_text_capped,  # noqa: F401 — re-export, see ws_sessions
    _register_message_attachments,
    _remove_session_attachments,
    _resolve_workspace_binding,
    _run_mcp_prompt_into_chat,  # noqa: F401 — re-export, see ws_sessions
    _unbind_sessions_bound_to,
    _workspace_session_counts,
    handle_artifact_get,
)
from aether_engine.routes.ws_task_exec import (
    execute_task_generator,
    start_task_message,
)
from aether_engine.routes.ws_voice import (
    cancel_voice_background_tasks,
    close_speech_engines,
    create_stt_engine,  # noqa: F401 — re-exported: tests setattr the factories on this module
    create_tts_engine,  # noqa: F401 — re-exported: tests setattr the factories on this module
    dictation_stt,
    enqueue_dictation,
    enqueue_voice_op,
    open_read_aloud_tts,
    read_aloud_tts,
    send_dictation_chunk_result,
    send_dictation_stop_result,
    send_voice_session_start_result,
    send_voice_session_stop_result,
    stop_read_aloud,
    stop_voice_session,
)
from aether_engine.routing.intent import classify_intent  # noqa: F401 — patch target: tests patch it here; ws_task_exec/ws_voice resolve it through this module at call time
from aether_engine.routing.policy import local_only_from_vision_override
from aether_engine.secrets.storage import SecretStore
from aether_engine.state import engine_state
from aether_engine.tools.registry import build_tool_registry
from aether_engine.validation.diagnostics import run_environment_diagnostics
from aether_engine.version import APP_VERSION

logger = logging.getLogger("aether_engine.routes.websocket")

# Unified logging (5 Nov 2026): incoming message-TYPE tracing. The
# receive loop records every message's type — INFO for the
# allowlist below, DEBUG for everything else — and never a payload:
# the 5 Nov QA model-switch failure was undiagnosable partly
# because the engine logged no WS traffic at all, so there was no
# record of the switch request ever arriving. The prefix set
# mirrors the renderer's (desktop_app/src/logUi.js) — keep them in
# step.
_WS_TRACE_INFO_PREFIXES = (
    "MODEL_", "TASK_", "PROVIDER_", "DIAGNOSTICS_", "CONFIG_", "ENVIRONMENT_",
)


def _log_ws_message_type(msg_type: Any) -> None:
    """Trace one incoming message type. Types only — never payloads."""
    name = msg_type.value if isinstance(msg_type, EventType) else str(msg_type)
    if name.startswith(_WS_TRACE_INFO_PREFIXES):
        logger.info("WS message received: %s", name)
    else:
        logger.debug("WS message received: %s", name)


def _coerce_sample_rate(value: Any) -> int:
    """Coerce a client-supplied dictation sample rate (E1-01).

    The rate rides an untrusted payload: a non-numeric value must
    fall back to the 16 kHz default the message would have carried
    had it omitted the field, never raise into the receive loop and
    kill the session.
    """
    try:
        return int(value or 16000)
    except (TypeError, ValueError):
        return 16000

# Phase 10.3, B1: the only task class with a real engine seam — see
# AUXILIARY_TASK_CLASSES in aether_engine/config.py (single source of truth).
# The Settings UI dropped the other six helper tasks in Phase 9 (commit
# ce99048) because none of them has an engine call site — routing/policy.py
# reads only "vision" — but this handler kept accepting and persisting all
# seven, so a client got success:true for a setting the engine would
# silently ignore. Keep this set to exactly the task classes the routing
# policy honors; anything else is rejected, never stored.

router = APIRouter()

# RUX-B-03: overall budget for one capability-check "Run Now"
# (CAPABILITY_CHECK_RUN_NOW). A run fans out to per-provider
# discovery plus a live self-test per model; with no budget a single
# click could run for minutes. 180 s is generous for a real sweep
# yet still bounded — past it the run is cancelled and reported
# exactly like a failed run whose error is the timeout (see
# send_capability_check_result). Tests patch this module attribute
# to shorten the budget.
CAPABILITY_CHECK_RUN_BUDGET_SECONDS = 180.0


def _try_save_config() -> str | None:
    """Persist the user config, returning the error text on failure.

    E1-10: most settings handlers used to call save_config bare —
    one OSError (full disk, locked file) propagated out of the
    handler, tore down the WebSocket connection and cancelled the
    running task mid-flight, and the client never even got a reply.
    Handlers now fold the failure into their own reply vocabulary
    through this helper: the change simply fails, honestly.
    Returns None on success.
    """
    try:
        save_config(engine_state.user_config)
        return None
    except OSError as e:
        logger.warning(f"save_config failed: {e}")
        return str(e)


class _SendLockedWebSocket:
    """Serialise ``send_text`` on one connection (N-05).

    A task connection has dozens of concurrent senders — the task
    pump, media/download/plugin background helpers, voice — and
    Starlette/FastAPI sockets do not tolerate interleaved frames.
    ``ws_tasks`` rebinds its socket reference to this proxy right
    after accept, so every sender holding the endpoint's ``ws``
    serialises through the one lock. All other attributes delegate
    to the wrapped socket.
    """

    def __init__(self, ws: WebSocket):
        self._ws = ws
        self._send_lock = asyncio.Lock()

    async def send_text(self, data: str) -> None:
        async with self._send_lock:
            await self._ws.send_text(data)

    def __getattr__(self, name: str):
        return getattr(self._ws, name)

def should_attempt_task_fallback(*, attempt: int, saw_tool_activity: bool, has_factory: bool) -> bool:
    """§6.4 gate for task-level fallback (Epic 5, E5-02).

    A failed task may be rebuilt on another provider only on its
    first attempt, only before any tool activity, and only when the
    caller supplied a retry factory (the approval-resume path never
    does — a resumed task has history).
    """
    return has_factory and attempt == 1 and not saw_tool_activity


async def _execute_tool_in_worker(tool_name: str, args: dict[str, Any], task_class: str) -> Any:
    """Run an approved risky tool via the shared sandbox worker. Thin wrapper that injects engine_state context."""
    from aether_engine.workers.sandbox import execute_tool_in_worker
    return await execute_tool_in_worker(
        tool_name=tool_name,
        args=args,
        task_class=task_class,
        audit_logger=engine_state.audit_logger,
        task_classes=engine_state.user_config.task_classes,
    )

def _audit_approval_arguments_edited(
    audit_logger: Any,
    approval_key: str,
    tool_name: str | None,
    original_args: dict[str, Any],
    edited_args: dict[str, Any],
) -> list[str]:
    """Epic 8, E8-1.3: audit an approval-time argument edit.

    Records ``APPROVAL_ARGUMENTS_EDITED`` with the changed field NAMES
    only — argument values must never reach the audit log (audit.py
    redaction is the second line of defence, not the first). Returns
    the changed field list; empty (and no event) when the "edit"
    changed nothing.
    """
    from aether_engine.langgraph.nodes.pause import changed_arg_fields

    changed = changed_arg_fields(original_args or {}, edited_args or {})
    if changed:
        audit_logger.log_event("APPROVAL_ARGUMENTS_EDITED", {
            "approval_key": approval_key,
            "tool_name": tool_name,
            "changed_fields": changed,
        })
    return changed


def _extract_token(ws: WebSocket) -> str | None:
    # S1-M3: header ONLY. A token in the URL query string (?token=...)
    # is REFUSED — never read here, and the endpoint declares no query
    # parameter for it — because URLs land in uvicorn access logs and
    # proxy/browser histories. Route choice: the connection is owned by
    # the Electron MAIN process (desktop_app/main.cjs connectEngine,
    # Node `ws`), which can set a real Authorization header; the
    # renderer never opens this socket and never holds the token, and
    # the legacy Python client (aether_ui/ws_client.py) sets the same
    # header via the `websockets` library. Browser WebSocket cannot set
    # headers, but no browser/renderer code connects here directly, so
    # neither a subprotocol carrier nor a first-message auth frame is
    # needed.
    auth = ws.headers.get("authorization", "")
    return auth[7:].strip() if auth.startswith("Bearer ") else None


# Wave 5: the session-side services that used to live here —
# workspace binding, the artifact GET service and message
# attachment registration — moved to routes/ws_sessions.py and
# are re-exported above, so every existing import and patch
# site keeps resolving against this module.


@router.websocket("/ws/tasks")
async def ws_tasks(ws: WebSocket):
    ip = ws.client.host if ws.client else "unknown"
    port = ws.client.port if ws.client else None

    # S1-M3: the token check stays BEFORE accept / any message
    # handling, and the only carrier is the Authorization header —
    # a ?token=... in the URL is ignored here and therefore rejected
    # by the verify below (no query-auth fallback exists).
    extracted = _extract_token(ws)
    if not extracted or not verify_token(extracted, engine_state.auth_token):
        engine_state.audit_logger.log_connection(False, ip, port, "Unauthorized")
        logger.warning(f"Rejected unauthenticated connection from {ip}:{port}")
        await ws.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await ws.accept()
    # N-05: from here on, every send on this connection serialises
    # through one lock — the receive loop, the task pump and every
    # background helper below all hold this `ws` reference.
    ws = _SendLockedWebSocket(ws)  # type: ignore[assignment]
    engine_state.audit_logger.log_connection(True, ip, port, "Authenticated successfully")
    logger.info(f"Accepted authenticated connection from {ip}:{port}")

    # Send Hello
    await ws.send_text(
        Event(
            type=EventType.HELLO,
            payload={
                "message": "AETHER Engine Ready",
                "version": APP_VERSION,
                "active_provider": engine_state.active_provider,
                "active_model": engine_state.active_model,
                "health": engine_state.health_manager.get_all_statuses(),
            },
        ).to_json()
    )

    # ── Recover Pending Approvals (Checkpoint Durability) ──
    try:
        pending_approvals = await get_pending_approvals()
        for p in pending_approvals:
            await ws.send_text(Event(
                type=EventType.TOOL_APPROVAL_REQUEST,
                request_id=p["thread_id"],
                payload={
                    "tool_name": p["tool_name"],
                    "args": p["args"],
                    "task_id": p["task_id"],
                    "approval_key": p["thread_id"],
                    "task_class": p["task_class"],
                    "workspace_roots": engine_state.user_config.workspace_roots or [str(Path.home() / "AetherWorkspace")],
                },
            ).to_json())
    except Exception as e:  # noqa: BLE001 — best-effort approval recovery on connect: failure must not block the socket
        logger.warning(f"Error fetching pending approvals: {e}")

    # Wave 5, slice 3: this connection's task state — the
    # runner, its session, the chained launch tail, the active
    # downloads and the current task id — lives on the
    # WsConnection (routes/ws_connection.py); the task-exec
    # core that drives it lives in routes/ws_task_exec.py.
    secret_store = SecretStore()
    # Wave 5: the per-connection working state — send-locked
    # socket, audit logger, secret store, background-task registry
    # (N-19), per-name locks, voice-session state — lives on one
    # WsConnection the handler families share
    # (routes/ws_connection.py). The local names below stay as
    # aliases into it.
    conn = WsConnection(ws, engine_state.audit_logger, secret_store)
    _conn_background_tasks = conn.background_tasks
    _spawn_background = conn.spawn_background
    # The task pump (routes/ws_task_exec.py), bound to this
    # connection: the voice family's escalated background jobs
    # drive it through the conn slot, as they did the closure.
    conn.execute_task_generator = partial(execute_task_generator, conn)


    # Wave 5: the provider/model handler family lives in
    # routes/ws_providers.py — the dispatch branches below call
    # its module-level handlers with this connection's context.

    # Wave 5: the MCP handler family (RUX-B-04) lives in
    # routes/ws_mcp_plugins.py — the dispatch branches below call
    # its module-level handlers with this connection's context.

    # ------------------------------------------------------------------
    # RUX-B-08: read-path lookups + exports, answered from background
    # tasks. Each of these branches used to await external HTTP (or
    # run synchronous bulk work) inline in the message loop — seconds
    # per call, and the two exports scale with memory size. The async
    # lookups now run in tasks like the Wave 1 helpers; the sync
    # exports run on worker threads. Response shapes are unchanged.
    # ------------------------------------------------------------------

    # Wave 5: the media/memory handler family (RUX-B-08) lives in
    # routes/ws_memory_media.py — the dispatch branches below call
    # its module-level handlers with this connection's context.
    # Wave 5: the plugin lifecycle family (RUX-B-10) lives in
    # routes/ws_mcp_plugins.py, with the pending-install state.

    # Wave 5: the voice handler family (RUX-B-09) lives in
    # routes/ws_voice.py — the dispatch branches below call its
    # module-level handlers with this connection's context.

    # Epic 2, E2-10: install the web-search seam for this connection —
    # keys resolve through the encrypted secret store (provider name =
    # "exa"/"brave"/"tavily"/"firecrawl"), and the provider choice is
    # read live from the user config on every search.
    from aether_engine.tools.web_search import (
        set_search_config_loader,
        set_search_key_loader,
    )

    # N-43: these loaders are MODULE-GLOBAL seams. This connection
    # captures whatever was installed before it so its disconnect
    # cleanup can put it back — previously every connection's
    # lambdas (bound to its own secret store) simply stayed
    # installed after the connection died.
    _prev_search_key_loader = set_search_key_loader(secret_store.load_provider)
    _prev_search_config_loader = set_search_config_loader(
        lambda: (engine_state.user_config.search_provider, engine_state.user_config.search_fallback_to_ddgs)
    )

    # Epic 8, E8-6.2: the browser session layer reads the live
    # browser policy through this seam (master switch for new
    # sessions), read fresh from the user config on every check.
    from aether_engine.browser.policy import (
        policy_from_user_config,
        set_browser_policy_loader,
    )

    _prev_browser_policy_loader = set_browser_policy_loader(
        lambda: policy_from_user_config(engine_state.user_config))

    # Master build S20: the desktop seams get the same treatment —
    # the live desktop policy (read fresh from the user config on
    # every gate/tool check) and the vision-grounding provider
    # resolver (the configured auxiliary "vision" model through
    # THIS connection's secret store, the advisor pattern). Both
    # restored on disconnect with the rest (N-43).
    from aether_engine.desktop.policy import (
        policy_from_user_config as desktop_policy_from_user_config,
    )
    from aether_engine.desktop.policy import (
        set_desktop_policy_loader,
    )
    from aether_engine.desktop.vision import (
        default_vision_provider,
        set_vision_provider_resolver,
    )

    _prev_desktop_policy_loader = set_desktop_policy_loader(
        lambda: desktop_policy_from_user_config(engine_state.user_config))
    _prev_vision_resolver = set_vision_provider_resolver(
        lambda: default_vision_provider(
            engine_state.user_config, secret_store))

    # Orchestration programme P4c: the deep_research backend (GPT
    # Researcher) reads the live user config and THIS connection's
    # secret store through the same module-global seam pattern —
    # installed here, restored on disconnect with the rest (N-43).
    from aether_engine.langgraph.backends.researcher_gpt import (
        DeepResearchRuntime,
        set_deep_research_runtime_loader,
    )

    _prev_deep_research_loader = set_deep_research_runtime_loader(
        lambda: DeepResearchRuntime(
            user_config=engine_state.user_config,
            secret_store=secret_store))

    # N07: forward production-analyzer events (MEDIA_ANALYSIS_PROGRESS
    # / MEDIA_ANALYZED) to this connection so the Media Library
    # reflects analysis state live. Analysis is triggered from many
    # places, so it cannot ride the per-task EventBus — the analyzer
    # fans out to subscribers instead (see media/analyzer.py). The
    # subscription is removed when this connection closes (the
    # disconnect handlers below).
    from aether_engine.media.analyzer import subscribe_media_events

    async def _push_media_event(event):
        try:
            await ws.send_text(event.to_json())
        except Exception:  # the socket may be mid-close; delivery is best-effort
            logger.debug("Media event push failed", exc_info=True)

    _unsubscribe_media_events = subscribe_media_events(_push_media_event)

    # Calendar layer C4 (14 Nov 2026): the same fan-out pattern for
    # REMINDER_DUE — the scheduler fires reminders outside any task,
    # so they reach this connection by subscription, not by the
    # per-task EventBus. Removed on disconnect, like media above.
    from aether_engine.calendar.reminders import subscribe_reminder_events

    async def _push_reminder_event(event):
        try:
            await ws.send_text(event.to_json())
        except Exception:  # the socket may be mid-close; delivery is best-effort
            logger.debug("Reminder event push failed", exc_info=True)

    _unsubscribe_reminder_events = subscribe_reminder_events(_push_reminder_event)


    # ── Voice session (Epic 8, E8-5.1a) ─────────────────────────────
    # The cascade itself lives in aether_engine.voice.pipeline; this
    # layer only wires it to the socket: events out, transcript
    # commits into the session store, teardown on stop/disconnect.

    # Wave 5: the voice pipeline lifecycle helpers, speech-engine
    # cache, escalated background-job runner and teardown helpers
    # live in routes/ws_voice.py.

    async def _cancel_connection_background_work() -> None:
        """Disconnect cleanup for this connection's own work (N-19).

        The pre-existing teardown cancelled the task runner and the
        voice lane but left everything else this connection started:
        in-flight model downloads, dictation/voice chains, and every
        fire-and-forget ``_send_*`` helper — which then sent into a
        dead socket or, for downloads, kept running to completion
        for nobody. Also drops this connection's pending plugin
        installs (E1-06) and restores the module-global seam
        loaders this connection replaced (N-43).
        """
        for dl_task in list(conn.active_downloads.values()):
            if not dl_task.done():
                dl_task.cancel()
        conn.active_downloads.clear()
        for tail in (conn.dictation_tail, conn.voice_op_tail):
            if tail is not None and not tail.done():
                tail.cancel()
        pending = [t for t in _conn_background_tasks if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for install_id in conn.own_plugin_install_ids:
            pending_plugin_installs.pop(install_id, None)
            pending_plugin_install_created.pop(install_id, None)
        conn.own_plugin_install_ids.clear()
        set_search_key_loader(_prev_search_key_loader)
        set_search_config_loader(_prev_search_config_loader)
        set_browser_policy_loader(_prev_browser_policy_loader)
        set_desktop_policy_loader(_prev_desktop_policy_loader)
        set_vision_provider_resolver(_prev_vision_resolver)
        set_deep_research_runtime_loader(_prev_deep_research_loader)

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = Event.from_json(raw)
            except (SchemaVersionMismatchError, InvalidEventPayloadError) as e:
                await ws.send_text(Event(type=EventType.ERROR, payload={"error": str(e)}).to_json())
                continue

            req_id = msg.request_id

            # Unified logging: trace the message TYPE as received
            # (before any handler mutates msg.type, e.g. steer ->
            # START_TASK). Types only — payloads are never logged.
            _log_ws_message_type(msg.type)

            # -----------------------------------------------------------
            # Steering (Epic 1, E1-07): a message for a session whose
            # task is running — or paused for approval — is queued for
            # that task (saved + enqueued, nothing cancelled). With no
            # live task for the session it falls through and is handled
            # as a normal START_TASK below, so a steer can never strand
            # a message.
            # -----------------------------------------------------------
            if msg.type == EventType.STEER_TASK:
                _raw_steer_prompt = msg.payload.get("prompt")
                steer_prompt = (
                    _raw_steer_prompt.strip()
                    if isinstance(_raw_steer_prompt, str) else "")
                if not steer_prompt:
                    # E1-13: an empty steer names nothing to steer
                    # with. It must be answered with an error — never
                    # fall through to START_TASK, where it used to
                    # degrade into a spurious "Say hello" task that
                    # cancelled the user's live task.
                    await ws.send_text(Event(
                        type=EventType.ERROR,
                        request_id=req_id,
                        payload={"error": "STEER_TASK requires a non-empty prompt; nothing was steered or started."},
                    ).to_json())
                    continue
                steer_session = msg.payload.get("session_id") or conn.task_session_id
                steer_active = bool(
                    steer_session
                    and conn.task_runner is not None
                    and not conn.task_runner.done()
                    and (conn.task_session_id is None or conn.task_session_id == steer_session)
                )
                steer_paused = False
                if steer_session and not steer_active:
                    try:
                        pending = await get_pending_approvals()
                        steer_paused = any(
                            p.get("thread_id") == steer_session for p in pending)
                    except Exception:  # noqa: BLE001 — the approval scan is best-effort: failure degrades a steer to a normal new task, never a lost message
                        steer_paused = False
                steer_attachments = msg.payload.get("attachments") or []
                if steer_prompt and steer_session and (steer_active or steer_paused):
                    try:
                        await asyncio.to_thread(
                            session_store.add_message, steer_session, "user",
                            steer_prompt, None, msg.payload.get("message_id"), steer_attachments)
                        await _register_message_attachments(
                            steer_session, msg.payload.get("message_id"), steer_attachments, ws)
                    except Exception as e:  # noqa: BLE001 — best-effort persistence, same as START_TASK: a save failure must not lose the steer
                        logger.error(f"Failed to save steering message: {e}")
                    # PF-1 (xref M16): the attachments ride the steer
                    # into the queue, so the steered turn presents
                    # them to the model exactly like an initial turn —
                    # persisting them with the saved message is not
                    # enough on its own.
                    enqueue_steer(
                        steer_session, steer_prompt,
                        msg.payload.get("message_id"), steer_attachments)
                    continue
                msg.type = EventType.START_TASK

            handler = _DISPATCH.get(msg.type)
            if handler is not None:
                await handler(conn, msg)
            else:
                # S2 (master build): a message that parses as a valid
                # EventType but has no dispatch entry (e.g. shell/engine
                # version skew) must not vanish silently — the UI would
                # wait forever on a response that never comes. Answer
                # it with ERROR and log the type, like the parse-level
                # failures above.
                logger.warning(
                    f"No handler for valid message type {msg.type.value}; "
                    "answering ERROR")
                await ws.send_text(Event(
                    type=EventType.ERROR,
                    request_id=req_id,
                    payload={
                        "error": (
                            f"No handler for message type "
                            f"{msg.type.value}; the engine cannot "
                            "process this request."),
                    },
                ).to_json())

    except WebSocketDisconnect:
        logger.info(f"Client disconnected from {ip}:{port}")
        _unsubscribe_media_events()
        _unsubscribe_reminder_events()
        if conn.task_start_tail and not conn.task_start_tail.done():
            conn.task_start_tail.cancel()
        if conn.task_runner and not conn.task_runner.done():
            conn.task_runner.cancel()
        await stop_voice_session(conn)
        await stop_read_aloud(conn)
        await close_speech_engines(conn)
        await cancel_voice_background_tasks(conn)
        await _cancel_connection_background_work()
        engine_state.audit_logger.log_event("CLIENT_DISCONNECTED", {"client_ip": ip})
    except Exception as e:  # noqa: BLE001 — top-level socket boundary: any session failure is logged and the task is cancelled, never propagated
        logger.error(f"WebSocket session error: {e}")
        _unsubscribe_media_events()
        _unsubscribe_reminder_events()
        if conn.task_start_tail and not conn.task_start_tail.done():
            conn.task_start_tail.cancel()
        if conn.task_runner and not conn.task_runner.done():
            conn.task_runner.cancel()
        await stop_voice_session(conn)
        await stop_read_aloud(conn)
        await close_speech_engines(conn)
        await cancel_voice_background_tasks(conn)
        await _cancel_connection_background_work()


# ---------------------------------------------------------------------------
# Dispatch handlers (Wave 5, slice 3)
#
# The receive loop's message-type chain, as module-level handlers
# over the connection context. Each handler is the branch it
# replaces: its disposition — await inline, spawn a background
# task, chain onto a tail, enqueue — is encoded in its body,
# exactly as the branch encoded it, so the table below only
# answers "which handler", never "how". STEER_TASK is not in
# the table: the loop's pre-pass still owns it (it may rewrite
# the message into a START_TASK, which then dispatches here).
# Three event pairs share one handler each, as the branches
# they came from were membership tests over the pair.
# ---------------------------------------------------------------------------


# -----------------------------------------------------------
# Task lifecycle events
# -----------------------------------------------------------


async def _handle_start_task(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # RUX-B-07: the pre-work runs in a launch task (see
    # _start_task_message); the loop only chains it
    # after the previous launch and moves on, so a slow
    # classification / cancel-wait / slash run can no
    # longer wedge every other message on the socket.
    _previous_start = conn.task_start_tail
    conn.task_start_tail = asyncio.create_task(
        start_task_message(conn, msg, req_id, _previous_start))

async def _handle_cancel_task(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # RUX-B-07: a task still in its launch pre-work is
    # cancellable too — cancelling the launch prevents
    # the runner from ever being created, which is the
    # outcome the old serial loop produced by reading
    # the cancel only after the (now-cancelled) start.
    launch_pending = conn.task_start_tail is not None and not conn.task_start_tail.done()
    if launch_pending:
        conn.task_start_tail.cancel()
        logger.info("Cancellation requested for pending task launch.")
    if conn.task_runner and not conn.task_runner.done():
        conn.task_runner.cancel()
        logger.info("Cancellation requested for active task runner.")
    if launch_pending and not (conn.task_runner and not conn.task_runner.done()):
        # A launch cancelled in its pre-work never
        # creates the pump, so the pump's own
        # TASK_CANCELLED audit record
        # (execute_task_generator's CancelledError
        # handler) never fires for this cancel. Write
        # the same record here so every honoured cancel
        # leaves exactly one audit entry; when a runner
        # is active, its pump writes the record instead
        # and this branch must not double-log.
        engine_state.audit_logger.log_event(
            "TASK_CANCELLED", {"task_id": "unknown"})
    if launch_pending or (conn.task_runner and not conn.task_runner.done()):
        await conn.ws.send_text(Event(
            type=EventType.TASK_CANCELLED,
            request_id=req_id,
            # N-44: name the task actually cancelled
            # (the pump tracks it from TASK_CREATED);
            # "unknown" only for a launch cancelled
            # before its task ever had an id.
            payload={"task_id": conn.active_task_id or "unknown", "state": TaskState.CANCELLED.value},
        ).to_json())
    else:
        await conn.ws.send_text(Event(
            type=EventType.ERROR,
            request_id=req_id,
            payload={"error": "No active task to cancel"},
        ).to_json())

async def _handle_ping(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    await conn.ws.send_text(Event(
        type=EventType.PONG,
        request_id=req_id,
        payload={"echo_ts": msg.ts},
    ).to_json())

async def _handle_tool_approval_decision(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    approval_key = msg.payload.get("approval_key") or msg.request_id
    is_approved = (msg.type == EventType.TOOL_APPROVAL_GRANTED)
                
    if is_approved:
        engine_state.audit_logger.log_event("APPROVAL_GRANTED", {"approval_key": approval_key})
    else:
        engine_state.audit_logger.log_event("APPROVAL_REJECTED", {"approval_key": approval_key})

    # Epic 8, E8-1.3: approval-time argument edits ride the
    # GRANTED event. Audit the changed field names against
    # the pending call's original args (best-effort, like
    # the scoped-grant recording below: a lookup failure
    # must never block the approval the user just gave).
    edited_args = msg.payload.get("edited_args") if is_approved else None
    if not isinstance(edited_args, dict):
        edited_args = None
    # Epic 8, E8-6.2: an envelope grant the owner made at
    # a browser confirmation rides the GRANTED event; the
    # pause node scopes it to the confirmed action.
    browser_envelope = msg.payload.get("browser_envelope") if is_approved else None
    if not isinstance(browser_envelope, dict):
        browser_envelope = None
    # Master build S20: the desktop confirmation's extras ride
    # the same event — the per-task envelope grant (scoped by
    # the pause node to the confirmed action, exactly like the
    # browser's) and the remembered-grant flag (the pause node
    # and the grant store both refuse the hard-confirm class).
    desktop_envelope = msg.payload.get("desktop_envelope") if is_approved else None
    if not isinstance(desktop_envelope, dict):
        desktop_envelope = None
    desktop_remember = bool(msg.payload.get("desktop_remember")) if is_approved else False
    if edited_args is not None:
        try:
            pending = await get_pending_approvals()
            match = next((p for p in pending if p.get("thread_id") == approval_key), None)
            if match:
                _audit_approval_arguments_edited(
                    engine_state.audit_logger,
                    approval_key,
                    match.get("tool_name"),
                    match.get("args") or {},
                    edited_args,
                )
        except Exception:
            logger.debug("Failed to audit approval argument edits", exc_info=True)

    # E1-F04: the resume rebuilds the PAUSED task's
    # effective configuration — the run_config snapshot
    # checkpointed with the thread's state when the task
    # started: provider/model, per-agent models/tools,
    # forced agent, workspace roots and the layered user
    # config including its MCP risk overrides. The
    # current globals are only the fallback for threads
    # paused before snapshots existed. E1-F03: the
    # resumed execution is bound to the paused task's own
    # session (from that same durable state), never to a
    # connection-local variable — an approval arriving on
    # a fresh connection resumes exactly the task that
    # paused.
    run_cfg = await get_persisted_run_config(approval_key) or {}
    resume_session_id = run_cfg.get("session_id") or approval_key
    resume_user_config = engine_state.user_config
    if isinstance(run_cfg.get("user_config"), dict):
        try:
            resume_user_config = UserConfig.from_dict(run_cfg["user_config"])
        except Exception:  # a corrupt snapshot degrades to the live config, never blocks an approval
            logger.debug("Could not restore task user_config for approval resume", exc_info=True)
    resume_provider_name = run_cfg.get("provider_name") or engine_state.active_provider
    resume_model = run_cfg.get("model") or engine_state.active_model
    resume_agent_models = run_cfg.get(
        "agent_models", resume_user_config.agent_models)
    resume_agent_tools = run_cfg.get(
        "agent_tools", resume_user_config.agent_tools)
    resume_workspace_roots = (
        run_cfg.get("workspace_roots")
        or resume_user_config.workspace_roots
        or [str(Path.home() / "AetherWorkspace")])

    # M17 residual: the recreated instance honours the
    # run's routing constraints. With a snapshot they are
    # the original decision's own values; without one they
    # are re-derived from the same sources that decision
    # used — local_only from the auxiliary vision override
    # in user config, and image presence from the thread's
    # stored messages (the executor re-derives both again
    # from the checkpoint for the resumed graph config).
    if run_cfg:
        resume_local_only = bool(run_cfg.get("local_only"))
        resume_has_images = bool(run_cfg.get("require_vision"))
    else:
        resume_local_only = local_only_from_vision_override(
            engine_state.user_config.auxiliary_models)
        resume_has_images = False
        try:
            _thread_session = await asyncio.to_thread(
                session_store.get_session, approval_key)
            resume_has_images = any(
                m.get("attachments")
                for m in (_thread_session or {}).get("messages", []))
        except Exception:
            logger.debug(
                "Could not inspect resumed thread attachments "
                "for the vision constraint", exc_info=True)
    try:
        provider_instance = _create_provider_instance(
            resume_provider_name, resume_model, conn.secret_store,
            local_only=resume_local_only,
            require_vision=resume_has_images,
        )
    except Exception:  # provider construction can fail on config/key errors; skipping the resume is the designed degradation
        logger.debug("Could not instantiate provider for approval resume", exc_info=True)
        return # Ignore if provider cannot be instantiated

    # Same central builder as task start (PRD Q3) — the resume
    # path used to register all file tools unconditionally while
    # task start gated them on coding tasks. The MCP risk
    # overrides are the resumed task's effective ones
    # (E1-F04), not the current globals.
    from aether_engine.mcp.registry import GLOBAL_MCP_REGISTRY

    tools = build_tool_registry(
        mcp_tools=GLOBAL_MCP_REGISTRY.get_mcp_tools(risk_overrides=resume_user_config.mcp_tool_risk_overrides),
    )

    # Epic 2, E2-09: a scoped approval ("this file" / "this
    # folder") becomes a stored grant before the resume, so
    # the NEXT matching call is covered. Best-effort by
    # design: a grant-recording failure must never block
    # the approval the user just gave.
    grant_scope = msg.payload.get("scope") if is_approved else None
    if grant_scope in ("file", "folder"):
        try:
            from aether_engine.tools.approval_cache import get_default_cache
            from aether_engine.tools.paths import candidate_paths

            pending = await get_pending_approvals()
            match = next((p for p in pending if p.get("thread_id") == approval_key), None)
            if match:
                paths = candidate_paths(match.get("args"))
                if paths:
                    prefix = paths[0] if grant_scope == "file" else paths[0].parent
                    risk = tools.get_tool(match["tool_name"]).risk.value
                    grant_id = get_default_cache().grant(str(prefix), risk, tool_name=match["tool_name"])
                    engine_state.audit_logger.log_event(
                        "APPROVAL_GRANT_CREATED",
                        {"grant_id": grant_id, "tool_name": match["tool_name"], "prefix": str(prefix), "ceiling": risk, "scope": grant_scope},
                    )
        except Exception:
            # Grant recording is best-effort beside an approval already given.
            logger.debug("Failed to record scoped approval grant", exc_info=True)

    # Pass the approved decision back into LangGraph,
    # with the paused task's session binding and forced
    # agent restored alongside its configuration (E1-F03
    # + E1-F04).
    resume_gen = resume_langgraph_task(
        thread_id=approval_key,
        approved=is_approved,
        provider=provider_instance,
        tools=tools,
        workspace_roots=resume_workspace_roots,
        disabled_nodes=engine_state.disabled_nodes,
        agent_models=resume_agent_models,
        agent_tools=resume_agent_tools,
        secret_store=conn.secret_store,
        user_config=resume_user_config,
        session_id=resume_session_id,
        forced_agent=run_cfg.get("forced_agent"),
        edited_args=edited_args,
        browser_envelope=browser_envelope,
        desktop_envelope=desktop_envelope,
        desktop_remember=desktop_remember,
        # M17 residual: the resumed run's graph config
        # carries the same constraints (the executor also
        # re-derives them from the checkpoint as a net).
        local_only=resume_local_only,
        require_vision=resume_has_images,
    )

    if conn.task_runner and not conn.task_runner.done():
        conn.task_runner.cancel()
        # E1-09: AWAIT the displaced runner before the
        # resume pump starts — the same cancel-then-await
        # idiom _start_task_message uses. Cancelling
        # without awaiting let the old pump keep
        # streaming (and writing usage/persistence) into
        # the resumed task's lifetime.
        try:
            await conn.task_runner
        except asyncio.CancelledError:
            pass

    req_id = msg.request_id or "resume"
    conn.task_session_id = approval_key
    conn.task_runner = asyncio.create_task(
        conn.execute_task_generator(resume_gen, resume_provider_name, resume_model, req_id, session_id=resume_session_id)
    )

# -----------------------------------------------------------
# Provider management & health events
# -----------------------------------------------------------


async def _handle_provider_list_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # RUX-B-02: answer from the model cache immediately;
    # discovery for providers with an empty cache runs in
    # a background task and pushes a refreshed list when
    # it lands (see send_provider_list_update).
    providers = await _build_provider_list_async(conn.secret_store, discover=False)
    cfg = engine_state.user_config
    await conn.ws.send_text(Event(
        type=EventType.PROVIDER_LIST_RESPONSE,
        request_id=req_id,
        payload={"providers": providers, "default_provider": cfg.default_provider, "default_model": cfg.default_model, "health": engine_state.health_manager.get_all_statuses()},
    ).to_json())
    conn.spawn_background(send_provider_list_update(conn, req_id))

async def _handle_provider_validate_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    pname = msg.payload.get("provider", "")
    key = msg.payload.get("api_key", "")
    base_url = msg.payload.get("base_url", "")
    resp_type = (
        EventType.SETTINGS_PROVIDER_VALIDATE_RESULT
        if msg.type == EventType.SETTINGS_PROVIDER_VALIDATE_REQUEST
        else EventType.PROVIDER_VALIDATE_RESPONSE
    )
    is_custom = pname == "custom_openai" or engine_state.user_config.custom_provider_types.get(pname) == "openai_compatible"
                
    # If key is empty (e.g. masked in UI), attempt to load the saved key
    if not key:
        saved_key = conn.secret_store.load_provider(pname)
        if saved_key:
            key = saved_key
                
    # If base_url is empty, attempt to load the saved base_url
    if not base_url:
        saved_url = engine_state.user_config.custom_base_urls.get(pname)
        if saved_url:
            base_url = saved_url
                
    # send_validation_result validates with
    # persist_models=False: testing a throwaway/template
    # provider must not leak discovery data into the
    # persisted config.
    if not key and not is_custom:
        await conn.ws.send_text(Event(
            type=resp_type,
            request_id=req_id,
            payload={"provider": pname, "status": "invalid_key", "message": "API key cannot be empty."},
        ).to_json())
    else:
        # Off the message loop (see send_validation_result):
        # the Test no longer blocks the socket, and repeat
        # clicks share the in-flight validation.
        conn.spawn_background(send_validation_result(conn, resp_type, req_id, pname, key, base_url))

async def _handle_provider_save_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    pname = msg.payload.get("provider", "")
    key = msg.payload.get("api_key", "")
    base_url = msg.payload.get("base_url", "")
    is_default = msg.payload.get("is_default", False)
    default_model = msg.payload.get("default_model", "")
    models = msg.payload.get("models", [])
    display_name = msg.payload.get("display_name", "")
    provider_type = msg.payload.get("provider_type", "")
    try:
        if not isinstance(pname, str) or not pname.strip():
            raise ValueError("Provider name is required.")
        is_custom_provider = (
            pname == "custom_openai"
            or pname.startswith("custom_")
            or provider_type == "openai_compatible"
        )
        if is_custom_provider:
            if display_name:
                engine_state.user_config.custom_provider_names[pname] = display_name
            if provider_type:
                engine_state.user_config.custom_provider_types[pname] = provider_type
            engine_state.user_config.custom_base_urls[pname] = base_url
        elif base_url:
            engine_state.user_config.custom_base_urls[pname] = base_url

        # Save config before fetching models so custom names/URLs are persisted
        # even if the model fetch raises an exception.
        save_config(engine_state.user_config)

        # A new provider derived from the "custom_openai" template leaves
        # throwaway discovery data on that template (written during Test /
        # Refresh). Clear it unless the template is itself a real provider.
        if is_custom_provider and pname != "custom_openai":
            template_in_use = (
                "custom_openai" in set(conn.secret_store.list_providers())
                or "custom_openai" in engine_state.user_config.custom_base_urls
                or "custom_openai" in engine_state.user_config.custom_provider_types
            )
            if not template_in_use:
                engine_state.user_config.provider_models.pop("custom_openai", None)
                engine_state.model_registry.remove_provider_models("custom_openai")
                engine_state.health_manager.remove_provider("custom_openai")

        # RUX-B-06: a key saved without a model list used
        # to discover inline here (≤10 s on the message
        # loop). The save itself — key, config, defaults —
        # is instant and answers now; discovery moves to
        # complete_provider_save_discovery, which applies
        # the same results and pushes the refreshed list.
        deferred_discovery = bool(key and not models)
        if key:
            conn.secret_store.save_provider(pname, key)
            engine_state.health_manager.reset_provider(pname)
            if models:
                engine_state.user_config.provider_models[pname] = models
                engine_state.model_registry.update_provider_models(pname, models)
        elif models:
            engine_state.user_config.provider_models[pname] = models
            engine_state.model_registry.update_provider_models(pname, models)

        if default_model:
            if is_default or engine_state.user_config.default_provider == pname:
                engine_state.user_config.default_provider = pname
                engine_state.user_config.default_model = default_model
                engine_state.active_provider = pname
                engine_state.active_model = default_model
        elif is_default and pname:
            engine_state.user_config.default_provider = pname
            if deferred_discovery:
                # The model choice waits for the deferred
                # discovery (same registry-priority
                # resolution, in complete_provider_save_
                # discovery) — resolving now could only use
                # the stale pre-discovery list.
                engine_state.active_provider = pname
            else:
                # F-10 residual: resolve the default through the
                # registry's curated priority, never from the raw
                # provider_models list's [0] — that list's order
                # is not priority and may be stale or
                # client-supplied (a pre-fix list with the 404
                # gemini-2.5-flash first would otherwise be
                # silently re-pinned as the default here).
                default_entry = engine_state.model_registry.get_default_model_for_provider(pname)
                resolved_default = default_entry.id if default_entry else ""
                if not resolved_default:
                    cur_models = engine_state.user_config.provider_models.get(pname, [])
                    resolved_default = cur_models[0] if cur_models else ""
                if resolved_default:
                    engine_state.user_config.default_model = resolved_default
                    engine_state.active_model = resolved_default
                engine_state.active_provider = pname

        save_config(engine_state.user_config)
        engine_state.audit_logger.log_event("PROVIDER_SAVED", {"provider": pname})
        save_payload = {
            "provider": pname,
            "success": True,
            "models": engine_state.user_config.provider_models.get(pname, []),
            "excluded_models": engine_state.user_config.provider_excluded_models.get(pname, []),
        }
        await conn.ws.send_text(Event(
            type=EventType.PROVIDER_SAVE_RESPONSE,
            request_id=req_id,
            payload=save_payload,
        ).to_json())
        if deferred_discovery:
            conn.spawn_background(complete_provider_save_discovery(conn, 
                req_id, pname, key, base_url,
                bool(is_default and not default_model)))
    except Exception as e:  # noqa: BLE001 — handler boundary: any save failure is reported to the client in the response payload
        await conn.ws.send_text(Event(
            type=EventType.PROVIDER_SAVE_RESPONSE,
            request_id=req_id,
            payload={
                "provider": pname,
                "success": False,
                "error": f"{type(e).__name__}: {e}",
            },
        ).to_json())

async def _handle_provider_remove_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    pname = msg.payload.get("provider", "")
    deleted = conn.secret_store.delete_provider(pname)
    engine_state.user_config.provider_models.pop(pname, None)
    engine_state.user_config.custom_base_urls.pop(pname, None)
    engine_state.user_config.custom_provider_names.pop(pname, None)
    engine_state.user_config.custom_provider_types.pop(pname, None)
    engine_state.model_registry.remove_provider_models(pname)
    # E8-07 bookkeeping: this handler deregistered the
    # provider itself (ALL entries, curated included —
    # an explicit removal), so resync must not treat it
    # as config-registered and restore its curated
    # baseline on a later pass.
    engine_state._config_registered_providers.discard(pname)
    engine_state.health_manager.remove_provider(pname)
    if pname in engine_state.configured_providers:
        engine_state.configured_providers.remove(pname)
    if engine_state.user_config.default_provider == pname:
        remaining = [p for p in conn.secret_store.list_providers() if p != pname]
        new_default = remaining[0] if remaining else "google_gemini"
        engine_state.user_config.default_provider = new_default
        # F-10 residual: as in PROVIDER_SAVE_REQUEST, the
        # replacement default model comes from the
        # registry's curated priority, not list position.
        default_entry = engine_state.model_registry.get_default_model_for_provider(new_default)
        if default_entry:
            engine_state.user_config.default_model = default_entry.id
        else:
            new_models = engine_state.user_config.provider_models.get(new_default, [])
            engine_state.user_config.default_model = new_models[0] if new_models else ""
        engine_state.active_provider = new_default
        engine_state.active_model = engine_state.user_config.default_model
    # E1-10: a save failure here is answered, not raised
    # — the removal already happened in-session (secret
    # store + registry), so the audit record stands; the
    # reply reports that persistence failed.
    save_err = _try_save_config()
    engine_state.audit_logger.log_event("PROVIDER_REMOVED", {"provider": pname})
    remove_payload: dict[str, Any] = {
        "provider": pname, "deleted": deleted,
        "success": save_err is None}
    if save_err is not None:
        remove_payload["error"] = (
            f"Provider removed, but settings could not be saved: {save_err}")
    await conn.ws.send_text(Event(
        type=EventType.PROVIDER_REMOVE_RESPONSE,
        request_id=req_id,
        payload=remove_payload,
    ).to_json())

async def _handle_provider_reveal_key_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    pname = msg.payload.get("provider", "")
    try:
        decrypted_key = conn.secret_store.load_provider(pname)
        await conn.ws.send_text(Event(
            type=EventType.PROVIDER_REVEAL_KEY_RESPONSE,
            request_id=req_id,
            payload={"provider": pname, "api_key": decrypted_key, "success": True},
        ).to_json())
    except Exception as e:  # noqa: BLE001 — handler boundary: any decrypt/load failure is reported to the client in the response payload
        await conn.ws.send_text(Event(
            type=EventType.PROVIDER_REVEAL_KEY_RESPONSE,
            request_id=req_id,
            payload={"provider": pname, "success": False, "error": str(e)},
        ).to_json())

async def _handle_model_set_default(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    pname = msg.payload.get("provider", engine_state.user_config.default_provider)
    model = msg.payload.get("model", "")
    invalid = (
        not model
        or not pname
        or (isinstance(model, str) and not model.strip())
        or (isinstance(pname, str) and not pname.strip())
    )
    if invalid:
        # E1-07: validate at the boundary. The old code
        # answered an empty model with a MODEL_DEFAULT_CHANGED
        # echoing the *old* default — a success-shaped lie
        # that could even relabel another provider as the
        # default in the UI. Mutate nothing, save nothing.
        await conn.ws.send_text(Event(
            type=EventType.ERROR,
            request_id=req_id,
            payload={"error": "MODEL_SET_DEFAULT requires a non-empty provider and model"},
        ).to_json())
    else:
        engine_state.user_config.default_provider = pname
        engine_state.user_config.default_model = model
        # B10 anchor: the applied choice joins the
        # provider's persisted model list in the same save,
        # so a later discovery replace cannot silently
        # orphan it (anchor_default_model keeps it in the
        # list from then on). The registry update mirrors
        # what resync_active_state would derive at the
        # next launch from the same list.
        anchored = engine_state.user_config.provider_models.setdefault(pname, [])
        if model not in anchored:
            anchored.append(model)
        engine_state.model_registry.update_provider_models(pname, anchored)
        engine_state.active_provider = pname
        engine_state.active_model = model
        save_err = _try_save_config()
        if save_err is not None:
            # E1-10: like the E1-07 refusal above, a
            # switch that could not be persisted is an
            # ERROR reply — never a MODEL_DEFAULT_CHANGED
            # for a choice the next launch would not see.
            await conn.ws.send_text(Event(
                type=EventType.ERROR,
                request_id=req_id,
                payload={"error": f"Default model could not be saved: {save_err}"},
            ).to_json())
        else:
            # Unified logging: the applied value goes on
            # record — a switch that "didn't stick" can now
            # be checked against what the engine applied.
            logger.info(f"MODEL_SET_DEFAULT applied: {pname}/{model}")
            await conn.ws.send_text(Event(
                type=EventType.MODEL_DEFAULT_CHANGED,
                request_id=req_id,
                payload={"provider": pname, "model": engine_state.user_config.default_model},
            ).to_json())

# -----------------------------------------------------------
# Session Events
# -----------------------------------------------------------


async def _handle_session_list_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Slice 3: an optional workspace filter with the
    # store's three states — field absent = all sessions
    # (exactly the pre-slice behaviour), a path = that
    # workspace (canonicalised before the store sees it,
    # so any spelling filters the one workspace), explicit
    # null = unbound only. Every row already carries its
    # `workspace` binding from the store.
    if "workspace" not in msg.payload:
        sessions = await asyncio.to_thread(session_store.list_sessions)
        list_error = ""
    elif msg.payload.get("workspace") is None:
        sessions = await asyncio.to_thread(session_store.list_sessions, None)
        list_error = ""
    elif isinstance(msg.payload.get("workspace"), str):
        sessions = await asyncio.to_thread(
            session_store.list_sessions,
            canonical_workspace_root(msg.payload["workspace"]))
        list_error = ""
    else:
        sessions = []
        list_error = "workspace filter must be a workspace path or null"
    list_payload: dict[str, Any] = {"sessions": sessions}
    if list_error:
        list_payload["error"] = list_error
    await conn.ws.send_text(Event(
        type=EventType.SESSION_LIST_RESPONSE,
        request_id=req_id,
        payload=list_payload,
    ).to_json())

async def _handle_session_get_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    sess_id = msg.payload.get("session_id")
    session_data = await asyncio.to_thread(session_store.get_session, sess_id) if sess_id else None
    await conn.ws.send_text(Event(
        type=EventType.SESSION_GET_RESPONSE,
        request_id=req_id,
        payload={"session": session_data},
    ).to_json())

async def _handle_session_create_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    title = msg.payload.get("title", "New Chat")
    # Slice 3 (grill Q3/Q4): the optional workspace field
    # follows the one binding rule — absent binds the
    # active root (or unbound with no roots configured),
    # a string binds that configured root by canonical
    # identity, explicit null creates unbound. An
    # unconfigured target is refused honestly and creates
    # nothing. The reply names the new session's binding.
    bind_ok, binding, bind_error = _resolve_workspace_binding(
        msg.payload, required=False)
    if not bind_ok:
        await conn.ws.send_text(Event(
            type=EventType.SESSION_CREATE_RESPONSE,
            request_id=req_id,
            payload={"session_id": None, "workspace": None,
                     "success": False, "error": bind_error},
        ).to_json())
    else:
        sess_id = await asyncio.to_thread(
            session_store.create_session, title, binding)
        await conn.ws.send_text(Event(
            type=EventType.SESSION_CREATE_RESPONSE,
            request_id=req_id,
            payload={"session_id": sess_id, "workspace": binding},
        ).to_json())

async def _handle_session_delete_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    sess_id = msg.payload.get("session_id")
    deleted = await asyncio.to_thread(session_store.delete_session, sess_id) if sess_id else False
    if sess_id:
        try:
            from aether_engine.artifacts.store import get_artifact_store
            await asyncio.to_thread(get_artifact_store().delete_by_session, sess_id)
        except Exception as cascade_err:  # noqa: BLE001
            logger.warning(f"Failed to cascade delete artifacts for session {sess_id}: {cascade_err}")
        try:
            from aether_engine.media.store import get_media_store
            await asyncio.to_thread(get_media_store().delete_by_session, sess_id)
        except Exception as media_err:  # noqa: BLE001
            logger.warning(f"Failed to cascade delete media for session {sess_id}: {media_err}")
        try:
            await asyncio.to_thread(_remove_session_attachments, sess_id)
        except Exception as attach_err:  # noqa: BLE001
            logger.warning(f"Failed to remove attachments for session {sess_id}: {attach_err}")
    await conn.ws.send_text(Event(
        type=EventType.SESSION_DELETE_RESPONSE,
        request_id=req_id,
        payload={"session_id": sess_id, "deleted": deleted},
    ).to_json())

async def _handle_session_truncate_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    sess_id = msg.payload.get("session_id")
    msg_id = msg.payload.get("message_id")
    content = msg.payload.get("content")
    role = msg.payload.get("role", "user")
    try:
        occurrence = int(msg.payload["occurrence"]) if msg.payload.get("occurrence") is not None else None
    except (TypeError, ValueError):
        occurrence = None
    truncated = False
    if sess_id:
        if msg_id:
            truncated = await asyncio.to_thread(session_store.truncate_session, sess_id, msg_id)
        if not truncated and content:
            # The UI id is not always a stored row id (MCP
            # prompt messages, transferred chats): fall back
            # to the occurrence-aware content match instead
            # of silently truncating nothing.
            truncated = await asyncio.to_thread(
                session_store.truncate_session_by_content,
                sess_id, role, content, occurrence=occurrence)
    if truncated and sess_id:
        # Invalidate the graph thread (keyed by session id):
        # without this the next run keeps reasoning over the
        # deleted branch. Best-effort — the store is already
        # correct, and the executor re-seeds a fresh thread
        # from the retained history.
        try:
            await delete_thread_checkpoints(sess_id)
        except Exception:  # checkpoint cleanup must not fail the truncate response.
            logger.warning(
                f"Could not delete checkpoints for truncated session {sess_id}",
                exc_info=True)
    await conn.ws.send_text(Event(
        type=EventType.SESSION_TRUNCATE_RESPONSE,
        request_id=req_id,
        payload={"session_id": sess_id, "truncated": truncated},
    ).to_json())

async def _handle_session_rename_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    sess_id = msg.payload.get("session_id")
    new_title = (msg.payload.get("title") or "").strip()
    renamed = False
    if sess_id and new_title:
        renamed = await asyncio.to_thread(
            session_store.update_session_title, sess_id, new_title[:120])
    await conn.ws.send_text(Event(
        type=EventType.SESSION_RENAME_RESPONSE,
        request_id=req_id,
        payload={"session_id": sess_id, "title": new_title, "renamed": renamed},
    ).to_json())

async def _handle_session_move_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Per-workspace sessions, slice 3 (grill Q6): re-file
    # a session under another workspace, or unbind it
    # with a null workspace. Binding is organisational
    # only (Q11) — a move touches no history, context, or
    # checkpoints, and a running task keeps the roots it
    # snapshotted at launch. Unknown sessions and
    # unconfigured targets are refused honestly; the
    # reply reports the session's binding after the
    # request, moved or not.
    sess_id = msg.payload.get("session_id")
    session = await asyncio.to_thread(
        session_store.get_session, sess_id) if sess_id else None
    if session is None:
        move_ok = False
        move_binding = None
        move_error = ("A session_id is required." if not sess_id
                      else f"Session not found: {sess_id}")
    else:
        move_ok, move_binding, move_error = _resolve_workspace_binding(
            msg.payload, required=True)
        if move_ok:
            move_ok = await asyncio.to_thread(
                session_store.set_session_workspace,
                sess_id, move_binding)
            if not move_ok:
                move_error = f"Session not found: {sess_id}"
        if not move_ok:
            move_binding = session.get("workspace")
    move_payload: dict[str, Any] = {
        "session_id": sess_id,
        "workspace": move_binding,
        "moved": move_ok,
    }
    if not move_ok:
        move_payload["error"] = move_error
    await conn.ws.send_text(Event(
        type=EventType.SESSION_MOVE_RESPONSE,
        request_id=req_id,
        payload=move_payload,
    ).to_json())

async def _handle_session_search_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    query = msg.payload.get("query", "")
    results = await asyncio.to_thread(session_store.search_sessions, query)
    await conn.ws.send_text(Event(
        type=EventType.SESSION_SEARCH_RESPONSE,
        request_id=req_id,
        payload={"query": query, "results": results},
    ).to_json())

async def _handle_session_export_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    sess_id = msg.payload.get("session_id")
    fmt = (msg.payload.get("format") or "json").lower()
    content = None
    zip_path = None
    filename = ""
    err = None
    try:
        from aether_engine.session_export.exporter import (
            _safe_filename,
            export_session_json,
            export_session_markdown,
            export_session_zip,
        )
        sess = await asyncio.to_thread(session_store.get_session, sess_id)
        if not sess:
            raise ValueError(f"Session '{sess_id}' not found")
        title = sess.get("title", "Session")
        if fmt == "markdown":
            content = await asyncio.to_thread(export_session_markdown, sess_id)
            filename = _safe_filename(title, sess_id, "md")
        elif fmt == "zip":
            z_dest = await asyncio.to_thread(export_session_zip, sess_id)
            zip_path = str(z_dest)
            filename = z_dest.name
        else:
            fmt = "json"
            content = await asyncio.to_thread(export_session_json, sess_id)
            filename = _safe_filename(title, sess_id, "json")
    except Exception as e:
        logger.exception(f"Session export failed for {sess_id}")
        err = str(e)

    await conn.ws.send_text(Event(
        type=EventType.SESSION_EXPORT_RESPONSE,
        request_id=req_id,
        payload={
            "format": fmt,
            "content": content,
            "zip_path": zip_path,
            "filename": filename,
            "error": err,
        },
    ).to_json())

async def _handle_session_import_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    fmt = (msg.payload.get("format") or "json").lower()
    raw_content = msg.payload.get("content")
    fpath = msg.payload.get("file_path")
    try:
        from aether_engine.session_export.importer import (
            import_session_archive,
            import_session_json,
        )
        if fmt == "zip" or (fpath and str(fpath).endswith(".zip")):
            result = await asyncio.to_thread(
                import_session_archive,
                archive_path=fpath,
                content=raw_content,
            )
        else:
            result = await asyncio.to_thread(
                import_session_json,
                content=raw_content,
                file_path=fpath,
            )
    except Exception as e:
        logger.exception("Session import failed")
        result = {
            "ok": False,
            "session_id": "",
            "title": "",
            "message_count": 0,
            "error": str(e),
        }

    await conn.ws.send_text(Event(
        type=EventType.SESSION_IMPORT_RESPONSE,
        request_id=req_id,
        payload=result,
    ).to_json())

async def _handle_rewind_manifest_get_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Epic 8, E8-1.1a: the session's last task's rewind
    # points (recorded by the task runners). An explicit
    # task_id that is not the last task answers with no
    # entries — points exist only for the last task (Q5).
    sess_id = msg.payload.get("session_id")
    req_task_id = msg.payload.get("task_id")
    if sess_id:
        manifest_payload = await asyncio.to_thread(
            rewind_manifest.get_manifest, sess_id, req_task_id)
    else:
        manifest_payload = {
            "session_id": sess_id, "task_id": None, "entries": []}
    await conn.ws.send_text(Event(
        type=EventType.REWIND_MANIFEST_GET_RESPONSE,
        request_id=req_id,
        payload=manifest_payload,
    ).to_json())

async def _handle_task_rewind_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Epic 8, E8-1.1b: execute a rewind against the
    # manifest. Refused while this session's task is live
    # or paused for approval — the points describe the
    # last task, and rolling back under a running one
    # would corrupt both. The service validates the rest
    # (last task only, step exists, checkpoint matches).
    sess_id = msg.payload.get("session_id")
    rewind_busy = bool(
        sess_id
        and conn.task_runner is not None
        and not conn.task_runner.done()
        and (conn.task_session_id is None or conn.task_session_id == sess_id)
    )
    if sess_id and not rewind_busy:
        try:
            pending = await get_pending_approvals()
            rewind_busy = any(
                p.get("thread_id") == sess_id for p in pending)
        except Exception:  # noqa: BLE001 — best-effort scan, same doctrine as steering: a failed scan degrades to the service's own validation
            rewind_busy = False
    if rewind_busy:
        rewind_result = {
            "ok": False,
            "session_id": sess_id,
            "rewound_to_step": msg.payload.get("step_index")
            if isinstance(msg.payload.get("step_index"), int) else -1,
            "undo_expires_at": 0.0,
            "files_not_reverted": [],
            "messages": [],
            "error": "Cannot rewind while a task is running or waiting for approval in this session.",
        }
    else:
        rewind_result = await rewind_service.rewind(
            sess_id,
            msg.payload.get("task_id"),
            msg.payload.get("step_index"),
            msg.payload.get("checkpoint_id"),
        )
    await conn.ws.send_text(Event(
        type=EventType.TASK_REWIND_RESPONSE,
        request_id=req_id,
        payload=rewind_result,
    ).to_json())

async def _handle_task_rewind_undo_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Epic 8, E8-1.1b: restore the steps a staged rewind
    # discarded — only inside the 5-second window.
    sess_id = msg.payload.get("session_id")
    if isinstance(sess_id, str) and sess_id:
        undo_result = await rewind_service.undo(sess_id)
    else:
        undo_result = {
            "ok": False, "restored": False,
            "error": "A session_id is required.",
        }
    await conn.ws.send_text(Event(
        type=EventType.TASK_REWIND_UNDO_RESPONSE,
        request_id=req_id,
        payload=undo_result,
    ).to_json())

async def _handle_session_fork_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Epic 8, E8-1.2: fork the session at a completed
    # step boundary into a new session. Refused while
    # this session's task is live or paused for approval
    # — the boundary must describe settled state, and
    # copying a thread mid-run could catch it torn. The
    # service validates the boundary itself (manifest
    # step of the last task, or a message id).
    sess_id = msg.payload.get("session_id")
    fork_busy = bool(
        sess_id
        and conn.task_runner is not None
        and not conn.task_runner.done()
        and (conn.task_session_id is None or conn.task_session_id == sess_id)
    )
    if sess_id and not fork_busy:
        try:
            pending = await get_pending_approvals()
            fork_busy = any(
                p.get("thread_id") == sess_id for p in pending)
        except Exception:  # noqa: BLE001 — best-effort scan, same doctrine as the rewind guard: a failed scan degrades to the service's own validation
            fork_busy = False
    if fork_busy:
        fork_result = {
            "ok": False,
            "new_session_id": "",
            "source_session_id": sess_id,
            "title": "",
            "messages_copied": 0,
            "messages": [],
            "error": "Cannot fork while a task is running or waiting for approval in this session.",
        }
    else:
        fork_result = await asyncio.to_thread(
            fork_service.fork_session_at,
            sess_id,
            message_id=msg.payload.get("message_id"),
            step_index=msg.payload.get("step_index"),
            new_title=msg.payload.get("new_title"),
        )
    await conn.ws.send_text(Event(
        type=EventType.SESSION_FORK_RESPONSE,
        request_id=req_id,
        payload=fork_result,
    ).to_json())

async def _handle_approval_grants_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    from aether_engine.tools.approval_cache import get_default_cache

    grants = await asyncio.to_thread(get_default_cache().list_grants)
    await conn.ws.send_text(Event(
        type=EventType.APPROVAL_GRANTS_RESPONSE,
        request_id=req_id,
        payload={"grants": grants},
    ).to_json())

async def _handle_approval_grant_revoke_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    from aether_engine.tools.approval_cache import get_default_cache

    grant_id = msg.payload.get("id")
    # E1-01: the id rides an untrusted payload — a
    # non-numeric id is a rejected revoke (answered
    # below with revoked=False), never a ValueError that
    # kills the session.
    try:
        grant_pk = int(grant_id) if grant_id is not None else None
    except (TypeError, ValueError):
        grant_pk = None
    cache = get_default_cache()
    revoked = bool(grant_id) and grant_pk is not None and await asyncio.to_thread(cache.revoke, grant_pk)
    if revoked:
        engine_state.audit_logger.log_event("APPROVAL_GRANT_REVOKED", {"grant_id": grant_id})
    grants = await asyncio.to_thread(cache.list_grants)
    await conn.ws.send_text(Event(
        type=EventType.APPROVAL_GRANT_REVOKE_RESPONSE,
        request_id=req_id,
        payload={"id": grant_id, "revoked": revoked, "grants": grants},
    ).to_json())

# -----------------------------------------------------------
# Artifact Store (Epic 8, E8-2.1)
# -----------------------------------------------------------


async def _handle_artifact_list_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    from aether_engine.artifacts.store import get_artifact_store
    art_store = get_artifact_store()
    sess_id = msg.payload.get("session_id")
    is_global = bool(msg.payload.get("global"))
    limit = msg.payload.get("limit")
    offset = msg.payload.get("offset")
    artifacts, total = await asyncio.to_thread(
        art_store.list_artifacts, session_id=sess_id, is_global=is_global, limit=limit, offset=offset
    )
    await conn.ws.send_text(Event(
        type=EventType.ARTIFACT_LIST_RESPONSE,
        request_id=req_id,
        payload={"artifacts": artifacts, "total": total},
    ).to_json())

async def _handle_artifact_get_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    art_id = msg.payload.get("artifact_id", "")
    result = await handle_artifact_get(art_id)
    await conn.ws.send_text(Event(
        type=EventType.ARTIFACT_GET_RESPONSE,
        request_id=req_id,
        payload=result,
    ).to_json())

async def _handle_dev_server_detect_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Epic 8, E8-2.2: probe for a running dev server so the
    # canvas Preview tab can load it in its sandboxed frame.
    from aether_engine.artifacts.dev_server import detect_dev_server

    result = await asyncio.to_thread(
        detect_dev_server,
        workspace_root=msg.payload.get("workspace_root"),
        project_path=msg.payload.get("project_path"),
    )
    await conn.ws.send_text(Event(
        type=EventType.DEV_SERVER_DETECT_RESPONSE,
        request_id=req_id,
        payload=result,
    ).to_json())

async def _handle_usage_ledger_query_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Epic 8, E8-A8.1a: the header cost popover and (with
    # E8-A8.1b) Settings → Usage read the ledger here.
    from aether_engine.cost.ledger import get_usage_ledger

    result = await asyncio.to_thread(
        get_usage_ledger().query,
        session_id=msg.payload.get("session_id"),
        task_id=msg.payload.get("task_id"),
        start_ts=msg.payload.get("start_ts"),
        end_ts=msg.payload.get("end_ts"),
    )
    await conn.ws.send_text(Event(
        type=EventType.USAGE_LEDGER_QUERY_RESPONSE,
        request_id=req_id,
        payload=result,
    ).to_json())

async def _handle_task_ledger_query_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Master build S3: the Task Ledger read model (aether_engine/
    # task_ledger.py) joins the five existing writers for one task;
    # an unknown task answers found=False, never fabricated data.
    from aether_engine.task_ledger import task_summary

    task_id = msg.payload.get("task_id")
    if not task_id:
        result = {"task_id": None, "found": False,
                  "error": "task_id is required"}
    else:
        result = await task_summary(
            str(task_id),
            session_id=msg.payload.get("session_id"),
            user_config=engine_state.user_config,
        )
    await conn.ws.send_text(Event(
        type=EventType.TASK_LEDGER_QUERY_RESPONSE,
        request_id=req_id,
        payload=result,
    ).to_json())

async def _handle_task_ledger_list_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Master build S3: recent tasks from the ledger's episodic + live
    # tap spine (see task_ledger.recent_tasks for the honest scope).
    from aether_engine.task_ledger import recent_tasks

    tasks = await asyncio.to_thread(
        recent_tasks,
        msg.payload.get("limit") or 20,
        user_config=engine_state.user_config,
    )
    await conn.ws.send_text(Event(
        type=EventType.TASK_LEDGER_LIST_RESPONSE,
        request_id=req_id,
        payload={"tasks": tasks, "count": len(tasks)},
    ).to_json())

async def _handle_context_breakdown_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Epic 8, E8-A8.1b: segmented context breakdown for header and settings
    from aether_engine.cost.context_breakdown import get_context_breakdown

    session_id = msg.payload.get("session_id", "")
    task_id = msg.payload.get("task_id")
    model = msg.payload.get("model") or getattr(engine_state, "active_model", None)

    result = await asyncio.to_thread(
        get_context_breakdown,
        session_id=session_id,
        task_id=task_id,
        model=model,
    )
    await conn.ws.send_text(Event(
        type=EventType.CONTEXT_BREAKDOWN_RESPONSE,
        request_id=req_id,
        payload=result,
    ).to_json())

async def _handle_daily_budget_set_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Epic 8, E8-A8.1b: daily budget update with soft alert
    from aether_engine.cost.ledger import get_usage_ledger

    raw_budget = msg.payload.get("daily_budget_usd")
    try:
        budget = float(raw_budget) if raw_budget is not None else None
    except (TypeError, ValueError):
        # E1-01: a non-numeric budget is rejected with an
        # answer naming the unchanged value — never a
        # ValueError that kills the session, and the bad
        # value is neither applied nor saved.
        await conn.ws.send_text(Event(
            type=EventType.DAILY_BUDGET_SET_RESPONSE,
            request_id=req_id,
            payload={
                "daily_budget_usd": engine_state.user_config.daily_budget_usd,
                "ok": False,
                "error": f"Invalid daily budget value: {raw_budget!r} (expected a number)",
            },
        ).to_json())
        return
    if budget is not None and budget <= 0:
        budget = None
    engine_state.user_config.daily_budget_usd = budget
    save_err = _try_save_config()

    await conn.ws.send_text(Event(
        type=EventType.DAILY_BUDGET_SET_RESPONSE,
        request_id=req_id,
        # E1-10: a failed save is ok:False, not a
        # dropped connection.
        payload=(
            {"daily_budget_usd": budget, "ok": False,
             "error": f"Daily budget could not be saved: {save_err}"}
            if save_err is not None else
            {"daily_budget_usd": budget, "ok": True}),
    ).to_json())

    if budget is not None and budget > 0:
        today_spend = get_usage_ledger().today_total()
        if today_spend > budget:
            await conn.ws.send_text(Event(
                type=EventType.DAILY_BUDGET_EXCEEDED,
                payload={
                    "daily_budget_usd": float(budget),
                    "today_spend_usd": float(today_spend),
                },
            ).to_json())

async def _handle_browser_task_pause_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Epic 8, E8-6.1: browser control channel — pause /
    # resume / take-control / return-control / stop for
    # the dedicated agent browser (browser task card).
    from aether_engine.browser.session import get_browser_manager

    browser_result = await get_browser_manager().control(
        session_id=msg.payload.get("session_id"),
        chat_session_id=msg.payload.get("chat_session_id"),
        action=str(msg.payload.get("action", "")),
    )
    await conn.ws.send_text(Event(
        type=EventType.BROWSER_TASK_PAUSE_RESPONSE,
        request_id=req_id,
        payload=browser_result,
    ).to_json())

async def _handle_desktop_control_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Master build S20: the desktop control channel — pause /
    # resume / take-control / return-control / stop for Computer
    # Use actuation (the chat window's desktop control card).
    # "stop" refuses the named task's further desktop actions
    # WITHOUT killing the task (desktop/control.py). Only this
    # user-driven channel can move the lane; there is no agent
    # path back to AGENT state.
    from aether_engine.desktop.control import get_desktop_control

    result = get_desktop_control().control(
        str(msg.payload.get("action", "")),
        task_id=msg.payload.get("task_id"),
    )
    await conn.ws.send_text(Event(
        type=EventType.DESKTOP_CONTROL_RESPONSE,
        request_id=req_id,
        payload=result,
    ).to_json())

async def _handle_desktop_grants_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Master build S20: Settings → Computer Use reviews the
    # remembered grants — list, and revoke by id. Revocation is
    # effective for the very next gate decision (the gate reads
    # the store at decision time).
    from aether_engine.desktop.grants import get_default_grant_store

    store = get_default_grant_store()
    action = str(msg.payload.get("action", "list"))
    payload: dict = {"success": True}
    if action == "revoke":
        grant_id = msg.payload.get("grant_id")
        if not isinstance(grant_id, int) or isinstance(grant_id, bool):
            payload = {"success": False,
                       "error": "A grant id is required to revoke a grant."}
        else:
            revoked = store.revoke(grant_id)
            payload = {"success": revoked,
                       **({"error": "No such remembered grant."}
                          if not revoked else {})}
            if revoked:
                engine_state.audit_logger.log_event(
                    "DESKTOP_GRANT_REVOKED", {"grant_id": grant_id})
    elif action != "list":
        payload = {"success": False,
                   "error": f"Unknown desktop grants action: {action!r}."}
    if payload.get("success"):
        payload["grants"] = store.list_grants()
    await conn.ws.send_text(Event(
        type=EventType.DESKTOP_GRANTS_RESPONSE,
        request_id=req_id,
        payload=payload,
    ).to_json())

async def _handle_browser_policy_get_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Epic 8, E8-6.2: Settings → Browser Agent Policy reads
    # the confirmation policy (categories + toggles).
    from aether_engine.browser.policy import policy_from_user_config

    await conn.ws.send_text(Event(
        type=EventType.BROWSER_POLICY_GET_RESPONSE,
        request_id=req_id,
        payload=policy_from_user_config(engine_state.user_config).to_dict(),
    ).to_json())

async def _handle_browser_policy_set_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Epic 8, E8-6.2: partial policy update — validated
    # fully before anything mutates, so a rejected update
    # changes nothing. Persisted with the rest of the
    # user config and audited by field name.
    from aether_engine.browser.policy import (
        apply_policy_update,
        policy_from_user_config,
    )

    ok, error = apply_policy_update(engine_state.user_config, msg.payload)
    response_payload: dict = {
        "success": ok,
        "policy": policy_from_user_config(engine_state.user_config).to_dict(),
    }
    if ok:
        # E1-10: fold a save failure into the same
        # success/error vocabulary instead of raising.
        save_err = _try_save_config()
        if save_err is not None:
            response_payload["success"] = False
            response_payload["error"] = (
                f"Browser policy could not be saved: {save_err}")
            ok = False
    if ok:
        changed = sorted(
            key for key in ("enabled", "vault_autofill", "envelopes_enabled")
            if msg.payload.get(key) is not None
        )
        if isinstance(msg.payload.get("categories"), dict):
            changed += [f"categories.{k}" for k in msg.payload["categories"]]
        engine_state.audit_logger.log_event(
            "BROWSER_POLICY_UPDATED", {"fields": changed})
    elif "error" not in response_payload:
        response_payload["error"] = error
    await conn.ws.send_text(Event(
        type=EventType.BROWSER_POLICY_SET_RESPONSE,
        request_id=req_id,
        payload=response_payload,
    ).to_json())

async def _handle_vault_entry_list_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Epic 8, E8-6.3: Settings → Password Vault lists
    # entries METADATA ONLY (the store's list never
    # selects a secret column, so no payload can carry a
    # password). Agent-created entries appear here for
    # the owner's review like any other.
    from aether_engine.vault.store import get_credential_vault

    entries = await asyncio.to_thread(
        get_credential_vault().list_entries)
    await conn.ws.send_text(Event(
        type=EventType.VAULT_ENTRY_LIST_RESPONSE,
        request_id=req_id,
        payload={"entries": entries},
    ).to_json())

async def _handle_vault_entry_save_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Create/update one entry from the Settings form.
    # A blank password on an update means "keep the
    # stored one" (the form never receives the current
    # password, so it cannot send it back). The response
    # echoes only success + id — never the fields.
    from aether_engine.vault.store import (
        VaultError,
        get_credential_vault,
    )

    password = msg.payload.get("password") or None
    if password is not None and not str(password).strip():
        password = None
    totp = msg.payload.get("totp_secret")
    if totp is not None and not str(totp).strip():
        # Blank mirrors the password rule: on update it
        # keeps the stored secret (the form never
        # receives it, so it cannot send it back); on
        # create it simply means "no TOTP secret".
        totp = None
    try:
        entry_id = await asyncio.to_thread(
            get_credential_vault().save_entry,
            site=str(msg.payload.get("site", "")),
            username=str(msg.payload.get("username", "")),
            password=password,
            ask_every_time=bool(
                msg.payload.get("ask_every_time", False)),
            created_by="user",
            totp_secret=totp,
            entry_id=msg.payload.get("id") or None,
        )
        vault_payload: dict = {"success": True, "id": entry_id}
        engine_state.audit_logger.log_event(
            "VAULT_ENTRY_SAVED", {"entry_id": entry_id})
    except VaultError as exc:
        vault_payload = {
            "success": False,
            "id": msg.payload.get("id") or "",
            "error": str(exc),
        }
    await conn.ws.send_text(Event(
        type=EventType.VAULT_ENTRY_SAVE_RESPONSE,
        request_id=req_id,
        payload=vault_payload,
    ).to_json())

async def _handle_vault_entry_delete_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    from aether_engine.vault.store import get_credential_vault

    entry_id = str(msg.payload.get("id", ""))
    removed = await asyncio.to_thread(
        get_credential_vault().delete_entry, entry_id)
    if removed:
        engine_state.audit_logger.log_event(
            "VAULT_ENTRY_DELETED", {"entry_id": entry_id})
    await conn.ws.send_text(Event(
        type=EventType.VAULT_ENTRY_DELETE_RESPONSE,
        request_id=req_id,
        payload={"success": removed, "id": entry_id},
    ).to_json())

async def _handle_vault_totp_list_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Open Point 14: Settings → Password Vault's
    # Authenticator section lists standalone TOTP
    # entries METADATA ONLY (the store's listing never
    # selects the secret column).
    from aether_engine.vault.store import get_credential_vault

    totp_entries = await asyncio.to_thread(
        get_credential_vault().list_totp_entries)
    await conn.ws.send_text(Event(
        type=EventType.VAULT_TOTP_LIST_RESPONSE,
        request_id=req_id,
        payload={"entries": totp_entries},
    ).to_json())

async def _handle_vault_totp_save_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Enrol / update one authenticator entry from the
    # Settings form. The engine validates the Base32
    # setup key and stores it normalised + encrypted; a
    # blank secret on update means "keep the stored
    # one", exactly like the password rule above. The
    # response echoes only success + id — never fields.
    from aether_engine.vault.store import (
        VaultError,
        get_credential_vault,
    )

    setup_key = msg.payload.get("secret")
    if setup_key is not None and not str(setup_key).strip():
        setup_key = None
    try:
        totp_id = await asyncio.to_thread(
            get_credential_vault().save_totp_entry,
            label=str(msg.payload.get("label", "")),
            issuer=str(msg.payload.get("issuer", "")),
            entry_id=msg.payload.get("id") or None,
            **{"secret": setup_key},  # noqa: PIE804 — dict form is deliberate (see tests/test_vault_totp.py)
        )
        totp_payload: dict = {"success": True, "id": totp_id}
        engine_state.audit_logger.log_event(
            "VAULT_TOTP_SAVED", {"entry_id": totp_id})
    except VaultError as exc:
        totp_payload = {
            "success": False,
            "id": msg.payload.get("id") or "",
            "error": str(exc),
        }
    await conn.ws.send_text(Event(
        type=EventType.VAULT_TOTP_SAVE_RESPONSE,
        request_id=req_id,
        payload=totp_payload,
    ).to_json())

async def _handle_vault_totp_delete_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    from aether_engine.vault.store import get_credential_vault

    totp_id = str(msg.payload.get("id", ""))
    removed = await asyncio.to_thread(
        get_credential_vault().delete_totp_entry, totp_id)
    if removed:
        engine_state.audit_logger.log_event(
            "VAULT_TOTP_DELETED", {"entry_id": totp_id})
    await conn.ws.send_text(Event(
        type=EventType.VAULT_TOTP_DELETE_RESPONSE,
        request_id=req_id,
        payload={"success": removed, "id": totp_id},
    ).to_json())

async def _handle_vault_totp_codes_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # The ONLY code-carrying payload in the system,
    # served on the owner's Settings request. Secrets
    # never leave the engine: the store computes the
    # codes in-process from the decrypted secrets, and
    # each entry's read is audited VAULT_TOTP_ACCESSED
    # by the owner accessors there.
    from aether_engine.vault.store import get_credential_vault

    codes_payload = await asyncio.to_thread(
        get_credential_vault().totp_codes)
    await conn.ws.send_text(Event(
        type=EventType.VAULT_TOTP_CODES_RESPONSE,
        request_id=req_id,
        payload=codes_payload,
    ).to_json())

async def _handle_vault_fill_approval_response(conn: WsConnection, msg: Event) -> None:
    # The desktop's answer to an ask-every-time fill
    # prompt (VAULT_FILL_APPROVAL_REQUEST). No reply
    # event — the fill itself reports through the task.
    # Unknown/stale request ids are tolerated silently:
    # the service already failed that fill closed on its
    # timeout if the answer never mattered.
    from aether_engine.vault.service import get_vault_service

    get_vault_service().resolve_fill_approval(
        str(msg.payload.get("request_id", "")),
        bool(msg.payload.get("approved", False)),
    )

# -----------------------------------------------------------
# Media Store & Library (Epic 8, E8-4.2)
# -----------------------------------------------------------


async def _handle_media_list_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    from aether_engine.media.store import get_media_store
    media_store = get_media_store()
    collection = msg.payload.get("collection", "all")
    sess_id = msg.payload.get("session_id")
    limit = msg.payload.get("limit")
    offset = msg.payload.get("offset")
    query = msg.payload.get("query")
    media_items, total = await asyncio.to_thread(
        media_store.list_media,
        collection=collection,
        session_id=sess_id,
        limit=limit,
        offset=offset,
        query=query,
    )
    await conn.ws.send_text(Event(
        type=EventType.MEDIA_LIST_RESPONSE,
        request_id=req_id,
        payload={"media": media_items, "total": total},
    ).to_json())

async def _handle_media_search_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # N07: Library search is served by the backend recall
    # path (the same search_media_entries the media_recall
    # tool uses) so transcript and semantic matches reach
    # the UI. An empty query is an honest empty result —
    # recall is never invoked with nothing to search for.
    # Off the message loop (see send_media_search_result):
    # the recall embeds the query and can take seconds
    # (RUX-B-08). The payload parsing stays here — it is
    # instant and defines the request the task runs.
    search_query = str(msg.payload.get("query") or "").strip()
    search_collection = msg.payload.get("collection", "all")
    if search_collection not in ("all", "user", "generated"):
        search_collection = "all"
    try:
        search_limit = int(msg.payload.get("limit") or 20)
    except (TypeError, ValueError):
        search_limit = 20
    conn.spawn_background(send_media_search_result(conn, 
        req_id, search_query, search_collection, search_limit))

async def _handle_media_delete_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    from aether_engine.media.store import get_media_store
    media_store = get_media_store()
    mid = msg.payload.get("media_id", "")
    del_file = bool(msg.payload.get("delete_underlying_file", False))
    deleted, file_deleted = await asyncio.to_thread(
        media_store.delete_media, mid, delete_underlying_file=del_file
    )
    await conn.ws.send_text(Event(
        type=EventType.MEDIA_DELETE_RESPONSE,
        request_id=req_id,
        payload={"deleted": deleted, "media_id": mid, "file_deleted": file_deleted},
    ).to_json())

async def _handle_media_metadata_update_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    from aether_engine.media.store import get_media_store
    media_store = get_media_store()
    mid = msg.payload.get("media_id", "")
    tags = msg.payload.get("tags")
    notes = msg.payload.get("notes")
    updated = await asyncio.to_thread(
        media_store.update_metadata, mid, tags=tags, notes=notes
    )
    await conn.ws.send_text(Event(
        type=EventType.MEDIA_METADATA_UPDATE_RESPONSE,
        request_id=req_id,
        payload=updated,
    ).to_json())

# -----------------------------------------------------------
# Local Models (Ollama) & Task Download Events
# -----------------------------------------------------------


async def _handle_local_model_list_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Off the message loop (see send_local_model_list_result):
    # the daemon call can take seconds (RUX-B-08).
    conn.spawn_background(send_local_model_list_result(conn, req_id))

async def _handle_local_model_download_start(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    model_name = msg.payload.get("model", "") or msg.payload.get("repo_id", "")
    dest_dir = msg.payload.get("destination_dir")
    if conn.task_runner and not conn.task_runner.done():
        conn.task_runner.cancel()

    async def execute_local_download(model_name=model_name, destination_dir=dest_dir, request_id=req_id):
        async for dl_ev in engine_state.ollama_manager.download_model_task(
            model_name=model_name,
            destination_dir=destination_dir,
            request_id=request_id,
        ):
            await conn.ws.send_text(dl_ev.to_json())

    dl_task = asyncio.create_task(execute_local_download())
    conn.active_downloads[model_name] = dl_task
    conn.task_runner = dl_task

async def _handle_local_model_download_cancel(conn: WsConnection, msg: Event) -> None:
    cancel_model = msg.payload.get("model", "")
    dl = conn.active_downloads.pop(cancel_model, None)
    if dl and not dl.done():
        dl.cancel()
        logger.info(f"Download cancelled for model: {cancel_model}")

async def _handle_local_model_delete_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Off the message loop (see send_local_model_delete_result):
    # the daemon call can take seconds (RUX-B-08).
    conn.spawn_background(send_local_model_delete_result(conn, 
        req_id, msg.payload.get("model", "")))

# -----------------------------------------------------------
# Model Hub catalogue layer (H1, read-only)
# -----------------------------------------------------------


async def _handle_model_hub_search_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Off the message loop (see send_model_hub_search_result):
    # the catalogue call is live HTTP (RUX-B-08).
    conn.spawn_background(send_model_hub_search_result(conn, 
        req_id,
        msg.payload.get("source", "hf"),
        msg.payload.get("query", "") or "",
        msg.payload.get("family")))

async def _handle_model_hub_detail_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Off the message loop (see send_model_hub_detail_result):
    # the catalogue call is live HTTP (RUX-B-08).
    conn.spawn_background(send_model_hub_detail_result(conn, 
        req_id,
        msg.payload.get("source", "hf"),
        msg.payload.get("id", "") or ""))

# -----------------------------------------------------------
# Model Hub downloads (H2): begin/run split — the RESPONSE
# is the synchronous verdict (accepted, or the honest
# refusal: busy store, not runnable, voice components
# missing); progress then flows as the same task event
# stream the local-model pulls use. The download registers
# in active_downloads under its store key, so the existing
# LOCAL_MODEL_DOWNLOAD_CANCEL cancels hub downloads too.
# task_runner is deliberately NOT touched: a hub download
# must never cancel a running agent task.
# -----------------------------------------------------------


async def _handle_model_hub_download_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    plan = await engine_state.hub_downloads.begin(
        source=msg.payload.get("source", "hf"),
        model_id=msg.payload.get("id", "") or "",
        variant=msg.payload.get("variant"),
    )
    if not plan.get("success"):
        await conn.ws.send_text(Event(
            type=EventType.MODEL_HUB_DOWNLOAD_RESPONSE,
            request_id=req_id,
            payload={
                "success": False,
                "error": plan.get("error"),
            },
        ).to_json())
    else:
        await conn.ws.send_text(Event(
            type=EventType.MODEL_HUB_DOWNLOAD_RESPONSE,
            request_id=req_id,
            payload={
                "success": True,
                "model": plan["key"],
                "task_id": plan["task_id"],
            },
        ).to_json())

        async def execute_hub_download(plan=plan, request_id=req_id):
            try:
                async for dl_ev in engine_state.hub_downloads.run(
                    plan, request_id=request_id
                ):
                    await conn.ws.send_text(dl_ev.to_json())
            finally:
                engine_state.hub_downloads.release(plan)

        hub_dl_task = asyncio.create_task(execute_hub_download())
        conn.active_downloads[plan["key"]] = hub_dl_task

async def _handle_model_hub_downloaded_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    result = await engine_state.hub_downloads.downloaded()
    await conn.ws.send_text(Event(
        type=EventType.MODEL_HUB_DOWNLOADED_RESPONSE,
        request_id=req_id,
        payload=result,
    ).to_json())

async def _handle_model_hub_delete_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Off the message loop (see send_model_hub_delete_result):
    # the delete verifies against the daemon and removes
    # the repo tree (RUX-B-08).
    conn.spawn_background(send_model_hub_delete_result(conn, 
        req_id,
        msg.payload.get("source", "hf"),
        msg.payload.get("id", "") or ""))

# -----------------------------------------------------------
# Capabilities Check Scheduler Events
# -----------------------------------------------------------


async def _handle_capability_check_set_schedule(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    interval = msg.payload.get("schedule", "weekly")
    method = msg.payload.get("method", "Both")
    engine_state.scheduler_manager.set_schedule(interval, method)
    status_sum = engine_state.scheduler_manager.get_status_summary()
    await conn.ws.send_text(Event(
        type=EventType.CAPABILITY_CHECK_HISTORY_RESPONSE,
        request_id=req_id,
        payload={
            "summary": status_sum,
            "history": engine_state.scheduler_manager.get_history(),
        },
    ).to_json())

async def _handle_capability_check_run_now(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Off the message loop (see send_capability_check_result):
    # a run is minutes of discovery + live self-tests and
    # must not wedge the socket behind it (RUX-B-03).
    conn.spawn_background(send_capability_check_result(conn, req_id, msg.payload.get("method")))

async def _handle_capability_check_get_history(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    status_sum = engine_state.scheduler_manager.get_status_summary()
    history = engine_state.scheduler_manager.get_history()
    await conn.ws.send_text(Event(
        type=EventType.CAPABILITY_CHECK_HISTORY_RESPONSE,
        request_id=req_id,
        payload={"summary": status_sum, "history": history},
    ).to_json())

async def _handle_environment_diagnostic_run_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # The probes are synchronous (subprocess + urllib,
    # ~7 s of timeouts on a dead network): run them on a
    # worker thread so the event loop — and /health with
    # it — never freezes behind them (RUX-B-05).
    diag_logs = await asyncio.to_thread(run_environment_diagnostics)
    # Determine overall summary status based on logs
    failed_count = sum(1 for log in diag_logs if log["status"] == "FAILED")
    await conn.ws.send_text(Event(
        type=EventType.ENVIRONMENT_DIAGNOSTIC_RESPONSE,
        request_id=req_id,
        payload={
            "summary": {"failed_count": failed_count},
            "history": diag_logs,
        },
    ).to_json())

async def _handle_refresh_models_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Off the message loop (see send_refresh_models_result):
    # a refresh can take ~100 s of discovery and must not
    # wedge the socket behind it (RUX-B-01).
    conn.spawn_background(send_refresh_models_result(conn, req_id, dict(msg.payload)))

# -----------------------------------------------------------
# Tools, MCP, Plugins, Memory Events
# -----------------------------------------------------------


async def _handle_tool_list_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    from aether_engine.langgraph.nodes.supervisor import (
        SUPERVISOR_MEMORY_TOOLS,
        SUPERVISOR_SCREEN_TOOLS,
    )
    from aether_engine.mcp.registry import GLOBAL_MCP_REGISTRY
    from aether_engine.tools.policy import (
        KNOWN_NON_REGISTRY_TOOLS,
        resolve_policy_by_name,
    )
    from aether_engine.tools.registry import ToolRisk
                
    tool_registry = build_tool_registry(
        mcp_tools=GLOBAL_MCP_REGISTRY.get_mcp_tools(risk_overrides=engine_state.user_config.mcp_tool_risk_overrides),
    )

    def enrich_tool(d, name, risk, _registry=tool_registry):
        user_val = engine_state.user_config.tool_policies.get(name)
        # E2-F07: resolve through the SAME inputs enforcement
        # uses — the real registry (so a tool's registered
        # risk, not the EXECUTE fallback, drives the verdict)
        # and the live approval mode. Without them the list
        # reported "ask" for tools that actually run "allow".
        effective = resolve_policy_by_name(
            name,
            engine_state.user_config.tool_policies,
            _registry,
            approval_mode=engine_state.user_config.approval_mode,
        )
        d["risk"] = risk.value if isinstance(risk, ToolRisk) else risk
        d["effective_policy"] = effective
        d["policy_is_default"] = user_val is None
        d["always_allow_permitted"] = (risk == ToolRisk.READ_ONLY)
        d["policy"] = user_val or "Require Approval" # UI currently expects the raw setting in 'policy' too
        return d

    all_tools = tool_registry.get_tools()
    defs = []
    for t in all_tools:
        d = t.to_definition()
        defs.append(enrich_tool(d, t.name, t.risk))

    # Add Supervisor Memory Tools — and (S19) the supervisor's
    # desktop-observe tool, listed unconditionally like the
    # memory tools: its per-tool policy (incl. Deny) is managed
    # here even though the tool is only OFFERED to the model
    # when Computer Use + Observe are enabled.
    for mt in SUPERVISOR_MEMORY_TOOLS + SUPERVISOR_SCREEN_TOOLS:
        d = mt.copy()
        name = d["function"]["name"]
        risk = KNOWN_NON_REGISTRY_TOOLS.get(name, ToolRisk.EXECUTE)
        defs.append(enrich_tool(d, name, risk))

    await conn.ws.send_text(Event(
        type=EventType.TOOL_LIST_RESPONSE,
        request_id=req_id,
        payload={"tools": defs, "approval_mode": engine_state.user_config.approval_mode},
    ).to_json())

async def _handle_agent_list_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    agents = [
        {"id": "supervisor", "name": "Supervisor", "description": "Routes tasks and queries memory."},
        {"id": "planner", "name": "Planner Agent", "description": "Breaks goals into structured steps."},
        {"id": "coder", "name": "Coder Agent", "description": "Writes, tests, and debugs code."},
        {"id": "researcher", "name": "Researcher Agent", "description": "Gathers information and synthesizes findings."}
    ]
                
    for agent in agents:
        agent["model"] = engine_state.user_config.agent_models.get(agent["id"], "inherit")
        agent["tools"] = engine_state.user_config.agent_tools.get(agent["id"])
                
    await conn.ws.send_text(Event(
        type=EventType.AGENT_LIST_RESPONSE,
        request_id=req_id,
        payload={"agents": agents},
    ).to_json())

async def _handle_agent_update_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    agent_id = msg.payload.get("agent_id")
    model = msg.payload.get("model")
    tools = msg.payload.get("tools")

    # S1-M9: validate before persisting anything; the
    # update is atomic and success is only reported when
    # something was actually saved.
    _known_agent_ids = {"supervisor", "planner", "coder", "researcher"}
    _error: str | None = None
    if not agent_id:
        _error = "missing agent_id"
    elif agent_id not in _known_agent_ids:
        _error = f"unknown agent_id: {agent_id}"
    elif model is None and tools is None:
        _error = "nothing to update"
    elif model is not None and model != "inherit":
        if not isinstance(model, str) or ":" not in model:
            _error = f"invalid model format: {model!r} (expected 'provider:model' or 'inherit')"
        else:
            _provider, _model_id = model.split(":", 1)
            if not _provider or not _model_id:
                _error = f"invalid model format: {model!r} (expected 'provider:model' or 'inherit')"
            else:
                _known_providers: set[str] = set(
                    engine_state.configured_providers or []
                )
                _known_providers.update(
                    (engine_state.user_config.provider_models or {}).keys()
                )
                try:
                    _known_providers.update(
                        entry.provider
                        for entry in engine_state.model_registry.list_all_models()
                    )
                except Exception:  # noqa: BLE001, S110 — best-effort enrichment: a registry listing failure must not block validation; config-known providers are still checked
                    pass
                if _provider not in _known_providers:
                    _error = f"unknown provider: {_provider}"

    if _error is not None:
        await conn.ws.send_text(Event(
            type=EventType.AGENT_UPDATE_RESPONSE,
            request_id=req_id,
            payload={"success": False, "agent_id": agent_id, "error": _error},
        ).to_json())
    else:
        if model is not None:
            engine_state.user_config.agent_models[agent_id] = model
            # P1e: keep the delegation-settings row for this agent
            # in step with the pin on the live object (the load
            # migration folds agent_models into the rows; this is
            # the same fold for a write that happens after load).
            from aether_engine.delegation_settings import (
                sync_agent_model_row,
            )

            sync_agent_model_row(
                engine_state.user_config, agent_id, model)
        if tools is not None:
            engine_state.user_config.agent_tools[agent_id] = tools
        # E1-10: a save failure is reported in the same
        # vocabulary, not raised out of the handler.
        save_err = _try_save_config()
        if save_err is not None:
            await conn.ws.send_text(Event(
                type=EventType.AGENT_UPDATE_RESPONSE,
                request_id=req_id,
                payload={"success": False, "agent_id": agent_id,
                         "error": f"Agent update could not be saved: {save_err}"},
            ).to_json())
        else:
            await conn.ws.send_text(Event(
                type=EventType.AGENT_UPDATE_RESPONSE,
                request_id=req_id,
                payload={"success": True, "agent_id": agent_id},
            ).to_json())

async def _handle_tool_policy_set_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    tool_name = msg.payload.get("tool_name")
    policy = msg.payload.get("policy")
    response_payload = {"success": True, "tool_name": tool_name, "policy": policy}

    if tool_name and policy:
        from aether_engine.mcp.registry import GLOBAL_MCP_REGISTRY
        from aether_engine.tools.policy import tool_risk_by_name
        from aether_engine.tools.registry import ToolRisk

        risk = ToolRisk.EXECUTE
        try:
            # E2-F07: gate on the tool's risk in the FULL
            # registry — the legacy-factory reconstruction
            # this replaces knew nothing of the filesystem,
            # codebase, browser or MCP tools, so safe
            # registered tools (read_file, project_tree,
            # READ_ONLY MCP tools) hit the EXECUTE default
            # and "Always Allow" was wrongly downgraded.
            setter_registry = build_tool_registry(
                mcp_tools=GLOBAL_MCP_REGISTRY.get_mcp_tools(risk_overrides=engine_state.user_config.mcp_tool_risk_overrides),
            )
            risk = tool_risk_by_name(tool_name, setter_registry)
        except Exception:  # best-effort risk lookup: on failure the fail-closed EXECUTE default above stands
            logger.debug(f"Tool risk lookup failed for {tool_name}", exc_info=True)

        if policy == "Always Allow" and risk != ToolRisk.READ_ONLY:
            policy = "Require Approval"
            response_payload["policy"] = policy
            response_payload["message"] = "Always Allow is not permitted for risky tools. Downgraded to Require Approval."
            # E1-12: through the engine's own audit
            # logger — a fresh AuditLogger() wrote to
            # the default path, splitting the record
            # from this session's.
            engine_state.audit_logger.log_event("TOOL_POLICY_IGNORED", {"tool_name": tool_name, "requested": "Always Allow", "reason": "Risky tool"})

        engine_state.user_config.tool_policies[tool_name] = policy
        # E1-10: a save failure flips the verdict rather
        # than raising out of the handler.
        save_err = _try_save_config()
        if save_err is not None:
            response_payload["success"] = False
            response_payload["error"] = f"Tool policy could not be saved: {save_err}"
    else:
        # E1-12: nothing was (or could be) saved, so the
        # pre-built success:true above was a lie — the
        # refusal is reported honestly instead.
        response_payload["success"] = False
        response_payload["error"] = "tool_name and policy are required"

    await conn.ws.send_text(Event(
        type=EventType.TOOL_POLICY_SET_RESPONSE,
        request_id=req_id,
        payload=response_payload,
    ).to_json())

async def _handle_approval_mode_set_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Epic 2, E2-08: approval mode selector in Settings.
    mode = msg.payload.get("mode")
    success = mode in ("ask", "write")
    mode_error: str | None = None
    if success:
        engine_state.user_config.approval_mode = mode
        # E1-10: a save failure flips success rather
        # than raising out of the handler.
        save_err = _try_save_config()
        if save_err is not None:
            success = False
            mode_error = f"Approval mode could not be saved: {save_err}"
        else:
            engine_state.audit_logger.log_event("APPROVAL_MODE_SET", {"mode": mode})
    approval_payload: dict[str, Any] = {
        "success": success,
        "mode": engine_state.user_config.approval_mode}
    if mode_error is not None:
        approval_payload["error"] = mode_error
    await conn.ws.send_text(Event(
        type=EventType.APPROVAL_MODE_SET_RESPONSE,
        request_id=req_id,
        payload=approval_payload,
    ).to_json())

async def _handle_mcp_server_list_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    from aether_engine.mcp.registry import GLOBAL_MCP_REGISTRY
    servers = GLOBAL_MCP_REGISTRY.list_servers_with_status()
    await conn.ws.send_text(Event(
        type=EventType.MCP_SERVER_LIST_RESPONSE,
        request_id=req_id,
        payload={
            "servers": servers,
            "config_error": getattr(GLOBAL_MCP_REGISTRY, "config_load_error", None),
        },
    ).to_json())

async def _handle_config_get_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    await conn.ws.send_text(Event(
        type=EventType.CONFIG_GET_RESPONSE,
        request_id=req_id,
        payload={"config": engine_state.user_config.to_dict()},
    ).to_json())

async def _handle_config_update_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    config_data = msg.payload.get("config", {})
    if not isinstance(config_data, dict):
        # E1-01: a non-object config (a string passes the
        # "disable_fallbacks" membership test below as a
        # substring, then TypeErrors on indexing) is
        # refused with an answer, in the same vocabulary
        # as the E6-F05 refusal — nothing applied, nothing
        # saved, session alive.
        await conn.ws.send_text(Event(
            type=EventType.CONFIG_UPDATE_RESPONSE,
            request_id=req_id,
            payload={"success": False, "error": "Config update must be a JSON object of settings."},
        ).to_json())
        return
    # E6-F05: the fields handed over by Epics 1 and 2
    # (spawn policy, prewalk, output schemas, compaction
    # thresholds, path rules) are validated strictly —
    # a malformed write is refused with an error reply
    # and NOTHING in the update is applied or saved,
    # rather than silently dropped or coerced the way
    # the tolerant config-file loader treats bad values.
    from aether_engine.config_updates import (
        HANDED_OFF_FIELDS,
        validate_handed_off_updates,
    )
    handed_off_error = None
    handed_off_values: dict = {}
    if isinstance(config_data, dict) and any(
            field in config_data for field in HANDED_OFF_FIELDS):
        try:
            handed_off_values = validate_handed_off_updates(config_data)
        except ValueError as exc:
            handed_off_error = str(exc)
    if handed_off_error is not None:
        await conn.ws.send_text(Event(
            type=EventType.CONFIG_UPDATE_RESPONSE,
            request_id=req_id,
            payload={"success": False, "error": handed_off_error},
        ).to_json())
        return
    # Master build S9: the availability half of the
    # memory_backend contract. The NAME is already validated
    # strictly by the F05 handed-off validator above (E4-F12:
    # unknown names are refused there, and the cleaned value
    # is applied from handed_off_values below). What F05
    # cannot know is whether the named backend can run HERE:
    # the Settings picker lists all four backends INCLUDING
    # unavailable ones, so a valid-but-unavailable selection
    # is refused here with the probe's own reason — the same
    # words the picker shows — and NOTHING is applied or
    # saved. Saving it would leave a config whose every
    # memory call degrades loudly (facade.backend_or_none)
    # until the user fixed the file by hand. Probing never
    # constructs the backend (memory/backend_status.py).
    if "memory_backend" in config_data:
        from aether_engine.memory.backend_status import (
            validate_memory_backend_switch,
        )

        backend_error = validate_memory_backend_switch(
            config_data["memory_backend"])
        if backend_error is not None:
            await conn.ws.send_text(Event(
                type=EventType.CONFIG_UPDATE_RESPONSE,
                request_id=req_id,
                payload={"success": False, "error": backend_error},
            ).to_json())
            return
    # Master build S20: the first-enable acknowledgement rule
    # for desktop Control — the S9 pattern (a cross-field check
    # the stateless F05 validator cannot make). If the RESULTING
    # config would have Control on without the one-time
    # acknowledgement, the update is refused whole, with the
    # reason, and nothing is applied or saved. The engine gate
    # (desktop.control_permitted) independently requires the ack,
    # so even a hand-edited config cannot actuate unacknowledged;
    # this refusal keeps the Settings surface honest about it
    # instead of storing a Control switch that does nothing.
    if "computer_use_control" in config_data or "computer_use_control_ack" in config_data:
        prospective_control = handed_off_values.get(
            "computer_use_control",
            engine_state.user_config.computer_use_control)
        prospective_ack = handed_off_values.get(
            "computer_use_control_ack",
            engine_state.user_config.computer_use_control_ack)
        if prospective_control and not prospective_ack:
            await conn.ws.send_text(Event(
                type=EventType.CONFIG_UPDATE_RESPONSE,
                request_id=req_id,
                payload={
                    "success": False,
                    "error": (
                        "computer_use_control: desktop control "
                        "requires the one-time acknowledgement "
                        "first (Settings → Computer Use) — control "
                        "was not enabled."),
                },
            ).to_json())
            return
    # E6-F07: snapshot before mutating — if the save
    # below fails, live state rolls back to exactly this
    # and the failure is answered, never swallowed. (The
    # F05 refusal above runs before this point and applies
    # nothing, so it needs no snapshot.)
    previous_config = copy.deepcopy(engine_state.user_config)
    for field, value in handed_off_values.items():
        setattr(engine_state.user_config, field, value)
    if "disable_fallbacks" in config_data:
        engine_state.user_config.disable_fallbacks = config_data["disable_fallbacks"]
    if "onboarding_completed" in config_data:
        engine_state.user_config.onboarding_completed = bool(config_data["onboarding_completed"])
    if "fallback_chain" in config_data:
        engine_state.user_config.fallback_chain = config_data["fallback_chain"]
    if "vad_threshold" in config_data:
        try:
            engine_state.user_config.vad_threshold = float(config_data["vad_threshold"])
        except (TypeError, ValueError):
            pass
    if "silence_duration_ms" in config_data:
        try:
            engine_state.user_config.silence_duration_ms = int(config_data["silence_duration_ms"])
        except (TypeError, ValueError):
            pass
    if "smart_turn_sensitivity" in config_data:
        try:
            engine_state.user_config.smart_turn_sensitivity = min(
                1.0, max(0.0, float(config_data["smart_turn_sensitivity"])))
        except (TypeError, ValueError):
            pass
    # FUP-1: only the two literals apply; anything else
    # is ignored, matching the tuning fields above. The
    # value is read live per tool call (execute_tool
    # passes user_config.approval_binding_mode into
    # registry.execute), so this takes effect on the
    # next call — no restart, no session rebuild.
    if (
        "approval_binding_mode" in config_data
        and config_data["approval_binding_mode"] in ("rebind", "carry")
    ):
        engine_state.user_config.approval_binding_mode = config_data["approval_binding_mode"]
    if "snip_hotkey" in config_data:
        engine_state.user_config.snip_hotkey = str(config_data["snip_hotkey"])
    try:
        save_config(engine_state.user_config)
    except OSError as exc:
        # E6-F07: a failed save is a non-event — roll the
        # live state back and answer in the same
        # {success: False, error} vocabulary as the F05
        # refusal. Uncaught, this OSError killed the
        # handler and the client waited forever.
        engine_state.user_config = previous_config
        await conn.ws.send_text(Event(
            type=EventType.CONFIG_UPDATE_RESPONSE,
            request_id=req_id,
            payload={
                "success": False,
                "error": f"Settings could not be saved: {exc}",
            },
        ).to_json())
        return
    if "memory_backend" in config_data:
        engine_state.audit_logger.log_event("MEMORY_BACKEND_SET", {
            "backend": engine_state.user_config.memory_backend})
    await conn.ws.send_text(Event(
        type=EventType.CONFIG_UPDATE_RESPONSE,
        request_id=req_id,
        payload={"success": True, "config": engine_state.user_config.to_dict()},
    ).to_json())

async def _handle_config_reset_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Epic 6, E6-06: reset-to-defaults that really resets —
    # the user config is replaced with a fresh default
    # UserConfig and saved. API keys live in the secret
    # store (not the config) and files on disk are never
    # touched; the UI confirm says exactly that.
    # E6-F07: keep the pre-reset config until the save is
    # durable — a failed save restores it and is answered
    # with {success: False, error}; nothing is reset.
    previous_config = engine_state.user_config
    engine_state.user_config = UserConfig()
    try:
        save_config(engine_state.user_config)
    except OSError as exc:
        engine_state.user_config = previous_config
        await conn.ws.send_text(Event(
            type=EventType.CONFIG_RESET_RESPONSE,
            request_id=req_id,
            payload={
                "success": False,
                "error": f"Settings reset could not be saved: {exc}",
            },
        ).to_json())
        return
    # E6-F04: the live fields that mirror the config
    # (active provider/model, configured providers, skill
    # roots) are independent state — re-derive them from
    # the reset config exactly as initialize() does, or
    # HELLO and the skill catalogue keep serving the
    # pre-reset world until a restart.
    engine_state.resync_active_state()
    engine_state.audit_logger.log_event("CONFIG_RESET", {})
    await conn.ws.send_text(Event(
        type=EventType.CONFIG_RESET_RESPONSE,
        request_id=req_id,
        payload={"success": True, "config": engine_state.user_config.to_dict()},
    ).to_json())

async def _handle_auxiliary_model_set_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Phase 9, B1: per-task-class auxiliary model override. The Settings
    # UI sends {task, provider, model}; model "auto" clears the override.
    # Phase 10.3, B1: only task classes in AUXILIARY_TASK_CLASSES are
    # accepted. The other six (web, compression, skills, approval, mcp,
    # title) have no engine call site, so accepting them would be a
    # placebo — success:true for an override nothing ever reads.
    # Stale entries for those tasks (e.g. from a config file written
    # before Phase 10.3) are pruned here so the echoed dict never
    # advertises an override the engine will not honor.
    task = msg.payload.get("task", "")
    provider = msg.payload.get("provider", "")
    model = msg.payload.get("model", "auto")
    ok, error = True, ""
    if task not in AUXILIARY_TASK_CLASSES:
        ok, error = False, f"Unknown task class '{task}'."
    elif model != "auto":
        if not provider or not model:
            ok, error = False, "Provider and model are required (or 'auto')."
        elif provider not in (engine_state.configured_providers or []):
            ok, error = False, f"Provider '{provider}' is not configured."
    if ok:
        if model == "auto":
            engine_state.user_config.auxiliary_models.pop(task, None)
        else:
            engine_state.user_config.auxiliary_models[task] = f"{provider}:::{model}"
        # E1-10: a save failure flips ok/error rather
        # than raising out of the handler.
        save_err = _try_save_config()
        if save_err is not None:
            ok, error = False, f"Settings could not be saved: {save_err}"
        else:
            engine_state.audit_logger.log_event("AUXILIARY_MODEL_SET", {
                "task": task, "provider": provider, "model": model,
            })
    stale = [t for t in engine_state.user_config.auxiliary_models
             if t not in AUXILIARY_TASK_CLASSES]
    if stale:
        for t in stale:
            engine_state.user_config.auxiliary_models.pop(t, None)
        save_err = _try_save_config()
        if save_err is not None:
            ok, error = False, f"Settings could not be saved: {save_err}"
        else:
            engine_state.audit_logger.log_event("AUXILIARY_MODEL_PRUNED", {
                "tasks": stale,
                "reason": "task class has no engine call site",
            })
    await conn.ws.send_text(Event(
        type=EventType.AUXILIARY_MODEL_SET_RESPONSE,
        request_id=req_id,
        payload={
            "success": ok,
            "error": error,
            "auxiliary_models": dict(engine_state.user_config.auxiliary_models),
        },
    ).to_json())

async def _handle_reasoning_effort_set_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Phase 9, B2: persist the user's reasoning-effort preference so the
    # engine (not just localStorage) can act on it.
    effort = msg.payload.get("effort", "")
    ok, error = True, ""
    if effort not in ("standard", "medium", "high"):
        ok, error = False, f"Invalid reasoning effort '{effort}'."
    if ok:
        engine_state.user_config.reasoning_effort = effort
        # E1-10: a save failure flips ok/error rather
        # than raising out of the handler.
        save_err = _try_save_config()
        if save_err is not None:
            ok, error = False, f"Settings could not be saved: {save_err}"
        else:
            engine_state.audit_logger.log_event("REASONING_EFFORT_SET", {"effort": effort})
    await conn.ws.send_text(Event(
        type=EventType.REASONING_EFFORT_SET_RESPONSE,
        request_id=req_id,
        payload={"success": ok, "error": error, "effort": engine_state.user_config.reasoning_effort},
    ).to_json())

async def _handle_mcp_server_add_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    from aether_engine.mcp.registry import (
        GLOBAL_MCP_REGISTRY,
        MCPConfigError,
        redact_mcp_config_for_ui,
    )
    name = msg.payload.get("name")
    config = msg.payload.get("config", {})
    ok, error = True, ""
    if name and config:
        try:
            GLOBAL_MCP_REGISTRY.add_server(name, config)
        except (MCPConfigError, OSError) as e:
            # E3-F04: a storage OSError from the config
            # save must answer success:false like a
            # validation error — uncaught, it ended the
            # websocket session with no response at all.
            ok, error = False, str(e)
    else:
        ok, error = False, "Server name and config are required."
    await conn.ws.send_text(Event(
        type=EventType.MCP_SERVER_ADD_RESPONSE,
        request_id=req_id,
        # S1: never echo header/env values back to the UI.
        payload={"success": ok, "name": name, "config": redact_mcp_config_for_ui(config), "error": error},
    ).to_json())

async def _handle_mcp_server_remove_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Off the message loop (see send_mcp_server_remove_result):
    # removal closes any live session at the connect
    # timeout and must not wedge the socket (RUX-B-04).
    conn.spawn_background(send_mcp_server_remove_result(conn, 
        req_id, msg.payload.get("name")))

async def _handle_mcp_server_update_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Off the message loop (see send_mcp_server_update_result):
    # disabling a server disconnects it at the connect
    # timeout and must not wedge the socket (RUX-B-04).
    conn.spawn_background(send_mcp_server_update_result(conn, 
        req_id, msg.payload.get("name"),
        msg.payload.get("auto_connect"), msg.payload.get("enabled")))

async def _handle_mcp_server_connect_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Off the message loop (see send_mcp_server_connect_result):
    # a connect runs at the server's connect timeout —
    # 30 s default, up to 300 s — and must not wedge the
    # socket behind it (RUX-B-04).
    conn.spawn_background(send_mcp_server_connect_result(conn, 
        req_id, msg.payload.get("name")))

async def _handle_mcp_server_disconnect_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Off the message loop (see
    # send_mcp_server_disconnect_result): closing a hung
    # session is bounded by the same timeouts and must
    # not wedge the socket behind it (RUX-B-04).
    conn.spawn_background(send_mcp_server_disconnect_result(conn, 
        req_id, msg.payload.get("name")))

async def _handle_mcp_server_test_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Off the message loop (see send_mcp_server_test_result):
    # a test opens a throwaway session at the connect
    # timeout and must not wedge the socket (RUX-B-04).
    conn.spawn_background(send_mcp_server_test_result(conn, 
        req_id, msg.payload.get("name")))

async def _handle_mcp_resource_list_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Off the message loop (see send_mcp_resource_list_result):
    # the list runs against the live session at its read
    # timeout and must not wedge the socket (RUX-B-04).
    conn.spawn_background(send_mcp_resource_list_result(conn, 
        req_id, msg.payload.get("name")))

async def _handle_mcp_resource_read_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Off the message loop (see send_mcp_resource_read_result):
    # the read runs against the live session at its read
    # timeout and must not wedge the socket (RUX-B-04).
    conn.spawn_background(send_mcp_resource_read_result(conn, 
        req_id, msg.payload.get("name"), msg.payload.get("uri"),
        msg.payload.get("session_id")))

async def _handle_mcp_prompt_list_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Off the message loop (see send_mcp_prompt_list_result):
    # the list runs against the live session at its read
    # timeout and must not wedge the socket (RUX-B-04).
    conn.spawn_background(send_mcp_prompt_list_result(conn, 
        req_id, msg.payload.get("name")))

async def _handle_mcp_prompt_run_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Issue 007: run a prompt template and insert its messages
    # into the current chat (same pattern as resources in
    # 006, but with the prompt's own roles). Never auto-run:
    # only an explicit Run / slash command reaches here.
    # The arguments-type rejection is a pure payload check
    # — instant, answered inline exactly as before; the run
    # itself is off the message loop (see
    # send_mcp_prompt_run_result): it awaits the server at
    # its read timeout and must not wedge the socket
    # (RUX-B-04).
    name = msg.payload.get("name")
    prompt_name = msg.payload.get("prompt")
    arguments = msg.payload.get("arguments")
    if arguments is not None and not isinstance(arguments, dict):
        await conn.ws.send_text(Event(
            type=EventType.MCP_PROMPT_RUN_RESPONSE,
            request_id=req_id,
            payload={
                "success": False, "name": name, "prompt": prompt_name,
                "messages": [], "session_id": msg.payload.get("session_id"),
                "error": "MCP prompt arguments must be an object of string values.",
            },
        ).to_json())
    else:
        conn.spawn_background(send_mcp_prompt_run_result(conn, 
            req_id, name, prompt_name, arguments,
            msg.payload.get("session_id")))

async def _handle_plugin_list_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    plugins_dict = engine_state.plugin_installer.get_all()
    # E7-F05: carry each plugin's canonical identity as a
    # top-level "name". The stored records keep the name
    # only under manifest.name and the registry keys ARE
    # that name (install() keys by manifest.name), so
    # serialising the bare values discarded the one field
    # the Settings panel sends back on toggle/uninstall —
    # both actions then missed the plugin entirely.
    # Additive: existing consumers read only the array and
    # each record's "enabled"/"manifest".
    plugins_list = [
        {**record, "name": key}
        for key, record in plugins_dict.items()
    ] if plugins_dict else []
    await conn.ws.send_text(Event(
        type=EventType.PLUGIN_LIST_RESPONSE,
        request_id=req_id,
        payload={"plugins": plugins_list},
    ).to_json())

async def _handle_plugin_install_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    source = msg.payload.get("source")
    try:
        # E1-08: prepare_install copies/inspects the
        # plugin source synchronously and scales with
        # plugin size — it runs on a worker thread, not
        # the receive loop.
        data = await asyncio.to_thread(
            engine_state.plugin_installer.prepare_install, source)
        # E1-06: sweep expired entries, then record this
        # one with its creation time + owning connection
        # (the stored VALUE stays the raw prepare
        # payload — it is sent to the client verbatim).
        prune_expired_plugin_installs()
        pending_plugin_installs[req_id] = data
        pending_plugin_install_created[req_id] = time.time()
        conn.own_plugin_install_ids.add(req_id)
        await conn.ws.send_text(Event(
            type=EventType.PLUGIN_APPROVAL_REQUEST,
            request_id=req_id,
            payload={
                "plugin_name": data["manifest"].get("name", "Unknown"),
                "plugin_details": data,
            },
        ).to_json())
    except Exception as e:  # noqa: BLE001 — handler boundary: any prepare failure is reported to the client in the response payload
        await conn.ws.send_text(Event(
            type=EventType.PLUGIN_INSTALL_RESPONSE,
            request_id=req_id,
            payload={"success": False, "error": str(e)},
        ).to_json())

async def _handle_plugin_approval_granted(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Off the message loop (see send_plugin_install_
    # result): the install copy is synchronous and
    # scales with plugin size (RUX-B-10). The pending
    # entry is still popped here, in the loop, so a
    # duplicate grant can never install twice.
    prune_expired_plugin_installs()
    data = pending_plugin_installs.pop(req_id, None)
    pending_plugin_install_created.pop(req_id, None)
    conn.own_plugin_install_ids.discard(req_id)
    if data:
        conn.spawn_background(send_plugin_install_result(conn, req_id, data))
    else:
        # E1-06: a grant naming an unknown or expired
        # install used to get NO reply at all — the UI
        # waited forever. It is answered like every
        # other failed install.
        await conn.ws.send_text(Event(
            type=EventType.PLUGIN_INSTALL_RESPONSE,
            request_id=req_id,
            payload={"success": False,
                     "error": "No pending plugin install for this approval (it may have expired)."},
        ).to_json())

async def _handle_plugin_approval_rejected(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    pending_plugin_installs.pop(req_id, None)
    pending_plugin_install_created.pop(req_id, None)
    conn.own_plugin_install_ids.discard(req_id)
    await conn.ws.send_text(Event(
        type=EventType.PLUGIN_INSTALL_RESPONSE,
        request_id=req_id,
        payload={"success": False, "error": "Install rejected by user."},
    ).to_json())

async def _handle_plugin_uninstall_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Off the message loop (see send_plugin_uninstall_
    # result): the MCP disconnects await at the server
    # timeout and the file work is synchronous (RUX-B-10).
    conn.spawn_background(send_plugin_uninstall_result(conn, 
        req_id, msg.payload.get("name")))

async def _handle_plugin_toggle_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Off the message loop (see send_plugin_toggle_
    # result): a disable awaits the MCP disconnects at
    # the server timeout before its synchronous file
    # work (RUX-B-10).
    conn.spawn_background(send_plugin_toggle_result(conn, 
        req_id, msg.payload.get("name"), msg.payload.get("enabled", True)))

async def _handle_memory_episodes_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Epic 4, E4-01/E4-04: through the facade; the payload
    # also carries embedding health for Diagnostics.
    # E1-11: answered from a background task — the
    # backend reads are synchronous and scale with
    # memory size (see send_memory_episodes_result).
    conn.spawn_background(send_memory_episodes_result(conn, 
        req_id, msg.payload.get("limit", 50)))

async def _handle_memory_episode_delete_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Epic 4, E4-F08: the acknowledgement is derived from
    # what the backend actually did — an unavailable
    # backend, a missing id, a False result or a raised
    # error is a failure, never a simulated success.
    backend = backend_or_none(engine_state.user_config, context="memory episode delete")
    task_id = msg.payload.get("task_id")
    success, error = False, None
    if backend is None:
        error = "The configured memory backend is unavailable (see engine log)."
    elif not task_id:
        error = "task_id is required."
    else:
        try:
            success = bool(backend.delete_episode(task_id))
        except Exception as e:  # noqa: BLE001 — handler boundary: a failed delete is reported to the client, never dressed up as success or allowed to kill the receive loop
            error = str(e)
    if error is not None:
        engine_state.audit_logger.log_event("MEMORY_MANAGEMENT_FAILED", {
            "operation": "episode_delete", "task_id": task_id, "error": error})
    episodes = await asyncio.to_thread(backend.list_episodes, limit=msg.payload.get("limit", 50)) if backend is not None and error is None else []
    payload = {"success": success, "task_id": task_id, "episodes": episodes}
    if error is not None:
        payload["error"] = error
    await conn.ws.send_text(Event(
        type=EventType.MEMORY_EPISODE_DELETE_RESPONSE,
        request_id=req_id,
        payload=payload,
    ).to_json())

async def _handle_memory_episodes_clear_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    backend = backend_or_none(engine_state.user_config, context="memory episodes clear")
    success, error = False, None
    if backend is None:
        error = "The configured memory backend is unavailable (see engine log)."
    else:
        try:
            success = bool(backend.clear_episodes())
        except Exception as e:  # noqa: BLE001 — handler boundary: a failed clear is reported to the client, never dressed up as success or allowed to kill the receive loop
            error = str(e)
    if error is not None:
        engine_state.audit_logger.log_event("MEMORY_MANAGEMENT_FAILED", {
            "operation": "episodes_clear", "error": error})
    episodes = await asyncio.to_thread(backend.list_episodes, limit=msg.payload.get("limit", 50)) if backend is not None and error is None else []
    payload = {"success": success, "episodes": episodes}
    if error is not None:
        payload["error"] = error
    await conn.ws.send_text(Event(
        type=EventType.MEMORY_EPISODES_CLEAR_RESPONSE,
        request_id=req_id,
        payload=payload,
    ).to_json())

async def _handle_memory_graph_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # E1-11: answered from a background task — the fact
    # list/search is a synchronous backend read (see
    # send_memory_graph_result).
    conn.spawn_background(send_memory_graph_result(conn, 
        req_id, msg.payload.get("query"),
        msg.payload.get("limit", 100),
        bool(msg.payload.get("include_history"))))

async def _handle_memory_fact_delete_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # E4-F08: derived acknowledgement, same rule as the
    # episode delete handler above.
    backend = backend_or_none(engine_state.user_config, context="memory fact delete")
    fact_id = msg.payload.get("fact_id")
    success, error = False, None
    if backend is None:
        error = "The configured memory backend is unavailable (see engine log)."
    elif not fact_id:
        error = "fact_id is required."
    else:
        try:
            success = bool(backend.delete_fact(fact_id))
        except Exception as e:  # noqa: BLE001 — handler boundary: a failed delete is reported to the client, never dressed up as success or allowed to kill the receive loop
            error = str(e)
    if error is not None:
        engine_state.audit_logger.log_event("MEMORY_MANAGEMENT_FAILED", {
            "operation": "fact_delete", "fact_id": fact_id, "error": error})
    facts = await asyncio.to_thread(backend.list_facts, limit=msg.payload.get("limit", 100)) if backend is not None and error is None else []
    payload = {"success": success, "fact_id": fact_id, "facts": facts}
    if error is not None:
        payload["error"] = error
    await conn.ws.send_text(Event(
        type=EventType.MEMORY_FACT_DELETE_RESPONSE,
        request_id=req_id,
        payload=payload,
    ).to_json())

async def _handle_memory_entity_delete_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # E4-F08: derived acknowledgement. Backends that
    # honestly cannot group by entity (Mem0, LangMem)
    # raise NotImplementedError — that is an explicit
    # failure reply here, not a dead socket.
    backend = backend_or_none(engine_state.user_config, context="memory entity delete")
    entity_name = msg.payload.get("entity_name")
    success, error = False, None
    if backend is None:
        error = "The configured memory backend is unavailable (see engine log)."
    elif not entity_name:
        error = "entity_name is required."
    else:
        try:
            success = bool(backend.delete_entity(entity_name))
        except Exception as e:  # noqa: BLE001 — handler boundary: a failed delete is reported to the client, never dressed up as success or allowed to kill the receive loop
            error = str(e)
    if error is not None:
        engine_state.audit_logger.log_event("MEMORY_MANAGEMENT_FAILED", {
            "operation": "entity_delete", "entity_name": entity_name, "error": error})
    facts = await asyncio.to_thread(backend.list_facts, limit=msg.payload.get("limit", 100)) if backend is not None and error is None else []
    payload = {"success": success, "entity_name": entity_name, "facts": facts}
    if error is not None:
        payload["error"] = error
    await conn.ws.send_text(Event(
        type=EventType.MEMORY_ENTITY_DELETE_RESPONSE,
        request_id=req_id,
        payload=payload,
    ).to_json())

async def _handle_memory_graph_clear_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    backend = backend_or_none(engine_state.user_config, context="memory graph clear")
    success, error = False, None
    if backend is None:
        error = "The configured memory backend is unavailable (see engine log)."
    else:
        try:
            success = bool(backend.clear_facts())
        except Exception as e:  # noqa: BLE001 — handler boundary: a failed clear is reported to the client, never dressed up as success or allowed to kill the receive loop
            error = str(e)
    if error is not None:
        engine_state.audit_logger.log_event("MEMORY_MANAGEMENT_FAILED", {
            "operation": "graph_clear", "error": error})
    facts = await asyncio.to_thread(backend.list_facts, limit=msg.payload.get("limit", 100)) if backend is not None and error is None else []
    payload = {"success": success, "facts": facts}
    if error is not None:
        payload["error"] = error
    await conn.ws.send_text(Event(
        type=EventType.MEMORY_GRAPH_CLEAR_RESPONSE,
        request_id=req_id,
        payload=payload,
    ).to_json())

async def _handle_memory_fact_update_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Epic 4, E4-03: edit one fact's text via the facade.
    # E1-05: guarded exactly like its sibling handlers —
    # this was the one memory-management handler whose
    # backend call could raise straight out of the
    # receive loop and kill the session.
    backend = backend_or_none(engine_state.user_config, context="memory fact update")
    fact_id = msg.payload.get("fact_id")
    success, error = False, None
    if backend is None:
        error = "The configured memory backend is unavailable (see engine log)."
    elif not fact_id:
        error = "fact_id is required."
    else:
        try:
            success = bool(backend.update_fact(fact_id, msg.payload.get("fact_text", "")))
        except Exception as e:  # noqa: BLE001 — handler boundary: a failed update is reported to the client, never dressed up as success or allowed to kill the receive loop
            error = str(e)
    if error is not None:
        engine_state.audit_logger.log_event("MEMORY_MANAGEMENT_FAILED", {
            "operation": "fact_update", "fact_id": fact_id, "error": error})
    facts = await asyncio.to_thread(
        backend.list_facts,
        limit=msg.payload.get("limit", 100),
        include_history=bool(msg.payload.get("include_history")),
    ) if backend is not None and error is None else []
    update_payload = {"success": success, "fact_id": fact_id, "facts": facts}
    if error is not None:
        update_payload["error"] = error
    await conn.ws.send_text(Event(
        type=EventType.MEMORY_FACT_UPDATE_RESPONSE,
        request_id=req_id,
        payload=update_payload,
    ).to_json())

async def _handle_memory_export_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Epic 4, E4-03: the full memory as JSON (facts incl.
    # superseded history + episodes) for the UI to save.
    # Off the message loop (see send_memory_export_result):
    # the export is synchronous and scales with memory
    # size, so it runs on a worker thread (RUX-B-08).
    conn.spawn_background(send_memory_export_result(conn, req_id))

async def _handle_memory_backends_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Master build S9: the Settings picker's status report —
    # all four backends with probed states and reasons
    # (memory/backend_status.py; probing never constructs a
    # backend, so this is cheap and answered inline).
    from aether_engine.memory.backend_status import memory_backend_statuses

    report = memory_backend_statuses(engine_state.user_config)
    await conn.ws.send_text(Event(
        type=EventType.MEMORY_BACKENDS_RESPONSE,
        request_id=req_id,
        payload=report,
    ).to_json())

async def _handle_machine_capability_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Master build S10: the Settings "This machine" card —
    # the hardware probe + capability matrix
    # (machine_capability.py). Unlike the S9 report this one
    # runs subprocesses and network connects (strictly
    # timeout-bounded, a few seconds worst case), so it runs
    # on a worker thread, never on the message loop; the
    # probe itself is on-demand only, cached behind a TTL,
    # and never runs on the startup path.
    from aether_engine.machine_capability import machine_capability_report

    report = await asyncio.to_thread(
        machine_capability_report, bool(msg.payload.get("force")))
    await conn.ws.send_text(Event(
        type=EventType.MACHINE_CAPABILITY_RESPONSE,
        request_id=req_id,
        payload=report,
    ).to_json())

async def _handle_agent_routing_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Master build S14: the Settings → Agent Orchestration
    # "Model routing" card — per-agent effective models
    # (langgraph/model_routing.effective_models_report). The
    # report reads the usage/outcome stores (SQLite), so it runs
    # on a worker thread like the S10 probe, never on the
    # message loop; it is computed on demand, per request.
    from aether_engine.langgraph.model_routing import effective_models_report

    report = await asyncio.to_thread(
        effective_models_report, engine_state.user_config)
    await conn.ws.send_text(Event(
        type=EventType.AGENT_ROUTING_RESPONSE,
        request_id=req_id,
        payload=report,
    ).to_json())

async def _handle_backup_create_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Epic 10, E10-03: snapshot ~/.aether into a zip under
    # the active workspace's AetherBackups folder.
    try:
        from aether_engine.backup import create_backup

        roots = engine_state.user_config.workspace_roots or [str(Path.home() / "AetherWorkspace")]
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = Path(roots[0]) / "AetherBackups" / f"aether-backup-{stamp}.zip"
        manifest = await asyncio.to_thread(
            create_backup, dest,
            # E10-F02: consent is the boolean True only —
            # bool("false") is True, so truthiness at this
            # untyped boundary would include secrets.db
            # on a string "false".
            include_secrets=msg.payload.get("include_secrets") is True,
        )
        engine_state.audit_logger.log_event("BACKUP_CREATED", {
            "path": str(dest), "files": len(manifest["files"]),
            "include_secrets": manifest["include_secrets"],
        })
        payload = {"success": True, "path": str(dest), "files": len(manifest["files"]),
                   "include_secrets": manifest["include_secrets"]}
    except Exception as e:  # noqa: BLE001 — backup failure must surface, not crash the handler
        payload = {"success": False, "error": str(e)}
    await conn.ws.send_text(Event(
        type=EventType.BACKUP_CREATE_RESPONSE,
        request_id=req_id,
        payload=payload,
    ).to_json())

async def _handle_backup_restore_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Epic 10, E10-03: validate-then-write restore. The
    # running engine keeps in-memory state, so the
    # response is explicit that a restart applies it.
    try:
        from aether_engine.backup import restore_backup

        result = await asyncio.to_thread(
            restore_backup, Path(str(msg.payload.get("path", "")))
        )
        engine_state.audit_logger.log_event("BACKUP_RESTORED", {
            "files": result["files"],
        })
        payload = {"success": True, "files": result["files"],
                   "restart_required": True}
    except Exception as e:  # noqa: BLE001 — restore failure must surface, not crash the handler
        payload = {"success": False, "error": str(e)}
    await conn.ws.send_text(Event(
        type=EventType.BACKUP_RESTORE_RESPONSE,
        request_id=req_id,
        payload=payload,
    ).to_json())

async def _handle_diagnostics_export_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Epic 10, E10-04: redacted bundle — config scrubbed by
    # the audit redactor, memory as counts only.
    # Off the message loop (see send_diagnostics_export_
    # result): the bundle build is synchronous bulk work,
    # so it runs on a worker thread (RUX-B-08).
    conn.spawn_background(send_diagnostics_export_result(conn, req_id))

async def _handle_version_check_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Epic 10, E10-05: report-only update check against the
    # project's GitHub releases. Nothing downloads or
    # installs — the user decides.
    # Off the message loop (see send_version_check_result):
    # the check is live HTTP with a 10 s timeout (RUX-B-08).
    conn.spawn_background(send_version_check_result(conn, req_id))

async def _handle_skill_list_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    from aether_engine.skills.manager import GLOBAL_SKILL_MANAGER
    skills = [s.__dict__ for s in GLOBAL_SKILL_MANAGER.list_all()]
    await conn.ws.send_text(Event(
        type=EventType.SKILL_LIST_RESPONSE,
        request_id=req_id,
        payload={"skills": skills},
    ).to_json())

async def _handle_skill_toggle_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Epic 7, E7-03: enable/disable a skill. Disabled
    # skills leave the manager's index at once — the
    # agent's available set (list/match/slash/load) no
    # longer contains them; Settings still lists them
    # with enabled=False so they can be re-enabled.
    from aether_engine.skills.manager import GLOBAL_SKILL_MANAGER

    skill_name = (msg.payload.get("name") or "").strip()
    want_enabled = bool(msg.payload.get("enabled", True))
    known = any(s.name == skill_name for s in GLOBAL_SKILL_MANAGER.list_all())
    # E7-F04: a failed disable-state save must still get
    # a reply — the toggle is then a non-event (the
    # manager changed nothing) and the response reports
    # the skill's actual current state, never the
    # requested one.
    save_error = None
    if known:
        try:
            GLOBAL_SKILL_MANAGER.set_disabled(skill_name, not want_enabled)
        except Exception as e:  # noqa: BLE001 — the toggle must always reply; the session must survive
            save_error = str(e)
            logger.error(
                "Skill toggle for %r failed to persist: %s",
                skill_name, e)
    payload = {
        "success": known and save_error is None,
        "name": skill_name,
        "enabled": (
            GLOBAL_SKILL_MANAGER.is_enabled(skill_name)
            if known else False
        ),
        "skills": [s.__dict__ for s in GLOBAL_SKILL_MANAGER.list_all()],
    }
    if save_error is not None:
        payload["error"] = save_error
    await conn.ws.send_text(Event(
        type=EventType.SKILL_TOGGLE_RESPONSE,
        request_id=req_id,
        payload=payload,
    ).to_json())

async def _handle_slash_command_list_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    from aether_engine.skills.manager import GLOBAL_SKILL_MANAGER
    from aether_engine.skills.slash_registry import GLOBAL_SLASH_REGISTRY
    commands = [c.to_dict() for c in GLOBAL_SLASH_REGISTRY.get_all()]
                
    # Combine dynamic skill commands
    skill_commands = []
    for s in GLOBAL_SKILL_MANAGER.list_all():
        # E7-F11: list_all() is the management listing
        # (Settings needs disabled skills visible for
        # re-enable) — the advertised command list is an
        # invocation path, so disabled skills are not
        # offered here.
        if not s.enabled:
            continue
        for cmd_str in s.slash_commands:
            skill_commands.append({
                "command": cmd_str,
                "description": f"Trigger skill: {s.name} - {s.description}",
                "type": "skill_invoke",
                "target": s.name,
                "aliases": [],
                "flags": []
            })
                
    # De-duplicate by command name
    all_commands = {c["command"]: c for c in commands + skill_commands}
                
    await conn.ws.send_text(Event(
        type=EventType.SLASH_COMMAND_LIST_RESPONSE,
        request_id=req_id,
        payload={"commands": list(all_commands.values())},
    ).to_json())

async def _handle_workspace_list_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    roots = engine_state.user_config.workspace_roots or []
    # Per-workspace sessions, slice 3 (additive): each
    # row gains the count of sessions bound to it, and
    # the reply gains the unbound ("No workspace") count,
    # so the Projects view can render filing state — and
    # the Q7 removal confirmation its count — without a
    # second call.
    counts, unbound_count = await _workspace_session_counts()
    workspaces = []
    for idx, r in enumerate(roots):
        p = Path(r)
        workspaces.append({
            "path": r,
            "name": p.name,
            "is_indexed": False,  # No real indexing backend yet
            # Epic 6, E6-04: the first root is the active
            # workspace; `exists` drives the missing-folder
            # state in the Projects view.
            "is_active": idx == 0,
            "exists": p.is_dir(),
            "session_count": counts.get(r, 0),
        })
    await conn.ws.send_text(Event(
        type=EventType.WORKSPACE_LIST_RESPONSE,
        request_id=req_id,
        payload={"workspaces": workspaces,
                 "unbound_session_count": unbound_count},
    ).to_json())

async def _handle_workspace_add_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Per-workspace sessions, slice 2 (grill Q2/Q8): a
    # root's identity is its canonical form. The incoming
    # path is canonicalised BEFORE the duplicate check,
    # so a second spelling of a configured folder (a
    # trailing slash, a symlink, a dotted path) is refused
    # and the reply points at the existing entry instead
    # of adding a twin.
    # E1-01: the path rides an untrusted payload — a
    # non-string path coerces to empty (answered below
    # as "Path empty") instead of raising AttributeError
    # into the receive loop. Same guard in the REMOVE
    # and SET_ACTIVE branches below.
    _raw_ws_path = msg.payload.get("path")
    ws_path = _raw_ws_path.strip() if isinstance(_raw_ws_path, str) else ""
    roots = engine_state.user_config.workspace_roots
    existing = find_workspace_root(roots, ws_path)
    if ws_path and existing is None:
        canonical = canonical_workspace_root(ws_path)
        roots.append(canonical)
        # E1-10: a save failure is answered, not raised.
        save_err = _try_save_config()
        if save_err is not None:
            await conn.ws.send_text(Event(
                type=EventType.WORKSPACE_ADD_RESPONSE,
                request_id=req_id,
                payload={"success": False,
                         "error": f"Workspace could not be saved: {save_err}"},
            ).to_json())
        else:
            from aether_engine.skills.manager import GLOBAL_SKILL_MANAGER
            GLOBAL_SKILL_MANAGER.update_workspace_roots(roots)
            p = Path(canonical)
            await conn.ws.send_text(Event(
                type=EventType.WORKSPACE_ADD_RESPONSE,
                request_id=req_id,
                # E6-F11: the reply carries the same badge
                # fields WORKSPACE_LIST_RESPONSE computes
                # (first root is active; `exists` drives the
                # missing-folder state), so the Projects view
                # renders the new row correctly from the reply
                # itself instead of waiting for a LIST refetch.
                payload={"success": True, "workspace": {
                    "path": canonical, "name": p.name, "is_indexed": False,
                    "is_active": len(roots) == 1,
                    "exists": p.is_dir(),
                }},
            ).to_json())
    elif existing is not None:
        p = Path(existing)
        await conn.ws.send_text(Event(
            type=EventType.WORKSPACE_ADD_RESPONSE,
            request_id=req_id,
            # Q8 refusal: `exists` + the existing entry
            # (same badge shape as a LIST row) let the
            # Projects view highlight the real workspace
            # instead of dead-ending on an error string.
            payload={"success": False, "exists": True,
                     "error": "Path already exists",
                     "workspace": {
                         "path": existing, "name": p.name,
                         "is_indexed": False,
                         "is_active": roots.index(existing) == 0,
                         "exists": p.is_dir(),
                     }},
        ).to_json())
    else:
        await conn.ws.send_text(Event(
            type=EventType.WORKSPACE_ADD_RESPONSE,
            request_id=req_id,
            payload={"success": False, "error": "Path empty"},
        ).to_json())

async def _handle_workspace_remove_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Slice 2: removal addresses the canonical identity —
    # any spelling of the folder removes its one stored
    # entry, and the reply names the canonical root that
    # was actually removed (slice 3 unbinds that root's
    # sessions by this value).
    _raw_ws_path = msg.payload.get("path")
    ws_path = _raw_ws_path.strip() if isinstance(_raw_ws_path, str) else ""
    removed = remove_workspace_root(
        engine_state.user_config.workspace_roots, ws_path)
    if removed is not None:
        # E1-10: a save failure is answered, not raised.
        save_err = _try_save_config()
        if save_err is not None:
            await conn.ws.send_text(Event(
                type=EventType.WORKSPACE_REMOVE_RESPONSE,
                request_id=req_id,
                payload={"success": False,
                         "error": f"Workspace removal could not be saved: {save_err}"},
            ).to_json())
        else:
            from aether_engine.skills.manager import GLOBAL_SKILL_MANAGER
            GLOBAL_SKILL_MANAGER.update_workspace_roots(engine_state.user_config.workspace_roots)
            # Slice 3 (grill Q7): the removed root's sessions
            # fall back to unbound — removal never deletes a
            # session — and the reply reports how many were
            # unbound, keyed by the canonical root removed.
            unbound_sessions = await _unbind_sessions_bound_to(removed)
            await conn.ws.send_text(Event(
                type=EventType.WORKSPACE_REMOVE_RESPONSE,
                request_id=req_id,
                payload={"success": True, "path": removed,
                         "unbound_sessions": unbound_sessions},
            ).to_json())
    else:
        await conn.ws.send_text(Event(
            type=EventType.WORKSPACE_REMOVE_RESPONSE,
            request_id=req_id,
            payload={"success": False, "error": "Path not found"},
        ).to_json())

async def _handle_workspace_index_start_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Epic 8, E8-3.2: consent-first indexing (grill Q17).
    # Without confirm this is the consent preview: counts,
    # size, embedder and destination, nothing started and
    # nothing mutated. With confirm, consent is recorded,
    # indexing runs in the background with PROGRESS pushes,
    # and the watcher keeps the index fresh afterwards.
    from aether_engine.rag.prose_indexer import get_prose_indexer

    indexer = get_prose_indexer()
    ws_root = msg.payload.get("workspace_root", "")
    confirm = bool(msg.payload.get("confirm"))
    req_embedder = msg.payload.get("embedder")
    estimate = await asyncio.to_thread(indexer.estimate, ws_root)
    if confirm and ws_root:
        if req_embedder:
            await asyncio.to_thread(indexer.set_embedder, req_embedder)
        await asyncio.to_thread(indexer.grant_consent, ws_root)

        async def _run_index(root=ws_root, indexer=indexer, req_id=req_id):
            async def _progress(**kw):
                try:
                    await conn.ws.send_text(Event(
                        type=EventType.WORKSPACE_INDEX_PROGRESS,
                        request_id=req_id,
                        payload=kw,
                    ).to_json())
                except Exception:  # a dead socket must not kill indexing
                    logger.debug("Index progress push failed", exc_info=True)

            try:
                await indexer.index_workspace(root, progress_cb=_progress)
            except Exception:
                # Background index pass: logged, never
                # propagated into the socket loop.
                logger.exception("Workspace indexing failed for %s", root)
            indexer.start_watching(root)

        conn.spawn_background(_run_index())
    await conn.ws.send_text(Event(
        type=EventType.WORKSPACE_INDEX_START_RESPONSE,
        request_id=req_id,
        payload={
            "started": bool(confirm and ws_root),
            "estimated_files": estimate["estimated_files"],
            "estimated_bytes": estimate["estimated_bytes"],
            "embedder": indexer.embedder,
            "destination": str(indexer.store.db_path),
        },
    ).to_json())

async def _handle_workspace_index_status_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    from aether_engine.rag.prose_indexer import get_prose_indexer

    indexer = get_prose_indexer()
    status_payload = await asyncio.to_thread(
        indexer.status, msg.payload.get("workspace_root"))
    await conn.ws.send_text(Event(
        type=EventType.WORKSPACE_INDEX_STATUS_RESPONSE,
        request_id=req_id,
        payload=status_payload,
    ).to_json())

async def _handle_workspace_index_pause_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    from aether_engine.rag.prose_indexer import get_prose_indexer

    indexer = get_prose_indexer()
    pause = bool(msg.payload.get("pause", False))
    if pause:
        indexer.pause()
    else:
        indexer.resume()
    await conn.ws.send_text(Event(
        type=EventType.WORKSPACE_INDEX_PAUSE_RESPONSE,
        request_id=req_id,
        payload={"paused": indexer.paused},
    ).to_json())

async def _handle_workspace_index_config_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    from aether_engine.rag.prose_indexer import get_prose_indexer

    indexer = get_prose_indexer()
    req_embedder = msg.payload.get("embedder")
    old_embedder = indexer.embedder
    reindex_required = False
    index_save_err: str | None = None
    if req_embedder and req_embedder != old_embedder:
        reindex_required = True
        await asyncio.to_thread(indexer.set_embedder, req_embedder)
        # N02: the SAME switch invalidates episodic
        # vectors — they are stamped per model, and rows
        # from the old embedder are deleted here so none
        # stays recallable in the interim; the episodic
        # backfill rebuilds them under the new model.
        from aether_engine.memory.episodic import (
            reset_episodic_embeddings_for_embedder,
        )
        await asyncio.to_thread(
            reset_episodic_embeddings_for_embedder, req_embedder)
        if hasattr(engine_state, "user_config") and engine_state.user_config:
            engine_state.user_config.embedding_model = req_embedder
            # E1-10: a save failure flips ok, not the
            # connection.
            index_save_err = _try_save_config()
    index_payload: dict[str, Any] = {
        "ok": index_save_err is None,
        "reindex_required": reindex_required}
    if index_save_err is not None:
        index_payload["error"] = (
            f"Embedding model could not be saved: {index_save_err}")
    await conn.ws.send_text(Event(
        type=EventType.WORKSPACE_INDEX_CONFIG_RESPONSE,
        request_id=req_id,
        payload=index_payload,
    ).to_json())

async def _handle_workspace_set_active_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Epic 6, E6-04: the active workspace is the first
    # root (containment, skills, and task defaults all
    # key off roots[0]); activating = move to front.
    # Slice 2: the request addresses the canonical
    # identity — any spelling of the folder activates its
    # one stored (canonical) entry.
    _raw_ws_path = msg.payload.get("path")
    ws_path = _raw_ws_path.strip() if isinstance(_raw_ws_path, str) else ""
    roots = engine_state.user_config.workspace_roots
    existing = find_workspace_root(roots, ws_path)
    if existing is not None:
        roots.remove(existing)
        roots.insert(0, existing)
        # E1-10: a save failure is answered, not raised.
        save_err = _try_save_config()
        if save_err is not None:
            await conn.ws.send_text(Event(
                type=EventType.WORKSPACE_SET_ACTIVE_RESPONSE,
                request_id=req_id,
                payload={"success": False,
                         "error": f"Workspace change could not be saved: {save_err}"},
            ).to_json())
        else:
            from aether_engine.skills.manager import GLOBAL_SKILL_MANAGER
            GLOBAL_SKILL_MANAGER.update_workspace_roots(roots)
            await conn.ws.send_text(Event(
                type=EventType.WORKSPACE_SET_ACTIVE_RESPONSE,
                request_id=req_id,
                payload={"success": True, "path": existing},
            ).to_json())
    else:
        await conn.ws.send_text(Event(
            type=EventType.WORKSPACE_SET_ACTIVE_RESPONSE,
            request_id=req_id,
            payload={"success": False, "error": "Path not found"},
        ).to_json())

async def _handle_project_config_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # E6-F05 (the Epic 1 layered-config handoff): view and
    # edit the project layer — `.aether/config.json` in a
    # configured workspace — in exactly the shape
    # config_layers consumes it: the layer itself, never
    # the merged result. Roots are confined to
    # user_config.workspace_roots, defaulting to the
    # active (first) root.
    import json as _json

    from aether_engine.config_layers import (
        PROJECT_CONFIG_RELPATH,
        PROJECT_FORBIDDEN_KEYS,
    )
    is_update = msg.type == EventType.PROJECT_CONFIG_UPDATE_REQUEST
    response_type = (
        EventType.PROJECT_CONFIG_UPDATE_RESPONSE if is_update
        else EventType.PROJECT_CONFIG_GET_RESPONSE)
    roots = engine_state.user_config.workspace_roots or []
    requested_root = (msg.payload.get("workspace_root") or "").strip()
    project_root = requested_root or (roots[0] if roots else "")
    project_error = None
    if not roots:
        project_error = "No workspace is configured — add one in Projects first."
    elif project_root not in roots:
        project_error = f"Unknown workspace: {project_root}"
    if project_error is not None:
        await conn.ws.send_text(Event(
            type=response_type,
            request_id=req_id,
            payload={"success": False, "error": project_error},
        ).to_json())
        return
    layer_path = Path(project_root) / PROJECT_CONFIG_RELPATH
    if is_update:
        content = msg.payload.get("content", "")
        parsed_layer = None
        if not isinstance(content, str):
            project_error = "Project config content must be JSON text."
        else:
            try:
                parsed_layer = _json.loads(content)
            except ValueError as exc:
                project_error = f"Project config is not valid JSON: {exc}"
            else:
                if not isinstance(parsed_layer, dict):
                    project_error = "Project config must be a JSON object."
        if project_error is not None:
            await conn.ws.send_text(Event(
                type=response_type,
                request_id=req_id,
                payload={"success": False, "error": project_error},
            ).to_json())
            return
        # Write the validated dict (canonical form), via
        # a temp file + replace so a failed write can
        # never leave a truncated layer behind.
        layer_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_layer = layer_path.with_suffix(".tmp")
        tmp_layer.write_text(
            _json.dumps(parsed_layer, indent=2) + "\n", "utf-8")
        tmp_layer.replace(layer_path)
        engine_state.audit_logger.log_event(
            "PROJECT_CONFIG_UPDATED", {"workspace_root": project_root})
        await conn.ws.send_text(Event(
            type=response_type,
            request_id=req_id,
            payload={"success": True,
                     "workspace_root": project_root,
                     "config": parsed_layer},
        ).to_json())
    else:
        raw_text = ""
        parse_error = None
        parsed_layer = None
        exists = layer_path.is_file()
        if exists:
            raw_text = layer_path.read_text("utf-8", errors="replace")
            try:
                candidate = _json.loads(raw_text)
                if isinstance(candidate, dict):
                    parsed_layer = candidate
                else:
                    parse_error = "Project config is not a JSON object; the engine ignores it."
            except ValueError as exc:
                parse_error = f"Project config is not valid JSON ({exc}); the engine ignores it."
        await conn.ws.send_text(Event(
            type=response_type,
            request_id=req_id,
            payload={
                "success": True,
                "workspace_root": project_root,
                "exists": exists,
                "content": raw_text,
                "config": parsed_layer,
                "parse_error": parse_error,
                "forbidden_keys": sorted(PROJECT_FORBIDDEN_KEYS),
            },
        ).to_json())

# -----------------------------------------------------------
# Voice session (Epic 8, E8-5.1a)
# -----------------------------------------------------------


async def _handle_voice_session_start_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Off the receive loop (see send_voice_session_
    # start_result): opening the stages can load the
    # speech models and take seconds (RUX-B-09). The
    # start is chained with session stops (and other
    # starts) so their relative order is preserved.
    enqueue_voice_op(conn, send_voice_session_start_result(conn, 
        req_id, dict(msg.payload)))

async def _handle_voice_session_stop_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Chained with session starts (see enqueue_voice_op):
    # a stop sent while a start is still opening lands
    # after it, never before it (RUX-B-09).
    enqueue_voice_op(conn, send_voice_session_stop_result(conn, req_id))

async def _handle_voice_playback_progress(conn: WsConnection, msg: Event) -> None:
    # N09: the renderer's playback report for the live
    # session (the highest chunk_seq it has FINISHED
    # playing) — the input the pipeline's heard-word
    # accounting reads. Fire-and-forget by design: no
    # response; a report with no live session, or a
    # malformed one, is ignored by the pipeline's own
    # validation, never fatal to the socket.
    progress_pipeline = conn.voice_state.get("pipeline")
    if progress_pipeline is not None:
        progress_pipeline.note_playback_progress(
            msg.payload.get("played_seq"))

# -----------------------------------------------------------
# Voice RTC negotiation (F-12a): the renderer offers,
# the live session's transport answers; ICE trickles
# both ways. Every failure answers ok=False with the
# honest reason — a dead negotiation is never silent.
# -----------------------------------------------------------


async def _handle_voice_rtc_offer_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    rtc_pipeline = conn.voice_state.get("pipeline")
    rtc_answer = None
    rtc_error: str | None = None
    if rtc_pipeline is None:
        rtc_error = "voice unavailable: no active voice session"
    else:
        from aether_engine.voice import VoiceUnavailableError

        try:
            rtc_answer = await rtc_pipeline.handle_rtc_offer(
                msg.payload.get("sdp") or {})
        except VoiceUnavailableError as e:
            rtc_error = str(e)
        except Exception as e:  # noqa: BLE001 — negotiation failure must still produce a response
            rtc_error = (
                f"voice unavailable: RTC negotiation "
                f"failed ({e})")
    await conn.ws.send_text(Event(
        type=EventType.VOICE_RTC_OFFER_RESPONSE,
        request_id=req_id,
        payload={
            "ok": rtc_answer is not None,
            "session_id": msg.payload.get("session_id"),
            "answer": rtc_answer,
            "error": rtc_error,
        },
    ).to_json())

async def _handle_voice_rtc_ice_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    rtc_pipeline = conn.voice_state.get("pipeline")
    rtc_error = None
    if rtc_pipeline is None:
        rtc_error = "voice unavailable: no active voice session"
    else:
        from aether_engine.voice import VoiceUnavailableError

        try:
            await rtc_pipeline.add_rtc_ice_candidate(
                msg.payload.get("candidate"))
        except VoiceUnavailableError as e:
            rtc_error = str(e)
        except Exception as e:  # noqa: BLE001 — as above: always answer
            rtc_error = (
                f"voice unavailable: RTC negotiation "
                f"failed ({e})")
    await conn.ws.send_text(Event(
        type=EventType.VOICE_RTC_ICE_RESPONSE,
        request_id=req_id,
        payload={
            "ok": rtc_error is None,
            "session_id": msg.payload.get("session_id"),
            "error": rtc_error,
        },
    ).to_json())

# -----------------------------------------------------------
# Push-to-talk dictation + TTS read-aloud (Epic 8, E8-5.3)
# -----------------------------------------------------------


async def _handle_dictation_start_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    stt = dictation_stt(conn)
    reason = stt.unavailable_reason()
    if reason is not None:
        await conn.ws.send_text(Event(
            type=EventType.DICTATION_START_RESPONSE,
            request_id=req_id,
            payload={"ok": False, "error": f"voice unavailable: {reason}"},
        ).to_json())
    else:
        await conn.ws.send_text(Event(
            type=EventType.DICTATION_START_RESPONSE,
            request_id=req_id,
            payload={"ok": True, "error": None},
        ).to_json())

async def _handle_dictation_chunk_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # Off the receive loop, but chained in arrival
    # order (see enqueue_dictation): each chunk's
    # transcription can take seconds, and a later chunk
    # must never be applied before an earlier one
    # (RUX-B-09). The engine snapshot + availability
    # check stay here, at receipt, as before.
    _dictation_engine = dictation_stt(conn)
    enqueue_dictation(conn, send_dictation_chunk_result(conn, 
        req_id,
        _dictation_engine.unavailable_reason(),
        msg.payload.get("audio") or "",
        _coerce_sample_rate(msg.payload.get("sample_rate")),
        bool(msg.payload.get("is_final", False))))

async def _handle_dictation_stop_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    # The final transcription joins the same chain as
    # the chunks (see enqueue_dictation), so the stop
    # response can never overtake a chunk response
    # (RUX-B-09).
    enqueue_dictation(conn, send_dictation_stop_result(conn, 
        req_id,
        msg.payload.get("audio"),
        _coerce_sample_rate(msg.payload.get("sample_rate"))))

async def _handle_read_aloud_start_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    await stop_read_aloud(conn)
    tts = read_aloud_tts(conn)
    reason = tts.unavailable_reason()
    msg_id = msg.payload.get("message_id")
    text_to_speak = msg.payload.get("text") or ""
    voice_name = msg.payload.get("voice") or "af_heart"
    if reason is not None:
        await conn.ws.send_text(Event(
            type=EventType.READ_ALOUD_START_RESPONSE,
            request_id=req_id,
            payload={"ok": False, "message_id": msg_id, "error": f"voice unavailable: {reason}"},
        ).to_json())
    else:
        await conn.ws.send_text(Event(
            type=EventType.READ_ALOUD_START_RESPONSE,
            request_id=req_id,
            payload={"ok": True, "message_id": msg_id, "error": None},
        ).to_json())

        async def _run_read_aloud(mid: str | None, text: str, voice: str, tts_inst: Any, target_ws: WebSocket) -> None:
            try:
                # S4-M7: open the cached engine once for
                # the connection, not on every start.
                tts_inst = await open_read_aloud_tts(conn)
                async for chunk in tts_inst.synthesize(text, voice=voice):
                    b64_chunk = base64.b64encode(chunk).decode("ascii")
                    await target_ws.send_text(Event(
                        type=EventType.READ_ALOUD_CHUNK,
                        payload={"message_id": mid, "audio_chunk": b64_chunk, "is_final": False},
                    ).to_json())
                await target_ws.send_text(Event(
                    type=EventType.READ_ALOUD_CHUNK,
                    payload={"message_id": mid, "audio_chunk": None, "is_final": True},
                ).to_json())
            except asyncio.CancelledError:
                pass
            except Exception as e:  # noqa: BLE001
                logger.error(f"Read-aloud synthesis error: {e}")
                try:
                    await target_ws.send_text(Event(
                        type=EventType.READ_ALOUD_CHUNK,
                        payload={"message_id": mid, "audio_chunk": None, "is_final": True, "error": str(e)},
                    ).to_json())
                except Exception:
                    logger.debug("Failed to send error event", exc_info=True)

        conn.voice_state["read_aloud_id"] = msg_id
        conn.voice_state["read_aloud_task"] = asyncio.ensure_future(
            _run_read_aloud(msg_id, text_to_speak, voice_name, tts, conn.ws)
        )

async def _handle_read_aloud_stop_request(conn: WsConnection, msg: Event) -> None:
    req_id = msg.request_id
    msg_id = msg.payload.get("message_id")
    await stop_read_aloud(conn)
    await conn.ws.send_text(Event(
        type=EventType.READ_ALOUD_STOP_RESPONSE,
        request_id=req_id,
        payload={"ok": True, "message_id": msg_id},
    ).to_json())

_DISPATCH: dict[EventType, Callable[[WsConnection, Event], Awaitable[None]]] = {
    EventType.START_TASK: _handle_start_task,
    EventType.CANCEL_TASK: _handle_cancel_task,
    EventType.PING: _handle_ping,
    EventType.TOOL_APPROVAL_GRANTED: _handle_tool_approval_decision,
    EventType.TOOL_APPROVAL_REJECTED: _handle_tool_approval_decision,
    EventType.PROVIDER_LIST_REQUEST: _handle_provider_list_request,
    EventType.PROVIDER_VALIDATE_REQUEST: _handle_provider_validate_request,
    EventType.SETTINGS_PROVIDER_VALIDATE_REQUEST: _handle_provider_validate_request,
    EventType.PROVIDER_SAVE_REQUEST: _handle_provider_save_request,
    EventType.PROVIDER_REMOVE_REQUEST: _handle_provider_remove_request,
    EventType.PROVIDER_REVEAL_KEY_REQUEST: _handle_provider_reveal_key_request,
    EventType.MODEL_SET_DEFAULT: _handle_model_set_default,
    EventType.SESSION_LIST_REQUEST: _handle_session_list_request,
    EventType.SESSION_GET_REQUEST: _handle_session_get_request,
    EventType.SESSION_CREATE_REQUEST: _handle_session_create_request,
    EventType.SESSION_DELETE_REQUEST: _handle_session_delete_request,
    EventType.SESSION_TRUNCATE_REQUEST: _handle_session_truncate_request,
    EventType.SESSION_RENAME_REQUEST: _handle_session_rename_request,
    EventType.SESSION_MOVE_REQUEST: _handle_session_move_request,
    EventType.SESSION_SEARCH_REQUEST: _handle_session_search_request,
    EventType.SESSION_EXPORT_REQUEST: _handle_session_export_request,
    EventType.SESSION_IMPORT_REQUEST: _handle_session_import_request,
    EventType.REWIND_MANIFEST_GET_REQUEST: _handle_rewind_manifest_get_request,
    EventType.TASK_REWIND_REQUEST: _handle_task_rewind_request,
    EventType.TASK_REWIND_UNDO_REQUEST: _handle_task_rewind_undo_request,
    EventType.SESSION_FORK_REQUEST: _handle_session_fork_request,
    EventType.APPROVAL_GRANTS_REQUEST: _handle_approval_grants_request,
    EventType.APPROVAL_GRANT_REVOKE_REQUEST: _handle_approval_grant_revoke_request,
    EventType.ARTIFACT_LIST_REQUEST: _handle_artifact_list_request,
    EventType.ARTIFACT_GET_REQUEST: _handle_artifact_get_request,
    EventType.DEV_SERVER_DETECT_REQUEST: _handle_dev_server_detect_request,
    EventType.USAGE_LEDGER_QUERY_REQUEST: _handle_usage_ledger_query_request,
    EventType.TASK_LEDGER_QUERY_REQUEST: _handle_task_ledger_query_request,
    EventType.TASK_LEDGER_LIST_REQUEST: _handle_task_ledger_list_request,
    EventType.CONTEXT_BREAKDOWN_REQUEST: _handle_context_breakdown_request,
    EventType.DAILY_BUDGET_SET_REQUEST: _handle_daily_budget_set_request,
    EventType.BROWSER_TASK_PAUSE_REQUEST: _handle_browser_task_pause_request,
    EventType.BROWSER_POLICY_GET_REQUEST: _handle_browser_policy_get_request,
    EventType.BROWSER_POLICY_SET_REQUEST: _handle_browser_policy_set_request,
    EventType.DESKTOP_CONTROL_REQUEST: _handle_desktop_control_request,
    EventType.DESKTOP_GRANTS_REQUEST: _handle_desktop_grants_request,
    EventType.VAULT_ENTRY_LIST_REQUEST: _handle_vault_entry_list_request,
    EventType.VAULT_ENTRY_SAVE_REQUEST: _handle_vault_entry_save_request,
    EventType.VAULT_ENTRY_DELETE_REQUEST: _handle_vault_entry_delete_request,
    EventType.VAULT_TOTP_LIST_REQUEST: _handle_vault_totp_list_request,
    EventType.VAULT_TOTP_SAVE_REQUEST: _handle_vault_totp_save_request,
    EventType.VAULT_TOTP_DELETE_REQUEST: _handle_vault_totp_delete_request,
    EventType.VAULT_TOTP_CODES_REQUEST: _handle_vault_totp_codes_request,
    EventType.VAULT_FILL_APPROVAL_RESPONSE: _handle_vault_fill_approval_response,
    EventType.MEDIA_LIST_REQUEST: _handle_media_list_request,
    EventType.MEDIA_SEARCH_REQUEST: _handle_media_search_request,
    EventType.MEDIA_DELETE_REQUEST: _handle_media_delete_request,
    EventType.MEDIA_METADATA_UPDATE_REQUEST: _handle_media_metadata_update_request,
    EventType.LOCAL_MODEL_LIST_REQUEST: _handle_local_model_list_request,
    EventType.LOCAL_MODEL_DOWNLOAD_START: _handle_local_model_download_start,
    EventType.LOCAL_MODEL_DOWNLOAD_CANCEL: _handle_local_model_download_cancel,
    EventType.LOCAL_MODEL_DELETE_REQUEST: _handle_local_model_delete_request,
    EventType.MODEL_HUB_SEARCH_REQUEST: _handle_model_hub_search_request,
    EventType.MODEL_HUB_DETAIL_REQUEST: _handle_model_hub_detail_request,
    EventType.MODEL_HUB_DOWNLOAD_REQUEST: _handle_model_hub_download_request,
    EventType.MODEL_HUB_DOWNLOADED_REQUEST: _handle_model_hub_downloaded_request,
    EventType.MODEL_HUB_DELETE_REQUEST: _handle_model_hub_delete_request,
    EventType.CAPABILITY_CHECK_SET_SCHEDULE: _handle_capability_check_set_schedule,
    EventType.CAPABILITY_CHECK_RUN_NOW: _handle_capability_check_run_now,
    EventType.CAPABILITY_CHECK_GET_HISTORY: _handle_capability_check_get_history,
    EventType.ENVIRONMENT_DIAGNOSTIC_RUN_REQUEST: _handle_environment_diagnostic_run_request,
    EventType.REFRESH_MODELS_REQUEST: _handle_refresh_models_request,
    EventType.TOOL_LIST_REQUEST: _handle_tool_list_request,
    EventType.AGENT_LIST_REQUEST: _handle_agent_list_request,
    EventType.AGENT_UPDATE_REQUEST: _handle_agent_update_request,
    EventType.TOOL_POLICY_SET_REQUEST: _handle_tool_policy_set_request,
    EventType.APPROVAL_MODE_SET_REQUEST: _handle_approval_mode_set_request,
    EventType.MCP_SERVER_LIST_REQUEST: _handle_mcp_server_list_request,
    EventType.CONFIG_GET_REQUEST: _handle_config_get_request,
    EventType.CONFIG_UPDATE_REQUEST: _handle_config_update_request,
    EventType.CONFIG_RESET_REQUEST: _handle_config_reset_request,
    EventType.AUXILIARY_MODEL_SET_REQUEST: _handle_auxiliary_model_set_request,
    EventType.REASONING_EFFORT_SET_REQUEST: _handle_reasoning_effort_set_request,
    EventType.MCP_SERVER_ADD_REQUEST: _handle_mcp_server_add_request,
    EventType.MCP_SERVER_REMOVE_REQUEST: _handle_mcp_server_remove_request,
    EventType.MCP_SERVER_UPDATE_REQUEST: _handle_mcp_server_update_request,
    EventType.MCP_SERVER_CONNECT_REQUEST: _handle_mcp_server_connect_request,
    EventType.MCP_SERVER_DISCONNECT_REQUEST: _handle_mcp_server_disconnect_request,
    EventType.MCP_SERVER_TEST_REQUEST: _handle_mcp_server_test_request,
    EventType.MCP_RESOURCE_LIST_REQUEST: _handle_mcp_resource_list_request,
    EventType.MCP_RESOURCE_READ_REQUEST: _handle_mcp_resource_read_request,
    EventType.MCP_PROMPT_LIST_REQUEST: _handle_mcp_prompt_list_request,
    EventType.MCP_PROMPT_RUN_REQUEST: _handle_mcp_prompt_run_request,
    EventType.PLUGIN_LIST_REQUEST: _handle_plugin_list_request,
    EventType.PLUGIN_INSTALL_REQUEST: _handle_plugin_install_request,
    EventType.PLUGIN_APPROVAL_GRANTED: _handle_plugin_approval_granted,
    EventType.PLUGIN_APPROVAL_REJECTED: _handle_plugin_approval_rejected,
    EventType.PLUGIN_UNINSTALL_REQUEST: _handle_plugin_uninstall_request,
    EventType.PLUGIN_TOGGLE_REQUEST: _handle_plugin_toggle_request,
    EventType.CALENDAR_LIST_REQUEST: handle_calendar_list_request,
    EventType.MEMORY_EPISODES_REQUEST: _handle_memory_episodes_request,
    EventType.MEMORY_EPISODE_DELETE_REQUEST: _handle_memory_episode_delete_request,
    EventType.MEMORY_EPISODES_CLEAR_REQUEST: _handle_memory_episodes_clear_request,
    EventType.MEMORY_GRAPH_REQUEST: _handle_memory_graph_request,
    EventType.MEMORY_FACT_DELETE_REQUEST: _handle_memory_fact_delete_request,
    EventType.MEMORY_ENTITY_DELETE_REQUEST: _handle_memory_entity_delete_request,
    EventType.MEMORY_GRAPH_CLEAR_REQUEST: _handle_memory_graph_clear_request,
    EventType.MEMORY_FACT_UPDATE_REQUEST: _handle_memory_fact_update_request,
    EventType.MEMORY_EXPORT_REQUEST: _handle_memory_export_request,
    EventType.MEMORY_BACKENDS_REQUEST: _handle_memory_backends_request,
    EventType.MACHINE_CAPABILITY_REQUEST: _handle_machine_capability_request,
    EventType.AGENT_ROUTING_REQUEST: _handle_agent_routing_request,
    EventType.BACKUP_CREATE_REQUEST: _handle_backup_create_request,
    EventType.BACKUP_RESTORE_REQUEST: _handle_backup_restore_request,
    EventType.DIAGNOSTICS_EXPORT_REQUEST: _handle_diagnostics_export_request,
    EventType.VERSION_CHECK_REQUEST: _handle_version_check_request,
    EventType.SKILL_LIST_REQUEST: _handle_skill_list_request,
    EventType.SKILL_TOGGLE_REQUEST: _handle_skill_toggle_request,
    EventType.SLASH_COMMAND_LIST_REQUEST: _handle_slash_command_list_request,
    EventType.WORKSPACE_LIST_REQUEST: _handle_workspace_list_request,
    EventType.WORKSPACE_ADD_REQUEST: _handle_workspace_add_request,
    EventType.WORKSPACE_REMOVE_REQUEST: _handle_workspace_remove_request,
    EventType.WORKSPACE_INDEX_START_REQUEST: _handle_workspace_index_start_request,
    EventType.WORKSPACE_INDEX_STATUS_REQUEST: _handle_workspace_index_status_request,
    EventType.WORKSPACE_INDEX_PAUSE_REQUEST: _handle_workspace_index_pause_request,
    EventType.WORKSPACE_INDEX_CONFIG_REQUEST: _handle_workspace_index_config_request,
    EventType.WORKSPACE_SET_ACTIVE_REQUEST: _handle_workspace_set_active_request,
    EventType.PROJECT_CONFIG_GET_REQUEST: _handle_project_config_request,
    EventType.PROJECT_CONFIG_UPDATE_REQUEST: _handle_project_config_request,
    EventType.VOICE_SESSION_START_REQUEST: _handle_voice_session_start_request,
    EventType.VOICE_SESSION_STOP_REQUEST: _handle_voice_session_stop_request,
    EventType.VOICE_PLAYBACK_PROGRESS: _handle_voice_playback_progress,
    EventType.VOICE_RTC_OFFER_REQUEST: _handle_voice_rtc_offer_request,
    EventType.VOICE_RTC_ICE_REQUEST: _handle_voice_rtc_ice_request,
    EventType.DICTATION_START_REQUEST: _handle_dictation_start_request,
    EventType.DICTATION_CHUNK_REQUEST: _handle_dictation_chunk_request,
    EventType.DICTATION_STOP_REQUEST: _handle_dictation_stop_request,
    EventType.READ_ALOUD_START_REQUEST: _handle_read_aloud_start_request,
    EventType.READ_ALOUD_STOP_REQUEST: _handle_read_aloud_stop_request,
}

# --- Legacy unauthenticated HTTP management routes: REMOVED (Phase 9, A1) ---
# The seven HTTP routes that used to live here (GET/POST/DELETE/PATCH on
# /plugins and /agents/nodes) were deleted because they bypassed the ADR 0003
# token handshake enforced on /ws/tasks. Their only callers were the retired
# PySide6 UI (superseded by ADR-0019) and a test for that retired path.
# Plugin/agent management is served exclusively over the authenticated
# WebSocket (PLUGIN_INSTALL_REQUEST, AGENT_UPDATE_REQUEST, ...).
# Do NOT re-add unauthenticated HTTP management routes.
