"""Fast-chat runner (Epic 1, E1-04).

Streams a direct reply from the resolved chat provider for plain
conversation: no supervisor graph, no tool loop, no checkpoint writes.

It yields the same Event contract as ``run_langgraph_task``
(TASK_CREATED -> TASK_PROGRESS deltas -> TASK_COMPLETED / TASK_FAILED)
so the websocket layer's ``execute_task_generator`` handles routing
stamps, audit transitions, health recording and assistant-message
persistence exactly as it does for graph tasks — including the
CANCEL_TASK path, where cancelling the runner task throws
``asyncio.CancelledError`` into this generator at its current await,
exactly as it does into the graph runner.

History comes from the session store — the same store the supervisor's
``get_current_chat_history`` memory tool reads — so a conversation can
move between the fast path and the graph without losing its thread:
fast-chat turns are saved to the store by the shared websocket wrapper,
and a later graph task retrieves them from there.
"""
import asyncio
import logging
import time
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from aether_common.contracts import Event, EventType, TaskState, validate_transition
from aether_engine.langgraph.executor import _is_retriable_error
from aether_engine.memory import session_store
from aether_engine.providers.base import BaseProvider

logger = logging.getLogger("aether_engine.langgraph.fast_chat")

_HISTORY_LIMIT = 20
_HISTORY_MAX_CHARS = 24_000

_SYSTEM_PROMPT = (
    "You are Aether, a helpful AI assistant. Answer the user directly and "
    "conversationally, using Markdown when it improves readability. If the "
    "user asks for something that requires tools, files or live research, "
    "say so briefly instead of inventing results."
)


def load_session_history(
    session_id: str | None,
    current_prompt: str | None = None,
    *,
    limit: int = _HISTORY_LIMIT,
) -> list[dict[str, Any]]:
    """Prior user/assistant turns for *session_id* from the session store.

    The START_TASK handler saves the current user message to the store
    before the fast/graph split, so a trailing user message identical to
    *current_prompt* is that just-saved turn and is excluded — the runner
    appends the prompt itself.
    """
    if not session_id:
        return []
    try:
        sess = session_store.get_session(session_id)
    except Exception:  # History is best-effort; a broken store must not break the chat turn.
        logger.debug("Fast chat: session history unavailable", exc_info=True)
        return []
    if not sess:
        return []

    history: list[dict[str, Any]] = []
    for msg in sess.get("messages", []):
        role = msg.get("role")
        content = (msg.get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            continue
        history.append({"role": role, "content": content})

    if current_prompt:
        target = current_prompt.strip()
        if history and history[-1]["role"] == "user" and history[-1]["content"] == target:
            history.pop()

    history = history[-limit:]
    total = sum(len(m["content"]) for m in history)
    while total > _HISTORY_MAX_CHARS and len(history) > 1:
        total -= len(history.pop(0)["content"])
    return history


async def run_fast_chat(
    prompt: str,
    provider: BaseProvider,
    history: list[dict[str, Any]] | None = None,
    *,
    request_id: str | None = None,
    session_id: str | None = None,
    task_id: str | None = None,
) -> AsyncGenerator[Event, None]:
    """Stream a direct chat reply for *prompt*, mirroring the graph
    runner's event contract (see module docstring)."""
    task_id = task_id or str(uuid.uuid4())
    state_enum = TaskState.PENDING
    validate_transition(state_enum, TaskState.RUNNING)
    state_enum = TaskState.RUNNING

    yield Event(
        type=EventType.TASK_CREATED,
        request_id=request_id,
        payload={
            "task_id": task_id,
            "prompt": prompt,
            "state": state_enum.value,
            "path": "fast_chat",
        },
    )

    messages: list[dict[str, Any]] = [{"role": "system", "content": _SYSTEM_PROMPT}]
    messages.extend(history or [])
    messages.append({"role": "user", "content": prompt})

    start_time = time.time()
    parts: list[str] = []
    try:
        async for chunk in provider.call_stream(messages, tools=None, node_name="fast_chat"):
            if chunk.text:
                parts.append(chunk.text)
                yield Event(
                    type=EventType.TASK_PROGRESS,
                    request_id=request_id,
                    payload={
                        "task_id": task_id,
                        "text_delta": chunk.text,
                        "full_text": "".join(parts),
                        "turn": 0,
                        "node": "assistant",
                    },
                )
    except asyncio.CancelledError:
        # CANCEL_TASK contract: propagate so execute_task_generator can
        # log the cancellation; no partial reply is persisted, matching
        # the graph path.
        raise
    except Exception as e:  # Provider boundary: any failure becomes a TASK_FAILED event, mirroring the graph runner's contract.
        logger.exception("Fast chat failed")
        yield Event(
            type=EventType.TASK_FAILED,
            request_id=request_id,
            payload={
                "task_id": task_id,
                "state": TaskState.FAILED.value,
                "error": str(e),
                "retriable": _is_retriable_error(e),
                "latency_ms": round((time.time() - start_time) * 1000, 2),
            },
        )
        return

    full_text = "".join(parts).strip()
    yield Event(
        type=EventType.TASK_COMPLETED,
        request_id=request_id,
        payload={
            "task_id": task_id,
            "state": TaskState.SUCCEEDED.value,
            "latency_ms": round((time.time() - start_time) * 1000, 2),
            "response": full_text or "(task completed)",
            "thoughts": [],
            "session_id": session_id,
        },
    )
