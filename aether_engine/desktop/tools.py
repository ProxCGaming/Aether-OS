"""The ``desktop_act`` tool (S20): actuation, behind the gate.

ONE tool with an ``action`` verb — the desktop analogue of the
browser tools, built to ride the exact approval machinery they
use: it is an ordinary registry tool (source "desktop", risk
WRITE) executed by specialists, so the graph's approval pause
classifies every call through ``desktop/policy.py`` before it
runs, and the registry re-runs the classification on the final
arguments at execution time (the M03 analogue). The supervisor
deliberately does NOT get this tool: supervisor tools have no
approval pause (the S8 gap), and actuation without the pause is
exactly what S20 must never build.

Layering inside one call (the O1 decision, in order):

1. UIA by element first — invoke / set_value against the element
   a fresh read resolves from the locator.
2. Vision grounding only for UIA-blind targets — ``ground_click``:
   a screenshot the policy gate approved, coordinates +
   confidence, verified against UIA where possible, low
   confidence refused (``desktop/vision.py``).
3. Raw pixel input — ``click_at`` / ``type_text`` / ``key_press``:
   its own switchable class, consent-gated per session.

Execution re-checks at the last moment what the gate checked at
approval time — control permitted, the live control lane, the
app denial — because the screen (and the owner's switches) may
have changed between the two. Every outcome is audit-logged and
published as a structured NODE_ACTIVITY event in the S12 shape
(action "desktop_action"), which the Task Ledger taps into its
``desktop_actions`` list.

No tool here raises: outcomes are plain-language strings the
model can act on (including the refusals — a refusal the model
can explain to the user is the control working, not an error).
"""
from __future__ import annotations

import logging
import time
from typing import Any

from aether_engine.desktop import policy as desktop_policy
from aether_engine.desktop.control import get_desktop_control
from aether_engine.desktop.policy import (
    CATEGORY_CLOSE_OR_QUIT,
    CATEGORY_SYSTEM,
    HARD_CONFIRM_CATEGORIES,
    classify_desktop_action,
)
from aether_engine.tools.registry import Tool, ToolRisk

logger = logging.getLogger("aether_engine.desktop.tools")

DESKTOP_ACT = "desktop_act"

#: Clipboard reads are capped before they reach the model (the
#: clipboard can hold megabytes; a tool result cannot).
CLIPBOARD_RESULT_MAX_CHARS = 4000


def _audit(action: str, details: dict) -> None:
    """Best-effort audit — an audit failure must never break (or
    block) an actuation outcome the user already decided."""
    try:
        from aether_engine.audit import AuditLogger

        AuditLogger().log_event(action, details)
    except Exception:
        logger.debug("Desktop audit %s failed", action, exc_info=True)


def _publish_activity(task_id: str | None, *, classification,
                      status: str, detail: str = "") -> None:
    """The S12-shaped NODE_ACTIVITY event for one desktop action.
    No task id (an out-of-graph call) means no stream to publish
    to — the audit record still exists."""
    if not task_id:
        return
    try:
        from aether_common.contracts import Event, EventType
        from aether_engine.event_bus import event_bus

        event_bus.publish(task_id, Event(
            type=EventType.NODE_ACTIVITY,
            request_id=task_id,
            payload={
                "task_id": task_id,
                "from_node": "specialist",
                "to_node": "desktop",
                "action": "desktop_action",
                "reason": classification.description,
                "briefing": "",
                "input_payload": {
                    "action": classification.action,
                    "category": classification.category,
                    "app": classification.app,
                    "via": classification.via,
                },
                "output_payload": {
                    "status": status,
                    "category": classification.category,
                    "app": classification.app,
                    "via": classification.via,
                    "detail": detail[:500],
                },
                "status": status,
                "timestamp": int(time.time()),
            },
        ))
    except Exception:
        logger.debug("Desktop activity publish failed", exc_info=True)


