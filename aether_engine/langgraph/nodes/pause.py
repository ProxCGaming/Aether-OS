import logging

from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from aether_engine.browser.policy import BROWSER_TOOL_NAMES
from aether_engine.desktop.policy import DESKTOP_TOOL_NAMES
from aether_engine.langgraph.state import AetherState

logger = logging.getLogger("aether_engine.langgraph.nodes.pause")

_default_audit_logger = None


def _pause_audit_logger(config) -> object:
    """The audit logger for pause-node events: the graph config's
    logger when one is wired, else a lazily-built default (the same
    fallback ToolRegistry.execute uses)."""
    global _default_audit_logger
    configured = (config or {}).get("configurable", {}).get("audit_logger")
    if configured is not None:
        return configured
    if _default_audit_logger is None:
        from aether_engine.audit import AuditLogger

        _default_audit_logger = AuditLogger()
    return _default_audit_logger


def _audit_browser(config, action: str, details: dict) -> None:
    try:
        _pause_audit_logger(config).log_event(action, details)
    except Exception:
        logger.debug("Failed to write %s audit event", action, exc_info=True)


def _evaluate_browser_gate(tool_name: str, args: dict, state, config):
    """Run the E8-6.2 confirmation gate for one browser tool call.

    Returns ``(decision, task_id)``. Evaluation is side-effect free
    on the confirm/deny paths (an envelope is consumed only when it
    authorises the action), which matters here: LangGraph replays
    this node when an interrupt resumes, so anything the confirm
    path did would otherwise happen twice.
    """
    from aether_engine.browser.policy import (
        classify_browser_action,
        evaluate_browser_action,
        page_context_from_session,
        policy_from_user_config,
    )
    from aether_engine.browser.session import (
        BrowserControlState,
        get_browser_manager,
    )

    configurable = (config or {}).get("configurable", {})
    policy = policy_from_user_config(configurable.get("user_config"))
    session = get_browser_manager().active_session
    session_running = bool(
        session is not None
        and session.control_state != BrowserControlState.STOPPED
    )
    classification = classify_browser_action(
        tool_name, args, **page_context_from_session(session))
    task_id = state.get("task_id") or configurable.get("request_id")
    decision = evaluate_browser_action(
        policy, classification, task_id=task_id, session_running=session_running)
    return decision, task_id


def _authorization_record(classification, *, basis: str) -> dict:
    """What the pause authorised, stored on the pending call (M03).

    The execution-time gate compares the FINAL action (post-edit,
    post-hook, fresh page snapshot) against this record before the
    tool runs: category, target identity and payment amount are the
    material facts of the action that was authorised. ``basis`` is
    ``"confirmed"`` when the owner confirmed this exact action at
    the card, ``"policy"`` when the gate auto-allowed it (free /
    envelope / allow-freely) with no individual confirmation.
    """
    return {
        "basis": basis,
        "tool_name": classification.tool_name,
        "category": classification.category,
        "identity": classification.identity,
        "amount": classification.amount,
        "site_host": classification.site_host,
        "description": classification.description,
    }


