"""Live desktop control lane: Pause / Take control / Stop (S20).

The desktop analogue of the browser's :class:`BrowserControlState`,
with one structural difference the brief requires: the browser
parks a SESSION (one tab belongs to one task); desktop control is
a single machine-wide lane, and "Stop" is per TASK — stopping the
desktop lane for one task refuses that task's further desktop
actions with a clear reason while the task itself continues in
chat. Nothing here kills a task, and only the OWNER (through the
chat window's control card) can pause, take control, return
control, resume, or stop — there is deliberately no agent path
that returns control to the agent.

States (machine-wide lane):

- ``AGENT``  — the agent drives; desktop actions may run (each
  still policy-gated).
- ``PAUSED`` — the owner paused the lane; actions are refused in
  place until the owner resumes.
- ``USER``   — the owner took control (they are driving the
  desktop themselves); actions are refused until the owner
  returns control.

Per-task state:

- a stopped set: tasks whose desktop lane the owner stopped.
  Stopping names a task; stopping with no task id stops the task
  most recently driving (the one the control card is about). A
  stop lasts for the life of the task (or until the engine
  restarts) — there is deliberately no "un-stop": a stopped lane
  staying stopped is the safe failure mode, and the owner starts
  a fresh task to drive again.
- a pixel-consent set: sessions whose owner approved the first
  pixel action (the per-session consent the policy gate demands).
  Keyed by chat session id, falling back to task id.
"""
from __future__ import annotations

import logging
from enum import Enum

logger = logging.getLogger("aether_engine.desktop.control")


class DesktopControlState(Enum):
    AGENT = "agent"
    PAUSED = "paused"
    USER = "user"


class DesktopControl:
    """The machine-wide desktop control lane + per-task stops."""

    def __init__(self) -> None:
        self.state = DesktopControlState.AGENT
        self._stopped_tasks: set[str] = set()
        self._pixel_consented: set[str] = set()
        self._driving_task: str | None = None

    # --------------------------------------------------------------
    # Refusals
    # --------------------------------------------------------------

    def refusal_reason(self, task_id: str | None = None) -> str | None:
        """Why a desktop action must not run right now, or None.

        Mirrors ``BrowserControlState._require_agent``'s per-state
        messages — the reason is shown to the model (so it can tell
        the user) and to the audit log."""
        if task_id and task_id in self._stopped_tasks:
            return "desktop_stopped_for_task"
        if self.state is DesktopControlState.PAUSED:
            return "desktop_paused"
        if self.state is DesktopControlState.USER:
            return "desktop_user_driving"
        return None

    def refusal_message(self, reason: str) -> str:
        """The model-facing sentence for one refusal reason."""
        return {
            "desktop_stopped_for_task": (
                "The user stopped desktop control for this task. "
                "Do not retry desktop actions in this task; continue "
                "in chat, or ask the user to start a new task if "
                "desktop control is still needed."),
            "desktop_paused": (
                "Desktop control is paused by the user. Do not "
                "retry until the user resumes it from the chat "
                "window's desktop control card."),
            "desktop_user_driving": (
                "The user has taken control of the desktop. Do not "
                "interfere; wait until they return control from the "
                "chat window's desktop control card."),
        }.get(reason, "Desktop control is not available right now.")

    # --------------------------------------------------------------
    # Driving task + pixel consent
    # --------------------------------------------------------------

    def note_driving_task(self, task_id: str | None) -> None:
        if task_id:
            self._driving_task = task_id

    @property
    def driving_task(self) -> str | None:
        return self._driving_task

    def grant_pixel_consent(self, key: str | None) -> None:
        """Record the owner's approval of this session's first
        pixel action — the consent the gate demands before any
        further pixel input in the session runs under policy."""
        if key:
            self._pixel_consented.add(key)

    def has_pixel_consent(self, key: str | None) -> bool:
        return bool(key) and key in self._pixel_consented

    def is_task_stopped(self, task_id: str | None) -> bool:
        return bool(task_id) and task_id in self._stopped_tasks

    # --------------------------------------------------------------
    # Owner controls (the WS handler surface)
    # --------------------------------------------------------------

    def control(self, action: str, *, task_id: str | None = None) -> dict:
        """Apply one owner control action. Returns the result dict
        the WS handler merges into DESKTOP_CONTROL_RESPONSE.

        Actions: pause / resume / take_control / return_control /
        stop. Unknown actions fail with an error, never a guess.
        """
        action = (action or "").strip().lower()
        previous = self.state
        stopped_task: str | None = None
        if action == "pause":
            if self.state is DesktopControlState.AGENT:
                self.state = DesktopControlState.PAUSED
        elif action == "resume":
            if self.state is DesktopControlState.PAUSED:
                self.state = DesktopControlState.AGENT
        elif action == "take_control":
            self.state = DesktopControlState.USER
        elif action == "return_control":
            if self.state is DesktopControlState.USER:
                self.state = DesktopControlState.AGENT
        elif action == "stop":
            stopped_task = task_id or self._driving_task
            if stopped_task:
                self._stopped_tasks.add(stopped_task)
        else:
            return {
                "success": False,
                "error": f"Unknown desktop control action: {action!r}.",
                **self.status(),
            }
        self._audit(action, previous, stopped_task)
        return {"success": True, **self.status(), "stopped_task": stopped_task}

    def status(self) -> dict:
        return {
            "control_state": self.state.value,
            "stopped_tasks": sorted(self._stopped_tasks),
            "driving_task": self._driving_task,
        }

    def _audit(self, action: str, previous: DesktopControlState,
               stopped_task: str | None) -> None:
        """Best-effort audit (the pause node's pattern: an audit
        failure must never break the control itself)."""
        try:
            _default_audit_logger().log_event(
                "DESKTOP_CONTROL_CHANGED",
                {
                    "action": action,
                    "from_state": previous.value,
                    "to_state": self.state.value,
                    "stopped_task": stopped_task,
                },
            )
        except Exception:
            logger.debug("Desktop control audit failed", exc_info=True)


_audit_logger_singleton = None


def _default_audit_logger():
    """The lazily-built default audit logger (the pause node's
    fallback pattern)."""
    global _audit_logger_singleton
    if _audit_logger_singleton is None:
        from aether_engine.audit import AuditLogger

        _audit_logger_singleton = AuditLogger()
    return _audit_logger_singleton


_control: DesktopControl | None = None


def get_desktop_control() -> DesktopControl:
    global _control
    if _control is None:
        _control = DesktopControl()
    return _control


def reset_desktop_control() -> None:
    """Replace the process-wide lane (tests; engine shutdown)."""
    global _control
    _control = DesktopControl()