def _locator_from_args(args: dict[str, Any]) -> dict[str, Any]:
    element = args.get("element")
    if isinstance(element, dict):
        return {
            "name": element.get("name"),
            "role": element.get("role"),
            "automation_id": element.get("automation_id"),
        }
    return {
        "name": args.get("name"),
        "role": args.get("role"),
        "automation_id": args.get("automation_id"),
    }


async def _execute_desktop_act(**args: Any) -> str:
    from aether_engine.cost.ledger import get_usage_context
    from aether_engine.desktop import get_desktop_actuator

    ctx = get_usage_context()
    task_id = ctx.get("task_id")
    verb = str(args.get("action") or "").strip().lower()

    policy = desktop_policy.current_desktop_policy()
    control = get_desktop_control()
    control.note_driving_task(task_id)

    # Execution-time re-checks (the gate ran at approval; the
    # world may have moved since — most importantly the owner may
    # have stopped the lane or flipped a switch mid-task).
    actuator = get_desktop_actuator()
    resolved = _resolve_target(actuator, args)
    classification = classify_desktop_action(
        DESKTOP_ACT, args, resolved=resolved)

    def _finish(status: str, message: str, *, log_detail: str = "") -> str:
        # log_detail keeps sensitive payloads (clipboard text) out
        # of the audit record and the activity stream: the model
        # gets `message`, the records get the redacted form.
        recorded = log_detail or message
        _audit("DESKTOP_ACTION_EXECUTED" if status == "completed"
               else "DESKTOP_ACTION_FAILED", {
                   "task_id": task_id,
                   "action": verb,
                   "category": classification.category,
                   "app": classification.app,
                   "via": classification.via,
                   "status": status,
                   "detail": recorded[:500],
               })
        _publish_activity(task_id, classification=classification,
                          status=status, detail=recorded)
        return message

    if not policy.control_permitted:
        return _finish(
            "failed",
            "Desktop control is not enabled (Settings → Computer "
            "Use: the master switch, Control, and the one-time "
            "acknowledgement are all required). The action was "
            "not performed.")
    refusal = control.refusal_reason(task_id)
    if refusal:
        return _finish("failed", control.refusal_message(refusal))
    if classification.app \
            and policy.app_verdict(classification.app) == "deny":
        return _finish(
            "failed",
            f"The user has denied desktop control for "
            f"{classification.app}; the action was refused and "
            "logged. Do not retry it another way.")

    available, reason = actuator.availability()
    if not available:
        # Clipboard rides the same backend; availability covers
        # both halves (provider + adapter).
        return _finish(
            "failed", f"Desktop actuation is unavailable: {reason}")

    try:
        if verb == "focus_window":
            if resolved.window is None:
                return _finish("failed", "No window matched the target; nothing was focused.")
            ok, detail = await actuator.focus_window(resolved)
            return _finish("completed" if ok else "failed",
                           detail or ("Window focused." if ok else "Focus failed."))

        if verb == "close_window":
            if resolved.window is None:
                return _finish("failed", "No window matched the target; nothing was closed.")
            ok, detail = await actuator.close_window(resolved)
            return _finish("completed" if ok else "failed",
                           detail or ("Window closed." if ok else "Close failed."))

        if verb == "invoke":
            if resolved.element is None:
                return _finish(
                    "failed",
                    "The element could not be resolved on the "
                    "current screen (not found, or ambiguous). "
                    "Re-read the screen with get_screen_state and "
                    "use the exact name/role it reports.")
            if not resolved.element.can_invoke:
                return _finish(
                    "failed",
                    f"'{resolved.element.name}' does not support "
                    "UI Automation Invoke. If it needs a real click, "
                    "use click_at on its location (pixel input "
                    "rules apply) — do not assume it worked.")
            ok, detail = await actuator.invoke(resolved)
            return _finish("completed" if ok else "failed",
                           detail or ("Invoked." if ok else "Invoke failed."))

        if verb == "set_value":
            if resolved.element is None:
                return _finish(
                    "failed",
                    "The element could not be resolved on the "
                    "current screen (not found, or ambiguous). "
                    "Re-read the screen and retry with the exact "
                    "name/role it reports.")
            if not resolved.element.can_set_value:
                return _finish(
                    "failed",
                    f"'{resolved.element.name}' does not accept a "
                    "settable value through UI Automation. Focus it "
                    "and use type_text instead if appropriate.")
            ok, detail = await actuator.set_value(
                resolved, str(args.get("value") or ""))
            return _finish("completed" if ok else "failed",
                           detail or ("Value set." if ok else "Set failed."))

        if verb == "type_text":
            ok, detail = await actuator.type_text(str(args.get("text") or ""))
            return _finish("completed" if ok else "failed",
                           detail or ("Text typed." if ok else "Typing failed."))

        if verb == "key_press":
            ok, detail = await actuator.key_press(str(args.get("keys") or ""))
            return _finish("completed" if ok else "failed",
                           detail or ("Keys pressed." if ok else "Key press failed."))

        if verb == "click_at":
            x, y = args.get("x"), args.get("y")
            if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
                return _finish("failed", "click_at needs numeric x and y coordinates.")
            # Prefer the element route when the point lands on an
            # invocable element: UIA first is the standing order,
            # and a resolved Invoke is both safer and more honest
            # than a synthetic click at the same target.
            if resolved.element is not None and resolved.element.can_invoke:
                ok, detail = await actuator.invoke(resolved)
                if ok:
                    return _finish(
                        "completed",
                        f"Invoked '{resolved.element.name}' through "
                        "UI Automation (preferred over a pixel "
                        "click at the same point).")
            ok, detail = await actuator.mouse_click(int(x), int(y))
            return _finish("completed" if ok else "failed",
                           detail or (f"Clicked at ({int(x)}, {int(y)})." if ok else "Click failed."))

        if verb == "ground_click":
            return await _ground_click(args, classification, _finish)

        if verb == "clipboard_get":
            ok, text = await actuator.clipboard_get()
            if not ok:
                return _finish("failed", text)
            clipped = text[:CLIPBOARD_RESULT_MAX_CHARS]
            suffix = (" …[clipboard text truncated]"
                      if len(text) > CLIPBOARD_RESULT_MAX_CHARS else "")
            return _finish(
                "completed", f"Clipboard text: {clipped}{suffix}",
                log_detail=f"Read the clipboard ({len(text)} characters).")

        if verb == "clipboard_set":
            ok, detail = await actuator.clipboard_set(str(args.get("text") or ""))
            return _finish("completed" if ok else "failed",
                           detail or ("Clipboard replaced." if ok else "Clipboard set failed."))

        return _finish(
            "failed",
            f"Unknown desktop action {verb!r}. Valid actions: "
            "focus_window, close_window, invoke, set_value, "
            "type_text, key_press, click_at, ground_click, "
            "clipboard_get, clipboard_set.")
    except Exception as exc:
        logger.warning("desktop_act %s failed unexpectedly", verb, exc_info=True)
        return _finish("failed", f"The desktop action failed: {exc}")


