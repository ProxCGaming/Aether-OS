import asyncio
import base64
import copy
import logging
import re
import shutil
import time
import uuid
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
    custom_provider_flavor,
    save_config,
)
from aether_engine.cost.ledger import (
    reset_usage_context,
    set_usage_context,
    usage_context,
)
from aether_engine.langgraph.checkpointer import delete_thread_checkpoints
from aether_engine.langgraph.executor import (
    get_pending_approvals,
    get_persisted_run_config,
    resume_langgraph_task,
    run_langgraph_task,
)
from aether_engine.langgraph.fast_chat import load_session_history, run_fast_chat
from aether_engine.langgraph.steering import enqueue_steer
from aether_engine.memory import session_store
from aether_engine.memory.facade import backend_or_none
from aether_engine.orchestration.core import (
    _build_provider_list_async,
    _create_provider_instance,
    validate_provider_key_shared,
)
from aether_engine.providers.discovery import fetch_available_models
from aether_engine.providers.serving import (
    begin_serving_tracking,
    clear_serving_provider,
    end_serving_tracking,
    serving_provider,
)
from aether_engine.rewind import fork as fork_service
from aether_engine.rewind import manifest as rewind_manifest
from aether_engine.rewind import service as rewind_service
from aether_engine.routing.intent import classify_intent
from aether_engine.routing.policy import (
    RoutingDecision,
    infer_task_requirements,
    local_only_from_vision_override,
)
from aether_engine.routing.startup import (
    derive_configured_providers,
    is_keyless_custom_provider,
)
from aether_engine.secrets.dpapi import SecretDecryptionError
from aether_engine.secrets.storage import ProviderNotFoundError, SecretStore
from aether_engine.state import engine_state
from aether_engine.tools.registry import build_tool_registry
from aether_engine.validation.diagnostics import run_environment_diagnostics
from aether_engine.version import APP_VERSION

logger = logging.getLogger("aether_engine.routes.websocket")

# Phase 10.3, B1: the only task class with a real engine seam — see
# AUXILIARY_TASK_CLASSES in aether_engine/config.py (single source of truth).
# The Settings UI dropped the other six helper tasks in Phase 9 (commit
# ce99048) because none of them has an engine call site — routing/policy.py
# reads only "vision" — but this handler kept accepting and persisting all
# seven, so a client got success:true for a setting the engine would
# silently ignore. Keep this set to exactly the task classes the routing
# policy honors; anything else is rejected, never stored.

router = APIRouter()

# Phase 10.1 A1: pending plugin installs awaiting the user's approval.
# Keyed by request id; each value is the prepare_install() payload
# ({"source": ..., "manifest": {...}}) for one PLUGIN_INSTALL_REQUEST.
# The install is only executed when the matching PLUGIN_APPROVAL_GRANTED
# arrives; a PLUGIN_APPROVAL_REJECTED pops (discards) the entry.
pending_plugin_installs: dict[str, dict[str, Any]] = {}

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


def create_stt_engine():
    """Factory for local STT engine (E8-5.3 push-to-talk dictation)."""
    from aether_engine.voice.stt import FasterWhisperStt
    return FasterWhisperStt()


def create_tts_engine():
    """Factory for local TTS engine (E8-5.3 assistant read-aloud)."""
    from aether_engine.voice.tts import KokoroTts
    return KokoroTts()


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


async def _run_mcp_prompt_into_chat(
    name: str, prompt_name: str, arguments: Any, session_id: str | None
) -> dict[str, Any]:
    """Run MCP prompt *prompt_name* on server *name* and insert its
    messages into a chat session (issue 007).

    Shared by the MCP_PROMPT_RUN handler and the ``/mcp:`` slash
    command, so the two entry points cannot drift apart. Returns
    {"ok", "messages", "session_id", "error"}. Arguments are validated
    by the client (dict of strings, never executed). If no session was
    given (or it no longer exists), a session is created so the prompt
    is never lost. A chat-save failure returns an error, never kills
    the websocket.
    """
    from aether_engine.mcp.registry import GLOBAL_MCP_REGISTRY

    messages = []
    if not name or not prompt_name:
        return {"ok": False, "messages": [], "session_id": session_id,
                "error": "Server name and prompt name are required."}
    try:
        messages = await GLOBAL_MCP_REGISTRY.get_prompt(name, prompt_name, arguments)
    except Exception as e:  # noqa: BLE001 — MCP client calls raise arbitrary server/transport errors; surfaced to the caller as the error field
        return {"ok": False, "messages": [], "session_id": session_id, "error": str(e)}
    try:
        if session_id:
            sess = await asyncio.to_thread(session_store.get_session, session_id)
            if sess is None:
                session_id = None
        if not session_id:
            session_id = await asyncio.to_thread(
                session_store.create_session, f"MCP prompt: {prompt_name}"
            )
        for msg in messages:
            await asyncio.to_thread(
                session_store.add_message, session_id, msg["role"], msg["content"]
            )
    except Exception as e:  # noqa: BLE001 — chat-persistence boundary: a save failure becomes an error response, never a dead websocket
        logger.error(f"Failed to save MCP prompt to chat: {e}")
        return {"ok": False, "messages": [], "session_id": session_id,
                "error": f"Failed to save prompt to chat: {e}"}
    engine_state.audit_logger.log_event("MCP_PROMPT_RUN", {
        "server": name, "prompt": prompt_name, "session_id": session_id,
        "message_count": len(messages),
    })
    return {"ok": True, "messages": messages, "session_id": session_id, "error": ""}


async def handle_artifact_get(artifact_id: str) -> dict[str, Any]:
    """Retrieve an artifact and inspect file existence on disk (Epic 8, E8-2.1)."""
    from aether_engine.artifacts.store import get_artifact_store
    art_store = get_artifact_store()
    art = await asyncio.to_thread(art_store.get_artifact, artifact_id)
    if not art:
        return {
            "artifact": None,
            "content": None,
            "diff": None,
            "exists": False,
            "error": "Artifact not found",
        }

    kind = art.get("kind", "file")
    file_path = art.get("file_path")
    diff = None
    content = None
    exists = True
    error = None

    if kind == "diff":
        diff = art.get("content")
        if file_path:
            exists = Path(file_path).is_file()
            if not exists:
                error = f"File {file_path} no longer exists on disk"
    elif kind == "file":
        if not file_path or not Path(file_path).is_file():
            exists = False
            error = f"File {file_path} no longer exists on disk" if file_path else "File path not specified"
        else:
            try:
                ext = Path(file_path).suffix.lower()
                if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico"):
                    import base64
                    b64 = base64.b64encode(Path(file_path).read_bytes()).decode("ascii")
                    mime = (
                        "image/png" if ext == ".png"
                        else "image/jpeg" if ext in (".jpg", ".jpeg")
                        else "image/gif" if ext == ".gif"
                        else "image/webp" if ext == ".webp"
                        else "application/octet-stream"
                    )
                    content = f"data:{mime};base64,{b64}"
                else:
                    content = Path(file_path).read_text(encoding="utf-8", errors="replace")
            except Exception as read_err:  # noqa: BLE001
                exists = False
                error = f"Error reading file: {read_err}"
    else:  # report, html, etc.
        if file_path and Path(file_path).is_file():
            try:
                content = Path(file_path).read_text(encoding="utf-8", errors="replace")
            except Exception as read_err:  # noqa: BLE001
                error = f"Error reading file: {read_err}"
        else:
            content = art.get("content")

    return {
        "artifact": art,
        "content": content,
        "diff": diff,
        "exists": exists,
        "error": error,
    }


def _remove_session_attachments(session_id: str) -> None:
    """Remove a session's materialised attachments directory (S4-M3).

    Attachments/snips are written under
    ``~/.aether/attachments/<session_id>/`` by
    ``_register_message_attachments``; deleting the session must
    remove that directory too, not just its DB rows. Best-effort:
    failures are logged, never raised.
    """
    try:
        attach_dir = Path.home() / ".aether" / "attachments" / session_id
        shutil.rmtree(attach_dir, ignore_errors=True)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Failed to remove attachments for session {session_id}: {e}")