async def reauthorize_final_browser_action(
    tool_name: str,
    final_args: dict,
    *,
    state,
    config,
    authorization: dict | None,
) -> None:
    """Execution-time browser gate (review finding M03).

    Runs inside ``ToolRegistry.execute`` (Step 2.6), where the
    arguments are FINAL: approval-time edits have been applied and
    tool_call hooks have rewritten what they will. The action is
    classified afresh — against a FRESH page snapshot, re-read from
    the live page, not the pause-time cache — and bound to the
    authorisation the pause recorded:

    - Final action materially matches the authorised one (same
      category, same target identity, same payment amount): the
      original approval / envelope stands. No second prompt.
    - It differs and the pause's basis was an individual owner
      confirmation: that confirmation covered a DIFFERENT action,
      so the final action pauses for a NEW confirmation bound to
      itself — policy modes and envelopes authorised the confirmed
      action, never this one.
    - It differs (or no pause record exists) on any other basis:
      the final classification is evaluated under the policy;
      allow proceeds (an envelope consumed here binds to the
      FINAL classification), deny blocks, confirm pauses as above.

    Returns None when execution may proceed; raises
    ToolExecutionError to block; raises GraphInterrupt (via
    ``interrupt()``) to pause for the new confirmation.
    """
    from aether_engine.browser.policy import (
        action_matches_authorisation,
        classify_browser_action,
        evaluate_browser_action,
        grant_envelope_from_confirmation,
        page_context_from_session,
        policy_from_user_config,
    )
    from aether_engine.browser.session import (
        BrowserControlState,
        get_browser_manager,
    )
    from aether_engine.tools.registry import ToolExecutionError

    configurable = (config or {}).get("configurable", {})
    policy = policy_from_user_config(configurable.get("user_config"))
    session = get_browser_manager().active_session
    session_running = bool(
        session is not None
        and session.control_state != BrowserControlState.STOPPED
    )
    if session is not None:
        await session.refresh_page_snapshot()
    classification = classify_browser_action(
        tool_name, final_args, **page_context_from_session(session))
    task_id = (state or {}).get("task_id") or configurable.get("request_id")
    audit_details = {
        "task_id": task_id,
        "tool_name": tool_name,
        "category": classification.category,
        "site_host": classification.site_host,
        "description": classification.description,
    }

    if action_matches_authorisation(authorization, classification):
        _audit_browser(config, "BROWSER_FINAL_ACTION_AUTHORISED", audit_details)
        return

    basis = authorization.get("basis") if isinstance(authorization, dict) else None
    if basis == "confirmed":
        # The master switch still blocks outright; anything else
        # goes to a fresh confirmation (below). Deliberately NOT
        # evaluate_browser_action: an envelope check here could
        # consume an envelope for an action the owner never saw.
        if not policy.enabled and not session_running:
            _audit_browser(config, "BROWSER_FINAL_ACTION_BLOCKED_DISABLED",
                           audit_details)
            raise ToolExecutionError(
                "The browser agent is disabled in Settings (Browser Agent "
                "Policy), so this browser action was blocked at execution."
            )
    else:
        decision = evaluate_browser_action(
            policy, classification, task_id=task_id,
            session_running=session_running)
        if decision.verdict == "allow":
            _audit_browser(config, "BROWSER_FINAL_ACTION_AUTO_ALLOWED", {
                **audit_details, "reason": decision.reason})
            return
        if decision.verdict == "deny":
            _audit_browser(config, "BROWSER_FINAL_ACTION_BLOCKED_DISABLED",
                           audit_details)
            raise ToolExecutionError(
                "The browser agent is disabled in Settings (Browser Agent "
                "Policy), so this browser action was blocked at execution."
            )

    # ── New confirmation, bound to the FINAL action ──────────────
    _audit_browser(config, "BROWSER_RECONFIRMATION_REQUIRED", audit_details)
    decision_value = interrupt({
        "type": "approval_request",
        "tool_name": tool_name,
        "args": final_args,
        "request_id": configurable.get("request_id"),
        "task_class": configurable.get("task_class", "standard"),
        "browser_confirmation": classification.confirmation_payload(),
        "browser_reconfirmation": True,
    })
    approved = False
    envelope_spec = None
    if isinstance(decision_value, dict):
        approved = bool(decision_value.get("approved"))
        envelope_spec = decision_value.get("browser_envelope")
        edited = decision_value.get("edited_args")
        if approved and isinstance(edited, dict) \
                and changed_arg_fields(final_args, edited):
            # The re-confirmation approves the EXACT final action.
            # An edit here would itself be unvetted — fail closed
            # rather than execute arguments nobody confirmed.
            _audit_browser(config, "BROWSER_RECONFIRMATION_EDITED", audit_details)
            raise ToolExecutionError(
                f"Tool '{tool_name}' was not executed: the arguments were "
                "edited again at the re-confirmation of the changed browser "
                f"action ({classification.description}). Re-issue the action "
                "with the intended arguments so it can be confirmed as a whole."
            )
    elif decision_value:
        approved = True
    if not approved:
        _audit_browser(config, "BROWSER_RECONFIRMATION_DENIED", audit_details)
        raise ToolExecutionError(
            f"Tool '{tool_name}' was not executed: the user declined to "
            f"confirm the changed browser action ({classification.description})."
        )
    _audit_browser(config, "BROWSER_RECONFIRMATION_APPROVED", audit_details)
    if envelope_spec:
        envelope = grant_envelope_from_confirmation(
            task_id=task_id, action=classification, spec=envelope_spec)
        if envelope is not None:
            _audit_browser(config, "BROWSER_ENVELOPE_GRANTED", {
                "task_id": task_id,
                "category": envelope.category,
                "site_host": envelope.site_host,
                "max_actions": envelope.max_actions,
                "expires_at": envelope.expires_at,
            })