def _resolve_target(actuator, args: dict[str, Any]):
    """The resolution dispatch shared by the gate (pause node) and
    this tool: each verb's evidence comes from the read matching
    its target kind. Imported by the pause node so the gate and
    the execution resolve IDENTICALLY (one implementation — the
    classification can only be as honest as its evidence)."""
    verb = str(args.get("action") or "").strip().lower()
    if verb in ("invoke", "set_value"):
        return actuator.resolve_now(_locator_from_args(args))
    if verb in ("focus_window", "close_window"):
        return actuator.resolve_window_now(
            app=args.get("app"), title=args.get("title"))
    if verb == "click_at":
        x, y = args.get("x"), args.get("y")
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            return actuator.element_at_point_now(int(x), int(y))
        return None
    if verb == "type_text":
        return actuator.focused_now()
    return None


async def _ground_click(args: dict[str, Any], classification,
                        finish) -> str:
    """The vision-grounding execution: ground, re-check severity
    against the VERIFIED element, then click. The gate classified
    this action as generic interact (the model's description is
    all it had); if UIA verification finds the point is actually
    a close/quit or system control, the click is REFUSED here —
    a screenshot must never route around the hard-confirm gate,
    and the model is told to use the element route, where the
    owner will be asked properly."""
    from aether_engine.desktop import get_desktop_actuator
    from aether_engine.desktop.vision import ground_target

    description = str(args.get("description") or "")
    if not description:
        return finish("failed", "ground_click needs a description of the control to find.")
    result = await ground_target(description)
    if not result.ok:
        return finish("failed", result.reason)
    verified = result.verified_element
    element = getattr(verified, "element", None) if verified else None
    if element is not None:
        recheck = classify_desktop_action(
            DESKTOP_ACT,
            {"action": "invoke",
             "element": {"name": element.name, "role": element.role,
                         "automation_id": element.automation_id}},
            resolved=verified)
        if recheck.category in HARD_CONFIRM_CATEGORIES:
            label = CATEGORY_CLOSE_OR_QUIT if recheck.category == CATEGORY_CLOSE_OR_QUIT \
                else CATEGORY_SYSTEM
            return finish(
                "failed",
                f"Vision grounding resolved '{description}' to "
                f"'{element.name}', which is a {label.replace('_', ' ')} "
                "control. Those always need their own confirmation, "
                "so the click was refused: use invoke on that "
                "element instead, and the user will be asked.")
    actuator = get_desktop_actuator()
    ok, detail = await actuator.mouse_click(int(result.x), int(result.y))
    if not ok:
        return finish("failed", detail or "The grounded click failed.")
    verified_note = f"; verified as '{element.name}'" if element is not None else ""
    return finish(
        "completed",
        f"Clicked '{description}' at ({result.x}, {result.y}) "
        f"via vision grounding (confidence "
        f"{result.confidence:.2f}{verified_note}).")


