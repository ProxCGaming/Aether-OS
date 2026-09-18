from dataclasses import dataclass, field
from enum import Enum
import json, time, uuid
from typing import Any, Dict, Optional, Set

SCHEMA_VERSION = 1


class SchemaVersionMismatchError(Exception): pass
class InvalidStateTransitionError(Exception): pass
class InvalidEventPayloadError(Exception): pass


class EventType(str, Enum):
    HELLO = "HELLO"
    START_TASK = "START_TASK"
    CANCEL_TASK = "CANCEL_TASK"
    TASK_CREATED = "TASK_CREATED"
    TASK_PROGRESS = "TASK_PROGRESS"
    TASK_COMPLETED = "TASK_COMPLETED"
    TASK_FAILED = "TASK_FAILED"
    TASK_CANCELLED = "TASK_CANCELLED"
    ERROR = "ERROR"
    PING = "PING"
    PONG = "PONG"
    TOOL_APPROVAL_REQUEST = "TOOL_APPROVAL_REQUEST"
    PLUGIN_APPROVAL_REQUEST = "PLUGIN_APPROVAL_REQUEST"
    TOOL_APPROVAL_GRANTED = "TOOL_APPROVAL_GRANTED"
    TOOL_APPROVAL_REJECTED = "TOOL_APPROVAL_REJECTED"
    WORKER_EXECUTION_STARTED = "WORKER_EXECUTION_STARTED"
    WORKER_EXECUTION_COMPLETED = "WORKER_EXECUTION_COMPLETED"
    # Provider management (Phase 3)
    PROVIDER_LIST_REQUEST = "PROVIDER_LIST_REQUEST"
    PROVIDER_LIST_RESPONSE = "PROVIDER_LIST_RESPONSE"
    PROVIDER_VALIDATE_REQUEST = "PROVIDER_VALIDATE_REQUEST"
    PROVIDER_VALIDATE_RESPONSE = "PROVIDER_VALIDATE_RESPONSE"
    PROVIDER_SAVE_REQUEST = "PROVIDER_SAVE_REQUEST"
    PROVIDER_SAVE_RESPONSE = "PROVIDER_SAVE_RESPONSE"
    PROVIDER_REMOVE_REQUEST = "PROVIDER_REMOVE_REQUEST"
    PROVIDER_REMOVE_RESPONSE = "PROVIDER_REMOVE_RESPONSE"
    MODEL_SET_DEFAULT = "MODEL_SET_DEFAULT"
    MODEL_DEFAULT_CHANGED = "MODEL_DEFAULT_CHANGED"
    # Health, Routing & Fallbacks (Phase 3.5)
    PROVIDER_HEALTH_UPDATE = "PROVIDER_HEALTH_UPDATE"
    ROUTING_DECISION = "ROUTING_DECISION"
    FALLBACK_STARTED = "FALLBACK_STARTED"
    FALLBACK_COMPLETED = "FALLBACK_COMPLETED"
    FALLBACK_FAILED = "FALLBACK_FAILED"
    SETTINGS_PROVIDER_VALIDATE_REQUEST = "SETTINGS_PROVIDER_VALIDATE_REQUEST"
    SETTINGS_PROVIDER_VALIDATE_RESULT = "SETTINGS_PROVIDER_VALIDATE_RESULT"
    # Local Models (Ollama)
    LOCAL_MODEL_LIST_REQUEST = "LOCAL_MODEL_LIST_REQUEST"
    LOCAL_MODEL_LIST_RESPONSE = "LOCAL_MODEL_LIST_RESPONSE"
    LOCAL_MODEL_DOWNLOAD_START = "LOCAL_MODEL_DOWNLOAD_START"
    LOCAL_MODEL_DOWNLOAD_PROGRESS = "LOCAL_MODEL_DOWNLOAD_PROGRESS"
    LOCAL_MODEL_DELETE_REQUEST = "LOCAL_MODEL_DELETE_REQUEST"
    LOCAL_MODEL_DELETE_RESPONSE = "LOCAL_MODEL_DELETE_RESPONSE"
    # Capabilities Check Scheduler
    CAPABILITY_CHECK_SET_SCHEDULE = "CAPABILITY_CHECK_SET_SCHEDULE"
    CAPABILITY_CHECK_RUN_NOW = "CAPABILITY_CHECK_RUN_NOW"
    CAPABILITY_CHECK_GET_HISTORY = "CAPABILITY_CHECK_GET_HISTORY"
    CAPABILITY_CHECK_HISTORY_RESPONSE = "CAPABILITY_CHECK_HISTORY_RESPONSE"
    # Reachability & Refresh
    REFRESH_MODELS_REQUEST = "REFRESH_MODELS_REQUEST"
    REFRESH_MODELS_RESPONSE = "REFRESH_MODELS_RESPONSE"
    # Reveal stored key (decrypt & return to UI)
    PROVIDER_REVEAL_KEY_REQUEST = "PROVIDER_REVEAL_KEY_REQUEST"
    PROVIDER_REVEAL_KEY_RESPONSE = "PROVIDER_REVEAL_KEY_RESPONSE"