def make_browser_final_args_gate(state, config, authorization):
    """The registry final-args gate for one pending browser call:
    a callable ``ToolRegistry.execute`` invokes with the FINAL
    (post-edit, post-hook) arguments before executing (M03)."""

    async def _gate(tool_name: str, final_args: dict) -> None:
        await reauthorize_final_browser_action(
            tool_name, final_args,
            state=state, config=config, authorization=authorization)

    return _gate


# ---------------------------------------------------------------------
# Desktop confirmation gate (master build S20) — the browser gate's
# design on desktop objects. The taxonomy, envelope and gate itself
# live in aether_engine/desktop/policy.py; this section is the
# graph-side plumbing: evaluate at the pause, record what was
# authorised, re-authorise the FINAL arguments at execution (M03
# analogue), and apply the decision's extras (envelope grant,
# remembered grant, pixel session consent) exactly once.
# ---------------------------------------------------------------------

def _audit_desktop(config, action: str, details: dict) -> None:
    try:
        _pause_audit_logger(config).log_event(action, details)
    except Exception:
        logger.debug("Failed to write %s audit event", action, exc_info=True)


def _grant_covers_safely(app: str, category: str) -> bool:
    """The remembered-grant lookup for the gate: a broken grants
    store must tighten (no grant), never crash the gate."""
    try:
        from aether_engine.desktop.grants import get_default_grant_store

        return get_default_grant_store().covers(app, category)
    except Exception:
        logger.debug("Desktop grant lookup failed", exc_info=True)
        return False


def _evaluate_desktop_gate(tool_name: str, args: dict, state, config):
    """Run the S20 confirmation gate for one desktop tool call.

    Returns ``(decision, task_id, session_key)``. Side-effect free
    on the confirm/deny paths (an envelope is consumed only when
    it authorises), like the browser gate — LangGraph replays
    this node on resume. Classification evidence is resolved
    fresh from the screen (desktop/tools._resolve_target, the
    same resolver the tool executes with)."""
    from aether_engine.desktop import get_desktop_actuator
    from aether_engine.desktop.control import get_desktop_control
    from aether_engine.desktop.policy import (
        classify_desktop_action,
        evaluate_desktop_action,
        policy_from_user_config,
    )
    from aether_engine.desktop.tools import _resolve_target

    configurable = (config or {}).get("configurable", {})
    policy = policy_from_user_config(configurable.get("user_config"))
    task_id = state.get("task_id") or configurable.get("request_id")
    session_key = state.get("session_id") or task_id
    actuator = get_desktop_actuator()
    resolved = _resolve_target(actuator, args)
    classification = classify_desktop_action(
        tool_name, args, resolved=resolved)
    control = get_desktop_control()
    decision = evaluate_desktop_action(
        policy, classification, task_id=task_id,
        control_refusal=control.refusal_reason(task_id),
        pixel_consent=control.has_pixel_consent(session_key),
        grant_covers=_grant_covers_safely)
    return decision, task_id, session_key


def _desktop_authorization_record(classification, *, basis: str) -> dict:
    """What the pause authorised for a desktop call (M03
    analogue): the execution-time gate compares the final action
    against this — category, target identity, app, via."""
    return {
        "basis": basis,
        "tool_name": classification.tool_name,
        "category": classification.category,
        "identity": classification.identity,
        "app": classification.app,
        "via": classification.via,
        "description": classification.description,
    }


