"""Task resume websocket handlers (Wave 6, D-1 option B).

The manual Resume surface for restart-interrupted tasks, split out
of routes/websocket.py in the Wave 5 style: the dispatch table
there maps TASK_RESUME_AUDIT_REQUEST / TASK_RESUME_REQUEST here.

D-1 (DECIDED 17 Nov 20:19 IST): "Resume is manual-only" — these
two requests are the only trigger; nothing resumes at startup. The
resume is gated by the pre-resume audit
(``langgraph/resume_audit.build_resume_audit``): a refused audit
never starts anything, and an UNCERTAIN in-flight step starts only
when the request carries the owner's explicit ``confirm_uncertain``
confirmation. "Any tool that will actually run re-gates its
approval fresh" — this path refuses outright when the checkpoint
still carries a pending approval, so a resume can never deliver,
clear or inherit a stale grant; the one re-gate gap in the
executor's approval-shaped continuation entry is documented at
the call site below (with its xfail test) and reported as a
cross-fence need.

Patch doctrine (as in ws_task_exec): the provider factory, the
persisted-run-config read, the pending-approvals scan, the tool
registry builder and the executor's resume generator are patched
by the suites on ``aether_engine.routes.websocket`` — they resolve
through that module at call time, never through a binding captured
at import.
"""

import asyncio
import logging
from pathlib import Path
from typing import Any

from aether_common.contracts import Event, EventType
from aether_engine.config import UserConfig
from aether_engine.langgraph.resume_audit import build_resume_audit
from aether_engine.routes.ws_connection import WsConnection
from aether_engine.state import engine_state

logger = logging.getLogger("aether_engine.routes.ws_task_resume")

# Task ids with a resume launched through this module whose pump
# has not settled yet. The audit's tap gate catches tasks the tap
# has already seen settle or pause; this set closes the narrower
# window between launch and the resumed run's first tapped event,
# so one task can never be resumed twice concurrently.
_RESUMING: set[str] = set()


async def _reply(
    conn: WsConnection, req_id: str | None, event_type: EventType,
    payload: dict[str, Any],
) -> None:
    await conn.ws.send_text(Event(
        type=event_type, request_id=req_id, payload=payload,
    ).to_json())


async def handle_task_resume_audit_request(
    conn: WsConnection, msg: Event,
) -> None:
    """Answer the pre-resume audit for one task (a pure read)."""
    req_id = msg.request_id
    task_id = (msg.payload or {}).get("task_id")
    if not task_id:
        await _reply(conn, req_id, EventType.TASK_RESUME_AUDIT_RESPONSE, {
            "success": False, "task_id": None, "audit": None,
            "error": "task_id is required",
        })
        return
    audit = await build_resume_audit(
        str(task_id),
        session_id=(msg.payload or {}).get("session_id"),
    )
    await _reply(conn, req_id, EventType.TASK_RESUME_AUDIT_RESPONSE, {
        "success": True, "task_id": str(task_id), "audit": audit,
        "error": None,
    })