class TaskState(str, Enum):
    PENDING = "PENDING"
    PRE_FLIGHT_CHECK = "PRE_FLIGHT_CHECK"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    EXECUTING_WORKER = "EXECUTING_WORKER"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_STATES: Set[TaskState] = {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED}

LEGAL_TRANSITIONS: Dict[TaskState, Set[TaskState]] = {
    TaskState.PENDING:   {TaskState.RUNNING, TaskState.PRE_FLIGHT_CHECK, TaskState.CANCELLED},
    TaskState.PRE_FLIGHT_CHECK: {TaskState.RUNNING, TaskState.PENDING_APPROVAL, TaskState.FAILED, TaskState.CANCELLED},
    TaskState.PENDING_APPROVAL: {TaskState.RUNNING, TaskState.EXECUTING_WORKER, TaskState.FAILED, TaskState.CANCELLED},
    TaskState.EXECUTING_WORKER: {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED},
    TaskState.RUNNING:   {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED, TaskState.PRE_FLIGHT_CHECK, TaskState.PENDING_APPROVAL, TaskState.EXECUTING_WORKER},
    TaskState.SUCCEEDED: set(),
    TaskState.FAILED:    set(),
    TaskState.CANCELLED: set(),
}


def validate_transition(from_state: TaskState, to_state: TaskState) -> None:
    if to_state not in LEGAL_TRANSITIONS[from_state]:
        raise InvalidStateTransitionError(
            f"Illegal state transition: {from_state.value} -> {to_state.value}"
        )


@dataclass
class Event:
    type: EventType
    payload: Dict[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION
    ts: float = field(default_factory=time.time)
    request_id: Optional[str] = None
    event_id: Optional[str] = None

    def __post_init__(self):
        if self.event_id and not self.request_id:
            self.request_id = self.event_id
        elif self.request_id and not self.event_id:
            self.event_id = self.request_id

    def to_json(self) -> str:
        d = {
            "type": self.type.value if isinstance(self.type, EventType) else self.type,
            "schema_version": self.schema_version,
            "ts": self.ts,
            "payload": self.payload,
        }
        if self.request_id is not None:
            d["request_id"] = self.request_id
        if self.event_id is not None:
            d["event_id"] = self.event_id
        return json.dumps(d)

    @classmethod
    def from_json(cls, raw: str) -> "Event":
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise InvalidEventPayloadError(f"Malformed JSON: {e}")

        ver = data.get("schema_version")
        if ver != SCHEMA_VERSION:
            raise SchemaVersionMismatchError(
                f"Schema version mismatch: event has {ver}, expected {SCHEMA_VERSION}"
            )

        try:
            ev_type = EventType(data["type"])
        except (KeyError, ValueError) as e:
            raise InvalidEventPayloadError(f"Invalid or missing event type: {e}")

        req_id = data.get("request_id") or data.get("event_id")
        ev_id = data.get("event_id") or req_id

        return cls(
            type=ev_type,
            payload=data.get("payload", {}),
            schema_version=ver,
            ts=data.get("ts", time.time()),
            request_id=req_id,
            event_id=ev_id,
        )