def _desktop_deny_message(decision) -> str:
    """The model-facing sentence for a gate denial — the refusal
    is a control working, and the model should be able to explain
    it to the user and route around it honestly."""
    from aether_engine.desktop.control import get_desktop_control

    classification = decision.classification
    reason = decision.reason
    if reason == "app_denied":
        return (
            f"Error: The user has denied desktop control for "
            f"{classification.app} (Settings → Computer Use), so "
            "this action was refused and logged. Do not retry it "
            "another way.")
    if reason == "pixel_disabled":
        return (
            "Error: Pixel (mouse/keyboard) input is turned off in "
            "Settings → Computer Use, so this action was refused. "
            "Use an element-based action (invoke / set_value) or "
            "ask the user to enable the pixel fallback.")
    if reason == "vision_disabled":
        return (
            "Error: Vision capture is set to Off in Settings → "
            "Computer Use, so screenshot grounding is refused. "
            "Use an element-based action, or ask the user to "
            "allow vision capture.")
    if reason in ("desktop_paused", "desktop_user_driving",
                  "desktop_stopped_for_task"):
        return "Error: " + get_desktop_control().refusal_message(reason)
    return (
        "Error: Desktop control is not enabled (Settings → "
        "Computer Use: master switch, Control, and the one-time "
        "acknowledgement are all required), so this action was "
        "blocked.")


def _apply_desktop_decision_extras(config, *, task_id, session_key,
                                   classification, decision_value) -> None:
    """Apply an approval's side grants, exactly once (post-
    interrupt / post-reconfirmation code): the per-task envelope
    the card's checkbox granted, the remembered grant, and — for
    a pixel action — the session's pixel consent (approving the
    first pixel action IS the consent the gate asks for). Every
    grantor re-checks the hard rules itself (envelope/grant
    stores refuse the hard-confirm class), so this helper cannot
    widen anything even with a hand-crafted decision value."""
    from aether_engine.desktop.control import get_desktop_control
    from aether_engine.desktop.policy import (
        grant_envelope_from_confirmation,
    )

    audit_details = {
        "task_id": task_id,
        "tool_name": classification.tool_name,
        "category": classification.category,
        "app": classification.app,
        "via": classification.via,
        "description": classification.description,
    }
    spec = decision_value.get("desktop_envelope") \
        if isinstance(decision_value, dict) else None
    if spec:
        envelope = grant_envelope_from_confirmation(
            task_id=task_id, action=classification, spec=spec)
        if envelope is not None:
            _audit_desktop(config, "DESKTOP_ENVELOPE_GRANTED", {
                "task_id": task_id,
                "category": envelope.category,
                "app": envelope.app,
                "max_actions": envelope.max_actions,
                "expires_at": envelope.expires_at,
            })
    if isinstance(decision_value, dict) \
            and decision_value.get("desktop_remember") \
            and classification.app:
        try:
            from aether_engine.desktop.grants import (
                get_default_grant_store,
            )

            if get_default_grant_store().grant(
                    classification.app, classification.category):
                _audit_desktop(config, "DESKTOP_GRANT_REMEMBERED",
                               audit_details)
        except Exception:
            logger.debug("Desktop remembered grant failed", exc_info=True)
    if classification.via == "pixel":
        control = get_desktop_control()
        if not control.has_pixel_consent(session_key):
            control.grant_pixel_consent(session_key)
            _audit_desktop(config, "DESKTOP_PIXEL_CONSENT_GRANTED",
                           audit_details)


