"""Tool hooks — guardrails without pipeline edits (Epic 7, E7-01).

The single tool pipeline (``ToolRegistry.execute``) consults this
registry at two points:

- ``tool_call`` hooks run after policy + pre-flight validation and
  before execution. A hook may:
    * return ``None`` — pass the call through unchanged;
    * return a ``dict`` — replace the call arguments (audited);
    * raise :class:`HookBlock` — block the call (audited).
- ``tool_result`` hooks run after a successful execution. A hook
  may return ``None`` (keep the result) or a ``str`` (replace the
  result the agent sees; audited).

Fail-closed semantics (a throwing handler must never silently pass
— the gaps addendum's rule):
  * a ``tool_call`` hook that raises anything other than HookBlock
    blocks the call;
  * a ``tool_result`` hook that raises replaces the output with a
    hook-failure notice — the raw output does not reach the agent,
    because the hook existed precisely to vet it.

Registration is code-only in v1: guardrails register at engine
start (or in tests) via ``register_tool_call_hook`` /
``register_tool_result_hook``. Nothing here loads code from config
or from disk — a hook is trusted engine code by construction.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("aether_engine.tools.hooks")


class HookBlock(Exception):
    """Raised by a tool_call hook to block the call, with a reason."""


ToolCallHook = Callable[[str, dict, dict], "dict | None"]
ToolResultHook = Callable[[str, dict, Any, dict], "str | None"]

_call_hooks: dict[str, ToolCallHook] = {}
_result_hooks: dict[str, ToolResultHook] = {}


def register_tool_call_hook(name: str, fn: ToolCallHook) -> None:
    _call_hooks[name] = fn


def register_tool_result_hook(name: str, fn: ToolResultHook) -> None:
    _result_hooks[name] = fn


def unregister_hook(name: str) -> None:
    _call_hooks.pop(name, None)
    _result_hooks.pop(name, None)


def clear_hooks() -> None:
    _call_hooks.clear()
    _result_hooks.clear()


def hook_names() -> dict[str, list[str]]:
    return {"tool_call": sorted(_call_hooks), "tool_result": sorted(_result_hooks)}


def _audit(log: Any, event: str, payload: dict) -> None:
    if log is None:
        return
    try:
        log.log_event(event, payload)
    except Exception:  # best-effort, matching pipeline convention
        logger.debug("Failed to write %s audit event", event, exc_info=True)


def apply_tool_call_hooks(tool_name: str, args: dict, *, audit_logger: Any = None) -> dict:
    """Run tool_call hooks in registration order.

    Returns the (possibly rewritten) args. Raises HookBlock when any
    hook blocks — including a hook that raised unexpectedly
    (fail-closed).
    """
    current = args
    for name, fn in _call_hooks.items():
        try:
            outcome = fn(tool_name, current, {})
        except HookBlock:
            _audit(audit_logger, "HOOK_BLOCKED", {"tool_name": tool_name, "hook": name})
            raise
        except Exception as e:
            _audit(audit_logger, "HOOK_FAILED", {"tool_name": tool_name, "hook": name, "error": str(e)})
            raise HookBlock(f"Hook '{name}' failed and fails closed: {e}") from e
        if isinstance(outcome, dict):
            current = outcome
            _audit(audit_logger, "HOOK_REWRITTEN", {"tool_name": tool_name, "hook": name})
    return current


def apply_tool_result_hooks(tool_name: str, args: dict, result: Any, *, audit_logger: Any = None) -> Any:
    """Run tool_result hooks in registration order.

    Returns the (possibly replaced) result. A throwing hook replaces
    the output with a failure notice (fail-closed: unvetted output
    does not reach the agent).
    """
    current = result
    for name, fn in _result_hooks.items():
        try:
            outcome = fn(tool_name, args, current, {})
        except Exception as e:  # noqa: BLE001 — fail-closed: unvetted output is withheld
            _audit(audit_logger, "HOOK_FAILED", {"tool_name": tool_name, "hook": name, "error": str(e)})
            return f"[tool output withheld: result hook '{name}' failed: {e}]"
        if isinstance(outcome, str):
            current = outcome
            _audit(audit_logger, "HOOK_RESULT_REPLACED", {"tool_name": tool_name, "hook": name})
    return current
