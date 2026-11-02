"""Shared parallel-tool-call handling for the specialist nodes (S1-M5).

A model may emit several tool calls in one response. The tool loop
executes ONE call per round (specialist -> tool_router -> execute_tool
-> specialist), so the first call becomes ``pending_tool_call`` and
the rest are checkpointed in the ``queued_tool_calls`` state channel
instead of being silently discarded (the pre-S1-M5 behaviour). On the
next round the specialist drains the queue head into
``pending_tool_call`` without calling the model again — the assistant
message carrying ALL the calls was already recorded when they were
emitted, so no new message is needed for a drained call.
"""
from __future__ import annotations

import json
from typing import Any


def _as_list(accumulated: Any) -> list:
    """Normalise accumulated streamed tool calls to a list.

    The specialist nodes accumulate into a ``{call_id: ToolCall}``
    dict; accept that mapping or any iterable of ToolCall-likes.
    """
    if not accumulated:
        return []
    if isinstance(accumulated, dict):
        return list(accumulated.values())
    return list(accumulated)


def _pending_dict(tc: Any) -> dict[str, Any]:
    """One tool call in the pending/queued state shape."""
    args = tc.args if isinstance(getattr(tc, "args", None), dict) else {}
    return {"name": tc.name, "args": args, "call_id": tc.call_id}


def pending_and_queued(accumulated: Any) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Split accumulated tool calls into (pending, queued).

    The first call is the pending one for this tool-loop round; every
    further call is queued, in emission order, for later rounds.
    Returns ``(None, [])`` when there are no calls.
    """
    calls = _as_list(accumulated)
    if not calls:
        return None, []
    shaped = [_pending_dict(tc) for tc in calls]
    return shaped[0], shaped[1:]


def assistant_tool_calls(accumulated: Any) -> list[dict[str, Any]]:
    """ALL accumulated calls in the provider message shape, so the
    recorded assistant message reflects everything the model emitted
    (not just the call that happens to execute first)."""
    shaped = []
    for tc in _as_list(accumulated):
        args = tc.args if isinstance(getattr(tc, "args", None), dict) else {}
        shaped.append({
            "id": tc.call_id,
            "type": "function",
            "function": {"name": tc.name, "arguments": json.dumps(args)},
        })
    return shaped


def drain_queued_tool_call(state: Any) -> dict[str, Any] | None:
    """The state update that promotes the queue head to pending.

    Returns ``{"pending_tool_call": head, "queued_tool_calls": rest}``
    when the queue is non-empty, else None (the caller should proceed
    to call the model as usual).
    """
    queued = list((state or {}).get("queued_tool_calls") or [])
    if not queued:
        return None
    return {"pending_tool_call": queued[0], "queued_tool_calls": queued[1:]}