async def _register_message_attachments(
    session_id: str | None,
    message_id: str | None,
    attachments: list[Any] | None,
    ws: WebSocket | None = None,
) -> None:
    """Auto-register user message attachments into MediaStore in place (Epic 8, E8-4.2)."""
    if not attachments:
        return
    try:
        import base64

        from aether_engine.media.store import get_media_store
        store = get_media_store()
        attach_dir = Path.home() / ".aether" / "attachments" / (session_id or "global")

        for att in attachments:
            file_path = None
            origin = "user_upload"
            mime_type = None

            if isinstance(att, dict):
                file_path = att.get("path")
                data_url = att.get("data") or att.get("url")
                mime_type = att.get("mime_type")
                name = att.get("name") or "attachment"

                if "snip" in name.lower() or att.get("bounds"):
                    origin = "snip"

                if not file_path and data_url and isinstance(data_url, str) and data_url.startswith("data:"):
                    attach_dir.mkdir(parents=True, exist_ok=True)
                    header, _, b64_str = data_url.partition(",")
                    if ";base64" in header:
                        extracted_mime = header.split(":")[1].split(";")[0]
                        mime_type = mime_type or extracted_mime
                    ext = ".png"
                    if mime_type and "jpeg" in mime_type:
                        ext = ".jpg"
                    elif mime_type and "webp" in mime_type:
                        ext = ".webp"
                    target_file = attach_dir / f"{uuid.uuid4().hex[:8]}_{name}"
                    if not target_file.suffix:
                        target_file = target_file.with_suffix(ext)
                    target_file.write_bytes(base64.b64decode(b64_str))
                    file_path = str(target_file)
            elif isinstance(att, str):
                if att.startswith("data:"):
                    attach_dir.mkdir(parents=True, exist_ok=True)
                    header, _, b64_str = att.partition(",")
                    target_file = attach_dir / f"{uuid.uuid4().hex[:8]}_image.png"
                    target_file.write_bytes(base64.b64decode(b64_str))
                    file_path = str(target_file)
                else:
                    file_path = att

            if file_path:
                ext = Path(file_path).suffix.lower()
                is_img = ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp")
                is_vid = ext in (".mp4", ".webm", ".mkv", ".mov", ".avi")
                if is_img or is_vid or (mime_type and mime_type.startswith(("image/", "video/"))):
                    media_type = "video" if (is_vid or (mime_type and mime_type.startswith("video/"))) else "image"
                    item = await asyncio.to_thread(
                        store.register_media,
                        file_path=file_path,
                        collection="user",
                        media_type=media_type,
                        session_id=session_id,
                        message_id=message_id,
                        mime_type=mime_type,
                        probe_metadata={"origin": origin, "name": Path(file_path).name},
                    )
                    if ws:
                        try:
                            await ws.send_text(Event(
                                type=EventType.MEDIA_RECORDED,
                                payload=item,
                            ).to_json())
                        except Exception:  # noqa: BLE001, S110
                            pass
                    # Epic 8, E8-4.3: analyse on arrival — detached
                    # background task, never blocks the message flow.
                    try:
                        from aether_engine.media.analyzer import schedule_media_analysis

                        schedule_media_analysis(item["id"])
                    except Exception:  # noqa: BLE001, S110
                        pass
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Failed to auto-register message attachments into MediaStore: {e}")


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

    task_runner: asyncio.Task | None = None
    # Session of the task this connection most recently started or
    # resumed — lets STEER_TASK tell "a task is live for this session"
    # apart from "start a new task" (Epic 1, E1-07).
    task_session_id: str | None = None
    active_downloads: dict[str, asyncio.Task] = {}
    secret_store = SecretStore()
    # Epic 8, E8-5.1a: this connection's voice session, if any. The
    # pipeline runs as its own task — the talker lane never queues
    # behind task_runner (grill Q28/Q29).
    voice_state: dict[str, Any] = {
        "pipeline": None,
        "task": None,
        "read_aloud_task": None,
        "read_aloud_id": None,
        # S4-M7: per-connection speech-engine cache. Building a
        # FasterWhisperStt/KokoroTts loads a model, so dictation and
        # read-aloud reuse one engine for the connection's lifetime
        # instead of constructing a fresh one per chunk/request.
        "dictation_stt": None,
        "dictation_stt_opened": False,
        "read_aloud_tts": None,
        "read_aloud_tts_opened": False,
    }

    async def _send_validation_result(resp_type, req_id_, pname_, key_, base_url_):
        """Answer a provider Test request from a background task.

        Validation (discovery + pings) used to be awaited inline in
        the message loop, so one slow Test wedged the whole socket —
        chat, cancellations, everything queued behind it, and a
        double-click doubled the freeze. Now the loop stays free;
        duplicate clicks attach to the same in-flight run via
        validate_provider_key_shared and each requester still gets
        its own response.
        """
        try:
            result = await validate_provider_key_shared(pname_, key_, base_url=base_url_, persist_models=False)
            result["provider"] = pname_
            result["success"] = result.get("status") == "connected"
        except Exception as e:  # noqa: BLE001 — a validation failure must still produce a response, never silence
            logger.warning(f"Provider validation for {pname_} raised: {e}")
            result = {
                "provider": pname_,
                "status": "network_error",
                "message": f"Validation failed unexpectedly: {e}",
                "success": False,
            }
        try:
            await ws.send_text(Event(type=resp_type, request_id=req_id_, payload=result).to_json())
        except Exception as e:  # noqa: BLE001 — the client may have disconnected mid-validation; nothing else to do
            logger.debug(f"Could not send validation result for {pname_}: {e}")

    # Epic 2, E2-10: install the web-search seam for this connection —
    # keys resolve through the encrypted secret store (provider name =
    # "exa"/"brave"/"tavily"/"firecrawl"), and the provider choice is
    # read live from the user config on every search.
    from aether_engine.tools.web_search import (
        set_search_config_loader,
        set_search_key_loader,
    )

    set_search_key_loader(secret_store.load_provider)
    set_search_config_loader(
        lambda: (engine_state.user_config.search_provider, engine_state.user_config.search_fallback_to_ddgs)
    )

    # Epic 8, E8-6.2: the browser session layer reads the live
    # browser policy through this seam (master switch for new
    # sessions), read fresh from the user config on every check.
    from aether_engine.browser.policy import (
        policy_from_user_config,
        set_browser_policy_loader,
    )

    set_browser_policy_loader(lambda: policy_from_user_config(engine_state.user_config))

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

    async def _learn_fast_chat_in_background(task_id: str, prompt: str, response_text: str, p_name: str, p_model: str):
        """Epic 4, E4-05: fast-chat learn parity — detached after
        TASK_COMPLETED; the work itself lives in
        fast_chat.learn_fast_chat_exchange (thin wrapper here: build
        the chat's provider instance, then delegate)."""
        from aether_engine.langgraph.fast_chat import learn_fast_chat_exchange
        from aether_engine.orchestration.core import _create_provider_instance

        try:
            provider = _create_provider_instance(p_name, p_model, secret_store)
        except Exception:  # noqa: BLE001 — no provider instance: the episode still records; only fact extraction is skipped
            provider = None
        await learn_fast_chat_exchange(
            task_id, prompt, response_text, provider, engine_state.user_config)

    async def _title_session_in_background(session_id: str, p_name: str, p_model: str, response_text: str):
        """E1-08: give a still-default session title a model-written one.

        Runs detached after a task completes; every failure simply
        keeps the existing title.
        """
        try:
            from aether_engine.orchestration.core import _create_provider_instance
            from aether_engine.session_titles import (
                generate_session_title,
                is_default_title,
                resolve_title_provider,
            )

            session = await asyncio.to_thread(session_store.get_session, session_id)
            if not session:
                return
            messages = session.get("messages") or []
            first_user = next((m.get("content") for m in messages if m.get("role") == "user"), None)
            if not first_user or not is_default_title(session.get("title"), first_user):
                return
            try:
                fallback = _create_provider_instance(p_name, p_model, secret_store)
            except Exception:  # noqa: BLE001 — no provider instance, no title.
                return
            provider = resolve_title_provider(engine_state.user_config, secret_store, fallback)
            title = await generate_session_title(provider, first_user, response_text)
            if title and await asyncio.to_thread(session_store.update_session_title, session_id, title):
                await ws.send_text(Event(
                    type=EventType.SESSION_TITLE_UPDATED,
                    request_id="title",
                    payload={"session_id": session_id, "title": title},
                ).to_json())
        except Exception:  # Background decoration must never surface as a task error.
            logger.warning("Session title update failed", exc_info=True)

    # ── Voice session (Epic 8, E8-5.1a) ─────────────────────────────
    # The cascade itself lives in aether_engine.voice.pipeline; this
    # layer only wires it to the socket: events out, transcript
    # commits into the session store, teardown on stop/disconnect.

    async def _emit_voice_event(event: Event) -> None:
        try:
            await ws.send_text(event.to_json())
        except Exception:  # a dead socket must not kill the pipeline mid-turn
            logger.debug("voice event send failed", exc_info=True)

    async def _run_voice_pipeline(pipeline) -> None:
        # Finding M19: the talker and the escalation classifier make
        # real model calls from this task — outside the task pump, so
        # no pump ever sets their attribution. Carry the voice
        # session's context for the whole run; escalated background
        # tasks set their own context in the pump as before.
        usage_token = set_usage_context(session_id=pipeline.session_id)
        try:
            await pipeline.run()
        except asyncio.CancelledError:
            raise
        except Exception:  # the session is over either way; never wedge the socket
            logger.warning("voice pipeline ended with an error", exc_info=True)
        finally:
            reset_usage_context(usage_token)

    async def _stop_voice_session() -> None:
        """Stop the connection's voice pipeline, if any. Closing the
        pipeline's transport ends its frame loop; cancelling the run
        task is the backstop."""
        pipeline = voice_state.get("pipeline")
        task = voice_state.get("task")
        voice_state["pipeline"] = None
        voice_state["task"] = None
        if pipeline is not None:
            try:
                await pipeline.stop()
            except Exception:  # teardown is best-effort by definition
                logger.debug("voice pipeline stop failed", exc_info=True)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _stop_read_aloud() -> None:
        """Stop any running read-aloud synthesis task (Epic 8, E8-5.3)."""
        ra_task = voice_state.get("read_aloud_task")
        voice_state["read_aloud_task"] = None
        voice_state["read_aloud_id"] = None
        if ra_task is not None and not ra_task.done():
            ra_task.cancel()
            try:
                await ra_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug("read-aloud task cancel failed", exc_info=True)

    # ── Cached speech engines (S4-M7) ────────────────────────────────
    # One STT and one TTS engine per connection, closed only at
    # connection teardown — NOT on DICTATION_STOP / read-aloud end,
    # or push-to-talk would reload the model on every utterance.

    def _dictation_stt() -> Any:
        """The connection's cached dictation STT engine, built once."""
        engine = voice_state.get("dictation_stt")
        if engine is None:
            engine = create_stt_engine()
            voice_state["dictation_stt"] = engine
        return engine

    async def _open_dictation_stt() -> Any:
        """The cached STT engine, opened once. An open failure is
        logged, not raised — the transcribe call that follows reports
        the real error to the client (today's per-chunk tolerance),
        and a later chunk may retry the open."""
        engine = _dictation_stt()
        if not voice_state.get("dictation_stt_opened"):
            if hasattr(engine, "open"):
                try:
                    await engine.open()
                    voice_state["dictation_stt_opened"] = True
                except Exception:
                    logger.debug("STT open error", exc_info=True)
            else:
                voice_state["dictation_stt_opened"] = True
        return engine

    def _read_aloud_tts() -> Any:
        """The connection's cached read-aloud TTS engine, built once."""
        engine = voice_state.get("read_aloud_tts")
        if engine is None:
            engine = create_tts_engine()
            voice_state["read_aloud_tts"] = engine
        return engine

    async def _open_read_aloud_tts() -> Any:
        """The cached TTS engine, opened once. Same failure contract
        as ``_open_dictation_stt``: logged, not raised."""
        engine = _read_aloud_tts()
        if not voice_state.get("read_aloud_tts_opened"):
            if hasattr(engine, "open"):
                try:
                    await engine.open()
                    voice_state["read_aloud_tts_opened"] = True
                except Exception:
                    logger.debug("TTS open error", exc_info=True)
            else:
                voice_state["read_aloud_tts_opened"] = True
        return engine

    async def _close_speech_engines() -> None:
        """Close both cached speech engines (connection teardown).
        Guarded and best-effort: a failing close never wedges the
        disconnect path, and the slots are cleared regardless."""
        for engine_key, opened_key in (
            ("dictation_stt", "dictation_stt_opened"),
            ("read_aloud_tts", "read_aloud_tts_opened"),
        ):
            engine = voice_state.get(engine_key)
            voice_state[engine_key] = None
            voice_state[opened_key] = False
            if engine is None:
                continue
            try:
                if hasattr(engine, "close"):
                    await engine.close()
            except Exception:
                logger.debug("speech engine close failed", exc_info=True)

    async def _run_voice_background_task(text: str, session_id: str, provider) -> None:
        """One voice-escalated job (Epic 8, E8-5.1b): a normal
        supervisor task, built exactly like a typed work message
        (same central registry builder, same execute_task_generator
        pump), but running FREE in the background — the talker lane
        never waits on it. Its events stream to the socket, so the
        task shows in chat with every detail exactly as tasks show
        today; an approval pauses it for the normal in-chat
        approval (the resume path is keyed by thread, not by this
        pump — and nothing in the voice path can approve). When it
        lands, the final summary is spoken through the live voice
        pipeline; if voice mode has ended by then, the summary
        simply stands in chat as the task's own message (the pump
        already persisted it).

        N10: the pause itself is announced too — a task waiting on
        the owner's approval says so, factually, and points at the
        chat where the decision belongs. Every announcement carries
        a stable identity derived from the child task + event kind,
        and a completion announcement also carries the persisted
        message's own id, so the desktop renders the spoken text as
        that message rather than as a second assistant turn."""
        cfg = engine_state.user_config
        req_id = f"voice-task-{uuid.uuid4().hex[:12]}"
        terminal: dict[str, Any] = {}
        # N10: announcement bookkeeping for this job. Identities are
        # derived from the child task's own ids, so the same pause
        # or result always maps to the same announcement — an event
        # the stream re-reports never announces twice.
        announced: set[str] = set()
        announce_tasks: set[asyncio.Task] = set()
        identity: dict[str, Any] = {}

        def _announcement_id(kind: str, *parts: Any) -> str:
            base = identity.get("task_id") or req_id
            return ":".join(
                ["voice-ann", str(base), kind]
                + [str(p) for p in parts if p])

        async def _announce(
            announcement_id: str, kind: str, text: str, *,
            message_id: str | None = None,
        ) -> None:
            if announcement_id in announced:
                return
            announced.add(announcement_id)
            pipeline = voice_state.get("pipeline")
            if pipeline is None:
                return
            await pipeline.speak_announcement(
                text, announcement_id=announcement_id, kind=kind,
                message_id=message_id)

        def _schedule_announce(
            announcement_id: str, kind: str, text: str,
        ) -> None:
            # A mid-stream announcement (an approval pause) must
            # not block the pump — the approval card has to reach
            # chat promptly. The pipeline speaks at the next quiet
            # moment on its own.
            task = asyncio.ensure_future(
                _announce(announcement_id, kind, text))
            announce_tasks.add(task)
            task.add_done_callback(announce_tasks.discard)

        async def _task_message_id(response: str) -> str | None:
            """The id of the message the pump persisted for this
            job's result: the newest assistant message in the
            session with exactly this content. Best-effort — the
            announcement stays identity-tagged without it."""
            try:
                session = await asyncio.to_thread(
                    session_store.get_session, session_id)
            except Exception:  # noqa: BLE001 — a lookup failure must not break the announcement
                return None
            for message in reversed((session or {}).get("messages") or []):
                if (message.get("role") == "assistant"
                        and message.get("content") == response):
                    return message.get("id")
            return None

        try:
            from aether_engine.mcp.registry import GLOBAL_MCP_REGISTRY

            reqs = infer_task_requirements(text)
            tools = build_tool_registry(
                mcp_tools=GLOBAL_MCP_REGISTRY.get_mcp_tools(
                    risk_overrides=cfg.mcp_tool_risk_overrides),
                include_file_tools=reqs.wants_code,
            )
            generator = run_langgraph_task(
                prompt=text,
                provider=provider,
                tools=tools,
                request_id=req_id,
                approval_handler=None,
                workspace_roots=cfg.workspace_roots or [str(Path.home() / "AetherWorkspace")],
                disabled_nodes=engine_state.disabled_nodes,
                session_id=session_id,
                agent_models=cfg.agent_models,
                agent_tools=cfg.agent_tools,
                secret_store=secret_store,
                user_config=cfg,
                forced_agent=None,
                active_skills=[],
                plan_mode=False,
                attachments=None,
            )

            async def _observed():
                async for ev in generator:
                    payload = (
                        ev.payload if isinstance(ev.payload, dict)
                        else {})
                    if payload.get("task_id"):
                        identity["task_id"] = payload["task_id"]
                    if ev.type == EventType.TASK_COMPLETED:
                        terminal["response"] = payload.get("response")
                    elif ev.type == EventType.TASK_FAILED:
                        terminal["error"] = (
                            payload.get("error") or "unknown error")
                    elif ev.type == EventType.TOOL_APPROVAL_REQUEST:
                        # N10: the task is paused on the owner.
                        # Announce it — factually, and pointing at
                        # the chat, where the decision belongs:
                        # nothing in the voice path can approve.
                        tool_name = payload.get("tool_name")
                        if tool_name:
                            notice = (
                                "That background task is paused, "
                                "waiting for your approval to use "
                                f"{tool_name}. You can review and "
                                "decide in the chat.")
                        else:
                            notice = (
                                "That background task is paused, "
                                "waiting for your approval. You can "
                                "review and decide in the chat.")
                        _schedule_announce(
                            _announcement_id(
                                "approval", payload.get("approval_key"),
                                tool_name),
                            "approval", notice)
                    yield ev

            await execute_task_generator(
                _observed(), cfg.default_provider, cfg.default_model,
                req_id, session_id=session_id,
                routing_reason="Voice escalation: spoken request classified as work",
            )
        except asyncio.CancelledError:
            for task in announce_tasks:
                task.cancel()
            raise
        except Exception:  # a background job must never wedge the socket
            logger.warning("voice background task failed to run", exc_info=True)
            terminal.setdefault("error", "the background task could not be started")
        pipeline = voice_state.get("pipeline")
        if pipeline is None:
            return
        if terminal.get("response"):
            await _announce(
                _announcement_id("completed"), "completed",
                str(terminal["response"]),
                message_id=await _task_message_id(
                    str(terminal["response"])))
        elif terminal.get("error"):
            await _announce(
                _announcement_id("failed"), "failed",
                f"That background task failed: {terminal['error']}")

    async def _cancel_voice_background_tasks() -> None:
        """Disconnect teardown for escalated jobs: their event
        stream dies with the socket, so they stop with it. (Voice
        STOP alone does NOT cancel them — they run free; only
        their spoken summary is skipped.)"""
        tasks = voice_state.get("background_tasks") or set()
        voice_state["background_tasks"] = set()
        for task in tasks:
            if not task.done():
                task.cancel()

    async def execute_task_generator(generator, p_name: str, p_model: str, req_id: str, session_id: str | None = None, *, routing_reason: str = "Default Routing", routing_capabilities: list | None = None, retry_factory=None, fallback_blocked_message: str | None = None):
        """Stream one task's events to the client.

        Epic 5, E5-03: the TASK_CREATED routing stamp carries the
        routing decision's real reason + capabilities, not a
        hard-coded "Default Routing".

        Epic 5, E5-02 — task-level fallback, §6.4-shaped: when the
        run fails with a retriable error BEFORE any tool activity
        (no TOOL_ACTIVITY / TOOL_APPROVAL_REQUEST seen), a supplied
        ``retry_factory`` may rebuild the task once on the top
        fallback candidate; the switch is audited as
        FALLBACK_TRIGGERED and the retry streams as the same task.
        Once tools have run, or on a second failure, the failure
        event is delivered as before. The resume path passes no
        factory: a resumed task has history, so §6.4 forbids it.
        """
        # Epic 8, E8-A8.1a: attribute the provider calls made while
        # this task streams to the usage ledger. The session is known
        # now; the task id is refined in the event loop below, once
        # TASK_CREATED flows past. Reset when the pump finishes so a
        # later task on this connection starts clean.
        usage_token = set_usage_context(session_id=session_id)
        # Finding E5-F07: open the serving-provider scope beside the
        # usage context. Provider calls made while this task streams
        # note which provider actually served them; the completion
        # credit below follows that note instead of the outer
        # provider variable.
        serving_token = begin_serving_tracking()
        try:
            attempt = 0
            current_reason = routing_reason
            while True:
                attempt += 1
                primary_failed = False
                failure_event = None
                failure_recorded = False
                clear_serving_provider()
                saw_tool_activity = False
                task_path: str | None = None
                task_prompt: str | None = None

                async for ev in generator:
                    # Epic 8, E8-A8.1a: keep the usage attribution
                    # current — provider calls record against the
                    # task id as soon as any event names it
                    # (TASK_CREATED is always first for fresh tasks).
                    if isinstance(ev.payload, dict):
                        _ev_task_id = ev.payload.get("task_id")
                        if _ev_task_id:
                            set_usage_context(task_id=_ev_task_id)
                        if ev.type == EventType.TASK_CREATED:
                            set_usage_context(
                                task_class=ev.payload.get("path")
                                or ev.payload.get("task_class"))
                    if ev.type in (EventType.TOOL_ACTIVITY, EventType.TOOL_APPROVAL_REQUEST):
                        saw_tool_activity = True
                    if ev.type == EventType.TASK_CREATED:
                        task_path = ev.payload.get("path")
                        task_prompt = ev.payload.get("prompt")
                        ev.payload["routing"] = {
                            "provider": p_name,
                            "model": p_model,
                            "reason": current_reason,
                            "capabilities": routing_capabilities or [],
                        }
                        engine_state.audit_logger.log_task_transition(
                            ev.payload.get("task_id", "unknown"),
                            TaskState.PENDING,
                            TaskState.RUNNING,
                            ev.payload,
                        )
                    elif ev.type in (EventType.TASK_COMPLETED, EventType.TASK_CANCELLED, EventType.TASK_FAILED, EventType.TASK_INTERRUPTED):
                        to = {
                            EventType.TASK_COMPLETED: TaskState.SUCCEEDED,
                            EventType.TASK_CANCELLED: TaskState.CANCELLED,
                            EventType.TASK_FAILED: TaskState.FAILED,
                            EventType.TASK_INTERRUPTED: TaskState.PENDING_APPROVAL,
                        }[ev.type]
                        engine_state.audit_logger.log_task_transition(
                            ev.payload.get("task_id", "unknown"),
                            TaskState.RUNNING,
                            to,
                            ev.payload,
                        )

                    if ev.type == EventType.TASK_FAILED:
                        if ev.payload.get("retriable"):
                            primary_failed = True
                            failure_event = ev
                            break
                        # Finding E5-F07: a nonretriable failure (a
                        # 401, a bad request) is still this provider's
                        # task outcome — record it as a permanent
                        # failure instead of letting the provider keep
                        # a clean health slate. The retriable path
                        # records after the loop, ahead of the §6.4
                        # retry decision; the event itself is still
                        # delivered to the client unchanged below.
                        if not failure_recorded:
                            failure_recorded = True
                            engine_state.health_manager.record_failure(
                                p_name,
                                ev.payload.get("error", "Unknown error"),
                                is_retriable=False)

                    if ev.type == EventType.TASK_COMPLETED:
                        latency = ev.payload.get("latency_ms", 100.0)
                        # Finding E5-F07: credit the provider that
                        # served the completing call (per-call
                        # fallback may have handed the work to another
                        # provider); with no serving note, the outer
                        # provider is credited exactly as before.
                        engine_state.health_manager.record_success(
                            serving_provider() or p_name, latency)
                        if session_id:
                            ev.payload["session_id"] = session_id
                        if session_id and ev.payload.get("response"):
                            # Save the final response from assistant with full thought history
                            try:
                                _msg_id = await asyncio.to_thread(session_store.add_message, session_id, "assistant", ev.payload["response"], ev.payload.get("thoughts"))
                                # N10: stamp the persisted message's own
                                # identity onto the event before it is
                                # sent, so consumers (voice announcements,
                                # the desktop) can refer to the exact
                                # message this completion produced.
                                if _msg_id:
                                    ev.payload["message_id"] = _msg_id
                            except Exception as e:  # noqa: BLE001 — best-effort persistence: a save failure must not interrupt the task stream
                                logger.error(f"Failed to save session message: {e}")
                            asyncio.create_task(
                                _title_session_in_background(session_id, p_name, p_model, ev.payload["response"]))
                            if task_path == "fast_chat" and task_prompt:
                                # Epic 4, E4-05: graph tasks learn in the
                                # executor; fast chat learns here (parity).
                                asyncio.create_task(_learn_fast_chat_in_background(
                                    ev.payload.get("task_id", "unknown"), task_prompt,
                                    ev.payload["response"], p_name, p_model))

                    if ev.type == EventType.TOOL_APPROVAL_REQUEST:
                        engine_state.audit_logger.log_event("APPROVAL_REQUESTED", {
                            "tool_name": ev.payload.get("tool_name"),
                            "args": ev.payload.get("args"),
                            "task_id": ev.payload.get("task_id", "unknown"),
                        })

                    await ws.send_text(ev.to_json())

                if not (primary_failed and failure_event):
                    return

                err_msg = failure_event.payload.get("error", "Unknown error")
                engine_state.health_manager.record_failure(p_name, err_msg, is_retriable=True)

                # §6.4 gate: one retry, only before any tool ran,
                # only when the caller supplied a factory.
                if should_attempt_task_fallback(
                        attempt=attempt, saw_tool_activity=saw_tool_activity,
                        has_factory=retry_factory is not None):
                    rebuilt = await retry_factory(
                        p_name, p_model, failure_event.payload.get("task_id", "unknown"))
                    if rebuilt is not None:
                        generator, fallback_provider, fallback_model = rebuilt
                        engine_state.audit_logger.log_event("FALLBACK_TRIGGERED", {
                            "task_id": failure_event.payload.get("task_id", "unknown"),
                            "failed_provider": p_name,
                            "failed_model": p_model,
                            "fallback_provider": fallback_provider,
                            "fallback_model": fallback_model,
                            "error": err_msg,
                        })
                        logger.warning(
                            f"FALLBACK_TRIGGERED: {p_name}/{p_model} failed before any tool ran; "
                            f"retrying on {fallback_provider}/{fallback_model}")
                        current_reason = (
                            f"Fallback: {p_name}/{p_model} failed before any tool ran; "
                            f"retried on {fallback_provider}/{fallback_model}")
                        p_name, p_model = fallback_provider, fallback_model
                        # E5-F12: the retry's TASK_CREATED restamp must
                        # advertise the FALLBACK model's capabilities,
                        # not the failed primary's. Re-derive them from
                        # the registry entry for the (provider, model)
                        # now serving — the same source the original
                        # RoutingDecision stamp came from. No entry
                        # means no advertised capabilities, never the
                        # primary's leftovers.
                        _fb_entry = engine_state.model_registry.get_model(
                            fallback_model, provider=fallback_provider)
                        routing_capabilities = (
                            list(_fb_entry.capabilities)
                            if _fb_entry is not None else [])
                        continue
                    elif fallback_blocked_message:
                        # The route was constrained (finding M17) and
                        # no lawful fallback exists: replace the raw
                        # provider error with the honest explanation
                        # before the failure reaches the client, so
                        # the user learns the local model is down AND
                        # that nothing was sent anywhere else.
                        failure_event.payload["error"] = fallback_blocked_message

                await ws.send_text(failure_event.to_json())
                return
        except asyncio.CancelledError:
            engine_state.audit_logger.log_event("TASK_CANCELLED", {"task_id": "unknown"})
            raise
        except Exception as e:  # task-generator boundary: any failure is converted into an ERROR event for the client
            logger.exception("Task generator failed with exception")
            try:
                await ws.send_text(Event(
                    type=EventType.ERROR,
                    request_id=req_id,
                    payload={"error": f"Internal engine error: {e!s}"}
                ).to_json())
            except Exception:  # the socket is already broken; nothing further can be signalled
                logger.debug("Could not send task error event to client", exc_info=True)
        finally:
            reset_usage_context(usage_token)
            end_serving_tracking(serving_token)

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = Event.from_json(raw)
            except (SchemaVersionMismatchError, InvalidEventPayloadError) as e:
                await ws.send_text(Event(type=EventType.ERROR, payload={"error": str(e)}).to_json())
                continue

            req_id = msg.request_id

            # -----------------------------------------------------------
            # Steering (Epic 1, E1-07): a message for a session whose
            # task is running — or paused for approval — is queued for
            # that task (saved + enqueued, nothing cancelled). With no
            # live task for the session it falls through and is handled
            # as a normal START_TASK below, so a steer can never strand
            # a message.
            # -----------------------------------------------------------
            if msg.type == EventType.STEER_TASK:
                steer_prompt = (msg.payload.get("prompt") or "").strip()
                steer_session = msg.payload.get("session_id") or task_session_id
                steer_active = bool(
                    steer_session
                    and task_runner is not None
                    and not task_runner.done()
                    and (task_session_id is None or task_session_id == steer_session)
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

            # -----------------------------------------------------------
            # Task lifecycle events
            # -----------------------------------------------------------
            if msg.type == EventType.START_TASK:
                if task_runner and not task_runner.done():
                    task_runner.cancel()
                    try:
                        await task_runner
                    except asyncio.CancelledError:
                        pass

                prompt = msg.payload.get("prompt", "").strip() or "Say hello"
                attachments = msg.payload.get("attachments") or []
                cfg = engine_state.user_config
                requested_model = msg.payload.get("model")
                session_id = msg.payload.get("session_id")
                
                if not session_id:
                    # Create new session if none provided
                    title = prompt[:30] + "..." if len(prompt) > 30 else prompt
                    session_id = await asyncio.to_thread(session_store.create_session, title)
                    try:
                        await ws.send_text(Event(
                            type=EventType.SESSION_CREATE_RESPONSE,
                            request_id=req_id,
                            payload={"session_id": session_id, "preserve_messages": True}
                        ).to_json())
                        await ws.send_text(Event(
                            type=EventType.SESSION_LIST_RESPONSE,
                            request_id=req_id,
                            payload={"sessions": await asyncio.to_thread(session_store.list_sessions)}
                        ).to_json())
                    except Exception:  # best-effort notification: the session is already created; a failed send must not abort task start
                        logger.debug("Could not send session create/list events to client", exc_info=True)
                else:
                    sess_info = await asyncio.to_thread(session_store.get_session, session_id)
                    if sess_info and sess_info.get("title") in ("New Conversation", "New Chat") and not sess_info.get("messages"):
                        title = prompt[:30] + "..." if len(prompt) > 30 else prompt
                        await asyncio.to_thread(session_store.update_session_title, session_id, title)
                        try:
                            await ws.send_text(Event(
                                type=EventType.SESSION_LIST_RESPONSE,
                                request_id=req_id,
                                payload={"sessions": await asyncio.to_thread(session_store.list_sessions)}
                            ).to_json())
                        except Exception:  # best-effort notification: a failed send must not abort task start
                            logger.debug("Could not send session list event to client", exc_info=True)
                # Epic 8, E8-1.1b: a new message in a session with a
                # staged rewind commits it — the undo window closes
                # and the discarded steps are pruned now, before the
                # new task builds on the rewound state.
                await rewind_service.commit(session_id)

                # Issue 007: ``/mcp:<server>:<prompt>`` runs a prompt
                # template into the chat instead of starting a graph task.
                # No arguments in the slash form (use the MCP tab for
                # prompts with arguments). The slash text itself is not
                # saved as a user message — only the prompt's messages are.
                _mcp_slash = re.match(r"^/mcp:([A-Za-z0-9_.\-]{1,100}):([A-Za-z0-9_.\-]+)$", prompt)
                if _mcp_slash:
                    _run = await _run_mcp_prompt_into_chat(
                        _mcp_slash.group(1), _mcp_slash.group(2), None, session_id
                    )
                    await ws.send_text(Event(
                        type=EventType.MCP_PROMPT_RUN_RESPONSE,
                        request_id=req_id,
                        payload={
                            "success": _run["ok"], "name": _mcp_slash.group(1),
                            "prompt": _mcp_slash.group(2),
                            "messages": _run["messages"],
                            "session_id": _run["session_id"], "error": _run["error"],
                        },
                    ).to_json())
                    continue

                try:
                    # The UI's client-generated id becomes the row id, so
                    # the rendered message and the stored message share
                    # one identity (edit-and-resend addresses the row).
                    await asyncio.to_thread(
                        session_store.add_message, session_id, "user", prompt,
                        None, msg.payload.get("message_id"), attachments)
                    await _register_message_attachments(
                        session_id, msg.payload.get("message_id"), attachments, ws)
                except Exception as e:  # noqa: BLE001 — best-effort persistence: a save failure must not block the task
                    logger.error(f"Failed to save user message: {e}")

                # E5-F04: "configured" is derived ONCE, here as
                # everywhere (routing.startup.derive_configured_providers):
                # stored secrets plus keyless custom providers whose
                # saved configuration is complete. Deriving from
                # secrets alone made an accepted keyless custom
                # provider invisible to routing.
                engine_state.configured_providers = derive_configured_providers(
                    cfg, secret_store, engine_state.model_registry)

                decision: RoutingDecision = engine_state.routing_policy.select_route(
                    prompt=prompt,
                    configured_providers=engine_state.configured_providers,
                    health_manager=engine_state.health_manager,
                    user_default_provider=cfg.default_provider,
                    user_default_model=cfg.default_model,
                    requested_model=requested_model,
                    auxiliary_models=cfg.auxiliary_models,
                    images=attachments,
                )

                workspace_roots = engine_state.user_config.workspace_roots or [str(Path.home() / "AetherWorkspace")]

                # Epic 1, E1-14 config layers, computed for BOTH paths:
                # the graph branch uses it for the task, and the Epic 5
                # retry-factory snapshot below reads it unconditionally
                # — when it lived only in the graph branch, every
                # fast-chat task died here with an UnboundLocalError
                # and the client waited forever for a reply.
                # E5-F02: it is resolved BEFORE the provider is built,
                # and the provider factory receives it — the per-call
                # fallback enforcement point must be governed by the
                # task's effective fallback policy (disable / chain),
                # not by the globals alone.
                from aether_engine.config_layers import effective_user_config

                task_user_config = effective_user_config(
                    engine_state.user_config,
                    workspace_root=workspace_roots[0] if workspace_roots else None,
                    overlay=msg.payload.get("config_overlay"),
                )

                try:
                    provider_instance = _create_provider_instance(
                        decision.provider, decision.model, secret_store,
                        local_only=decision.local_only,
                        require_vision=bool(attachments),
                        user_config=task_user_config,
                    )
                except ProviderNotFoundError as e:
                    if is_keyless_custom_provider(decision.provider, task_user_config):
                        # E5-F04: this provider is configured WITHOUT
                        # a key by design — a construction failure here
                        # is not a missing key, and the error must name
                        # the real cause instead of demanding a secret
                        # the server does not have.
                        error_text = (
                            f"Provider {decision.provider} is configured without an API key "
                            f"but could not be started: {e}. Check its base URL in Settings → Models."
                        )
                    else:
                        error_text = f"API key not found for {decision.provider}. Please add your key in Settings → Models."
                    await ws.send_text(Event(
                        type=EventType.TASK_FAILED,
                        request_id=req_id,
                        payload={
                            "task_id": "none", "retriable": False, "state": TaskState.FAILED.value,
                            "error": error_text,
                        },
                    ).to_json())
                    continue
                except SecretDecryptionError as e:
                    await ws.send_text(Event(
                        type=EventType.TASK_FAILED,
                        request_id=req_id,
                        payload={
                            "task_id": "none", "retriable": False, "state": TaskState.FAILED.value,
                            "error": f"Failed to decrypt API key: {e}. Please re-enter your key in Settings → Models.",
                        },
                    ).to_json())
                    continue

                slash_cmd_str = msg.payload.get("slash_command")
                if not slash_cmd_str and prompt.startswith("/"):
                    slash_cmd_str = prompt.split(" ")[0]
                
                forced_agent = None
                active_skills = []
                
                if slash_cmd_str:
                    from aether_engine.skills.manager import GLOBAL_SKILL_MANAGER
                    from aether_engine.skills.slash_registry import (
                        GLOBAL_SLASH_REGISTRY,
                    )
                    
                    resolved = GLOBAL_SLASH_REGISTRY.resolve(slash_cmd_str)
                    
                    if resolved:
                        if resolved.type == "agent_route":
                            forced_agent = resolved.target
                    else:
                        skill_manifest = GLOBAL_SKILL_MANAGER.resolve_slash_command(slash_cmd_str)
                        if skill_manifest:
                            active_skills.append(skill_manifest.name)
                            if skill_manifest.requirements.agents:
                                forced_agent = skill_manifest.requirements.agents[0]

                # Epic 1, E1-04: fast-chat split. Slash commands, skill
                # invocations and forced agents always take the graph;
                # everything else is classified — plain conversation
                # streams straight from the resolved chat model with no
                # supervisor round-trip, work goes to the graph as before.
                intent = None
                if not slash_cmd_str and not forced_agent and not active_skills:
                    # Finding M19: classification is a real model call
                    # made BEFORE the task pump sets the attribution
                    # context — scope it to the session so its ledger
                    # row is attributed instead of landing unattributed.
                    with usage_context(session_id=session_id):
                        intent = await classify_intent(prompt, provider=provider_instance)

                if intent is not None and intent.is_chat:
                    history = await asyncio.to_thread(
                        load_session_history, session_id, prompt)
                    task_generator = run_fast_chat(
                        prompt,
                        provider_instance,
                        history,
                        request_id=req_id,
                        session_id=session_id,
                        attachments=attachments,
                        # S1-M8: fast chat reads attachment paths only
                        # inside sanctioned roots — the session
                        # attachments dir plus these granted roots.
                        workspace_roots=workspace_roots,
                    )
                else:
                    # One central builder (PRD Q3) — task start, approval
                    # resume and tool list used to build registries
                    # independently and had already drifted apart.
                    from aether_engine.mcp.registry import GLOBAL_MCP_REGISTRY

                    reqs = infer_task_requirements(prompt)
                    tools = build_tool_registry(
                        # E3-F02: the MCP risk overrides are the
                        # task's EFFECTIVE ones (global -> project ->
                        # per-task overlay, resolved above), exactly
                        # as the approval-resume path uses the resumed
                        # task's effective overrides (E1-F04) — not
                        # the current globals, which would discard a
                        # task-level tightening or loosening.
                        mcp_tools=GLOBAL_MCP_REGISTRY.get_mcp_tools(risk_overrides=task_user_config.mcp_tool_risk_overrides),
                        include_file_tools=reqs.wants_code or (requested_model is not None),
                    )

                    # Config layers were resolved above for both paths
                    # (E1-14); engine_state.user_config is never mutated.
                    task_generator = run_langgraph_task(
                        prompt=prompt,
                        provider=provider_instance,
                        tools=tools,
                        request_id=req_id,
                        approval_handler=None,
                        workspace_roots=workspace_roots,
                        disabled_nodes=engine_state.disabled_nodes,
                        session_id=session_id,
                        agent_models=task_user_config.agent_models,
                        agent_tools=task_user_config.agent_tools,
                        secret_store=secret_store,
                        user_config=task_user_config,
                        forced_agent=forced_agent,
                        active_skills=active_skills,
                        plan_mode=bool(msg.payload.get("plan_mode")),
                        attachments=attachments,
                        # N08: the originating user message id — media
                        # the task generates is registered against it
                        # so "jump to conversation" lands exactly.
                        message_id=msg.payload.get("message_id"),
                        # M17 residual: the decision's constraints
                        # travel into the graph run, whose internal
                        # provider constructions read them from the
                        # run config.
                        local_only=decision.local_only,
                        require_vision=bool(attachments),
                    )

                # Epic 5, E5-02: task-level fallback factory (§6.4).
                # Rebuilds THIS task — same path, same registry and
                # config — on the top healthy fallback candidate,
                # reusing the failed task's id so the client sees one
                # task. execute_task_generator only calls it when the
                # failure was retriable and no tool activity occurred.
                # Snapshot the per-message values the factory needs
                # (also keeps the closure from binding loop variables).
                _rebuild = {
                    "prompt": prompt,
                    "use_fast": intent is not None and intent.is_chat,
                    "history": history if (intent is not None and intent.is_chat) else None,
                    "tools": None if (intent is not None and intent.is_chat) else tools,
                    "req_id": req_id,
                    "session_id": session_id,
                    "workspace_roots": workspace_roots,
                    "disabled_nodes": engine_state.disabled_nodes,
                    "agent_models": task_user_config.agent_models,
                    "agent_tools": task_user_config.agent_tools,
                    "user_config": task_user_config,
                    "forced_agent": forced_agent,
                    "active_skills": active_skills,
                    "plan_mode": bool(msg.payload.get("plan_mode")),
                    "attachments": attachments,
                    "message_id": msg.payload.get("message_id"),
                    # Finding M17: the routing decision's constraints
                    # travel with the snapshot so the retry factory
                    # re-checks them instead of re-routing freely.
                    "local_only": decision.local_only,
                    "has_images": bool(attachments),
                }

                async def _task_retry_factory(failed_provider, failed_model, failed_task_id, _ctx=_rebuild):
                    from aether_engine.routing.fallback import (
                        get_configured_fallback_candidates,
                    )

                    # E5-F02: the task's effective fallback policy
                    # governs this enforcement point too — the same
                    # authority function, fed the same effective
                    # config the per-call point was built with. A
                    # disabled fallback or an empty chain-derived
                    # list means no retry (return None), exactly as
                    # an exhausted candidate list always has.
                    candidates = get_configured_fallback_candidates(
                        failed_provider,
                        _ctx["user_config"],
                        configured_providers=engine_state.configured_providers,
                        health_manager=engine_state.health_manager,
                        registry=engine_state.model_registry,
                        local_only=_ctx["local_only"],
                        require_vision=_ctx["has_images"],
                    )
                    if not candidates:
                        return None
                    entry = candidates[0]
                    try:
                        fallback_instance = _create_provider_instance(
                            entry.provider, entry.id, secret_store,
                            local_only=_ctx["local_only"],
                            require_vision=_ctx["has_images"],
                            user_config=_ctx["user_config"],
                        )
                    except Exception:
                        # Provider construction can fail on config/key
                        # errors; then there is no fallback to offer.
                        logger.warning(f"Task fallback: could not build provider for {entry.provider}", exc_info=True)
                        return None
                    if _ctx["use_fast"]:
                        gen = run_fast_chat(
                            _ctx["prompt"], fallback_instance, _ctx["history"],
                            request_id=_ctx["req_id"], session_id=_ctx["session_id"],
                            task_id=failed_task_id, attachments=_ctx.get("attachments"),
                            workspace_roots=_ctx.get("workspace_roots"))
                    else:
                        gen = run_langgraph_task(
                            prompt=_ctx["prompt"],
                            provider=fallback_instance,
                            tools=_ctx["tools"],
                            request_id=_ctx["req_id"],
                            approval_handler=None,
                            workspace_roots=_ctx["workspace_roots"],
                            disabled_nodes=_ctx["disabled_nodes"],
                            session_id=_ctx["session_id"],
                            agent_models=_ctx["agent_models"],
                            agent_tools=_ctx["agent_tools"],
                            secret_store=secret_store,
                            user_config=_ctx["user_config"],
                            forced_agent=_ctx["forced_agent"],
                            active_skills=_ctx["active_skills"],
                            plan_mode=_ctx["plan_mode"],
                            task_id=failed_task_id,
                            attachments=_ctx.get("attachments"),
                            message_id=_ctx.get("message_id"),
                            # M17 residual: as at task start, the
                            # rebuilt run carries the constraints.
                            local_only=_ctx["local_only"],
                            require_vision=_ctx["has_images"],
                        )
                    return gen, entry.provider, entry.id

                task_session_id = session_id
                task_runner = asyncio.create_task(
                    execute_task_generator(
                        task_generator, decision.provider, decision.model, req_id,
                        session_id=session_id,
                        routing_reason=decision.reason,
                        routing_capabilities=decision.capabilities,
                        retry_factory=_task_retry_factory,
                        # Finding M17: on a local-only route, a failure
                        # with no lawful fallback must tell the user
                        # the local vision model is unavailable and
                        # that the image was NOT sent anywhere.
                        fallback_blocked_message=(
                            f"The local vision model ({decision.provider}/{decision.model}) is unavailable, "
                            "so the image was not processed. Because a local vision model is configured, "
                            "Aether did not send the image to a cloud provider. Check that the local "
                            "model is running, or pick a different vision model in Settings → Models."
                        ) if decision.local_only else None)
                )

            elif msg.type == EventType.CANCEL_TASK:
                if task_runner and not task_runner.done():
                    task_runner.cancel()
                    logger.info("Cancellation requested for active task runner.")
                    await ws.send_text(Event(
                        type=EventType.TASK_CANCELLED,
                        request_id=req_id,
                        payload={"task_id": "unknown", "state": TaskState.CANCELLED.value},
                    ).to_json())
                else:
                    await ws.send_text(Event(
                        type=EventType.ERROR,
                        request_id=req_id,
                        payload={"error": "No active task to cancel"},
                    ).to_json())

            elif msg.type == EventType.PING:
                await ws.send_text(Event(
                    type=EventType.PONG,
                    request_id=req_id,
                    payload={"echo_ts": msg.ts},
                ).to_json())

            elif msg.type in (EventType.TOOL_APPROVAL_GRANTED, EventType.TOOL_APPROVAL_REJECTED):
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
                        resume_provider_name, resume_model, secret_store,
                        local_only=resume_local_only,
                        require_vision=resume_has_images,
                    )
                except Exception:  # provider construction can fail on config/key errors; skipping the resume is the designed degradation
                    logger.debug("Could not instantiate provider for approval resume", exc_info=True)
                    continue # Ignore if provider cannot be instantiated

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
                    secret_store=secret_store,
                    user_config=resume_user_config,
                    session_id=resume_session_id,
                    forced_agent=run_cfg.get("forced_agent"),
                    edited_args=edited_args,
                    browser_envelope=browser_envelope,
                    # M17 residual: the resumed run's graph config
                    # carries the same constraints (the executor also
                    # re-derives them from the checkpoint as a net).
                    local_only=resume_local_only,
                    require_vision=resume_has_images,
                )

                if task_runner and not task_runner.done():
                    task_runner.cancel()

                req_id = msg.request_id or "resume"
                task_session_id = approval_key
                task_runner = asyncio.create_task(
                    execute_task_generator(resume_gen, resume_provider_name, resume_model, req_id, session_id=resume_session_id)
                )

            # -----------------------------------------------------------
            # Provider management & health events
            # -----------------------------------------------------------
            elif msg.type == EventType.PROVIDER_LIST_REQUEST:
                providers = await _build_provider_list_async(secret_store)
                cfg = engine_state.user_config
                await ws.send_text(Event(
                    type=EventType.PROVIDER_LIST_RESPONSE,
                    request_id=req_id,
                    payload={"providers": providers, "default_provider": cfg.default_provider, "default_model": cfg.default_model, "health": engine_state.health_manager.get_all_statuses()},
                ).to_json())

            elif msg.type in (EventType.PROVIDER_VALIDATE_REQUEST, EventType.SETTINGS_PROVIDER_VALIDATE_REQUEST):
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
                    saved_key = secret_store.load_provider(pname)
                    if saved_key:
                        key = saved_key
                
                # If base_url is empty, attempt to load the saved base_url
                if not base_url:
                    saved_url = engine_state.user_config.custom_base_urls.get(pname)
                    if saved_url:
                        base_url = saved_url
                
                # Only persist discovered models for providers that are already
                # saved; testing a throwaway/template provider must not leak
                # discovery data into the persisted config.
                is_registered = (
                    pname in set(secret_store.list_providers())
                    or pname in engine_state.user_config.custom_base_urls
                    or pname in engine_state.user_config.custom_provider_types
                )

                if not key and not is_custom:
                    await ws.send_text(Event(
                        type=resp_type,
                        request_id=req_id,
                        payload={"provider": pname, "status": "invalid_key", "message": "API key cannot be empty."},
                    ).to_json())
                else:
                    # Off the message loop (see _send_validation_result):
                    # the Test no longer blocks the socket, and repeat
                    # clicks share the in-flight validation.
                    asyncio.create_task(_send_validation_result(resp_type, req_id, pname, key, base_url))

            elif msg.type == EventType.PROVIDER_SAVE_REQUEST:
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
                    discovery_error = None
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
                            "custom_openai" in set(secret_store.list_providers())
                            or "custom_openai" in engine_state.user_config.custom_base_urls
                            or "custom_openai" in engine_state.user_config.custom_provider_types
                        )
                        if not template_in_use:
                            engine_state.user_config.provider_models.pop("custom_openai", None)
                            engine_state.model_registry.remove_provider_models("custom_openai")
                            engine_state.health_manager.remove_provider("custom_openai")

                    if key:
                        secret_store.save_provider(pname, key)
                        engine_state.health_manager.reset_provider(pname)
                        if not models:
                            # E5-F05: discover through the flavour
                            # just saved above (already persisted to
                            # custom_provider_types and config).
                            disc = await fetch_available_models(
                                pname, key, base_url=base_url,
                                provider_flavor=custom_provider_flavor(
                                    pname, engine_state.user_config))
                            models = disc.models
                            if disc.error is not None:
                                discovery_error = disc.error.to_dict()
                            elif disc.excluded:
                                engine_state.user_config.provider_excluded_models[pname] = disc.excluded
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
                    if discovery_error is not None:
                        save_payload["discovery_error"] = discovery_error
                    await ws.send_text(Event(
                        type=EventType.PROVIDER_SAVE_RESPONSE,
                        request_id=req_id,
                        payload=save_payload,
                    ).to_json())
                except Exception as e:  # noqa: BLE001 — handler boundary: any save failure is reported to the client in the response payload
                    await ws.send_text(Event(
                        type=EventType.PROVIDER_SAVE_RESPONSE,
                        request_id=req_id,
                        payload={
                            "provider": pname,
                            "success": False,
                            "error": f"{type(e).__name__}: {e}",
                        },
                    ).to_json())

            elif msg.type == EventType.PROVIDER_REMOVE_REQUEST:
                pname = msg.payload.get("provider", "")
                deleted = secret_store.delete_provider(pname)
                engine_state.user_config.provider_models.pop(pname, None)
                engine_state.user_config.custom_base_urls.pop(pname, None)
                engine_state.user_config.custom_provider_names.pop(pname, None)
                engine_state.user_config.custom_provider_types.pop(pname, None)
                engine_state.model_registry.remove_provider_models(pname)
                engine_state.health_manager.remove_provider(pname)
                if pname in engine_state.configured_providers:
                    engine_state.configured_providers.remove(pname)
                if engine_state.user_config.default_provider == pname:
                    remaining = [p for p in secret_store.list_providers() if p != pname]
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
                save_config(engine_state.user_config)
                engine_state.audit_logger.log_event("PROVIDER_REMOVED", {"provider": pname})
                await ws.send_text(Event(
                    type=EventType.PROVIDER_REMOVE_RESPONSE,
                    request_id=req_id,
                    payload={"provider": pname, "deleted": deleted, "success": True},
                ).to_json())

            elif msg.type == EventType.PROVIDER_REVEAL_KEY_REQUEST:
                pname = msg.payload.get("provider", "")
                try:
                    decrypted_key = secret_store.load_provider(pname)
                    await ws.send_text(Event(
                        type=EventType.PROVIDER_REVEAL_KEY_RESPONSE,
                        request_id=req_id,
                        payload={"provider": pname, "api_key": decrypted_key, "success": True},
                    ).to_json())
                except Exception as e:  # noqa: BLE001 — handler boundary: any decrypt/load failure is reported to the client in the response payload
                    await ws.send_text(Event(
                        type=EventType.PROVIDER_REVEAL_KEY_RESPONSE,
                        request_id=req_id,
                        payload={"provider": pname, "success": False, "error": str(e)},
                    ).to_json())

            elif msg.type == EventType.MODEL_SET_DEFAULT:
                pname = msg.payload.get("provider", engine_state.user_config.default_provider)
                model = msg.payload.get("model", "")
                if model:
                    engine_state.user_config.default_provider = pname
                    engine_state.user_config.default_model = model
                    engine_state.active_provider = pname
                    engine_state.active_model = model
                    save_config(engine_state.user_config)
                await ws.send_text(Event(
                    type=EventType.MODEL_DEFAULT_CHANGED,
                    request_id=req_id,
                    payload={"provider": pname, "model": engine_state.user_config.default_model},
                ).to_json())

            # -----------------------------------------------------------
            # Session Events
            # -----------------------------------------------------------
            elif msg.type == EventType.SESSION_LIST_REQUEST:
                sessions = await asyncio.to_thread(session_store.list_sessions)
                await ws.send_text(Event(
                    type=EventType.SESSION_LIST_RESPONSE,
                    request_id=req_id,
                    payload={"sessions": sessions},
                ).to_json())

            elif msg.type == EventType.SESSION_GET_REQUEST:
                sess_id = msg.payload.get("session_id")
                session_data = await asyncio.to_thread(session_store.get_session, sess_id) if sess_id else None
                await ws.send_text(Event(
                    type=EventType.SESSION_GET_RESPONSE,
                    request_id=req_id,
                    payload={"session": session_data},
                ).to_json())

            elif msg.type == EventType.SESSION_CREATE_REQUEST:
                title = msg.payload.get("title", "New Chat")
                sess_id = await asyncio.to_thread(session_store.create_session, title)
                await ws.send_text(Event(
                    type=EventType.SESSION_CREATE_RESPONSE,
                    request_id=req_id,
                    payload={"session_id": sess_id},
                ).to_json())

            elif msg.type == EventType.SESSION_DELETE_REQUEST:
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
                await ws.send_text(Event(
                    type=EventType.SESSION_DELETE_RESPONSE,
                    request_id=req_id,
                    payload={"session_id": sess_id, "deleted": deleted},
                ).to_json())

            elif msg.type == EventType.SESSION_TRUNCATE_REQUEST:
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
                await ws.send_text(Event(
                    type=EventType.SESSION_TRUNCATE_RESPONSE,
                    request_id=req_id,
                    payload={"session_id": sess_id, "truncated": truncated},
                ).to_json())

            elif msg.type == EventType.SESSION_RENAME_REQUEST:
                sess_id = msg.payload.get("session_id")
                new_title = (msg.payload.get("title") or "").strip()
                renamed = False
                if sess_id and new_title:
                    renamed = await asyncio.to_thread(
                        session_store.update_session_title, sess_id, new_title[:120])
                await ws.send_text(Event(
                    type=EventType.SESSION_RENAME_RESPONSE,
                    request_id=req_id,
                    payload={"session_id": sess_id, "title": new_title, "renamed": renamed},
                ).to_json())

            elif msg.type == EventType.SESSION_SEARCH_REQUEST:
                query = msg.payload.get("query", "")
                results = await asyncio.to_thread(session_store.search_sessions, query)
                await ws.send_text(Event(
                    type=EventType.SESSION_SEARCH_RESPONSE,
                    request_id=req_id,
                    payload={"query": query, "results": results},
                ).to_json())

            elif msg.type == EventType.SESSION_EXPORT_REQUEST:
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

                await ws.send_text(Event(
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

            elif msg.type == EventType.SESSION_IMPORT_REQUEST:
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

                await ws.send_text(Event(
                    type=EventType.SESSION_IMPORT_RESPONSE,
                    request_id=req_id,
                    payload=result,
                ).to_json())

            elif msg.type == EventType.REWIND_MANIFEST_GET_REQUEST:
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
                await ws.send_text(Event(
                    type=EventType.REWIND_MANIFEST_GET_RESPONSE,
                    request_id=req_id,
                    payload=manifest_payload,
                ).to_json())

            elif msg.type == EventType.TASK_REWIND_REQUEST:
                # Epic 8, E8-1.1b: execute a rewind against the
                # manifest. Refused while this session's task is live
                # or paused for approval — the points describe the
                # last task, and rolling back under a running one
                # would corrupt both. The service validates the rest
                # (last task only, step exists, checkpoint matches).
                sess_id = msg.payload.get("session_id")
                rewind_busy = bool(
                    sess_id
                    and task_runner is not None
                    and not task_runner.done()
                    and (task_session_id is None or task_session_id == sess_id)
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
                await ws.send_text(Event(
                    type=EventType.TASK_REWIND_RESPONSE,
                    request_id=req_id,
                    payload=rewind_result,
                ).to_json())

            elif msg.type == EventType.TASK_REWIND_UNDO_REQUEST:
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
                await ws.send_text(Event(
                    type=EventType.TASK_REWIND_UNDO_RESPONSE,
                    request_id=req_id,
                    payload=undo_result,
                ).to_json())

            elif msg.type == EventType.SESSION_FORK_REQUEST:
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
                    and task_runner is not None
                    and not task_runner.done()
                    and (task_session_id is None or task_session_id == sess_id)
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
                await ws.send_text(Event(
                    type=EventType.SESSION_FORK_RESPONSE,
                    request_id=req_id,
                    payload=fork_result,
                ).to_json())

            elif msg.type == EventType.APPROVAL_GRANTS_REQUEST:
                from aether_engine.tools.approval_cache import get_default_cache

                grants = await asyncio.to_thread(get_default_cache().list_grants)
                await ws.send_text(Event(
                    type=EventType.APPROVAL_GRANTS_RESPONSE,
                    request_id=req_id,
                    payload={"grants": grants},
                ).to_json())

            elif msg.type == EventType.APPROVAL_GRANT_REVOKE_REQUEST:
                from aether_engine.tools.approval_cache import get_default_cache

                grant_id = msg.payload.get("id")
                cache = get_default_cache()
                revoked = bool(grant_id) and await asyncio.to_thread(cache.revoke, int(grant_id))
                if revoked:
                    engine_state.audit_logger.log_event("APPROVAL_GRANT_REVOKED", {"grant_id": grant_id})
                grants = await asyncio.to_thread(cache.list_grants)
                await ws.send_text(Event(
                    type=EventType.APPROVAL_GRANT_REVOKE_RESPONSE,
                    request_id=req_id,
                    payload={"id": grant_id, "revoked": revoked, "grants": grants},
                ).to_json())

            # -----------------------------------------------------------
            # Artifact Store (Epic 8, E8-2.1)
            # -----------------------------------------------------------
            elif msg.type == EventType.ARTIFACT_LIST_REQUEST:
                from aether_engine.artifacts.store import get_artifact_store
                art_store = get_artifact_store()
                sess_id = msg.payload.get("session_id")
                is_global = bool(msg.payload.get("global"))
                limit = msg.payload.get("limit")
                offset = msg.payload.get("offset")
                artifacts, total = await asyncio.to_thread(
                    art_store.list_artifacts, session_id=sess_id, is_global=is_global, limit=limit, offset=offset
                )
                await ws.send_text(Event(
                    type=EventType.ARTIFACT_LIST_RESPONSE,
                    request_id=req_id,
                    payload={"artifacts": artifacts, "total": total},
                ).to_json())

            elif msg.type == EventType.ARTIFACT_GET_REQUEST:
                art_id = msg.payload.get("artifact_id", "")
                result = await handle_artifact_get(art_id)
                await ws.send_text(Event(
                    type=EventType.ARTIFACT_GET_RESPONSE,
                    request_id=req_id,
                    payload=result,
                ).to_json())

            elif msg.type == EventType.DEV_SERVER_DETECT_REQUEST:
                # Epic 8, E8-2.2: probe for a running dev server so the
                # canvas Preview tab can load it in its sandboxed frame.
                from aether_engine.artifacts.dev_server import detect_dev_server

                result = await asyncio.to_thread(
                    detect_dev_server,
                    workspace_root=msg.payload.get("workspace_root"),
                    project_path=msg.payload.get("project_path"),
                )
                await ws.send_text(Event(
                    type=EventType.DEV_SERVER_DETECT_RESPONSE,
                    request_id=req_id,
                    payload=result,
                ).to_json())

            elif msg.type == EventType.USAGE_LEDGER_QUERY_REQUEST:
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
                await ws.send_text(Event(
                    type=EventType.USAGE_LEDGER_QUERY_RESPONSE,
                    request_id=req_id,
                    payload=result,
                ).to_json())

            elif msg.type == EventType.CONTEXT_BREAKDOWN_REQUEST:
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
                await ws.send_text(Event(
                    type=EventType.CONTEXT_BREAKDOWN_RESPONSE,
                    request_id=req_id,
                    payload=result,
                ).to_json())

            elif msg.type == EventType.DAILY_BUDGET_SET_REQUEST:
                # Epic 8, E8-A8.1b: daily budget update with soft alert
                from aether_engine.cost.ledger import get_usage_ledger

                raw_budget = msg.payload.get("daily_budget_usd")
                budget = float(raw_budget) if raw_budget is not None else None
                if budget is not None and budget <= 0:
                    budget = None
                engine_state.user_config.daily_budget_usd = budget
                save_config(engine_state.user_config)

                await ws.send_text(Event(
                    type=EventType.DAILY_BUDGET_SET_RESPONSE,
                    request_id=req_id,
                    payload={"daily_budget_usd": budget, "ok": True},
                ).to_json())

                if budget is not None and budget > 0:
                    today_spend = get_usage_ledger().today_total()
                    if today_spend > budget:
                        await ws.send_text(Event(
                            type=EventType.DAILY_BUDGET_EXCEEDED,
                            payload={
                                "daily_budget_usd": float(budget),
                                "today_spend_usd": float(today_spend),
                            },
                        ).to_json())

            elif msg.type == EventType.BROWSER_TASK_PAUSE_REQUEST:
                # Epic 8, E8-6.1: browser control channel — pause /
                # resume / take-control / return-control / stop for
                # the dedicated agent browser (browser task card).
                from aether_engine.browser.session import get_browser_manager

                browser_result = await get_browser_manager().control(
                    session_id=msg.payload.get("session_id"),
                    chat_session_id=msg.payload.get("chat_session_id"),
                    action=str(msg.payload.get("action", "")),
                )
                await ws.send_text(Event(
                    type=EventType.BROWSER_TASK_PAUSE_RESPONSE,
                    request_id=req_id,
                    payload=browser_result,
                ).to_json())

            elif msg.type == EventType.BROWSER_POLICY_GET_REQUEST:
                # Epic 8, E8-6.2: Settings → Browser Agent Policy reads
                # the confirmation policy (categories + toggles).
                from aether_engine.browser.policy import policy_from_user_config

                await ws.send_text(Event(
                    type=EventType.BROWSER_POLICY_GET_RESPONSE,
                    request_id=req_id,
                    payload=policy_from_user_config(engine_state.user_config).to_dict(),
                ).to_json())

            elif msg.type == EventType.BROWSER_POLICY_SET_REQUEST:
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
                    save_config(engine_state.user_config)
                    changed = sorted(
                        key for key in ("enabled", "vault_autofill", "envelopes_enabled")
                        if msg.payload.get(key) is not None
                    )
                    if isinstance(msg.payload.get("categories"), dict):
                        changed += [f"categories.{k}" for k in msg.payload["categories"]]
                    engine_state.audit_logger.log_event(
                        "BROWSER_POLICY_UPDATED", {"fields": changed})
                else:
                    response_payload["error"] = error
                await ws.send_text(Event(
                    type=EventType.BROWSER_POLICY_SET_RESPONSE,
                    request_id=req_id,
                    payload=response_payload,
                ).to_json())

            elif msg.type == EventType.VAULT_ENTRY_LIST_REQUEST:
                # Epic 8, E8-6.3: Settings → Password Vault lists
                # entries METADATA ONLY (the store's list never
                # selects a secret column, so no payload can carry a
                # password). Agent-created entries appear here for
                # the owner's review like any other.
                from aether_engine.vault.store import get_credential_vault

                entries = await asyncio.to_thread(
                    get_credential_vault().list_entries)
                await ws.send_text(Event(
                    type=EventType.VAULT_ENTRY_LIST_RESPONSE,
                    request_id=req_id,
                    payload={"entries": entries},
                ).to_json())

            elif msg.type == EventType.VAULT_ENTRY_SAVE_REQUEST:
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
                await ws.send_text(Event(
                    type=EventType.VAULT_ENTRY_SAVE_RESPONSE,
                    request_id=req_id,
                    payload=vault_payload,
                ).to_json())

            elif msg.type == EventType.VAULT_ENTRY_DELETE_REQUEST:
                from aether_engine.vault.store import get_credential_vault

                entry_id = str(msg.payload.get("id", ""))
                removed = await asyncio.to_thread(
                    get_credential_vault().delete_entry, entry_id)
                if removed:
                    engine_state.audit_logger.log_event(
                        "VAULT_ENTRY_DELETED", {"entry_id": entry_id})
                await ws.send_text(Event(
                    type=EventType.VAULT_ENTRY_DELETE_RESPONSE,
                    request_id=req_id,
                    payload={"success": removed, "id": entry_id},
                ).to_json())

            elif msg.type == EventType.VAULT_TOTP_LIST_REQUEST:
                # Open Point 14: Settings → Password Vault's
                # Authenticator section lists standalone TOTP
                # entries METADATA ONLY (the store's listing never
                # selects the secret column).
                from aether_engine.vault.store import get_credential_vault

                totp_entries = await asyncio.to_thread(
                    get_credential_vault().list_totp_entries)
                await ws.send_text(Event(
                    type=EventType.VAULT_TOTP_LIST_RESPONSE,
                    request_id=req_id,
                    payload={"entries": totp_entries},
                ).to_json())

            elif msg.type == EventType.VAULT_TOTP_SAVE_REQUEST:
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
                await ws.send_text(Event(
                    type=EventType.VAULT_TOTP_SAVE_RESPONSE,
                    request_id=req_id,
                    payload=totp_payload,
                ).to_json())

            elif msg.type == EventType.VAULT_TOTP_DELETE_REQUEST:
                from aether_engine.vault.store import get_credential_vault

                totp_id = str(msg.payload.get("id", ""))
                removed = await asyncio.to_thread(
                    get_credential_vault().delete_totp_entry, totp_id)
                if removed:
                    engine_state.audit_logger.log_event(
                        "VAULT_TOTP_DELETED", {"entry_id": totp_id})
                await ws.send_text(Event(
                    type=EventType.VAULT_TOTP_DELETE_RESPONSE,
                    request_id=req_id,
                    payload={"success": removed, "id": totp_id},
                ).to_json())

            elif msg.type == EventType.VAULT_TOTP_CODES_REQUEST:
                # The ONLY code-carrying payload in the system,
                # served on the owner's Settings request. Secrets
                # never leave the engine: the store computes the
                # codes in-process from the decrypted secrets, and
                # each entry's read is audited VAULT_TOTP_ACCESSED
                # by the owner accessors there.
                from aether_engine.vault.store import get_credential_vault

                codes_payload = await asyncio.to_thread(
                    get_credential_vault().totp_codes)
                await ws.send_text(Event(
                    type=EventType.VAULT_TOTP_CODES_RESPONSE,
                    request_id=req_id,
                    payload=codes_payload,
                ).to_json())

            elif msg.type == EventType.VAULT_FILL_APPROVAL_RESPONSE:
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
            elif msg.type == EventType.MEDIA_LIST_REQUEST:
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
                await ws.send_text(Event(
                    type=EventType.MEDIA_LIST_RESPONSE,
                    request_id=req_id,
                    payload={"media": media_items, "total": total},
                ).to_json())

            elif msg.type == EventType.MEDIA_SEARCH_REQUEST:
                # N07: Library search is served by the backend recall
                # path (the same search_media_entries the media_recall
                # tool uses) so transcript and semantic matches reach
                # the UI. An empty query is an honest empty result —
                # recall is never invoked with nothing to search for.
                from aether_engine.tools.media_recall import search_media_entries
                search_query = str(msg.payload.get("query") or "").strip()
                search_collection = msg.payload.get("collection", "all")
                if search_collection not in ("all", "user", "generated"):
                    search_collection = "all"
                try:
                    search_limit = int(msg.payload.get("limit") or 20)
                except (TypeError, ValueError):
                    search_limit = 20
                if search_query:
                    recall = await search_media_entries(
                        search_query,
                        limit=search_limit,
                        collection=search_collection,
                    )
                    search_results = recall["results"]
                    search_degraded = bool(recall["degraded"])
                else:
                    search_results = []
                    search_degraded = False
                await ws.send_text(Event(
                    type=EventType.MEDIA_SEARCH_RESPONSE,
                    request_id=req_id,
                    payload={
                        "query": search_query,
                        "media": search_results,
                        "degraded": search_degraded,
                    },
                ).to_json())

            elif msg.type == EventType.MEDIA_DELETE_REQUEST:
                from aether_engine.media.store import get_media_store
                media_store = get_media_store()
                mid = msg.payload.get("media_id", "")
                del_file = bool(msg.payload.get("delete_underlying_file", False))
                deleted, file_deleted = await asyncio.to_thread(
                    media_store.delete_media, mid, delete_underlying_file=del_file
                )
                await ws.send_text(Event(
                    type=EventType.MEDIA_DELETE_RESPONSE,
                    request_id=req_id,
                    payload={"deleted": deleted, "media_id": mid, "file_deleted": file_deleted},
                ).to_json())

            elif msg.type == EventType.MEDIA_METADATA_UPDATE_REQUEST:
                from aether_engine.media.store import get_media_store
                media_store = get_media_store()
                mid = msg.payload.get("media_id", "")
                tags = msg.payload.get("tags")
                notes = msg.payload.get("notes")
                updated = await asyncio.to_thread(
                    media_store.update_metadata, mid, tags=tags, notes=notes
                )
                await ws.send_text(Event(
                    type=EventType.MEDIA_METADATA_UPDATE_RESPONSE,
                    request_id=req_id,
                    payload=updated,
                ).to_json())

            # -----------------------------------------------------------
            # Local Models (Ollama) & Task Download Events
            # -----------------------------------------------------------
            elif msg.type == EventType.LOCAL_MODEL_LIST_REQUEST:
                models = await engine_state.ollama_manager.list_models()
                await ws.send_text(Event(
                    type=EventType.LOCAL_MODEL_LIST_RESPONSE,
                    request_id=req_id,
                    payload={"models": models},
                ).to_json())

            elif msg.type == EventType.LOCAL_MODEL_DOWNLOAD_START:
                model_name = msg.payload.get("model", "") or msg.payload.get("repo_id", "")
                dest_dir = msg.payload.get("destination_dir")
                if task_runner and not task_runner.done():
                    task_runner.cancel()

                async def execute_local_download(model_name=model_name, destination_dir=dest_dir, request_id=req_id):
                    async for dl_ev in engine_state.ollama_manager.download_model_task(
                        model_name=model_name,
                        destination_dir=destination_dir,
                        request_id=request_id,
                    ):
                        await ws.send_text(dl_ev.to_json())

                dl_task = asyncio.create_task(execute_local_download())
                active_downloads[model_name] = dl_task
                task_runner = dl_task

            elif msg.type == EventType.LOCAL_MODEL_DOWNLOAD_CANCEL:
                cancel_model = msg.payload.get("model", "")
                dl = active_downloads.pop(cancel_model, None)
                if dl and not dl.done():
                    dl.cancel()
                    logger.info(f"Download cancelled for model: {cancel_model}")

            elif msg.type == EventType.LOCAL_MODEL_DELETE_REQUEST:
                model_name = msg.payload.get("model", "")
                deleted = await engine_state.ollama_manager.delete_model(model_name)
                await ws.send_text(Event(
                    type=EventType.LOCAL_MODEL_DELETE_RESPONSE,
                    request_id=req_id,
                    payload={"model": model_name, "deleted": deleted},
                ).to_json())

            # -----------------------------------------------------------
            # Capabilities Check Scheduler Events
            # -----------------------------------------------------------
            elif msg.type == EventType.CAPABILITY_CHECK_SET_SCHEDULE:
                interval = msg.payload.get("schedule", "weekly")
                method = msg.payload.get("method", "Both")
                engine_state.scheduler_manager.set_schedule(interval, method)
                status_sum = engine_state.scheduler_manager.get_status_summary()
                await ws.send_text(Event(
                    type=EventType.CAPABILITY_CHECK_HISTORY_RESPONSE,
                    request_id=req_id,
                    payload={
                        "summary": status_sum,
                        "history": engine_state.scheduler_manager.get_history(),
                    },
                ).to_json())

            elif msg.type == EventType.CAPABILITY_CHECK_RUN_NOW:
                method = msg.payload.get("method")
                run_res = await engine_state.scheduler_manager.run_now(method=method)
                status_sum = engine_state.scheduler_manager.get_status_summary()
                await ws.send_text(Event(
                    type=EventType.CAPABILITY_CHECK_HISTORY_RESPONSE,
                    request_id=req_id,
                    payload={
                        "latest_run": run_res,
                        "summary": status_sum,
                        "history": engine_state.scheduler_manager.get_history(),
                    },
                ).to_json())

            elif msg.type == EventType.CAPABILITY_CHECK_GET_HISTORY:
                status_sum = engine_state.scheduler_manager.get_status_summary()
                history = engine_state.scheduler_manager.get_history()
                await ws.send_text(Event(
                    type=EventType.CAPABILITY_CHECK_HISTORY_RESPONSE,
                    request_id=req_id,
                    payload={"summary": status_sum, "history": history},
                ).to_json())

            elif msg.type == EventType.ENVIRONMENT_DIAGNOSTIC_RUN_REQUEST:
                diag_logs = run_environment_diagnostics()
                # Determine overall summary status based on logs
                failed_count = sum(1 for log in diag_logs if log["status"] == "FAILED")
                await ws.send_text(Event(
                    type=EventType.ENVIRONMENT_DIAGNOSTIC_RESPONSE,
                    request_id=req_id,
                    payload={
                        "summary": {"failed_count": failed_count},
                        "history": diag_logs,
                    },
                ).to_json())

            elif msg.type == EventType.REFRESH_MODELS_REQUEST:
                target_provider = msg.payload.get("provider")
                stored = set(secret_store.list_providers())
                cfg = engine_state.user_config
                
                providers_to_refresh = set(stored)
                for p in cfg.custom_provider_types:
                    if cfg.custom_provider_types[p] == "openai_compatible":
                        providers_to_refresh.add(p)
                providers_to_refresh.add("custom_openai")
                
                if target_provider:
                    providers_to_refresh = [target_provider]
                else:
                    providers_to_refresh = list(providers_to_refresh)

                refreshed_models: dict[str, list[str]] = {}

                for pname in providers_to_refresh:
                    try:
                        payload_key = msg.payload.get("api_key")
                        payload_url = msg.payload.get("base_url")
                        
                        if payload_key is not None:
                            key = payload_key
                        else:
                            key = secret_store.load_provider(pname) if pname in stored else ""
                            
                        if payload_url is not None:
                            base_url = payload_url
                        else:
                            base_url = cfg.custom_base_urls.get(pname)

                        is_custom = pname == "custom_openai" or cfg.custom_provider_types.get(pname) == "openai_compatible"
                        if key or is_custom:
                            disc = await fetch_available_models(
                                provider_name=pname,
                                api_key=key,
                                base_url=base_url,
                                filter_reachability=True,
                                force_reachability=True,
                                provider_flavor=custom_provider_flavor(pname, cfg),
                            )
                            # Do not persist discovery data for unsaved/template
                            # providers; models still flow back to the UI response.
                            if disc.models or disc.excluded or disc.error:
                                refreshed_models[pname] = disc
                            is_registered = (
                                pname in stored
                                or pname in cfg.custom_base_urls
                                or pname in cfg.custom_provider_types
                            )
                            # E5-F14: a targeted refresh of a REGISTERED
                            # (saved) provider applies and persists
                            # exactly like the full refresh — the old
                            # "manual click should not save" gate left
                            # config/registry/disk holding the stale
                            # catalog while the response showed the new
                            # one, so the refresh evaporated on the next
                            # listing/restart. Unsaved templates stay
                            # unpersisted via is_registered above.
                            if disc.models and is_registered:
                                cfg.provider_models[pname] = disc.models
                                cfg.provider_excluded_models[pname] = disc.excluded
                                engine_state.model_registry.update_provider_models(pname, disc.models)
                                save_config(cfg)
                    except Exception as ref_err:  # noqa: BLE001 — per-provider isolation: one provider's refresh failure must not abort the refresh for the rest
                        logger.error(f"Error during refresh models for {pname}: {ref_err}")

                updated_providers = await _build_provider_list_async(secret_store)
                # Surface freshly discovered models for the target provider even if
                # it is an unsaved template (whose data we intentionally don't persist).
                for prov in updated_providers:
                    disc = refreshed_models.get(prov.get("name"))
                    if disc is not None:
                        prov["models"] = disc.models
                        prov["excluded_models"] = disc.excluded
                        if disc.error is not None:
                            prov["discovery_error"] = disc.error.to_dict()
                await ws.send_text(Event(
                    type=EventType.REFRESH_MODELS_RESPONSE,
                    request_id=req_id,
                    payload={
                        "providers": updated_providers,
                        "default_provider": cfg.default_provider,
                        "default_model": cfg.default_model,
                    },
                ).to_json())

            # -----------------------------------------------------------
            # Tools, MCP, Plugins, Memory Events
            # -----------------------------------------------------------
            elif msg.type == EventType.TOOL_LIST_REQUEST:
                from aether_engine.langgraph.nodes.supervisor import (
                    SUPERVISOR_MEMORY_TOOLS,
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

                # Add Supervisor Memory Tools
                for mt in SUPERVISOR_MEMORY_TOOLS:
                    d = mt.copy()
                    name = d["function"]["name"]
                    risk = KNOWN_NON_REGISTRY_TOOLS.get(name, ToolRisk.EXECUTE)
                    defs.append(enrich_tool(d, name, risk))

                await ws.send_text(Event(
                    type=EventType.TOOL_LIST_RESPONSE,
                    request_id=req_id,
                    payload={"tools": defs, "approval_mode": engine_state.user_config.approval_mode},
                ).to_json())

            elif msg.type == EventType.AGENT_LIST_REQUEST:
                agents = [
                    {"id": "supervisor", "name": "Supervisor", "description": "Routes tasks and queries memory."},
                    {"id": "planner", "name": "Planner Agent", "description": "Breaks goals into structured steps."},
                    {"id": "coder", "name": "Coder Agent", "description": "Writes, tests, and debugs code."},
                    {"id": "researcher", "name": "Researcher Agent", "description": "Gathers information and synthesizes findings."}
                ]
                
                for agent in agents:
                    agent["model"] = engine_state.user_config.agent_models.get(agent["id"], "inherit")
                    agent["tools"] = engine_state.user_config.agent_tools.get(agent["id"])
                
                await ws.send_text(Event(
                    type=EventType.AGENT_LIST_RESPONSE,
                    request_id=req_id,
                    payload={"agents": agents},
                ).to_json())

            elif msg.type == EventType.AGENT_UPDATE_REQUEST:
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
                            except Exception:  # noqa: BLE001, S110 — best-effort enrichment: a registry listing failure must not block validation
                                pass
                            if _provider not in _known_providers:
                                _error = f"unknown provider: {_provider}"

                if _error is not None:
                    await ws.send_text(Event(
                        type=EventType.AGENT_UPDATE_RESPONSE,
                        request_id=req_id,
                        payload={"success": False, "agent_id": agent_id, "error": _error},
                    ).to_json())
                else:
                    if model is not None:
                        engine_state.user_config.agent_models[agent_id] = model
                    if tools is not None:
                        engine_state.user_config.agent_tools[agent_id] = tools
                    save_config(engine_state.user_config)

                    await ws.send_text(Event(
                        type=EventType.AGENT_UPDATE_RESPONSE,
                        request_id=req_id,
                        payload={"success": True, "agent_id": agent_id},
                    ).to_json())

            elif msg.type == EventType.TOOL_POLICY_SET_REQUEST:
                tool_name = msg.payload.get("tool_name")
                policy = msg.payload.get("policy")
                response_payload = {"success": True, "tool_name": tool_name, "policy": policy}
                
                if tool_name and policy:
                    from aether_engine.audit import AuditLogger
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
                        AuditLogger().log_event("TOOL_POLICY_IGNORED", {"tool_name": tool_name, "requested": "Always Allow", "reason": "Risky tool"})

                    engine_state.user_config.tool_policies[tool_name] = policy
                    save_config(engine_state.user_config)
                    
                await ws.send_text(Event(
                    type=EventType.TOOL_POLICY_SET_RESPONSE,
                    request_id=req_id,
                    payload=response_payload,
                ).to_json())

            elif msg.type == EventType.APPROVAL_MODE_SET_REQUEST:
                # Epic 2, E2-08: approval mode selector in Settings.
                mode = msg.payload.get("mode")
                success = mode in ("ask", "write")
                if success:
                    engine_state.user_config.approval_mode = mode
                    save_config(engine_state.user_config)
                    engine_state.audit_logger.log_event("APPROVAL_MODE_SET", {"mode": mode})
                await ws.send_text(Event(
                    type=EventType.APPROVAL_MODE_SET_RESPONSE,
                    request_id=req_id,
                    payload={"success": success, "mode": engine_state.user_config.approval_mode},
                ).to_json())

            elif msg.type == EventType.MCP_SERVER_LIST_REQUEST:
                from aether_engine.mcp.registry import GLOBAL_MCP_REGISTRY
                servers = GLOBAL_MCP_REGISTRY.list_servers_with_status()
                await ws.send_text(Event(
                    type=EventType.MCP_SERVER_LIST_RESPONSE,
                    request_id=req_id,
                    payload={
                        "servers": servers,
                        "config_error": getattr(GLOBAL_MCP_REGISTRY, "config_load_error", None),
                    },
                ).to_json())

            elif msg.type == EventType.CONFIG_GET_REQUEST:
                await ws.send_text(Event(
                    type=EventType.CONFIG_GET_RESPONSE,
                    request_id=req_id,
                    payload={"config": engine_state.user_config.to_dict()},
                ).to_json())

            elif msg.type == EventType.CONFIG_UPDATE_REQUEST:
                config_data = msg.payload.get("config", {})
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
                    await ws.send_text(Event(
                        type=EventType.CONFIG_UPDATE_RESPONSE,
                        request_id=req_id,
                        payload={"success": False, "error": handed_off_error},
                    ).to_json())
                    continue
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
                    await ws.send_text(Event(
                        type=EventType.CONFIG_UPDATE_RESPONSE,
                        request_id=req_id,
                        payload={
                            "success": False,
                            "error": f"Settings could not be saved: {exc}",
                        },
                    ).to_json())
                    continue
                await ws.send_text(Event(
                    type=EventType.CONFIG_UPDATE_RESPONSE,
                    request_id=req_id,
                    payload={"success": True, "config": engine_state.user_config.to_dict()},
                ).to_json())

            elif msg.type == EventType.CONFIG_RESET_REQUEST:
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
                    await ws.send_text(Event(
                        type=EventType.CONFIG_RESET_RESPONSE,
                        request_id=req_id,
                        payload={
                            "success": False,
                            "error": f"Settings reset could not be saved: {exc}",
                        },
                    ).to_json())
                    continue
                # E6-F04: the live fields that mirror the config
                # (active provider/model, configured providers, skill
                # roots) are independent state — re-derive them from
                # the reset config exactly as initialize() does, or
                # HELLO and the skill catalogue keep serving the
                # pre-reset world until a restart.
                engine_state.resync_active_state()
                engine_state.audit_logger.log_event("CONFIG_RESET", {})
                await ws.send_text(Event(
                    type=EventType.CONFIG_RESET_RESPONSE,
                    request_id=req_id,
                    payload={"success": True, "config": engine_state.user_config.to_dict()},
                ).to_json())

            elif msg.type == EventType.AUXILIARY_MODEL_SET_REQUEST:
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
                    save_config(engine_state.user_config)
                    engine_state.audit_logger.log_event("AUXILIARY_MODEL_SET", {
                        "task": task, "provider": provider, "model": model,
                    })
                stale = [t for t in engine_state.user_config.auxiliary_models
                         if t not in AUXILIARY_TASK_CLASSES]
                if stale:
                    for t in stale:
                        engine_state.user_config.auxiliary_models.pop(t, None)
                    save_config(engine_state.user_config)
                    engine_state.audit_logger.log_event("AUXILIARY_MODEL_PRUNED", {
                        "tasks": stale,
                        "reason": "task class has no engine call site",
                    })
                await ws.send_text(Event(
                    type=EventType.AUXILIARY_MODEL_SET_RESPONSE,
                    request_id=req_id,
                    payload={
                        "success": ok,
                        "error": error,
                        "auxiliary_models": dict(engine_state.user_config.auxiliary_models),
                    },
                ).to_json())

            elif msg.type == EventType.REASONING_EFFORT_SET_REQUEST:
                # Phase 9, B2: persist the user's reasoning-effort preference so the
                # engine (not just localStorage) can act on it.
                effort = msg.payload.get("effort", "")
                ok, error = True, ""
                if effort not in ("standard", "medium", "high"):
                    ok, error = False, f"Invalid reasoning effort '{effort}'."
                if ok:
                    engine_state.user_config.reasoning_effort = effort
                    save_config(engine_state.user_config)
                    engine_state.audit_logger.log_event("REASONING_EFFORT_SET", {"effort": effort})
                await ws.send_text(Event(
                    type=EventType.REASONING_EFFORT_SET_RESPONSE,
                    request_id=req_id,
                    payload={"success": ok, "error": error, "effort": engine_state.user_config.reasoning_effort},
                ).to_json())

            elif msg.type == EventType.MCP_SERVER_ADD_REQUEST:
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
                await ws.send_text(Event(
                    type=EventType.MCP_SERVER_ADD_RESPONSE,
                    request_id=req_id,
                    # S1: never echo header/env values back to the UI.
                    payload={"success": ok, "name": name, "config": redact_mcp_config_for_ui(config), "error": error},
                ).to_json())

            elif msg.type == EventType.MCP_SERVER_REMOVE_REQUEST:
                from aether_engine.mcp.registry import (
                    GLOBAL_MCP_REGISTRY,
                    MCPConfigError,
                )
                name = msg.payload.get("name")
                ok, error = True, ""
                if name:
                    # Issue 011 (S5): wrap like ADD/UPDATE — remove_server
                    # raises MCPConfigError via _save_config when the
                    # config file failed to load (corrupt), and without
                    # this the outer handler ends the websocket session
                    # with no response. A failed remove answers
                    # success:false + error; the session stays alive.
                    try:
                        # remove_server closes any live session inside the
                        # removal (S5), awaited here in the session-owner task.
                        await GLOBAL_MCP_REGISTRY.remove_server(name)
                    except (MCPConfigError, OSError) as e:
                        # OSError: the config save's storage failure
                        # (E3-F04) — same answer-don't-die rule as above.
                        ok, error = False, str(e)
                await ws.send_text(Event(
                    type=EventType.MCP_SERVER_REMOVE_RESPONSE,
                    request_id=req_id,
                    payload={"success": ok, "name": name, "error": error},
                ).to_json())

            elif msg.type == EventType.MCP_SERVER_UPDATE_REQUEST:
                from aether_engine.mcp.registry import (
                    GLOBAL_MCP_REGISTRY,
                    MCPConfigError,
                )
                name = msg.payload.get("name")
                ok, error, config = True, "", None
                if not name:
                    ok, error = False, "Server name is required."
                else:
                    try:
                        config = await GLOBAL_MCP_REGISTRY.update_server(
                            name,
                            auto_connect=msg.payload.get("auto_connect"),
                            enabled=msg.payload.get("enabled"),
                        )
                    except (MCPConfigError, OSError) as e:
                        # OSError: the config save's storage failure
                        # (E3-F04) — same answer-don't-die rule as ADD.
                        ok, error = False, str(e)
                await ws.send_text(Event(
                    type=EventType.MCP_SERVER_UPDATE_RESPONSE,
                    request_id=req_id,
                    payload={"success": ok, "name": name, "config": config, "error": error},
                ).to_json())

            elif msg.type == EventType.MCP_SERVER_CONNECT_REQUEST:
                from aether_engine.mcp.registry import GLOBAL_MCP_REGISTRY
                name = msg.payload.get("name")
                result = (
                    await GLOBAL_MCP_REGISTRY.connect_server(name)
                    if name
                    else {"ok": False, "error": "Server name is required."}
                )
                server_status = GLOBAL_MCP_REGISTRY.get_server_status(name) if name else {}
                await ws.send_text(Event(
                    type=EventType.MCP_SERVER_CONNECT_RESPONSE,
                    request_id=req_id,
                    payload={
                        "success": result["ok"], "name": name, "error": result.get("error", ""),
                        "status": server_status.get("status"), "tool_count": server_status.get("tool_count", 0),
                    },
                ).to_json())

            elif msg.type == EventType.MCP_SERVER_DISCONNECT_REQUEST:
                from aether_engine.mcp.registry import GLOBAL_MCP_REGISTRY
                name = msg.payload.get("name")
                result = (
                    await GLOBAL_MCP_REGISTRY.disconnect_server(name)
                    if name
                    else {"ok": False, "error": "Server name is required."}
                )
                server_status = GLOBAL_MCP_REGISTRY.get_server_status(name) if name else {}
                await ws.send_text(Event(
                    type=EventType.MCP_SERVER_DISCONNECT_RESPONSE,
                    request_id=req_id,
                    payload={
                        "success": result["ok"], "name": name, "error": result.get("error", ""),
                        "status": server_status.get("status"), "tool_count": server_status.get("tool_count", 0),
                    },
                ).to_json())

            elif msg.type == EventType.MCP_SERVER_TEST_REQUEST:
                from aether_engine.mcp.registry import GLOBAL_MCP_REGISTRY
                name = msg.payload.get("name")
                result = (
                    await GLOBAL_MCP_REGISTRY.test_connection(name)
                    if name
                    else {"ok": False, "error": "Server name is required.", "tools": []}
                )
                await ws.send_text(Event(
                    type=EventType.MCP_SERVER_TEST_RESPONSE,
                    request_id=req_id,
                    payload={
                        "success": result["ok"],
                        "name": name,
                        "error": result.get("error", ""),
                        "tools": result.get("tools", []),
                    },
                ).to_json())

            elif msg.type == EventType.MCP_RESOURCE_LIST_REQUEST:
                # Issue 006: list a connected server's resources.
                from aether_engine.mcp.registry import GLOBAL_MCP_REGISTRY
                name = msg.payload.get("name")
                resources, ok, error = [], True, ""
                if not name:
                    ok, error = False, "Server name is required."
                else:
                    try:
                        resources = await GLOBAL_MCP_REGISTRY.list_resources(name)
                    except Exception as e:  # noqa: BLE001 — MCP client calls raise arbitrary server/transport errors; surfaced in the response payload
                        ok, error = False, str(e)
                await ws.send_text(Event(
                    type=EventType.MCP_RESOURCE_LIST_RESPONSE,
                    request_id=req_id,
                    payload={"success": ok, "name": name, "resources": resources, "error": error},
                ).to_json())

            elif msg.type == EventType.MCP_RESOURCE_READ_REQUEST:
                # Issue 006: read a resource and load it into the current
                # chat as a context message. Role "system": it is context,
                # not something the user said — and the supervisor's
                # get_current_chat_history briefing reads it back to the
                # model on the next turn. If no session was given (or it
                # no longer exists), create one so the resource is never
                # lost. The UI also gets the message live in this response
                # (App.jsx appends it) — the Settings panel alone cannot
                # update the chat view.
                from aether_engine.mcp.registry import GLOBAL_MCP_REGISTRY
                name = msg.payload.get("name")
                uri = msg.payload.get("uri")
                ok, error, content, session_id = True, "", "", msg.payload.get("session_id")
                if not name or not uri:
                    ok, error = False, "Server name and resource URI are required."
                else:
                    try:
                        content = await GLOBAL_MCP_REGISTRY.read_resource(name, uri)
                    except Exception as e:  # noqa: BLE001 — MCP client calls raise arbitrary server/transport errors; surfaced in the response payload
                        ok, error = False, str(e)
                message = ""
                truncated = content.endswith("...[resource truncated to 20000 chars]")
                if ok:
                    # A chat-save failure must return an error, never kill
                    # the websocket (an exception here used to take the
                    # whole connection down with it).
                    try:
                        if session_id:
                            sess = await asyncio.to_thread(session_store.get_session, session_id)
                            if sess is None:
                                session_id = None
                        if not session_id:
                            session_id = await asyncio.to_thread(
                                session_store.create_session, f"MCP resource: {uri}"
                            )
                        message = f"[MCP resource from server '{name}': {uri}]\n{content}"
                        await asyncio.to_thread(
                            session_store.add_message, session_id, "system", message
                        )
                    except Exception as e:  # noqa: BLE001 — chat-persistence boundary: a save failure becomes an error response, never a dead websocket
                        logger.error(f"Failed to save MCP resource to chat: {e}")
                        ok, error = False, f"Failed to save resource to chat: {e}"
                        message = ""
                    if ok:
                        engine_state.audit_logger.log_event("MCP_RESOURCE_READ", {
                            "server": name, "uri": uri, "session_id": session_id,
                            "truncated": truncated,
                        })
                await ws.send_text(Event(
                    type=EventType.MCP_RESOURCE_READ_RESPONSE,
                    request_id=req_id,
                    payload={
                        "success": ok, "name": name, "uri": uri, "content": content,
                        "message": message, "truncated": truncated,
                        "session_id": session_id, "error": error,
                    },
                ).to_json())

            elif msg.type == EventType.MCP_PROMPT_LIST_REQUEST:
                # Issue 007: list a connected server's prompt templates.
                from aether_engine.mcp.registry import GLOBAL_MCP_REGISTRY
                name = msg.payload.get("name")
                prompts, ok, error = [], True, ""
                if not name:
                    ok, error = False, "Server name is required."
                else:
                    try:
                        prompts = await GLOBAL_MCP_REGISTRY.list_prompts(name)
                    except Exception as e:  # noqa: BLE001 — MCP client calls raise arbitrary server/transport errors; surfaced in the response payload
                        ok, error = False, str(e)
                await ws.send_text(Event(
                    type=EventType.MCP_PROMPT_LIST_RESPONSE,
                    request_id=req_id,
                    payload={"success": ok, "name": name, "prompts": prompts, "error": error},
                ).to_json())

            elif msg.type == EventType.MCP_PROMPT_RUN_REQUEST:
                # Issue 007: run a prompt template and insert its messages
                # into the current chat (same pattern as resources in
                # 006, but with the prompt's own roles). Never auto-run:
                # only an explicit Run / slash command reaches here.
                name = msg.payload.get("name")
                prompt_name = msg.payload.get("prompt")
                arguments = msg.payload.get("arguments")
                if arguments is not None and not isinstance(arguments, dict):
                    await ws.send_text(Event(
                        type=EventType.MCP_PROMPT_RUN_RESPONSE,
                        request_id=req_id,
                        payload={
                            "success": False, "name": name, "prompt": prompt_name,
                            "messages": [], "session_id": msg.payload.get("session_id"),
                            "error": "MCP prompt arguments must be an object of string values.",
                        },
                    ).to_json())
                else:
                    result = await _run_mcp_prompt_into_chat(
                        name, prompt_name, arguments, msg.payload.get("session_id")
                    )
                    await ws.send_text(Event(
                        type=EventType.MCP_PROMPT_RUN_RESPONSE,
                        request_id=req_id,
                        payload={
                            "success": result["ok"], "name": name, "prompt": prompt_name,
                            "messages": result["messages"],
                            "session_id": result["session_id"], "error": result["error"],
                        },
                    ).to_json())

            elif msg.type == EventType.PLUGIN_LIST_REQUEST:
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
                await ws.send_text(Event(
                    type=EventType.PLUGIN_LIST_RESPONSE,
                    request_id=req_id,
                    payload={"plugins": plugins_list},
                ).to_json())

            elif msg.type == EventType.PLUGIN_INSTALL_REQUEST:
                source = msg.payload.get("source")
                try:
                    data = engine_state.plugin_installer.prepare_install(source)
                    pending_plugin_installs[req_id] = data
                    await ws.send_text(Event(
                        type=EventType.PLUGIN_APPROVAL_REQUEST,
                        request_id=req_id,
                        payload={
                            "plugin_name": data["manifest"].get("name", "Unknown"),
                            "plugin_details": data,
                        },
                    ).to_json())
                except Exception as e:  # noqa: BLE001 — handler boundary: any prepare failure is reported to the client in the response payload
                    await ws.send_text(Event(
                        type=EventType.PLUGIN_INSTALL_RESPONSE,
                        request_id=req_id,
                        payload={"success": False, "error": str(e)},
                    ).to_json())

            elif msg.type == EventType.PLUGIN_APPROVAL_GRANTED:
                data = pending_plugin_installs.pop(req_id, None)
                if data:
                    try:
                        engine_state.plugin_installer.install(data)
                        engine_state.audit_logger.log_event("PLUGIN_INSTALLED", {"manifest": data["manifest"]})
                        await ws.send_text(Event(
                            type=EventType.PLUGIN_INSTALL_RESPONSE,
                            request_id=req_id,
                            payload={"success": True},
                        ).to_json())
                    except Exception as e:  # noqa: BLE001 — handler boundary: any install failure is reported to the client in the response payload
                        await ws.send_text(Event(
                            type=EventType.PLUGIN_INSTALL_RESPONSE,
                            request_id=req_id,
                            payload={"success": False, "error": str(e)},
                        ).to_json())

            elif msg.type == EventType.PLUGIN_APPROVAL_REJECTED:
                pending_plugin_installs.pop(req_id, None)
                await ws.send_text(Event(
                    type=EventType.PLUGIN_INSTALL_RESPONSE,
                    request_id=req_id,
                    payload={"success": False, "error": "Install rejected by user."},
                ).to_json())

            elif msg.type == EventType.PLUGIN_UNINSTALL_REQUEST:
                name = msg.payload.get("name")
                try:
                    # Disconnect the plugin's MCP servers first, in this
                    # (session-owner) task — remove_server_sync inside
                    # uninstall() cannot close a live session from a sync
                    # caller (anyio cancel-scope rule, see registry.py).
                    from aether_engine.mcp.registry import GLOBAL_MCP_REGISTRY
                    installed = engine_state.plugin_installer.get_all().get(name, {})
                    for mcp_name in installed.get("installed_mcp", []):
                        await GLOBAL_MCP_REGISTRY.disconnect_server(mcp_name)
                    engine_state.plugin_installer.uninstall(name)
                    await ws.send_text(Event(
                        type=EventType.PLUGIN_UNINSTALL_RESPONSE,
                        request_id=req_id,
                        payload={"success": True, "name": name},
                    ).to_json())
                except Exception as e:  # noqa: BLE001 — handler boundary: any uninstall failure is reported to the client in the response payload
                    await ws.send_text(Event(
                        type=EventType.PLUGIN_UNINSTALL_RESPONSE,
                        request_id=req_id,
                        payload={"success": False, "error": str(e)},
                    ).to_json())
                    
            elif msg.type == EventType.PLUGIN_TOGGLE_REQUEST:
                name = msg.payload.get("name")
                enabled = msg.payload.get("enabled", True)
                try:
                    if not enabled:
                        # Disconnect the plugin's MCP servers first, in
                        # this (session-owner) task — remove_server_sync
                        # inside toggle() cannot close a live session
                        # from a sync caller (anyio cancel-scope rule,
                        # see registry.py). Mirrors the
                        # PLUGIN_UNINSTALL_REQUEST branch above (E7-F01):
                        # if a disconnect fails, toggle() is never
                        # reached, so the plugin stays enabled with its
                        # sessions and skills untouched.
                        from aether_engine.mcp.registry import GLOBAL_MCP_REGISTRY
                        installed = engine_state.plugin_installer.get_all().get(name, {})
                        for mcp_name in installed.get("installed_mcp", []):
                            await GLOBAL_MCP_REGISTRY.disconnect_server(mcp_name)
                    engine_state.plugin_installer.toggle(name, enabled)
                    await ws.send_text(Event(
                        type=EventType.PLUGIN_TOGGLE_RESPONSE,
                        request_id=req_id,
                        payload={"success": True, "name": name, "enabled": enabled},
                    ).to_json())
                except Exception as e:  # noqa: BLE001 — handler boundary: any toggle failure is reported to the client in the response payload
                    await ws.send_text(Event(
                        type=EventType.PLUGIN_TOGGLE_RESPONSE,
                        request_id=req_id,
                        payload={"success": False, "error": str(e)},
                    ).to_json())

            elif msg.type == EventType.MEMORY_EPISODES_REQUEST:
                # Epic 4, E4-01/E4-04: through the facade; the payload
                # also carries embedding health for Diagnostics.
                backend = backend_or_none(engine_state.user_config, context="memory episodes list")
                if backend is None:
                    payload = {"episodes": [], "error": "The configured memory backend is unavailable (see engine log)."}
                else:
                    payload = {
                        "episodes": backend.list_episodes(limit=msg.payload.get("limit", 50)),
                        "backend": backend.name,
                        "embedding_stats": backend.embedding_stats(),
                    }
                await ws.send_text(Event(
                    type=EventType.MEMORY_EPISODES_RESPONSE,
                    request_id=req_id,
                    payload=payload,
                ).to_json())

            elif msg.type == EventType.MEMORY_EPISODE_DELETE_REQUEST:
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
                episodes = backend.list_episodes(limit=msg.payload.get("limit", 50)) if backend is not None and error is None else []
                payload = {"success": success, "task_id": task_id, "episodes": episodes}
                if error is not None:
                    payload["error"] = error
                await ws.send_text(Event(
                    type=EventType.MEMORY_EPISODE_DELETE_RESPONSE,
                    request_id=req_id,
                    payload=payload,
                ).to_json())

            elif msg.type == EventType.MEMORY_EPISODES_CLEAR_REQUEST:
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
                episodes = backend.list_episodes(limit=msg.payload.get("limit", 50)) if backend is not None and error is None else []
                payload = {"success": success, "episodes": episodes}
                if error is not None:
                    payload["error"] = error
                await ws.send_text(Event(
                    type=EventType.MEMORY_EPISODES_CLEAR_RESPONSE,
                    request_id=req_id,
                    payload=payload,
                ).to_json())
            
            elif msg.type == EventType.MEMORY_GRAPH_REQUEST:
                backend = backend_or_none(engine_state.user_config, context="memory graph list")
                if backend is None:
                    # E4-F08: report unavailability like the episodes
                    # list and export handlers do — not a bare empty
                    # list the UI cannot tell from "no facts".
                    payload = {"facts": [], "error": "The configured memory backend is unavailable (see engine log)."}
                elif msg.payload.get("query"):
                    # Epic 4, E4-08: fact search for the Memory UI.
                    payload = {"facts": backend.search_facts(
                        str(msg.payload["query"]),
                        limit=msg.payload.get("limit", 100),
                        include_history=bool(msg.payload.get("include_history")),
                    )}
                else:
                    payload = {"facts": backend.list_facts(
                        limit=msg.payload.get("limit", 100),
                        include_history=bool(msg.payload.get("include_history")),
                    )}
                await ws.send_text(Event(
                    type=EventType.MEMORY_GRAPH_RESPONSE,
                    request_id=req_id,
                    payload=payload,
                ).to_json())

            elif msg.type == EventType.MEMORY_FACT_DELETE_REQUEST:
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
                facts = backend.list_facts(limit=msg.payload.get("limit", 100)) if backend is not None and error is None else []
                payload = {"success": success, "fact_id": fact_id, "facts": facts}
                if error is not None:
                    payload["error"] = error
                await ws.send_text(Event(
                    type=EventType.MEMORY_FACT_DELETE_RESPONSE,
                    request_id=req_id,
                    payload=payload,
                ).to_json())

            elif msg.type == EventType.MEMORY_ENTITY_DELETE_REQUEST:
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
                facts = backend.list_facts(limit=msg.payload.get("limit", 100)) if backend is not None and error is None else []
                payload = {"success": success, "entity_name": entity_name, "facts": facts}
                if error is not None:
                    payload["error"] = error
                await ws.send_text(Event(
                    type=EventType.MEMORY_ENTITY_DELETE_RESPONSE,
                    request_id=req_id,
                    payload=payload,
                ).to_json())

            elif msg.type == EventType.MEMORY_GRAPH_CLEAR_REQUEST:
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
                facts = backend.list_facts(limit=msg.payload.get("limit", 100)) if backend is not None and error is None else []
                payload = {"success": success, "facts": facts}
                if error is not None:
                    payload["error"] = error
                await ws.send_text(Event(
                    type=EventType.MEMORY_GRAPH_CLEAR_RESPONSE,
                    request_id=req_id,
                    payload=payload,
                ).to_json())

            elif msg.type == EventType.MEMORY_FACT_UPDATE_REQUEST:
                # Epic 4, E4-03: edit one fact's text via the facade.
                backend = backend_or_none(engine_state.user_config, context="memory fact update")
                fact_id = msg.payload.get("fact_id")
                success = bool(
                    backend is not None and fact_id
                    and backend.update_fact(fact_id, msg.payload.get("fact_text", ""))
                )
                facts = backend.list_facts(
                    limit=msg.payload.get("limit", 100),
                    include_history=bool(msg.payload.get("include_history")),
                ) if backend is not None else []
                await ws.send_text(Event(
                    type=EventType.MEMORY_FACT_UPDATE_RESPONSE,
                    request_id=req_id,
                    payload={"success": success, "fact_id": fact_id, "facts": facts},
                ).to_json())

            elif msg.type == EventType.MEMORY_EXPORT_REQUEST:
                # Epic 4, E4-03: the full memory as JSON (facts incl.
                # superseded history + episodes) for the UI to save.
                backend = backend_or_none(engine_state.user_config, context="memory export")
                if backend is None:
                    payload = {"success": False, "error": "The configured memory backend is unavailable (see engine log)."}
                else:
                    try:
                        payload = {"success": True, "export": backend.export()}
                    except Exception as e:  # noqa: BLE001 — export failure must surface, not crash the handler
                        payload = {"success": False, "error": str(e)}
                await ws.send_text(Event(
                    type=EventType.MEMORY_EXPORT_RESPONSE,
                    request_id=req_id,
                    payload=payload,
                ).to_json())

            elif msg.type == EventType.BACKUP_CREATE_REQUEST:
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
                await ws.send_text(Event(
                    type=EventType.BACKUP_CREATE_RESPONSE,
                    request_id=req_id,
                    payload=payload,
                ).to_json())

            elif msg.type == EventType.BACKUP_RESTORE_REQUEST:
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
                await ws.send_text(Event(
                    type=EventType.BACKUP_RESTORE_RESPONSE,
                    request_id=req_id,
                    payload=payload,
                ).to_json())

            elif msg.type == EventType.DIAGNOSTICS_EXPORT_REQUEST:
                # Epic 10, E10-04: redacted bundle — config scrubbed by
                # the audit redactor, memory as counts only.
                try:
                    from aether_engine.diagnostics import build_diagnostics
                    from aether_engine.memory import episodic as _episodic
                    from aether_engine.memory import knowledge_graph as _kg

                    store = SecretStore()
                    with _kg._get_conn() as _conn:
                        kg_count = _conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
                    bundle = build_diagnostics(
                        user_config=engine_state.user_config,
                        health=engine_state.health_manager.get_all_statuses(),
                        kg_count=kg_count,
                        episodic_count=_episodic.embedding_stats().get("total", 0),
                        audit_log_path=engine_state.audit_logger.log_path,
                        providers=store.list_providers(),
                    )
                    payload = {"success": True, "bundle": bundle}
                except Exception as e:  # noqa: BLE001 — diagnostics failure must surface, not crash the handler
                    payload = {"success": False, "error": str(e)}
                await ws.send_text(Event(
                    type=EventType.DIAGNOSTICS_EXPORT_RESPONSE,
                    request_id=req_id,
                    payload=payload,
                ).to_json())

            elif msg.type == EventType.VERSION_CHECK_REQUEST:
                # Epic 10, E10-05: report-only update check against the
                # project's GitHub releases. Nothing downloads or
                # installs — the user decides.
                try:
                    import httpx

                    def _parse(v: str) -> tuple:
                        # E10-F09: SemVer precedence, not digit
                        # stripping. Build metadata (+...) never
                        # affects precedence; a prerelease precedes
                        # its release; prerelease identifiers compare
                        # numerically when numeric (numeric < alpha-
                        # numeric), lexically otherwise, and a
                        # shorter identifier list precedes a longer
                        # one it prefixes. An unparseable tag raises
                        # — the handler turns that into a failed
                        # check, not a successful "no update".
                        text = v.strip()
                        if text[:1] in ("v", "V"):
                            text = text[1:]
                        core, _, _build = text.partition("+")
                        core, dash, prerelease = core.partition("-")
                        pieces = core.split(".")
                        if len(pieces) > 3 or not all(p.isdigit() for p in pieces):
                            raise ValueError(f"not a semantic version: {v!r}")
                        nums = [int(p) for p in pieces] + [0] * (3 - len(pieces))
                        if not dash:
                            return (*nums, 1, ())
                        pre_key = []
                        for ident in prerelease.split("."):
                            if ident.isdigit():
                                pre_key.append((0, int(ident), ""))
                            elif ident:
                                pre_key.append((1, 0, ident))
                            else:
                                raise ValueError(f"not a semantic version: {v!r}")
                        return (*nums, 0, tuple(pre_key))

                    async with httpx.AsyncClient(timeout=10.0) as client:
                        resp = await client.get(
                            "https://api.github.com/repos/ProxCGaming/Aether-OS/releases/latest",
                            headers={"Accept": "application/vnd.github+json"},
                        )
                    if resp.status_code == 404:
                        payload = {"success": False, "error": "No releases have been published yet.",
                                   "current": APP_VERSION}
                    elif resp.status_code != 200:
                        payload = {"success": False, "error": f"GitHub answered {resp.status_code}.",
                                   "current": APP_VERSION}
                    else:
                        latest = str(resp.json().get("tag_name", "")).lstrip("v")
                        payload = {
                            "success": True,
                            "current": APP_VERSION,
                            "latest": latest,
                            "update_available": _parse(latest) > _parse(APP_VERSION),
                        }
                except Exception as e:  # noqa: BLE001 — a failed check must surface, not crash the handler
                    payload = {"success": False, "error": str(e), "current": APP_VERSION}
                await ws.send_text(Event(
                    type=EventType.VERSION_CHECK_RESPONSE,
                    request_id=req_id,
                    payload=payload,
                ).to_json())

            elif msg.type == EventType.SKILL_LIST_REQUEST:
                from aether_engine.skills.manager import GLOBAL_SKILL_MANAGER
                skills = [s.__dict__ for s in GLOBAL_SKILL_MANAGER.list_all()]
                await ws.send_text(Event(
                    type=EventType.SKILL_LIST_RESPONSE,
                    request_id=req_id,
                    payload={"skills": skills},
                ).to_json())

            elif msg.type == EventType.SKILL_TOGGLE_REQUEST:
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
                await ws.send_text(Event(
                    type=EventType.SKILL_TOGGLE_RESPONSE,
                    request_id=req_id,
                    payload=payload,
                ).to_json())

            elif msg.type == EventType.SLASH_COMMAND_LIST_REQUEST:
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
                
                await ws.send_text(Event(
                    type=EventType.SLASH_COMMAND_LIST_RESPONSE,
                    request_id=req_id,
                    payload={"commands": list(all_commands.values())},
                ).to_json())

            elif msg.type == EventType.WORKSPACE_LIST_REQUEST:
                roots = engine_state.user_config.workspace_roots or []
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
                    })
                await ws.send_text(Event(
                    type=EventType.WORKSPACE_LIST_RESPONSE,
                    request_id=req_id,
                    payload={"workspaces": workspaces},
                ).to_json())

            elif msg.type == EventType.WORKSPACE_ADD_REQUEST:
                ws_path = msg.payload.get("path", "").strip()
                if ws_path and ws_path not in engine_state.user_config.workspace_roots:
                    engine_state.user_config.workspace_roots.append(ws_path)
                    save_config(engine_state.user_config)
                    from aether_engine.skills.manager import GLOBAL_SKILL_MANAGER
                    GLOBAL_SKILL_MANAGER.update_workspace_roots(engine_state.user_config.workspace_roots)
                    p = Path(ws_path)
                    await ws.send_text(Event(
                        type=EventType.WORKSPACE_ADD_RESPONSE,
                        request_id=req_id,
                        # E6-F11: the reply carries the same badge
                        # fields WORKSPACE_LIST_RESPONSE computes
                        # (first root is active; `exists` drives the
                        # missing-folder state), so the Projects view
                        # renders the new row correctly from the reply
                        # itself instead of waiting for a LIST refetch.
                        payload={"success": True, "workspace": {
                            "path": ws_path, "name": p.name, "is_indexed": False,
                            "is_active": len(engine_state.user_config.workspace_roots) == 1,
                            "exists": p.is_dir(),
                        }},
                    ).to_json())
                else:
                    await ws.send_text(Event(
                        type=EventType.WORKSPACE_ADD_RESPONSE,
                        request_id=req_id,
                        payload={"success": False, "error": "Path empty or already exists"},
                    ).to_json())

            elif msg.type == EventType.WORKSPACE_REMOVE_REQUEST:
                ws_path = msg.payload.get("path", "").strip()
                if ws_path in engine_state.user_config.workspace_roots:
                    engine_state.user_config.workspace_roots.remove(ws_path)
                    save_config(engine_state.user_config)
                    from aether_engine.skills.manager import GLOBAL_SKILL_MANAGER
                    GLOBAL_SKILL_MANAGER.update_workspace_roots(engine_state.user_config.workspace_roots)
                    await ws.send_text(Event(
                        type=EventType.WORKSPACE_REMOVE_RESPONSE,
                        request_id=req_id,
                        payload={"success": True, "path": ws_path},
                    ).to_json())
                else:
                    await ws.send_text(Event(
                        type=EventType.WORKSPACE_REMOVE_RESPONSE,
                        request_id=req_id,
                        payload={"success": False, "error": "Path not found"},
                    ).to_json())

            elif msg.type == EventType.WORKSPACE_INDEX_START_REQUEST:
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
                                await ws.send_text(Event(
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

                    asyncio.create_task(_run_index())
                await ws.send_text(Event(
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

            elif msg.type == EventType.WORKSPACE_INDEX_STATUS_REQUEST:
                from aether_engine.rag.prose_indexer import get_prose_indexer

                indexer = get_prose_indexer()
                status_payload = await asyncio.to_thread(
                    indexer.status, msg.payload.get("workspace_root"))
                await ws.send_text(Event(
                    type=EventType.WORKSPACE_INDEX_STATUS_RESPONSE,
                    request_id=req_id,
                    payload=status_payload,
                ).to_json())

            elif msg.type == EventType.WORKSPACE_INDEX_PAUSE_REQUEST:
                from aether_engine.rag.prose_indexer import get_prose_indexer

                indexer = get_prose_indexer()
                pause = bool(msg.payload.get("pause", False))
                if pause:
                    indexer.pause()
                else:
                    indexer.resume()
                await ws.send_text(Event(
                    type=EventType.WORKSPACE_INDEX_PAUSE_RESPONSE,
                    request_id=req_id,
                    payload={"paused": indexer.paused},
                ).to_json())

            elif msg.type == EventType.WORKSPACE_INDEX_CONFIG_REQUEST:
                from aether_engine.rag.prose_indexer import get_prose_indexer

                indexer = get_prose_indexer()
                req_embedder = msg.payload.get("embedder")
                old_embedder = indexer.embedder
                reindex_required = False
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
                        save_config(engine_state.user_config)
                await ws.send_text(Event(
                    type=EventType.WORKSPACE_INDEX_CONFIG_RESPONSE,
                    request_id=req_id,
                    payload={"ok": True, "reindex_required": reindex_required},
                ).to_json())

            elif msg.type == EventType.WORKSPACE_SET_ACTIVE_REQUEST:
                # Epic 6, E6-04: the active workspace is the first
                # root (containment, skills, and task defaults all
                # key off roots[0]); activating = move to front.
                ws_path = msg.payload.get("path", "").strip()
                roots = engine_state.user_config.workspace_roots
                if ws_path in roots:
                    roots.remove(ws_path)
                    roots.insert(0, ws_path)
                    save_config(engine_state.user_config)
                    from aether_engine.skills.manager import GLOBAL_SKILL_MANAGER
                    GLOBAL_SKILL_MANAGER.update_workspace_roots(roots)
                    await ws.send_text(Event(
                        type=EventType.WORKSPACE_SET_ACTIVE_RESPONSE,
                        request_id=req_id,
                        payload={"success": True, "path": ws_path},
                    ).to_json())
                else:
                    await ws.send_text(Event(
                        type=EventType.WORKSPACE_SET_ACTIVE_RESPONSE,
                        request_id=req_id,
                        payload={"success": False, "error": "Path not found"},
                    ).to_json())

            elif msg.type in (EventType.PROJECT_CONFIG_GET_REQUEST,
                              EventType.PROJECT_CONFIG_UPDATE_REQUEST):
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
                    await ws.send_text(Event(
                        type=response_type,
                        request_id=req_id,
                        payload={"success": False, "error": project_error},
                    ).to_json())
                    continue
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
                        await ws.send_text(Event(
                            type=response_type,
                            request_id=req_id,
                            payload={"success": False, "error": project_error},
                        ).to_json())
                        continue
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
                    await ws.send_text(Event(
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
                    await ws.send_text(Event(
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
            elif msg.type == EventType.VOICE_SESSION_START_REQUEST:
                # Fail-closed start: an unavailable stage (missing
                # local STT/TTS/VAD/transport) or an unconfigured
                # chat model answers ok=False with the honest reason
                # and opens nothing — no device, no model, no peer.
                await _stop_voice_session()
                voice_session_id = msg.payload.get("session_id")
                if not voice_session_id:
                    voice_session_id = await asyncio.to_thread(
                        session_store.create_session, "Voice conversation")
                cfg = engine_state.user_config
                voice_error: str | None = None
                voice_pipeline = None
                webrtc_config = None
                try:
                    voice_provider = _create_provider_instance(
                        cfg.default_provider, cfg.default_model, secret_store)
                except (ProviderNotFoundError, SecretDecryptionError) as e:
                    voice_provider = None
                    voice_error = (
                        f"voice unavailable: chat model {cfg.default_provider}/"
                        f"{cfg.default_model} is not configured ({e})")
                if voice_provider is not None:
                    from aether_engine.voice import VoiceUnavailableError
                    from aether_engine.voice.pipeline import (
                        create_voice_pipeline,
                        fast_chat_talker,
                    )
                    from aether_engine.voice.vad import EndpointerConfig

                    async def _persist_voice_user(text: str, _sid: str = voice_session_id) -> None:
                        try:
                            await asyncio.to_thread(
                                session_store.add_message,
                                _sid, "user", text)
                        except Exception as e:  # noqa: BLE001 — best-effort persistence, as START_TASK: a save failure must not lose the turn
                            logger.error(f"Failed to save voice user message: {e}")

                    async def _persist_voice_assistant(text: str, _sid: str = voice_session_id) -> None:
                        try:
                            await asyncio.to_thread(
                                session_store.add_message,
                                _sid, "assistant", text)
                        except Exception as e:  # noqa: BLE001 — best-effort persistence, as task completion: a save failure must not lose the turn
                            logger.error(f"Failed to save voice assistant message: {e}")

                    async def _voice_turn_error(message: str) -> None:
                        await _emit_voice_event(Event(
                            type=EventType.ERROR,
                            payload={"message": f"Voice reply failed: {message}"},
                        ))

                    async def _voice_should_escalate(text: str, _provider=voice_provider) -> bool:
                        """E8-5.1b: the same E1-04 split the typed
                        path uses — classify_intent decides; chat
                        stays with the talker, work escalates to
                        the supervisor graph."""
                        try:
                            intent = await classify_intent(text, provider=_provider)
                        except Exception:  # a routing failure keeps the turn with the talker
                            logger.debug("voice intent classification failed", exc_info=True)
                            return False
                        return not intent.is_chat

                    async def _voice_escalation_launch(text: str, _sid: str = voice_session_id, _provider=voice_provider) -> None:
                        """Launch (never await) the background job:
                        the task lane is a separate execution, so
                        the talker stays free while it runs."""
                        task = asyncio.ensure_future(
                            _run_voice_background_task(text, _sid, _provider))
                        background = voice_state.setdefault("background_tasks", set())
                        background.add(task)
                        task.add_done_callback(background.discard)

                    voice_pipeline, voice_error = create_voice_pipeline(
                        session_id=voice_session_id,
                        talker=fast_chat_talker(
                            voice_provider, session_id=voice_session_id),
                        emit=_emit_voice_event,
                        on_user_final=_persist_voice_user,
                        on_assistant_final=_persist_voice_assistant,
                        on_turn_error=_voice_turn_error,
                        escalation_classifier=_voice_should_escalate,
                        on_escalation=_voice_escalation_launch,
                        endpointer_config=EndpointerConfig(
                            speech_threshold=cfg.vad_threshold,
                            silence_duration_ms=cfg.silence_duration_ms,
                            smart_turn_sensitivity=cfg.smart_turn_sensitivity,
                        ),
                    )
                    if voice_pipeline is not None:
                        try:
                            webrtc_config = await voice_pipeline.start()
                        except VoiceUnavailableError as e:
                            voice_error = str(e)
                            voice_pipeline = None
                if voice_pipeline is not None:
                    voice_state["pipeline"] = voice_pipeline
                    voice_state["task"] = asyncio.ensure_future(
                        _run_voice_pipeline(voice_pipeline))

                    # F-12a: the engine's own ICE candidates trickle
                    # back to the renderer as VOICE_RTC_ICE pushes.
                    async def _push_rtc_ice(candidate, _sid=voice_session_id):
                        await _emit_voice_event(Event(
                            type=EventType.VOICE_RTC_ICE,
                            payload={
                                "session_id": _sid,
                                "candidate": candidate,
                            },
                        ))

                    voice_pipeline.set_rtc_ice_listener(_push_rtc_ice)
                await ws.send_text(Event(
                    type=EventType.VOICE_SESSION_START_RESPONSE,
                    request_id=req_id,
                    payload={
                        "ok": voice_pipeline is not None,
                        "session_id": voice_session_id,
                        "webrtc_config": webrtc_config,
                        "error": voice_error,
                    },
                ).to_json())

            elif msg.type == EventType.VOICE_SESSION_STOP_REQUEST:
                await _stop_voice_session()
                await ws.send_text(Event(
                    type=EventType.VOICE_SESSION_STOP_RESPONSE,
                    request_id=req_id,
                    payload={"ok": True},
                ).to_json())

            elif msg.type == EventType.VOICE_PLAYBACK_PROGRESS:
                # N09: the renderer's playback report for the live
                # session (the highest chunk_seq it has FINISHED
                # playing) — the input the pipeline's heard-word
                # accounting reads. Fire-and-forget by design: no
                # response; a report with no live session, or a
                # malformed one, is ignored by the pipeline's own
                # validation, never fatal to the socket.
                progress_pipeline = voice_state.get("pipeline")
                if progress_pipeline is not None:
                    progress_pipeline.note_playback_progress(
                        msg.payload.get("played_seq"))

            # -----------------------------------------------------------
            # Voice RTC negotiation (F-12a): the renderer offers,
            # the live session's transport answers; ICE trickles
            # both ways. Every failure answers ok=False with the
            # honest reason — a dead negotiation is never silent.
            # -----------------------------------------------------------
            elif msg.type == EventType.VOICE_RTC_OFFER_REQUEST:
                rtc_pipeline = voice_state.get("pipeline")
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
                await ws.send_text(Event(
                    type=EventType.VOICE_RTC_OFFER_RESPONSE,
                    request_id=req_id,
                    payload={
                        "ok": rtc_answer is not None,
                        "session_id": msg.payload.get("session_id"),
                        "answer": rtc_answer,
                        "error": rtc_error,
                    },
                ).to_json())

            elif msg.type == EventType.VOICE_RTC_ICE_REQUEST:
                rtc_pipeline = voice_state.get("pipeline")
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
                await ws.send_text(Event(
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
            elif msg.type == EventType.DICTATION_START_REQUEST:
                stt = _dictation_stt()
                reason = stt.unavailable_reason()
                if reason is not None:
                    await ws.send_text(Event(
                        type=EventType.DICTATION_START_RESPONSE,
                        request_id=req_id,
                        payload={"ok": False, "error": f"voice unavailable: {reason}"},
                    ).to_json())
                else:
                    await ws.send_text(Event(
                        type=EventType.DICTATION_START_RESPONSE,
                        request_id=req_id,
                        payload={"ok": True, "error": None},
                    ).to_json())

            elif msg.type == EventType.DICTATION_CHUNK_REQUEST:
                stt = _dictation_stt()
                reason = stt.unavailable_reason()
                if reason is not None:
                    await ws.send_text(Event(
                        type=EventType.DICTATION_CHUNK_RESPONSE,
                        request_id=req_id,
                        payload={"ok": False, "text": "", "is_final": False, "error": f"voice unavailable: {reason}"},
                    ).to_json())
                else:
                    raw_b64 = msg.payload.get("audio") or ""
                    sample_rate = int(msg.payload.get("sample_rate") or 16000)
                    is_final = bool(msg.payload.get("is_final", False))
                    try:
                        audio_bytes = base64.b64decode(raw_b64)
                        stt = await _open_dictation_stt()
                        text = await stt.transcribe(audio_bytes, sample_rate=sample_rate)
                        await ws.send_text(Event(
                            type=EventType.DICTATION_CHUNK_RESPONSE,
                            request_id=req_id,
                            payload={"ok": True, "text": text, "is_final": is_final, "error": None},
                        ).to_json())
                    except Exception as e:  # noqa: BLE001
                        await ws.send_text(Event(
                            type=EventType.DICTATION_CHUNK_RESPONSE,
                            request_id=req_id,
                            payload={"ok": False, "text": "", "is_final": is_final, "error": str(e)},
                        ).to_json())

            elif msg.type == EventType.DICTATION_STOP_REQUEST:
                raw_b64 = msg.payload.get("audio")
                sample_rate = int(msg.payload.get("sample_rate") or 16000)
                text = ""
                stop_error: str | None = None
                if raw_b64:
                    try:
                        stt = await _open_dictation_stt()
                        audio_bytes = base64.b64decode(raw_b64)
                        text = await stt.transcribe(audio_bytes, sample_rate=sample_rate)
                    except Exception as e:  # noqa: BLE001 — S4-M7: a failed final transcription must be distinguishable from silence
                        logger.warning(f"Dictation stop transcribe error: {e}")
                        stop_error = str(e)
                await ws.send_text(Event(
                    type=EventType.DICTATION_STOP_RESPONSE,
                    request_id=req_id,
                    payload={"ok": stop_error is None, "text": text if stop_error is None else "", "error": stop_error},
                ).to_json())

            elif msg.type == EventType.READ_ALOUD_START_REQUEST:
                await _stop_read_aloud()
                tts = _read_aloud_tts()
                reason = tts.unavailable_reason()
                msg_id = msg.payload.get("message_id")
                text_to_speak = msg.payload.get("text") or ""
                voice_name = msg.payload.get("voice") or "af_heart"
                if reason is not None:
                    await ws.send_text(Event(
                        type=EventType.READ_ALOUD_START_RESPONSE,
                        request_id=req_id,
                        payload={"ok": False, "message_id": msg_id, "error": f"voice unavailable: {reason}"},
                    ).to_json())
                else:
                    await ws.send_text(Event(
                        type=EventType.READ_ALOUD_START_RESPONSE,
                        request_id=req_id,
                        payload={"ok": True, "message_id": msg_id, "error": None},
                    ).to_json())

                    async def _run_read_aloud(mid: str | None, text: str, voice: str, tts_inst: Any, target_ws: WebSocket) -> None:
                        try:
                            # S4-M7: open the cached engine once for
                            # the connection, not on every start.
                            tts_inst = await _open_read_aloud_tts()
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

                    voice_state["read_aloud_id"] = msg_id
                    voice_state["read_aloud_task"] = asyncio.ensure_future(
                        _run_read_aloud(msg_id, text_to_speak, voice_name, tts, ws)
                    )

            elif msg.type == EventType.READ_ALOUD_STOP_REQUEST:
                msg_id = msg.payload.get("message_id")
                await _stop_read_aloud()
                await ws.send_text(Event(
                    type=EventType.READ_ALOUD_STOP_RESPONSE,
                    request_id=req_id,
                    payload={"ok": True, "message_id": msg_id},
                ).to_json())

    except WebSocketDisconnect:
        logger.info(f"Client disconnected from {ip}:{port}")
        _unsubscribe_media_events()
        if task_runner and not task_runner.done():
            task_runner.cancel()
        await _stop_voice_session()
        await _stop_read_aloud()
        await _close_speech_engines()
        await _cancel_voice_background_tasks()
        engine_state.audit_logger.log_event("CLIENT_DISCONNECTED", {"client_ip": ip})
    except Exception as e:  # noqa: BLE001 — top-level socket boundary: any session failure is logged and the task is cancelled, never propagated
        logger.error(f"WebSocket session error: {e}")
        _unsubscribe_media_events()
        if task_runner and not task_runner.done():
            task_runner.cancel()
        await _stop_voice_session()
        await _stop_read_aloud()
        await _close_speech_engines()
        await _cancel_voice_background_tasks()

# --- Legacy unauthenticated HTTP management routes: REMOVED (Phase 9, A1) ---
# The seven HTTP routes that used to live here (GET/POST/DELETE/PATCH on
# /plugins and /agents/nodes) were deleted because they bypassed the ADR 0003
# token handshake enforced on /ws/tasks. Their only callers were the retired
# PySide6 UI (superseded by ADR-0019) and a test for that retired path.
# Plugin/agent management is served exclusively over the authenticated
# WebSocket (PLUGIN_INSTALL_REQUEST, AGENT_UPDATE_REQUEST, ...).
# Do NOT re-add unauthenticated HTTP management routes.
