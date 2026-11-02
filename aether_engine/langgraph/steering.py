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
"""
from collections import deque
from dataclasses import dataclass, field
from threading import Lock
from typing import Any


@dataclass(frozen=True)
class Steer:
    """One queued steering message."""

    text: str
    message_id: str | None = None
    session_id: str | None = None


@dataclass
class SteeringQueue:
    """FIFO queues of pending steers, isolated per session."""

    _queues: dict[str, deque[Steer]] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock)

    @staticmethod
    def _key(session_id: str | None) -> str:
        return session_id or ""

    def enqueue(
        self, session_id: str | None, text: str, message_id: str | None = None
    ) -> Steer:
        """Append a steer to the session's queue; returns the Steer."""
        steer = Steer(text=text, message_id=message_id, session_id=session_id)
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
    session_id: str | None, text: str, message_id: str | None = None
) -> Steer:
    return STEERING_QUEUE.enqueue(session_id, text, message_id)


def drain_steers(session_id: str | None) -> list[Steer]:
    return STEERING_QUEUE.drain(session_id)


def peek_steers(session_id: str | None) -> list[Steer]:
    return STEERING_QUEUE.peek(session_id)


def has_pending_steers(session_id: str | None) -> bool:
    return STEERING_QUEUE.has_pending(session_id)


def steer_message_dicts(steers: list[Steer]) -> list[dict[str, Any]]:
    """Render steers as graph user messages."""
    return [{"role": "user", "content": steer.text} for steer in steers]
