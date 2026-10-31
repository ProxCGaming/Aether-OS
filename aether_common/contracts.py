import json
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

SCHEMA_VERSION = 1


class SchemaVersionMismatchError(Exception): pass
class InvalidStateTransitionError(Exception): pass
class InvalidEventPayloadError(Exception): pass


class EventType(str, Enum):
    HELLO = "HELLO"
    START_TASK = "START_TASK"
    STEER_TASK = "STEER_TASK"
    STEERING_APPLIED = "STEERING_APPLIED"
    CANCEL_TASK = "CANCEL_TASK"
    TASK_CREATED = "TASK_CREATED"
    TASK_PROGRESS = "TASK_PROGRESS"
    TASK_COMPLETED = "TASK_COMPLETED"
    TASK_FAILED = "TASK_FAILED"
    TASK_CANCELLED = "TASK_CANCELLED"
    TASK_INTERRUPTED = "TASK_INTERRUPTED"
    ERROR = "ERROR"
    PING = "PING"
    PONG = "PONG"
    TOOL_ACTIVITY = "TOOL_ACTIVITY"
    NODE_ACTIVITY = "NODE_ACTIVITY"
    TOOL_APPROVAL_REQUEST = "TOOL_APPROVAL_REQUEST"
    PLUGIN_APPROVAL_REQUEST = "PLUGIN_APPROVAL_REQUEST"
    # TOOL_APPROVAL_GRANTED payload: {"approval_key": str,
    # "scope": "file" | "folder" | None (Epic 2 scoped grants),
    # "edited_args": dict | None (Epic 8 E8-1.3: the full replacement
    # argument set the user edited in the approval drawer; edited args
    # re-run the full policy pipeline before execution)}. TOOL_ACTIVITY
    # payloads carry "edited": bool so executed cards can show the
    # "edited by you" pill.
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
    LOCAL_MODEL_DOWNLOAD_CANCEL = "LOCAL_MODEL_DOWNLOAD_CANCEL"
    CONFIG_GET_REQUEST = "CONFIG_GET_REQUEST"
    CONFIG_GET_RESPONSE = "CONFIG_GET_RESPONSE"
    CONFIG_UPDATE_REQUEST = "CONFIG_UPDATE_REQUEST"
    CONFIG_UPDATE_RESPONSE = "CONFIG_UPDATE_RESPONSE"
    CONFIG_RESET_REQUEST = "CONFIG_RESET_REQUEST"
    CONFIG_RESET_RESPONSE = "CONFIG_RESET_RESPONSE"
    # Auxiliary model overrides (Phase 9): per-task-class model selection
    AUXILIARY_MODEL_SET_REQUEST = "AUXILIARY_MODEL_SET_REQUEST"
    AUXILIARY_MODEL_SET_RESPONSE = "AUXILIARY_MODEL_SET_RESPONSE"
    # Reasoning effort (Phase 9): user-selected reasoning depth
    REASONING_EFFORT_SET_REQUEST = "REASONING_EFFORT_SET_REQUEST"
    REASONING_EFFORT_SET_RESPONSE = "REASONING_EFFORT_SET_RESPONSE"
    # Capabilities Check Scheduler
    CAPABILITY_CHECK_SET_SCHEDULE = "CAPABILITY_CHECK_SET_SCHEDULE"
    CAPABILITY_CHECK_RUN_NOW = "CAPABILITY_CHECK_RUN_NOW"
    CAPABILITY_CHECK_GET_HISTORY = "CAPABILITY_CHECK_GET_HISTORY"
    CAPABILITY_CHECK_HISTORY_RESPONSE = "CAPABILITY_CHECK_HISTORY_RESPONSE"
    ENVIRONMENT_DIAGNOSTIC_RUN_REQUEST = "ENVIRONMENT_DIAGNOSTIC_RUN_REQUEST"
    ENVIRONMENT_DIAGNOSTIC_RESPONSE = "ENVIRONMENT_DIAGNOSTIC_RESPONSE"
    # Reachability & Refresh
    REFRESH_MODELS_REQUEST = "REFRESH_MODELS_REQUEST"
    REFRESH_MODELS_RESPONSE = "REFRESH_MODELS_RESPONSE"
    # Reveal stored key (decrypt & return to UI)
    PROVIDER_REVEAL_KEY_REQUEST = "PROVIDER_REVEAL_KEY_REQUEST"
    PROVIDER_REVEAL_KEY_RESPONSE = "PROVIDER_REVEAL_KEY_RESPONSE"
    
    # Phase 6: Session Management
    SESSION_LIST_REQUEST = "SESSION_LIST_REQUEST"
    SESSION_LIST_RESPONSE = "SESSION_LIST_RESPONSE"
    SESSION_GET_REQUEST = "SESSION_GET_REQUEST"
    SESSION_GET_RESPONSE = "SESSION_GET_RESPONSE"
    SESSION_CREATE_REQUEST = "SESSION_CREATE_REQUEST"
    SESSION_CREATE_RESPONSE = "SESSION_CREATE_RESPONSE"
    SESSION_DELETE_REQUEST = "SESSION_DELETE_REQUEST"
    SESSION_DELETE_RESPONSE = "SESSION_DELETE_RESPONSE"
    SESSION_TRUNCATE_REQUEST = "SESSION_TRUNCATE_REQUEST"
    SESSION_TRUNCATE_RESPONSE = "SESSION_TRUNCATE_RESPONSE"
    # Epic 1, E1-08: rename + content search + model-written titles
    SESSION_RENAME_REQUEST = "SESSION_RENAME_REQUEST"
    SESSION_RENAME_RESPONSE = "SESSION_RENAME_RESPONSE"
    SESSION_SEARCH_REQUEST = "SESSION_SEARCH_REQUEST"
    SESSION_SEARCH_RESPONSE = "SESSION_SEARCH_RESPONSE"
    SESSION_TITLE_UPDATED = "SESSION_TITLE_UPDATED"
    # Epic 8, E8-A7.1: session export + import
    SESSION_EXPORT_REQUEST = "SESSION_EXPORT_REQUEST"
    SESSION_EXPORT_RESPONSE = "SESSION_EXPORT_RESPONSE"
    SESSION_IMPORT_REQUEST = "SESSION_IMPORT_REQUEST"
    SESSION_IMPORT_RESPONSE = "SESSION_IMPORT_RESPONSE"
    # Epic 8, E8-1.1a: per-task rewind manifest query. The response
    # carries the session's LAST task's recorded rewind points
    # ({step_index, checkpoint_id, label, files_written, timestamp});
    # asking for an older task returns no entries — rewind points
    # exist only for the last task (grill Q5). Rewind execution
    # contracts arrive with E8-1.1b.
    REWIND_MANIFEST_GET_REQUEST = "REWIND_MANIFEST_GET_REQUEST"
    REWIND_MANIFEST_GET_RESPONSE = "REWIND_MANIFEST_GET_RESPONSE"
    # Epic 8, E8-1.1b: rewind execution. TASK_REWIND rolls the last
    # task back to a manifest point (state-only — files are never
    # reverted), holding the discarded steps for a 5-second undo
    # window; TASK_REWIND_UNDO restores them inside the window, and
    # expiry (or a new message) prunes them outright.
    TASK_REWIND_REQUEST = "TASK_REWIND_REQUEST"
    TASK_REWIND_RESPONSE = "TASK_REWIND_RESPONSE"
    TASK_REWIND_UNDO_REQUEST = "TASK_REWIND_UNDO_REQUEST"
    TASK_REWIND_UNDO_RESPONSE = "TASK_REWIND_UNDO_RESPONSE"
    # Epic 8, E8-1.2: fork from anywhere. SESSION_FORK copies a
    # session up to a completed step boundary — a manifest
    # step_index of the last task, or a message_id for older steps —
    # into a NEW session named "Fork of <title>"; the checkpoint
    # thread is copied up to the point when the boundary carries
    # one. The source session is never altered.
    SESSION_FORK_REQUEST = "SESSION_FORK_REQUEST"
    SESSION_FORK_RESPONSE = "SESSION_FORK_RESPONSE"
    # Epic 8, E8-2.1: Canvas A - artifact store + per-session panel
    ARTIFACT_LIST_REQUEST = "ARTIFACT_LIST_REQUEST"
    ARTIFACT_LIST_RESPONSE = "ARTIFACT_LIST_RESPONSE"
    ARTIFACT_GET_REQUEST = "ARTIFACT_GET_REQUEST"
    ARTIFACT_GET_RESPONSE = "ARTIFACT_GET_RESPONSE"
    ARTIFACT_RECORDED = "ARTIFACT_RECORDED"
    # Epic 8, E8-2.2: Canvas B - project preview dev-server detection.
    # DEV_SERVER_DETECT_REQUEST payload: {"workspace_root": str | None,
    # "project_path": str | None}; the engine probes the candidate
    # localhost ports (5173, 3000, 8000, 8080) and reads package.json.
    # DEV_SERVER_DETECT_RESPONSE payload: {"running": bool,
    # "port": int | None, "url": str | None, "framework": str | None}.
    DEV_SERVER_DETECT_REQUEST = "DEV_SERVER_DETECT_REQUEST"
    DEV_SERVER_DETECT_RESPONSE = "DEV_SERVER_DETECT_RESPONSE"
    # Epic 8, E8-A8.1a: cost ledger query + live per-call cost push.
    # USAGE_LEDGER_QUERY_RESPONSE payload: {"entries": [...],
    # "totals": {"total_cost_usd", "input_tokens", "output_tokens",
    # "by_model", "by_agent"}} — totals aggregate the returned
    # entries. TASK_COST_UPDATE is an outbound push per recorded call:
    # {"session_id", "task_id", "delta_cost_usd",
    # "task_total_cost_usd", "input_tokens", "output_tokens",
    # "model"}. The context-breakdown and daily-budget contracts
    # arrive with E8-A8.1b, which owns those surfaces.
    USAGE_LEDGER_QUERY_REQUEST = "USAGE_LEDGER_QUERY_REQUEST"
    USAGE_LEDGER_QUERY_RESPONSE = "USAGE_LEDGER_QUERY_RESPONSE"
    TASK_COST_UPDATE = "TASK_COST_UPDATE"
    # Epic 8, E8-4.2: Media index + Library section
    MEDIA_LIST_REQUEST = "MEDIA_LIST_REQUEST"
    MEDIA_LIST_RESPONSE = "MEDIA_LIST_RESPONSE"
    MEDIA_DELETE_REQUEST = "MEDIA_DELETE_REQUEST"
    MEDIA_DELETE_RESPONSE = "MEDIA_DELETE_RESPONSE"
    MEDIA_METADATA_UPDATE_REQUEST = "MEDIA_METADATA_UPDATE_REQUEST"
    MEDIA_METADATA_UPDATE_RESPONSE = "MEDIA_METADATA_UPDATE_RESPONSE"
    MEDIA_RECORDED = "MEDIA_RECORDED"
    # Epic 8, E8-4.3: background media analysis pushes. PROGRESS
    # payload: {"media_id", "status", ...} as analysis starts/fails;
    # ANALYZED payload: the full updated media item dict.
    MEDIA_ANALYSIS_PROGRESS = "MEDIA_ANALYSIS_PROGRESS"
    MEDIA_ANALYZED = "MEDIA_ANALYZED"
    # Epic 8, E8-5.1: streaming voice conversation. START_REQUEST
    # payload: {"session_id": str | None} — the engine creates a
    # session when omitted and answers {"ok": bool, "session_id": str,
    # "webrtc_config": dict | None, "error": str | None}; a refused
    # start is always ok=False with an honest "voice unavailable: ..."
    # error and never opens an audio device. STOP_REQUEST payload:
    # {"session_id": str}; STOP_RESPONSE: {"ok": bool}.
    # VOICE_PARTIAL_TRANSCRIPT is an outbound push for the user's
    # speech: {"session_id", "speaker": "user", "text": str,
    # "is_final": bool} — partials are provisional display only; the
    # is_final text is the one committed to session history.
    # VOICE_OUTPUT_CHUNK is an outbound push for the talker's reply:
    # {"session_id", "speaker": "assistant", "text_delta": str,
    # "audio_chunk": str | None (base64 audio), "timestamp": float}.
    # VOICE_BARGE_IN (E8-5.1b behaviour; the contract lands with the
    # pipeline's interrupt seam in E8-5.1a): {"session_id",
    # "stopped_at_char": int, "committed_text": str} — committed
    # history keeps only the assistant words actually heard.
    VOICE_SESSION_START_REQUEST = "VOICE_SESSION_START_REQUEST"
    VOICE_SESSION_START_RESPONSE = "VOICE_SESSION_START_RESPONSE"
    VOICE_SESSION_STOP_REQUEST = "VOICE_SESSION_STOP_REQUEST"
    VOICE_SESSION_STOP_RESPONSE = "VOICE_SESSION_STOP_RESPONSE"
    VOICE_PARTIAL_TRANSCRIPT = "VOICE_PARTIAL_TRANSCRIPT"
    VOICE_OUTPUT_CHUNK = "VOICE_OUTPUT_CHUNK"
    VOICE_BARGE_IN = "VOICE_BARGE_IN"
    # Epic 1, E1-16: model thinking, streamed apart from the answer
    THINKING = "THINKING"
    # Epic 1, E1-17: agent scratchpad content for the display-only panel
    SCRATCHPAD = "SCRATCHPAD"
    # Epic 2, E2-08: approval mode get/set for Settings
    APPROVAL_MODE_SET_REQUEST = "APPROVAL_MODE_SET_REQUEST"
    APPROVAL_MODE_SET_RESPONSE = "APPROVAL_MODE_SET_RESPONSE"
    # Epic 2, E2-09: approval-cache grants (list + revoke for Settings)
    APPROVAL_GRANTS_REQUEST = "APPROVAL_GRANTS_REQUEST"
    APPROVAL_GRANTS_RESPONSE = "APPROVAL_GRANTS_RESPONSE"
    APPROVAL_GRANT_REVOKE_REQUEST = "APPROVAL_GRANT_REVOKE_REQUEST"
    APPROVAL_GRANT_REVOKE_RESPONSE = "APPROVAL_GRANT_REVOKE_RESPONSE"

    # Tools Management
    TOOL_LIST_REQUEST = "TOOL_LIST_REQUEST"
    TOOL_LIST_RESPONSE = "TOOL_LIST_RESPONSE"
    TOOL_POLICY_SET_REQUEST = "TOOL_POLICY_SET_REQUEST"
    TOOL_POLICY_SET_RESPONSE = "TOOL_POLICY_SET_RESPONSE"

    # MCP Management
    MCP_SERVER_LIST_REQUEST = "MCP_SERVER_LIST_REQUEST"
    MCP_SERVER_LIST_RESPONSE = "MCP_SERVER_LIST_RESPONSE"
    MCP_SERVER_ADD_REQUEST = "MCP_SERVER_ADD_REQUEST"
    MCP_SERVER_ADD_RESPONSE = "MCP_SERVER_ADD_RESPONSE"
    MCP_SERVER_REMOVE_REQUEST = "MCP_SERVER_REMOVE_REQUEST"
    MCP_SERVER_REMOVE_RESPONSE = "MCP_SERVER_REMOVE_RESPONSE"
    MCP_SERVER_CONNECT_REQUEST = "MCP_SERVER_CONNECT_REQUEST"
    MCP_SERVER_CONNECT_RESPONSE = "MCP_SERVER_CONNECT_RESPONSE"
    MCP_SERVER_DISCONNECT_REQUEST = "MCP_SERVER_DISCONNECT_REQUEST"
    MCP_SERVER_DISCONNECT_RESPONSE = "MCP_SERVER_DISCONNECT_RESPONSE"
    MCP_SERVER_TEST_REQUEST = "MCP_SERVER_TEST_REQUEST"
    MCP_SERVER_TEST_RESPONSE = "MCP_SERVER_TEST_RESPONSE"
    MCP_SERVER_UPDATE_REQUEST = "MCP_SERVER_UPDATE_REQUEST"
    MCP_SERVER_UPDATE_RESPONSE = "MCP_SERVER_UPDATE_RESPONSE"
    # MCP resources (issue 006): list/read a server's resources; reading
    # loads the content into the current chat as a context message.
    MCP_RESOURCE_LIST_REQUEST = "MCP_RESOURCE_LIST_REQUEST"
    MCP_RESOURCE_LIST_RESPONSE = "MCP_RESOURCE_LIST_RESPONSE"
    MCP_RESOURCE_READ_REQUEST = "MCP_RESOURCE_READ_REQUEST"
    MCP_RESOURCE_READ_RESPONSE = "MCP_RESOURCE_READ_RESPONSE"
    # MCP prompts (issue 007): list a server's prompt templates; running
    # one inserts its messages into the current chat, ready to send.
    # Never auto-run on connect.
    MCP_PROMPT_LIST_REQUEST = "MCP_PROMPT_LIST_REQUEST"
    MCP_PROMPT_LIST_RESPONSE = "MCP_PROMPT_LIST_RESPONSE"
    MCP_PROMPT_RUN_REQUEST = "MCP_PROMPT_RUN_REQUEST"
    MCP_PROMPT_RUN_RESPONSE = "MCP_PROMPT_RUN_RESPONSE"

    # Plugins Management
    PLUGIN_LIST_REQUEST = "PLUGIN_LIST_REQUEST"
    PLUGIN_LIST_RESPONSE = "PLUGIN_LIST_RESPONSE"
    PLUGIN_INSTALL_REQUEST = "PLUGIN_INSTALL_REQUEST"
    PLUGIN_INSTALL_RESPONSE = "PLUGIN_INSTALL_RESPONSE"
    PLUGIN_UNINSTALL_REQUEST = "PLUGIN_UNINSTALL_REQUEST"
    PLUGIN_UNINSTALL_RESPONSE = "PLUGIN_UNINSTALL_RESPONSE"
    PLUGIN_TOGGLE_REQUEST = "PLUGIN_TOGGLE_REQUEST"
    PLUGIN_TOGGLE_RESPONSE = "PLUGIN_TOGGLE_RESPONSE"
    PLUGIN_APPROVAL_GRANTED = "PLUGIN_APPROVAL_GRANTED"
    PLUGIN_APPROVAL_REJECTED = "PLUGIN_APPROVAL_REJECTED"

    # Skills & Slash Commands
    SLASH_COMMAND_LIST_REQUEST = "SLASH_COMMAND_LIST_REQUEST"
    SLASH_COMMAND_LIST_RESPONSE = "SLASH_COMMAND_LIST_RESPONSE"
    SKILL_LIST_REQUEST = "SKILL_LIST_REQUEST"
    SKILL_LIST_RESPONSE = "SKILL_LIST_RESPONSE"
    SKILL_TOGGLE_REQUEST = "SKILL_TOGGLE_REQUEST"
    SKILL_TOGGLE_RESPONSE = "SKILL_TOGGLE_RESPONSE"

    # Memory Management
    MEMORY_EPISODES_REQUEST = "MEMORY_EPISODES_REQUEST"
    MEMORY_EPISODES_RESPONSE = "MEMORY_EPISODES_RESPONSE"
    MEMORY_EPISODE_DELETE_REQUEST = "MEMORY_EPISODE_DELETE_REQUEST"
    MEMORY_EPISODE_DELETE_RESPONSE = "MEMORY_EPISODE_DELETE_RESPONSE"
    MEMORY_EPISODES_CLEAR_REQUEST = "MEMORY_EPISODES_CLEAR_REQUEST"
    MEMORY_EPISODES_CLEAR_RESPONSE = "MEMORY_EPISODES_CLEAR_RESPONSE"
    MEMORY_GRAPH_REQUEST = "MEMORY_GRAPH_REQUEST"
    MEMORY_GRAPH_RESPONSE = "MEMORY_GRAPH_RESPONSE"
    MEMORY_FACT_DELETE_REQUEST = "MEMORY_FACT_DELETE_REQUEST"
    MEMORY_FACT_DELETE_RESPONSE = "MEMORY_FACT_DELETE_RESPONSE"
    MEMORY_ENTITY_DELETE_REQUEST = "MEMORY_ENTITY_DELETE_REQUEST"
    MEMORY_ENTITY_DELETE_RESPONSE = "MEMORY_ENTITY_DELETE_RESPONSE"
    MEMORY_GRAPH_CLEAR_REQUEST = "MEMORY_GRAPH_CLEAR_REQUEST"
    MEMORY_GRAPH_CLEAR_RESPONSE = "MEMORY_GRAPH_CLEAR_RESPONSE"
    # Epic 4, E4-03: single-fact edit + full memory export.
    MEMORY_FACT_UPDATE_REQUEST = "MEMORY_FACT_UPDATE_REQUEST"
    MEMORY_FACT_UPDATE_RESPONSE = "MEMORY_FACT_UPDATE_RESPONSE"
    MEMORY_EXPORT_REQUEST = "MEMORY_EXPORT_REQUEST"
    MEMORY_EXPORT_RESPONSE = "MEMORY_EXPORT_RESPONSE"

    # Epic 10: backup / restore, diagnostics export, update check.
    BACKUP_CREATE_REQUEST = "BACKUP_CREATE_REQUEST"
    BACKUP_CREATE_RESPONSE = "BACKUP_CREATE_RESPONSE"
    BACKUP_RESTORE_REQUEST = "BACKUP_RESTORE_REQUEST"
    BACKUP_RESTORE_RESPONSE = "BACKUP_RESTORE_RESPONSE"
    DIAGNOSTICS_EXPORT_REQUEST = "DIAGNOSTICS_EXPORT_REQUEST"
    DIAGNOSTICS_EXPORT_RESPONSE = "DIAGNOSTICS_EXPORT_RESPONSE"
    VERSION_CHECK_REQUEST = "VERSION_CHECK_REQUEST"
    VERSION_CHECK_RESPONSE = "VERSION_CHECK_RESPONSE"

    # Workspace Management
    WORKSPACE_LIST_REQUEST = "WORKSPACE_LIST_REQUEST"
    WORKSPACE_LIST_RESPONSE = "WORKSPACE_LIST_RESPONSE"
    WORKSPACE_ADD_REQUEST = "WORKSPACE_ADD_REQUEST"
    WORKSPACE_ADD_RESPONSE = "WORKSPACE_ADD_RESPONSE"
    WORKSPACE_REMOVE_REQUEST = "WORKSPACE_REMOVE_REQUEST"
    WORKSPACE_REMOVE_RESPONSE = "WORKSPACE_REMOVE_RESPONSE"
    WORKSPACE_SET_ACTIVE_REQUEST = "WORKSPACE_SET_ACTIVE_REQUEST"
    WORKSPACE_SET_ACTIVE_RESPONSE = "WORKSPACE_SET_ACTIVE_RESPONSE"

    # Workspace RAG prose index (Epic 8, E8-3.2): consent-first
    # indexing lifecycle. START without confirm answers with the
    # estimate + embedder + destination (the consent preview); with
    # confirm it starts indexing and pushes PROGRESS events.
    WORKSPACE_INDEX_START_REQUEST = "WORKSPACE_INDEX_START_REQUEST"
    WORKSPACE_INDEX_START_RESPONSE = "WORKSPACE_INDEX_START_RESPONSE"
    WORKSPACE_INDEX_STATUS_REQUEST = "WORKSPACE_INDEX_STATUS_REQUEST"
    WORKSPACE_INDEX_STATUS_RESPONSE = "WORKSPACE_INDEX_STATUS_RESPONSE"
    WORKSPACE_INDEX_PROGRESS = "WORKSPACE_INDEX_PROGRESS"

    # Browser core (Epic 8, E8-6.1): dedicated-profile agent
    # browser. BROWSER_TASK_PAUSE_REQUEST is the browser control
    # channel — payload {"session_id": str | None, "action":
    # "pause" | "resume" | "take_control" | "return_control" |
    # "stop"} (session_id omitted targets the active session).
    # BROWSER_TASK_PAUSE_RESPONSE payload: {"success": bool,
    # "session_id": str | None, "control_state": "agent" |
    # "paused" | "user" | "waiting_user" | "stopped" | "none",
    # "current_url": str, "challenge": dict | None,
    # "error": str (only when success is False)}.
    # BROWSER_CHALLENGE_DETECTED is an outbound push when a CAPTCHA
    # or 2FA page parks the agent: payload {"session_id": str,
    # "task_id": str | None, "url": str, "challenge": "captcha" |
    # "two_factor"} — the agent waits for the user and never
    # attempts the challenge itself.
    BROWSER_TASK_PAUSE_REQUEST = "BROWSER_TASK_PAUSE_REQUEST"
    BROWSER_TASK_PAUSE_RESPONSE = "BROWSER_TASK_PAUSE_RESPONSE"
    BROWSER_CHALLENGE_DETECTED = "BROWSER_CHALLENGE_DETECTED"

    # Agent Management
    AGENT_LIST_REQUEST = "AGENT_LIST_REQUEST"
    AGENT_LIST_RESPONSE = "AGENT_LIST_RESPONSE"
    AGENT_UPDATE_REQUEST = "AGENT_UPDATE_REQUEST"
    AGENT_UPDATE_RESPONSE = "AGENT_UPDATE_RESPONSE"



class TaskState(str, Enum):
    PENDING = "PENDING"
    PRE_FLIGHT_CHECK = "PRE_FLIGHT_CHECK"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    EXECUTING_WORKER = "EXECUTING_WORKER"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_STATES: set[TaskState] = {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED}

LEGAL_TRANSITIONS: dict[TaskState, set[TaskState]] = {
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
    payload: dict[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION
    ts: float = field(default_factory=time.time)
    request_id: str | None = None
    event_id: str | None = None

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
