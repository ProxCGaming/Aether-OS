"""Task-scoped serving-provider attribution (finding E5-F07).

The websocket pump used to credit every task outcome to the OUTER
provider it started the task on. But a task's calls are served by
``LiteLLMProvider`` instances deep inside the run, and the per-call
fallback may hand a call to a different provider than the instance's
primary — so the pump credited a rate-limited primary for work its
fallback did, and the serving provider got no health entry at all.

This module is the write-back channel: a ContextVar holding a
mutable per-task dict, mirroring the usage-ledger's attribution
context (``cost/ledger.py``) but in the opposite direction. The pump
opens a scope for the duration of a task; the provider layer notes
the provider of every attempt that produces consumed output (the
same firing points as the usage recorder); the pump reads the note
back when the task completes.

The value is a plain dict shared by reference, so notes written
inside nested generators / copied contexts (graph nodes, fast chat)
land in the object the pump is holding — no reliance on ContextVar
set-propagation out of an async generator. Last note wins: the
completion credit follows the provider that served the LAST call,
which is the call that produced the completing response.
"""
from contextvars import ContextVar
from typing import Any

_serving_scope: ContextVar[dict[str, Any] | None] = ContextVar(
    "aether_serving_scope", default=None)


def begin_serving_tracking():
    """Open a fresh serving scope; returns the token for
    :func:`end_serving_tracking`. Called by the task pump."""
    return _serving_scope.set({})


def end_serving_tracking(token) -> None:
    _serving_scope.reset(token)


def clear_serving_provider() -> None:
    """Empty the current scope (the pump does this per attempt, so a
    task-level retry starts with no note from the failed attempt)."""
    scope = _serving_scope.get()
    if scope is not None:
        scope.clear()


def note_serving_provider(provider_name: str | None) -> None:
    """Record that ``provider_name`` served a call in the current
    task. A no-op outside a pump scope — provider calls also happen
    for probes, classification and titles, where there is no task
    outcome to attribute."""
    if not provider_name:
        return
    scope = _serving_scope.get()
    if scope is not None:
        scope["provider"] = provider_name


def serving_provider() -> str | None:
    """The provider that served the most recent noted call in this
    task, or None when no call noted one (or no scope is active)."""
    scope = _serving_scope.get()
    if scope:
        return scope.get("provider")
    return None