async def reauthorize_final_desktop_action(
    tool_name: str,
    final_args: dict,
    *,
    state,
    config,
    authorization: dict | None,
) -> None:
    """Execution-time desktop gate (the M03 analogue).

    Runs inside ``ToolRegistry.execute`` on the FINAL arguments:
    the action is classified afresh — against a fresh screen
    resolution — and bound to the pause's authorisation:

    - Materially the authorised action: proceed, no second prompt.
    - Differs, and the pause's basis was an individual owner
      confirmation: that confirmation covered a DIFFERENT action,
      so the final action pauses for a NEW confirmation bound to
      itself.
    - Differs (or no record) on any other basis: evaluate the
      final classification under the policy; allow proceeds, deny
      blocks, confirm pauses as above.
    """
    from aether_engine.desktop import get_desktop_actuator
    from aether_engine.desktop.control import get_desktop_control
    from aether_engine.desktop.policy import (
        action_matches_authorisation,
        classify_desktop_action,
        evaluate_desktop_action,
        policy_from_user_config,
    )
    from aether_engine.desktop.tools import _resolve_target
    from aether_engine.tools.registry import ToolExecutionError

    configurable = (config or {}).get("configurable", {})
    policy = policy_from_user_config(configurable.get("user_config"))
    task_id = (state or {}).get("task_id") or configurable.get("request_id")
    session_key = (state or {}).get("session_id") or task_id
    actuator = get_desktop_actuator()
    classification = classify_desktop_action(
        tool_name, final_args, resolved=_resolve_target(actuator, final_args))
    audit_details = {
        "task_id": task_id,
        "tool_name": tool_name,
        "category": classification.category,
        "app": classification.app,
        "via": classification.via,
        "description": classification.description,
    }

    if action_matches_authorisation(authorization, classification):
        _audit_desktop(config, "DESKTOP_FINAL_ACTION_AUTHORISED",
                       audit_details)
        return

    basis = authorization.get("basis") if isinstance(authorization, dict) else None
    control = get_desktop_control()
    if basis == "confirmed":
        # Deliberately NOT evaluate_desktop_action: an envelope or
        # grant consulted here could authorise an action the owner
        # never saw. Only the outright blocks apply before the
        # fresh confirmation.
        refusal = control.refusal_reason(task_id)
        if not policy.control_permitted or refusal \
                or (classification.app
                    and policy.app_verdict(classification.app) == "deny"):
            _audit_desktop(config, "DESKTOP_FINAL_ACTION_BLOCKED",
                           audit_details)
            raise ToolExecutionError(
                "This desktop action was blocked at execution: "
                "desktop control is no longer permitted for it.")
    else:
        decision = evaluate_desktop_action(
            policy, classification, task_id=task_id,
            control_refusal=control.refusal_reason(task_id),
            pixel_consent=control.has_pixel_consent(session_key),
            grant_covers=_grant_covers_safely)
        if decision.verdict == "allow":
            _audit_desktop(config, "DESKTOP_FINAL_ACTION_AUTO_ALLOWED", {
                **audit_details, "reason": decision.reason})
            return
        if decision.verdict == "deny":
            _audit_desktop(config, "DESKTOP_FINAL_ACTION_BLOCKED",
                           audit_details)
            raise ToolExecutionError(_desktop_deny_message(decision))

    # ── New confirmation, bound to the FINAL action ──────────────
    _audit_desktop(config, "DESKTOP_RECONFIRMATION_REQUIRED", audit_details)
    decision_value = interrupt({
        "type": "approval_request",
        "tool_name": tool_name,
        "args": final_args,
        "request_id": configurable.get("request_id"),
        "task_class": configurable.get("task_class", "standard"),
        "desktop_confirmation": classification.confirmation_payload(),
        "desktop_reconfirmation": True,
    })
    approved = False
    if isinstance(decision_value, dict):
        approved = bool(decision_value.get("approved"))
        edited = decision_value.get("edited_args")
        if approved and isinstance(edited, dict) \
                and changed_arg_fields(final_args, edited):
            _audit_desktop(config, "DESKTOP_RECONFIRMATION_EDITED",
                           audit_details)
            raise ToolExecutionError(
                f"Tool '{tool_name}' was not executed: the arguments "
                "were edited again at the re-confirmation of the "
                f"changed desktop action ({classification.description}). "
                "Re-issue the action with the intended arguments so "
                "it can be confirmed as a whole.")
    elif decision_value:
        approved = True
    if not approved:
        _audit_desktop(config, "DESKTOP_RECONFIRMATION_DENIED", audit_details)
        raise ToolExecutionError(
            f"Tool '{tool_name}' was not executed: the user declined "
            f"to confirm the changed desktop action "
            f"({classification.description}).")
    _audit_desktop(config, "DESKTOP_RECONFIRMATION_APPROVED", audit_details)
    _apply_desktop_decision_extras(
        config, task_id=task_id, session_key=session_key,
        classification=classification, decision_value=decision_value)


