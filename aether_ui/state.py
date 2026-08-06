from enum import Enum
from typing import Callable, List, Optional
from aether_common.contracts import Event, EventType


class UIState(str, Enum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    TASK_RUNNING = "TASK_RUNNING"
    ERROR = "ERROR"


class UIStateMachine:
    """Qt-free UI state machine. Zero Qt imports; fully unit-testable."""

    def __init__(self):
        self.state = UIState.DISCONNECTED
        self.active_task_id: Optional[str] = None
        self.task_progress = 0.0
        self.task_tick = 0
        self.total_ticks = 0
        self.error_message: Optional[str] = None
        self.event_history: List[Event] = []
        self._subs: List[Callable[["UIStateMachine"], None]] = []

    def subscribe(self, cb: Callable[["UIStateMachine"], None]) -> None:
        self._subs.append(cb)

    def _notify(self):
        for s in self._subs:
            try:
                s(self)
            except Exception:
                pass

    def set_connecting(self):
        self.state, self.error_message = UIState.CONNECTING, None
        self._notify()

    def set_disconnected(self):
        self.state, self.active_task_id, self.task_progress = UIState.DISCONNECTED, None, 0.0
        self._notify()

    def set_error(self, msg: str):
        self.state, self.error_message = UIState.ERROR, msg
        self._notify()

    def clear_error(self):
        if self.state == UIState.ERROR:
            self.state = UIState.CONNECTED
            self.error_message = None
            self._notify()

    def transition_on_event(self, event: Event) -> "UIState":
        self.event_history.append(event)
        t = event.type
        p = event.payload

        if t == EventType.HELLO:
            self.state, self.error_message = UIState.CONNECTED, None
        elif t in (EventType.START_TASK, EventType.TASK_CREATED):
            self.state = UIState.TASK_RUNNING
            self.error_message = None
            self.active_task_id = p.get("task_id")
            self.total_ticks = p.get("total_ticks", 10)
            self.task_tick, self.task_progress = 0, 0.0
        elif t == EventType.TASK_PROGRESS:
            self.state = UIState.TASK_RUNNING
            self.task_progress = float(p.get("progress", self.task_progress))
            self.task_tick = int(p.get("tick", self.task_tick))
            self.total_ticks = int(p.get("total_ticks", self.total_ticks))
        elif t == EventType.TASK_COMPLETED:
            self.state, self.task_progress, self.active_task_id = UIState.CONNECTED, 100.0, None
        elif t == EventType.TASK_CANCELLED:
            self.state, self.active_task_id = UIState.CONNECTED, None
        elif t == EventType.TASK_FAILED:
            self.state = UIState.ERROR
            self.error_message = p.get("error", "Task failed")
            self.active_task_id = None
        elif t in (EventType.TOOL_APPROVAL_REQUEST, EventType.WORKER_EXECUTION_STARTED):
            self.state = UIState.TASK_RUNNING
            self.error_message = None
        elif t in (EventType.TOOL_APPROVAL_GRANTED, EventType.TOOL_APPROVAL_REJECTED, EventType.WORKER_EXECUTION_COMPLETED):
            self.state = UIState.CONNECTED
            self.error_message = None
        elif t == EventType.ERROR:
            self.state = UIState.ERROR
            self.error_message = p.get("error", "Unknown error")

        self._notify()
        return self.state
