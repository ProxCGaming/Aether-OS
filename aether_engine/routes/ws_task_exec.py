"""Task execution core for the task websocket (Wave 5 split, slice 3).

The last closures to leave ``ws_tasks``: the task pump
(``execute_task_generator``) and the START_TASK launch pipeline
(``start_task_message``, RUX-B-07), plus the two detached
post-completion helpers only the pump spawns (fast-chat learn
parity, E4-05; session titling, E1-08). They run over the
connection's ``WsConnection`` (routes/ws_connection.py): the
per-connection task state the closures used to share with the
receive loop — the runner, its session, the chained launch
tail, the current task id — lives on the context now, so the
pump, the launch and the dispatch handlers all read and write
the same fields.

Patch doctrine (slices 1–2): provider construction, intent
classification, the fast-chat and graph runners, the tool
registry builder and the rewind service are patched by the
suites on ``aether_engine.routes.websocket`` — so they resolve
through that module at call time here, never through a binding
captured at import. Everything else is imported from its home
module, exactly as the endpoint imports it.
"""
import asyncio
import logging
import re
from pathlib import Path

from aether_common.contracts import Event, EventType, TaskState
from aether_engine.cost.ledger import (
    reset_usage_context,
    set_usage_context,
    usage_context,
)
from aether_engine.langgraph.fast_chat import load_session_history
from aether_engine.memory import session_store
from aether_engine.providers.serving import (
    begin_serving_tracking,
    clear_serving_provider,
    end_serving_tracking,
    serving_provider,
)
from aether_engine.routes.ws_connection import WsConnection
from aether_engine.routes.ws_sessions import (
    _create_bound_session,
    _register_message_attachments,
    _run_mcp_prompt_into_chat,
)
from aether_engine.routing.policy import RoutingDecision, infer_task_requirements
from aether_engine.routing.startup import (
    derive_configured_providers,
    is_keyless_custom_provider,
)
from aether_engine.secrets.dpapi import SecretDecryptionError
from aether_engine.secrets.storage import ProviderNotFoundError
from aether_engine.state import engine_state

logger = logging.getLogger("aether_engine.routes.ws_task_exec")


async def _learn_fast_chat_in_background(conn: WsConnection, task_id: str, prompt: str, response_text: str, p_name: str, p_model: str):
    """Epic 4, E4-05: fast-chat learn parity — detached after
    TASK_COMPLETED; the work itself lives in
    fast_chat.learn_fast_chat_exchange (thin wrapper here: build
    the chat's provider instance, then delegate)."""
    from aether_engine.langgraph.fast_chat import learn_fast_chat_exchange
    from aether_engine.orchestration.core import _create_provider_instance

    try:
        provider = _create_provider_instance(p_name, p_model, conn.secret_store)
    except Exception:  # noqa: BLE001 — no provider instance: the episode still records; only fact extraction is skipped
        provider = None
    await learn_fast_chat_exchange(
        task_id, prompt, response_text, provider, engine_state.user_config)


async def _title_session_in_background(conn: WsConnection, session_id: str, p_name: str, p_model: str, response_text: str):
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
            fallback = _create_provider_instance(p_name, p_model, conn.secret_store)
        except Exception:  # noqa: BLE001 — no provider instance, no title.
            return
        provider = resolve_title_provider(engine_state.user_config, conn.secret_store, fallback)
        title = await generate_session_title(provider, first_user, response_text)
        if title and await asyncio.to_thread(session_store.update_session_title, session_id, title):
            await conn.ws.send_text(Event(
                type=EventType.SESSION_TITLE_UPDATED,
                request_id="title",
                payload={"session_id": session_id, "title": title},
            ).to_json())
    except Exception:  # Background decoration must never surface as a task error.
        logger.warning("Session title update failed", exc_info=True)