async def handle_task_resume_request(
    conn: WsConnection, msg: Event,
) -> None:
    """Resume one restart-interrupted task, audit-gated (D-1)."""
    req_id = msg.request_id
    payload = msg.payload or {}
    task_id = payload.get("task_id")
    if not task_id:
        await _reply(conn, req_id, EventType.TASK_RESUME_RESPONSE, {
            "success": False, "task_id": None, "session_id": None,
            "error": "task_id is required",
            "requires_confirmation": False, "audit": None,
        })
        return
    task_id = str(task_id)
    confirm_uncertain = bool(payload.get("confirm_uncertain"))

    async def refuse(error: str, *, audit: dict | None,
                     requires_confirmation: bool = False) -> None:
        await _reply(conn, req_id, EventType.TASK_RESUME_RESPONSE, {
            "success": False, "task_id": task_id,
            "session_id": (audit or {}).get("session_id"),
            "error": error,
            "requires_confirmation": requires_confirmation,
            "audit": audit,
        })

    if task_id in _RESUMING:
        await refuse(
            "a resume for this task is already in progress", audit=None)
        return

    audit = await build_resume_audit(
        task_id, session_id=payload.get("session_id"))
    if audit["verdict"] == "refused":
        await refuse(audit["refusal_reason"] or "the audit refused "
                     "this resume", audit=audit)
        return
    if audit["verdict"] == "uncertain" and not confirm_uncertain:
        in_flight = audit.get("in_flight") or {}
        await refuse(
            "the step in flight when the engine stopped cannot be "
            f"verified: {in_flight.get('reason') or 'unknown'}. "
            "Confirm to resume anyway — the step will be run again "
            "and may repeat an effect that already happened.",
            audit=audit, requires_confirmation=True)
        return

    thread_id = audit.get("session_id")
    if not thread_id:
        await refuse(
            "the task's checkpoint thread cannot be identified — "
            "its state records no session, so there is no thread to "
            "continue", audit=audit)
        return

    from aether_engine.routes import websocket as ws_module

    # Belt-and-braces beside the audit's own gate: scan the
    # checkpointer's pending approvals for this thread directly.
    # If an approval is pending, the approval flow owns the task —
    # a resume here must never answer it by side effect (the
    # executor's continuation delivers its resume value to a
    # pending interrupt, so this check is what keeps a resume from
    # ever answering one).
    try:
        pending = await ws_module.get_pending_approvals()
    except Exception:
        logger.debug(
            "Pending-approval scan failed before resume", exc_info=True)
        pending = []
    if any(p.get("thread_id") == thread_id for p in pending):
        await refuse(
            "this task is waiting on an approval decision — answer "
            "the approval instead; resuming here would bypass it",
            audit=audit)
        return

    # Rebuild the interrupted task's own execution configuration
    # from the run-config snapshot checkpointed with its state —
    # the same derivation the approval-resume route performs
    # (E1-F04): provider/model, per-agent models/tools, forced
    # agent, workspace roots and the layered user config are the
    # task's own; the current globals are only the fallback.
    run_cfg = await ws_module.get_persisted_run_config(thread_id) or {}
    resume_session_id = run_cfg.get("session_id") or thread_id
    resume_user_config = engine_state.user_config
    if isinstance(run_cfg.get("user_config"), dict):
        try:
            resume_user_config = UserConfig.from_dict(run_cfg["user_config"])
        except Exception:  # a corrupt snapshot degrades to the live config, never blocks a resume
            logger.debug(
                "Could not restore task user_config for resume",
                exc_info=True)
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
    if run_cfg:
        resume_local_only = bool(run_cfg.get("local_only"))
        resume_has_images = bool(run_cfg.get("require_vision"))
    else:
        from aether_engine.routing.policy import (
            local_only_from_vision_override,
        )

        resume_local_only = local_only_from_vision_override(
            engine_state.user_config.auxiliary_models)
        resume_has_images = False

    try:
        provider_instance = ws_module._create_provider_instance(
            resume_provider_name, resume_model, conn.secret_store,
            local_only=resume_local_only,
            require_vision=resume_has_images,
        )
    except Exception:
        logger.debug(
            "Could not instantiate provider for task resume",
            exc_info=True)
        await refuse(
            f"could not rebuild the task's provider "
            f"({resume_provider_name} / {resume_model}) — the resume "
            "was not started", audit=audit)
        return

    from aether_engine.mcp.registry import GLOBAL_MCP_REGISTRY

    tools = ws_module.build_tool_registry(
        mcp_tools=GLOBAL_MCP_REGISTRY.get_mcp_tools(
            risk_overrides=resume_user_config.mcp_tool_risk_overrides),
        # D4-8 parity with task start + approval resume: the resumed
        # task's registry is one the model sees, so its Disabled set
        # applies exactly as it did before the interruption.
        disabled_tools=resume_user_config.disabled_tools,
    )

    if conn.execute_task_generator is None:
        await refuse(
            "this connection cannot run tasks — the resume was not "
            "started", audit=audit)
        return

    # The resume runs through the executor's plain continuation
    # entry: its first segment consumes None, so no approval
    # decision is delivered — and none can be consumed by the
    # first fresh approval interrupt the resumed run raises. Every
    # gated call re-gates and asks (D-1), proven by
    # tests/test_wave6_resume.py::test_real_resume_regates_approvals.
    # The gates above additionally guarantee no interrupt is
    # PENDING on this thread, so no stale approval can be
    # answered, cleared, or inherited here.
    resume_gen = ws_module.continue_langgraph_task(
        thread_id=thread_id,
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
        local_only=resume_local_only,
        require_vision=resume_has_images,
    )

    if conn.task_runner and not conn.task_runner.done():
        conn.task_runner.cancel()
        # E1-09 idiom, as on the approval route: AWAIT the displaced
        # runner before the resume pump starts.
        try:
            await conn.task_runner
        except asyncio.CancelledError:
            pass

    try:
        conn.audit_logger.log_event("TASK_RESUME_REQUESTED", {
            "task_id": task_id,
            "session_id": resume_session_id,
            "audit_verdict": audit["verdict"],
            "confirmed_uncertain": confirm_uncertain,
            "in_flight_tool": (audit.get("in_flight") or {}).get("tool_name"),
            "in_flight_classification": (
                audit.get("in_flight") or {}).get("classification"),
        })
    except Exception:
        logger.debug("Resume audit-log write failed", exc_info=True)

    _RESUMING.add(task_id)
    conn.task_session_id = thread_id
    conn.task_runner = asyncio.create_task(
        conn.execute_task_generator(
            resume_gen, resume_provider_name, resume_model,
            req_id or "resume", session_id=resume_session_id))
    conn.task_runner.add_done_callback(
        lambda _task: _RESUMING.discard(task_id))

    await _reply(conn, req_id, EventType.TASK_RESUME_RESPONSE, {
        "success": True, "task_id": task_id,
        "session_id": resume_session_id, "error": None,
        "requires_confirmation": False, "audit": audit,
    })
