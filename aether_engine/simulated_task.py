import asyncio, uuid
from typing import AsyncGenerator, Optional
from aether_common.contracts import Event, EventType, TaskState, validate_transition


class SimulatedTask:
    def __init__(self, task_id: Optional[str] = None,
                 total_ticks: int = 10, tick_interval: float = 1.0):
        self.task_id = task_id or str(uuid.uuid4())
        self.total_ticks = total_ticks
        self.tick_interval = tick_interval
        self.state = TaskState.PENDING
        self._cancel = asyncio.Event()

    def cancel(self) -> None:
        self._cancel.set()

    def is_cancelled(self) -> bool:
        return self._cancel.is_set()

    def _make_payload(self, **extra) -> dict:
        return {"task_id": self.task_id, "total_ticks": self.total_ticks, **extra}

    def _cancelled_event(self, last_tick: int) -> Event:
        validate_transition(self.state, TaskState.CANCELLED)
        self.state = TaskState.CANCELLED
        return Event(type=EventType.TASK_CANCELLED,
                     payload=self._make_payload(state="CANCELLED", last_tick=last_tick))

    async def run(self) -> AsyncGenerator[Event, None]:
        validate_transition(self.state, TaskState.RUNNING)
        self.state = TaskState.RUNNING

        yield Event(type=EventType.TASK_CREATED,
                     payload=self._make_payload(state="RUNNING", tick_interval=self.tick_interval))
        yield Event(type=EventType.TASK_PROGRESS,
                     payload=self._make_payload(progress=0.0, tick=0))

        for tick in range(1, self.total_ticks + 1):
            if self._cancel.is_set():
                yield self._cancelled_event(tick - 1)
                return
            try:
                await asyncio.wait_for(self._cancel.wait(), timeout=self.tick_interval)
                yield self._cancelled_event(tick - 1)
                return
            except asyncio.TimeoutError:
                pass

            yield Event(type=EventType.TASK_PROGRESS,
                        payload=self._make_payload(
                            progress=round(tick / self.total_ticks * 100, 2), tick=tick))

        validate_transition(self.state, TaskState.SUCCEEDED)
        self.state = TaskState.SUCCEEDED
        yield Event(type=EventType.TASK_COMPLETED,
                     payload=self._make_payload(state="SUCCEEDED"))