def create_desktop_tools() -> list[Tool]:
    """The desktop tool family: one actuation tool. Observe lives
    with the supervisor (S19); this is control."""
    return [
        Tool(
            name=DESKTOP_ACT,
            description=(
                "Act on the user's desktop (Windows UI Automation "
                "first; every call is policy-gated and may pause for "
                "the user's confirmation — closing/quitting and "
                "system actions always do). Actions: "
                "invoke {element:{name, role?, automation_id?}} — "
                "activate a control by its accessibility identity "
                "(from get_screen_state); "
                "set_value {element, value} — set a field's value; "
                "focus_window {app?, title?} / close_window "
                "{app?, title?} — window primitives; "
                "type_text {text} / key_press {keys} — keyboard "
                "input (pixel class); "
                "click_at {x, y} — click at screen coordinates "
                "(pixel class; an invocable element at the point "
                "is invoked through UIA instead); "
                "ground_click {description} — for controls "
                "accessibility cannot resolve: a screenshot is "
                "grounded by the vision model and clicked, with "
                "UIA verification; "
                "clipboard_get / clipboard_set {text}. "
                "Prefer invoke/set_value; use pixel actions only "
                "when the element route cannot reach the target."),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "focus_window", "close_window", "invoke",
                            "set_value", "type_text", "key_press",
                            "click_at", "ground_click",
                            "clipboard_get", "clipboard_set",
                        ],
                    },
                    "element": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "role": {"type": "string"},
                            "automation_id": {"type": "string"},
                        },
                    },
                    "value": {"type": "string"},
                    "text": {"type": "string"},
                    "keys": {"type": "string"},
                    "x": {"type": "number"},
                    "y": {"type": "number"},
                    "app": {"type": "string"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["action"],
            },
            execute_fn=_execute_desktop_act,
            risk=ToolRisk.WRITE,
            timeout_s=60,
            source="desktop",
        ),
    ]
