"""Epic 1, E1-07: the steering queue.

A message sent while a task is already running is *steering*, not a
new task: it must not cancel the run and must not be lost. The
websocket handler enqueues it here (keyed by session); the graph
executor drains the queue at step boundaries and appends the steers to
the running thread as user messages, and the resume path drains it
after an approval pause.

This module is deliberately pure — no websocket, executor or store
imports — so the queue semantics (FIFO order, per-session isolation,
drain empties) are unit-testable in isolation. The registry is
in-process: steering is a live-run concern, and a steer that somehow
outlives its run stays queued for the session's next graph turn rather
than being dropped.

The one outside dependency is the shared content builder in
``langgraph.state`` (guarded exactly as the executor guards it): a
steer carries the message's attachments (PF-1, xref M16), and its
graph message is built by the same ``multimodal_user_content()`` the
initial turn uses — one construction for both paths, never a parallel
renderer here.
"""
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any

try:
    from aether_engine.langgraph.state import multimodal_user_content
except ImportError:  # langgraph absent: no graph run can consume steers
    multimodal_user_content = None  # type: ignore


@dataclass(frozen=True)
class Steer:
    """One queued steering message."""

    text: str
    message_id: str | None = None
    session_id: str | None = None
    attachments: tuple[Any, ...] = ()


@dataclass
class SteeringQueue:
    """FIFO queues of pending steers, isolated per session."""

    _queues: dict[str, deque[Steer]] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock)

    @staticmethod
    def _key(session_id: str | None) -> str:
        return session_id or ""

    def enqueue(
        self,
        session_id: str | None,
        text: str,
        message_id: str | None = None,
        attachments: Any = None,
    ) -> Steer:
        """Append a steer to the session's queue; returns the Steer."""
        steer = Steer(
            text=text,
            message_id=message_id,
            session_id=session_id,
            attachments=tuple(attachments) if attachments else (),
        )
        with self._lock:
            self._queues.setdefault(self._key(session_id), deque()).append(steer)
        return steer

    def drain(self, session_id: str | None) -> list[Steer]:
        """Remove and return all pending steers for the session, in
        the order they were enqueued."""
        with self._lock:
            queue = self._queues.pop(self._key(session_id), None)
        return list(queue) if queue else []

    def peek(self, session_id: str | None) -> list[Steer]:
        """Return the pending steers without removing them."""
        with self._lock:
            return list(self._queues.get(self._key(session_id), ()))

    def has_pending(self, session_id: str | None) -> bool:
        with self._lock:
            return bool(self._queues.get(self._key(session_id)))

    def clear(self, session_id: str | None = None) -> None:
        """Drop pending steers — one session, or all when omitted."""
        with self._lock:
            if session_id is None:
                self._queues.clear()
            else:
                self._queues.pop(self._key(session_id), None)


#: The process-wide registry shared by the websocket handler (producer)
#: and the graph executor (consumer).
STEERING_QUEUE = SteeringQueue()


def enqueue_steer(
    session_id: str | None,
    text: str,
    message_id: str | None = None,
    attachments: Any = None,
) -> Steer:
    return STEERING_QUEUE.enqueue(session_id, text, message_id, attachments)


def drain_steers(session_id: str | None) -> list[Steer]:
    return STEERING_QUEUE.drain(session_id)


def peek_steers(session_id: str | None) -> list[Steer]:
    return STEERING_QUEUE.peek(session_id)


def has_pending_steers(session_id: str | None) -> bool:
    return STEERING_QUEUE.has_pending(session_id)


def _unloadable_attachment_note(att: Any) -> str:
    """The shared builder's degrade note, for the one environment
    where the builder itself is unavailable (no langgraph, so no
    graph run can receive the steer): the attachment could not be
    loaded, so say so in the message — never drop it silently."""
    name = "attachment"
    if isinstance(att, dict):
        raw = att.get("name") or att.get("path")
        if raw:
            name = Path(str(raw)).name or str(raw)
    elif isinstance(att, str) and att and not att.startswith(("data:", "http")):
        name = Path(att).name or att
    return (
        f'[Attachment "{name}" could not be loaded, '
        "so it was not sent to the model.]"
    )


def steer_message_dicts(steers: list[Steer]) -> list[dict[str, Any]]:
    """Render steers as graph user messages.

    Content is built by the shared ``multimodal_user_content()``
    (PF-1, xref M16): a text-only steer stays the plain string,
    byte-identical to before; a steer with attachments becomes the
    same parts list an initial turn with those attachments produces,
    including the honest degrade note for an attachment whose bytes
    are missing or corrupt.
    """
    messages: list[dict[str, Any]] = []
    for steer in steers:
        attachments = list(steer.attachments) if steer.attachments else None
        if multimodal_user_content is not None:
            content = multimodal_user_content(steer.text, attachments)
        elif attachments:
            notes = "\n".join(
                _unloadable_attachment_note(att) for att in attachments)
            content = f"{steer.text}\n{notes}" if steer.text else notes
        else:
            content = steer.text
        messages.append({"role": "user", "content": content})
    return messages