def make_desktop_final_args_gate(state, config, authorization):
    """The registry final-args gate for one pending desktop call
    (the M03 analogue of make_browser_final_args_gate)."""

    async def _gate(tool_name: str, final_args: dict) -> None:
        await reauthorize_final_desktop_action(
            tool_name, final_args,
            state=state, config=config, authorization=authorization)

    return _gate


def changed_arg_fields(original_args: dict, edited_args: dict) -> list[str]:
    """Field names whose value differs between two argument dicts.

    Epic 8, E8-1.3: the diff primitive behind the approval-edit audit
    event and the "edited" marker. Names only — values are compared
    here but never returned, logged, or stored by this helper. Sorted
    so audit entries and tests are deterministic.
    """
    original = original_args or {}
    edited = edited_args or {}
    return sorted(
        key
        for key in set(original) | set(edited)
        if key not in original or key not in edited or original[key] != edited[key]
    )


def _edit_preview(args: dict, workspace_roots: list) -> dict:
    """Best-effort diff preview for an edit_file approval (Epic 2, E2-03).

    Computed at pause time from the file as it stands NOW. Any failure
    (stale hash, bad anchor, containment) becomes a preview error the
    card shows — the preview must never break the pause itself, and
    execution re-verifies everything in the worker regardless.
    """
    try:
        from aether_common.text_edit import (
            apply_text_edits,
            content_hash,
            make_unified_diff,
        )
        from aether_engine.tools.paths import resolve_contained

        target = resolve_contained(str(args.get("path", "")), workspace_roots)
        data = target.read_bytes()
        # E2-F01: same rule as execution — a supplied hash (even an
        # empty string) is checked; only an absent/None hash is not.
        expected = args.get("expected_hash")
        if expected is not None and content_hash(data) != expected:
            return {"error": "The file changed since the agent read it — this edit will be refused at execution unless the agent re-reads the file."}
        old_text = data.decode("utf-8")
        new_text = apply_text_edits(old_text, args.get("edits") or [])
        return {"diff": make_unified_diff(old_text, new_text, str(args.get("path")))}
    except Exception as exc:  # noqa: BLE001 — preview boundary: a failed preview degrades to an error note, never a broken approval pause.
        return {"error": f"Preview unavailable: {exc}"}