async def execute_task_generator(conn: WsConnection, generator, p_name: str, p_model: str, req_id: str, session_id: str | None = None, *, routing_reason: str = "Default Routing", routing_capabilities: list | None = None, retry_factory=None, fallback_blocked_message: str | None = None):
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

    E8-03: the switch is also announced to the client with the
    contract events the renderer handles — FALLBACK_STARTED when
    the retry is accepted (payload names the fallback provider /
    model and the failed pair), FALLBACK_COMPLETED just before
    the retried task's TASK_COMPLETED is delivered.
    """
    from aether_engine.routes import websocket as _websocket
    # Epic 8, E8-A8.1a: attribute the provider calls made while
    # this task streams to the usage ledger. The session is known
    # now; the task id is refined in the event loop below, once
    # TASK_CREATED flows past. Reset when the pump finishes so a
    # later task on this connection starts clean.
    usage_token = set_usage_context(session_id=session_id)
    # N-44: this pump's own task id, learned from TASK_CREATED —
    # the connection-level holder lets CANCEL_TASK name the task
    # it cancelled; the local copy lets the cancellation audit
    # record below name it too.
    pump_task_id: str | None = None
    # Finding E5-F07: open the serving-provider scope beside the
    # usage context. Provider calls made while this task streams
    # note which provider actually served them; the completion
    # credit below follows that note instead of the outer
    # provider variable.
    serving_token = begin_serving_tracking()
    try:
        attempt = 0
        current_reason = routing_reason
        # E8-03: set when a §6.4 retry is accepted; cleared when
        # its FALLBACK_COMPLETED has been announced (exactly once).
        fallback_context: dict | None = None
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
                    pump_task_id = ev.payload.get("task_id")
                    conn.active_task_id = pump_task_id
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
                        conn.spawn_background(
                            _title_session_in_background(conn, session_id, p_name, p_model, ev.payload["response"]))
                        if task_path == "fast_chat" and task_prompt:
                            # Epic 4, E4-05: graph tasks learn in the
                            # executor; fast chat learns here (parity).
                            conn.spawn_background(_learn_fast_chat_in_background(
                                conn, ev.payload.get("task_id", "unknown"), task_prompt,
                                ev.payload["response"], p_name, p_model))

                if ev.type == EventType.TOOL_APPROVAL_REQUEST:
                    engine_state.audit_logger.log_event("APPROVAL_REQUESTED", {
                        "tool_name": ev.payload.get("tool_name"),
                        "args": ev.payload.get("args"),
                        "task_id": ev.payload.get("task_id", "unknown"),
                    })

                if ev.type == EventType.TASK_COMPLETED and fallback_context is not None:
                    # E8-03: announce the fallback's success
                    # before the completion itself is delivered,
                    # so the renderer's system messages bracket
                    # the retried answer in order. The payload
                    # carries `provider`/`model` — the fields
                    # App.jsx reads for FALLBACK_COMPLETED.
                    await conn.ws.send_text(Event(
                        type=EventType.FALLBACK_COMPLETED,
                        request_id=req_id,
                        payload={
                            **fallback_context,
                            "task_id": ev.payload.get("task_id", "unknown"),
                        },
                    ).to_json())
                    fallback_context = None

                await conn.ws.send_text(ev.to_json())

            if not (primary_failed and failure_event):
                return

            err_msg = failure_event.payload.get("error", "Unknown error")
            engine_state.health_manager.record_failure(p_name, err_msg, is_retriable=True)

            # §6.4 gate: one retry, only before any tool ran,
            # only when the caller supplied a factory.
            if _websocket.should_attempt_task_fallback(
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
                    # E8-03: announce the switch to the client
                    # (the renderer renders a system message) and
                    # remember the pair so the retry's completion
                    # can be announced as FALLBACK_COMPLETED.
                    fallback_context = {
                        "provider": fallback_provider,
                        "model": fallback_model,
                        "failed_provider": p_name,
                        "failed_model": p_model,
                    }
                    await conn.ws.send_text(Event(
                        type=EventType.FALLBACK_STARTED,
                        request_id=req_id,
                        payload={
                            **fallback_context,
                            "task_id": failure_event.payload.get("task_id", "unknown"),
                            "error": err_msg,
                        },
                    ).to_json())
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

            await conn.ws.send_text(failure_event.to_json())
            return
    except asyncio.CancelledError:
        # N-44: name the task that was actually cancelled —
        # "unknown" only when no TASK_CREATED ever named one.
        engine_state.audit_logger.log_event(
            "TASK_CANCELLED", {"task_id": pump_task_id or "unknown"})
        raise
    except Exception as e:  # task-generator boundary: any failure is converted into an ERROR event for the client
        logger.exception("Task generator failed with exception")
        try:
            await conn.ws.send_text(Event(
                type=EventType.ERROR,
                request_id=req_id,
                payload={"error": f"Internal engine error: {e!s}"}
            ).to_json())
        except Exception:  # the socket is already broken; nothing further can be signalled
            logger.debug("Could not send task error event to client", exc_info=True)
    finally:
        if conn.active_task_id == pump_task_id:
            conn.active_task_id = None
        reset_usage_context(usage_token)
        end_serving_tracking(serving_token)


async def start_task_message(conn: WsConnection, msg, req_id, previous_start):
    """Run one START_TASK's pre-work off the receive loop (RUX-B-07).

    Everything the START_TASK branch used to do inline before
    task_runner existed — cancelling the previous runner and
    waiting for it, committing a staged rewind, a /mcp: slash
    run, provider construction, and the intent-classification
    model call (12 s timeout) — now runs in this launch task.
    The ordering those steps depend on is preserved two ways:
    launches are chained (this launch shields and awaits the
    previous launch before touching anything), and the steps
    themselves stay in their original order inside the launch,
    so the task pipeline — never the socket — absorbs the
    waits. The task's own events keep their original order
    because they all originate from this one launch.
    """
    from aether_engine.routes import websocket as _websocket
    if previous_start is not None:
        try:
            await asyncio.shield(previous_start)
        except asyncio.CancelledError:
            if not previous_start.cancelled():
                raise
        except Exception as e:  # noqa: BLE001 — a failed earlier launch answered for itself; this launch still runs
            logger.debug(f"Earlier task launch ended with an error before a chained launch: {e}")
    # E1-13: validate the launch's content BEFORE cancelling the
    # live runner. A prompt-less, attachment-less start names no
    # task at all — answer it with an error and leave the live
    # task untouched, instead of replacing it with a fabricated
    # "Say hello" task (the old default below). Attachments alone
    # are real content and still launch, with an honest empty
    # prompt.
    _raw_prompt = msg.payload.get("prompt")
    prompt = _raw_prompt.strip() if isinstance(_raw_prompt, str) else ""
    attachments = msg.payload.get("attachments") or []
    if not prompt and not attachments:
        await conn.ws.send_text(Event(
            type=EventType.ERROR,
            request_id=req_id,
            payload={"error": "START_TASK requires a non-empty prompt or attachments; nothing was started."},
        ).to_json())
        return
    if conn.task_runner and not conn.task_runner.done():
        conn.task_runner.cancel()
        try:
            await conn.task_runner
        except asyncio.CancelledError:
            pass
    cfg = engine_state.user_config
    requested_model = msg.payload.get("model")
    session_id = msg.payload.get("session_id")

    if not session_id:
        # Create new session if none provided. Implicit
        # creation takes the SESSION_CREATE binding rule
        # (slice 3): the active root at creation time, or
        # unbound — via the one shared helper.
        title = prompt[:30] + "..." if len(prompt) > 30 else prompt
        session_id, session_binding = await _create_bound_session(title)
        try:
            await conn.ws.send_text(Event(
                type=EventType.SESSION_CREATE_RESPONSE,
                request_id=req_id,
                payload={"session_id": session_id, "workspace": session_binding, "preserve_messages": True}
            ).to_json())
            await conn.ws.send_text(Event(
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
                await conn.ws.send_text(Event(
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
    await _websocket.rewind_service.commit(session_id)

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
        await conn.ws.send_text(Event(
            type=EventType.MCP_PROMPT_RUN_RESPONSE,
            request_id=req_id,
            payload={
                "success": _run["ok"], "name": _mcp_slash.group(1),
                "prompt": _mcp_slash.group(2),
                "messages": _run["messages"],
                "session_id": _run["session_id"], "error": _run["error"],
            },
        ).to_json())
        return

    try:
        # The UI's client-generated id becomes the row id, so
        # the rendered message and the stored message share
        # one identity (edit-and-resend addresses the row).
        await asyncio.to_thread(
            session_store.add_message, session_id, "user", prompt,
            None, msg.payload.get("message_id"), attachments)
        await _register_message_attachments(
            session_id, msg.payload.get("message_id"), attachments, conn.ws)
    except Exception as e:  # noqa: BLE001 — best-effort persistence: a save failure must not block the task
        logger.error(f"Failed to save user message: {e}")

    # E5-F04: "configured" is derived ONCE, here as
    # everywhere (routing.startup.derive_configured_providers):
    # stored secrets plus keyless custom providers whose
    # saved configuration is complete. Deriving from
    # secrets alone made an accepted keyless custom
    # provider invisible to routing.
    engine_state.configured_providers = derive_configured_providers(
        cfg, conn.secret_store, engine_state.model_registry)

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
        provider_instance = _websocket._create_provider_instance(
            decision.provider, decision.model, conn.secret_store,
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
        await conn.ws.send_text(Event(
            type=EventType.TASK_FAILED,
            request_id=req_id,
            payload={
                "task_id": "none", "retriable": False, "state": TaskState.FAILED.value,
                "error": error_text,
            },
        ).to_json())
        return
    except SecretDecryptionError as e:
        await conn.ws.send_text(Event(
            type=EventType.TASK_FAILED,
            request_id=req_id,
            payload={
                "task_id": "none", "retriable": False, "state": TaskState.FAILED.value,
                "error": f"Failed to decrypt API key: {e}. Please re-enter your key in Settings → Models.",
            },
        ).to_json())
        return

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
            intent = await _websocket.classify_intent(prompt, provider=provider_instance)

    if intent is not None and intent.is_chat:
        history = await asyncio.to_thread(
            load_session_history, session_id, prompt)
        task_generator = _websocket.run_fast_chat(
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
            # S7: the run's resolved config layers drive the
            # memory briefing (toggle + sub-budget + backend).
            user_config=task_user_config,
        )
    else:
        # One central builder (PRD Q3) — task start, approval
        # resume and tool list used to build registries
        # independently and had already drifted apart.
        from aether_engine.mcp.registry import GLOBAL_MCP_REGISTRY

        reqs = infer_task_requirements(prompt)
        tools = _websocket.build_tool_registry(
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
        task_generator = _websocket.run_langgraph_task(
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
            secret_store=conn.secret_store,
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
            fallback_instance = _websocket._create_provider_instance(
                entry.provider, entry.id, conn.secret_store,
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
            gen = _websocket.run_fast_chat(
                _ctx["prompt"], fallback_instance, _ctx["history"],
                request_id=_ctx["req_id"], session_id=_ctx["session_id"],
                task_id=failed_task_id, attachments=_ctx.get("attachments"),
                workspace_roots=_ctx.get("workspace_roots"),
                # S7: same resolved config as the original run.
                user_config=_ctx["user_config"])
        else:
            gen = _websocket.run_langgraph_task(
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
                secret_store=conn.secret_store,
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

    conn.task_session_id = session_id
    conn.task_runner = asyncio.create_task(
        execute_task_generator(
            conn, task_generator, decision.provider, decision.model, req_id,
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
