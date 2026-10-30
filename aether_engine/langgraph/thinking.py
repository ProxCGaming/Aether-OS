"""Epic 1, E1-16: thinking stream plumbing.

When the selected model exposes its reasoning, providers surface it
on ``StreamChunk.thinking`` — always apart from the answer text.
Graph nodes forward those chunks on the event bus as THINKING events
(the executor is subscribed per task and passes them to the client,
which renders them in the Thinking collapsible). Models that expose
no thinking simply never produce these events: zero behaviour change.
"""
from __future__ import annotations

import logging
from typing import Any

from aether_common.contracts import Event, EventType
from aether_engine.event_bus import event_bus

logger = logging.getLogger("aether_engine.langgraph.thinking")


def forward_thinking(state: dict, node_name: str, chunk: Any) -> None:
    """Publish one chunk's thinking (if any) for the running task."""
    thinking = getattr(chunk, "thinking", None)
    if not thinking:
        return
    task_id = state.get("task_id")
    if not task_id:
        return
    try:
        event_bus.publish(task_id, Event(
            type=EventType.THINKING,
            request_id="",
            payload={"task_id": task_id, "node": node_name, "text_delta": thinking},
        ))
    except Exception as e:  # noqa: BLE001 — thinking display must never break a run.
        logger.debug(f"Failed to forward thinking chunk: {e}")