def dummy_pause_node(state: AetherState, config: RunnableConfig) -> dict:
    """A node that uses LangGraph native interrupt for human-in-the-loop approval."""
    pending = state.get("pending_tool_call")
    if not pending:
        return {}

    tool_name = pending.get("name")
    args = pending.get("args", {})
    call_id = pending.get("call_id") or f"call_{tool_name}"
    req_id = config.get("configurable", {}).get("request_id")

    # ── Browser confirmation gate (Epic 8, E8-6.2) ──────────────────
    # Browser writes always pass through this node (tool_router), and
    # the per-category policy decides what the generic approval means
    # for them: free / in-envelope / allow-freely actions are approved
    # in place with NO pause; a disabled browser agent rejects new
    # tasks in place; anything else falls through to the interrupt
    # below with a confirmation payload naming the exact action.
    browser_decision = None
    browser_task_id = None
    if tool_name in BROWSER_TOOL_NAMES:
        browser_decision, browser_task_id = _evaluate_browser_gate(
            tool_name, args, state, config)
        classification = browser_decision.classification
        audit_details = {
            "task_id": browser_task_id,
            "tool_name": tool_name,
            "category": classification.category,
            "site_host": classification.site_host,
            "reason": browser_decision.reason,
        }
        if browser_decision.verdict == "allow":
            _audit_browser(config, "BROWSER_ACTION_AUTO_ALLOWED", audit_details)
            return {"pending_tool_call": {
                **pending,
                "approved": True,
                # M03: what this auto-allow authorises, so the
                # execution-time gate can bind it to the final args.
                "browser_authorization": _authorization_record(
                    classification, basis="policy"),
            }}
        if browser_decision.verdict == "deny":
            _audit_browser(config, "BROWSER_ACTION_BLOCKED_DISABLED", audit_details)
            msg = {
                "role": "tool",
                "name": tool_name,
                "content": (
                    "Error: The browser agent is disabled in Settings "
                    "(Browser Agent Policy), so this browser action was "
                    "blocked. The user can enable it there to allow new "
                    "browser tasks."
                ),
                "tool_call_id": call_id,
            }
            return {"pending_tool_call": None, "messages": [msg]}

    # ── Desktop confirmation gate (master build S20) ────────────────
    # Desktop actuation always passes through this node (tool_router),
    # and the desktop policy (Settings → Computer Use) decides what
    # the generic approval means: envelope / remembered-grant /
    # allow-freely actions are approved in place with NO pause;
    # denials (control off, app denied, lane stopped/paused, pixel
    # or vision disabled) reject in place with the reason; anything
    # else falls through to the interrupt with a confirmation
    # payload naming the exact action. The hard-confirm categories
    # (close_or_quit, system) ALWAYS reach the interrupt.
    desktop_decision = None
    desktop_task_id = None
    desktop_session_key = None
    if tool_name in DESKTOP_TOOL_NAMES:
        desktop_decision, desktop_task_id, desktop_session_key = \
            _evaluate_desktop_gate(tool_name, args, state, config)
        classification = desktop_decision.classification
        audit_details = {
            "task_id": desktop_task_id,
            "tool_name": tool_name,
            "category": classification.category,
            "app": classification.app,
            "via": classification.via,
            "reason": desktop_decision.reason,
        }
        if desktop_decision.verdict == "allow":
            _audit_desktop(config, "DESKTOP_ACTION_AUTO_ALLOWED", audit_details)
            return {"pending_tool_call": {
                **pending,
                "approved": True,
                "desktop_authorization": _desktop_authorization_record(
                    classification, basis="policy"),
            }}
        if desktop_decision.verdict == "deny":
            _audit_desktop(config, "DESKTOP_ACTION_DENIED", audit_details)
            msg = {
                "role": "tool",
                "name": tool_name,
                "content": _desktop_deny_message(desktop_decision),
                "tool_call_id": call_id,
            }
            return {"pending_tool_call": None, "messages": [msg]}

    logger.info(f"Interrupting graph for approval of {tool_name}")
    
    # Pause execution and save state to sqlite
    interrupt_payload = {
        "type": "approval_request",
        "tool_name": tool_name,
        "args": args,
        "request_id": req_id,
        "task_class": config.get("configurable", {}).get("task_class", "standard")
    }
    if tool_name == "edit_file":
        roots = config.get("configurable", {}).get("workspace_roots") or []
        interrupt_payload["preview"] = _edit_preview(args, roots)
    if browser_decision is not None:
        # The confirmation card names the EXACT action (amount and
        # merchant from the page — see browser/policy.py).
        interrupt_payload["browser_confirmation"] = \
            browser_decision.classification.confirmation_payload()
    if desktop_decision is not None:
        # The confirmation card names the EXACT desktop action
        # (app + element as the OS states them — desktop/policy.py).
        interrupt_payload["desktop_confirmation"] = \
            desktop_decision.classification.confirmation_payload()
    decision = interrupt(interrupt_payload)

    # Epic 8, E8-1.3: the resume value is either the legacy boolean or
    # {"approved": bool, "edited_args": dict | None}. Edited args are
    # arguments ONLY — the tool name is never taken from the decision,
    # so an edit cannot swap the tool. The edited args are not trusted
    # here: execute_tool re-runs the full policy pipeline on them.
    approved = False
    edited_args = None
    if isinstance(decision, dict):
        approved = bool(decision.get("approved"))
        if isinstance(decision.get("edited_args"), dict):
            edited_args = decision["edited_args"]
    elif decision:
        approved = True

    if not approved:
        logger.info(f"User rejected {tool_name}")
        if browser_decision is not None:
            _audit_browser(config, "BROWSER_CONFIRMATION_DENIED", {
                "task_id": browser_task_id,
                "tool_name": tool_name,
                "category": browser_decision.classification.category,
                "site_host": browser_decision.classification.site_host,
                "description": browser_decision.classification.description,
            })
        if desktop_decision is not None:
            _audit_desktop(config, "DESKTOP_CONFIRMATION_DENIED", {
                "task_id": desktop_task_id,
                "tool_name": tool_name,
                "category": desktop_decision.classification.category,
                "app": desktop_decision.classification.app,
                "description": desktop_decision.classification.description,
            })
        # Clear the tool call and return a rejection message
        msg = {
            "role": "tool",
            "name": tool_name,
            "content": "Error: User rejected the execution of this tool.",
            "tool_call_id": call_id
        }
        return {
            "pending_tool_call": None,
            "messages": [msg]
        }

    if browser_decision is not None:
        # Post-interrupt code runs exactly once (on the resume
        # replay), so the decision audit + envelope grant land once.
        _audit_browser(config, "BROWSER_CONFIRMATION_APPROVED", {
            "task_id": browser_task_id,
            "tool_name": tool_name,
            "category": browser_decision.classification.category,
            "site_host": browser_decision.classification.site_host,
            "description": browser_decision.classification.description,
        })
        if isinstance(decision, dict) and decision.get("browser_envelope"):
            from aether_engine.browser.policy import (
                grant_envelope_from_confirmation,
            )

            envelope = grant_envelope_from_confirmation(
                task_id=browser_task_id,
                action=browser_decision.classification,
                spec=decision.get("browser_envelope"),
            )
            if envelope is not None:
                _audit_browser(config, "BROWSER_ENVELOPE_GRANTED", {
                    "task_id": browser_task_id,
                    "category": envelope.category,
                    "site_host": envelope.site_host,
                    "max_actions": envelope.max_actions,
                    "expires_at": envelope.expires_at,
                })

    if desktop_decision is not None:
        # Post-interrupt, exactly once (resume replay): decision
        # audit + the approval's side grants (envelope, remembered
        # grant, pixel session consent).
        _audit_desktop(config, "DESKTOP_CONFIRMATION_APPROVED", {
            "task_id": desktop_task_id,
            "tool_name": tool_name,
            "category": desktop_decision.classification.category,
            "app": desktop_decision.classification.app,
            "description": desktop_decision.classification.description,
        })
        _apply_desktop_decision_extras(
            config, task_id=desktop_task_id,
            session_key=desktop_session_key,
            classification=desktop_decision.classification,
            decision_value=decision)

    # If approved, mutate the pending_tool_call to reflect approval, and graph proceeds to execute_tool
    updated = {**pending, "approved": True}
    if browser_decision is not None:
        # M03: the owner confirmed THIS classification (the action
        # named on the card, from the pre-edit args). Edited args
        # below replace the pending args, but the authorisation
        # stays bound to what was actually confirmed — the
        # execution-time gate reconciles the two.
        updated["browser_authorization"] = _authorization_record(
            browser_decision.classification, basis="confirmed")
    if desktop_decision is not None:
        # The M03 analogue: bound to the classification the owner
        # confirmed, reconciled against the final args at execution.
        updated["desktop_authorization"] = _desktop_authorization_record(
            desktop_decision.classification, basis="confirmed")
    if edited_args is not None and changed_arg_fields(args, edited_args):
        logger.info(
            f"User edited arguments for {tool_name} at approval: "
            f"{changed_arg_fields(args, edited_args)}"
        )
        updated["args"] = edited_args
        updated["edited"] = True
    return {
        "pending_tool_call": updated
    }
