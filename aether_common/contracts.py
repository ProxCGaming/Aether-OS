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
    LOCAL_MODEL_UNLOAD_REQUEST = "LOCAL_MODEL_UNLOAD_REQUEST"
    LOCAL_MODEL_UNLOAD_RESPONSE = "LOCAL_MODEL_UNLOAD_RESPONSE"
    LOCAL_MODEL_DOWNLOAD_CANCEL = "LOCAL_MODEL_DOWNLOAD_CANCEL"
    # llama.cpp router (local serving backend beside Ollama): status,
    # the router's model list with load state, explicit load/unload
    # and supervised router start/stop.
    LLAMACPP_STATUS_REQUEST = "LLAMACPP_STATUS_REQUEST"
    LLAMACPP_STATUS_RESPONSE = "LLAMACPP_STATUS_RESPONSE"
    LLAMACPP_MODELS_REQUEST = "LLAMACPP_MODELS_REQUEST"
    LLAMACPP_MODELS_RESPONSE = "LLAMACPP_MODELS_RESPONSE"
    LLAMACPP_MODEL_LOAD_REQUEST = "LLAMACPP_MODEL_LOAD_REQUEST"
    LLAMACPP_MODEL_LOAD_RESPONSE = "LLAMACPP_MODEL_LOAD_RESPONSE"
    LLAMACPP_MODEL_UNLOAD_REQUEST = "LLAMACPP_MODEL_UNLOAD_REQUEST"
    LLAMACPP_MODEL_UNLOAD_RESPONSE = "LLAMACPP_MODEL_UNLOAD_RESPONSE"
    LLAMACPP_ROUTER_CONTROL_REQUEST = "LLAMACPP_ROUTER_CONTROL_REQUEST"
    LLAMACPP_ROUTER_CONTROL_RESPONSE = "LLAMACPP_ROUTER_CONTROL_RESPONSE"
    # Model Hub catalogue layer (H1): HF (live API) + Ollama library rows
    MODEL_HUB_SEARCH_REQUEST = "MODEL_HUB_SEARCH_REQUEST"
    MODEL_HUB_SEARCH_RESPONSE = "MODEL_HUB_SEARCH_RESPONSE"
    MODEL_HUB_DETAIL_REQUEST = "MODEL_HUB_DETAIL_REQUEST"
    MODEL_HUB_DETAIL_RESPONSE = "MODEL_HUB_DETAIL_RESPONSE"
    # Model Hub downloads (H2): pulls/downloads, inventory, delete
    MODEL_HUB_DOWNLOAD_REQUEST = "MODEL_HUB_DOWNLOAD_REQUEST"
    MODEL_HUB_DOWNLOAD_RESPONSE = "MODEL_HUB_DOWNLOAD_RESPONSE"
    MODEL_HUB_DOWNLOADED_REQUEST = "MODEL_HUB_DOWNLOADED_REQUEST"
    MODEL_HUB_DOWNLOADED_RESPONSE = "MODEL_HUB_DOWNLOADED_RESPONSE"
    MODEL_HUB_DELETE_REQUEST = "MODEL_HUB_DELETE_REQUEST"
    MODEL_HUB_DELETE_RESPONSE = "MODEL_HUB_DELETE_RESPONSE"
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
    # ({step_index, checkpoint_id, label, files_written, timestamp,
    # cut_message_id, write_events}); asking for an older task
    # returns no entries — rewind points exist only for the last
    # task (grill Q5). cut_message_id (M12) names the message each
    # point's retained history ends at, so pickers offer the actual
    # manifest boundaries. write_events (N01) lists the paths each
    # point's step wrote, repeats included, so the not-reverted
    # warning sees a file rewritten after the chosen point.
    # Rewind execution contracts arrive with E8-1.1b.
    REWIND_MANIFEST_GET_REQUEST = "REWIND_MANIFEST_GET_REQUEST"
    REWIND_MANIFEST_GET_RESPONSE = "REWIND_MANIFEST_GET_RESPONSE"
    # Epic 8, E8-1.1b: rewind execution. TASK_REWIND rolls the last
    # task back to a manifest point (state-only — files are never
    # reverted), holding the discarded steps for a 5-second undo
    # window; TASK_REWIND_UNDO restores them inside the window, and
    # expiry (or a new message) prunes them outright. The response
    # also carries the session's retained messages after staging
    # (M12), so the desktop renders the engine's own record — at
    # the final point that is the full history.
    TASK_REWIND_REQUEST = "TASK_REWIND_REQUEST"
    TASK_REWIND_RESPONSE = "TASK_REWIND_RESPONSE"
    TASK_REWIND_UNDO_REQUEST = "TASK_REWIND_UNDO_REQUEST"
    TASK_REWIND_UNDO_RESPONSE = "TASK_REWIND_UNDO_RESPONSE"
    # Epic 8, E8-1.2: fork from anywhere. SESSION_FORK copies a
    # session up to a completed step boundary — a manifest
    # step_index of the last task, or a message_id for older steps —
    # into a NEW session named "Fork of <title>"; the checkpoint
    # thread is copied up to the point when the boundary carries
    # one. The source session is never altered. The response carries
    # the fork's retained messages exactly as stored (M12).
    SESSION_FORK_REQUEST = "SESSION_FORK_REQUEST"
    SESSION_FORK_RESPONSE = "SESSION_FORK_RESPONSE"
    # Per-workspace sessions, slice 3: SESSION_MOVE re-files a
    # session under another workspace, or unbinds it with a null
    # workspace (bulk move is the renderer looping this pair).
    # Binding is organisational only (grill Q11) — a move touches
    # no history or checkpoints. Request: {session_id,
    # workspace: path | null}; response: {session_id, workspace,
    # moved} (+ error on refusal), where workspace is the session's
    # binding after the request, moved or not.
    SESSION_MOVE_REQUEST = "SESSION_MOVE_REQUEST"
    SESSION_MOVE_RESPONSE = "SESSION_MOVE_RESPONSE"
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
    # "model"}.
    USAGE_LEDGER_QUERY_REQUEST = "USAGE_LEDGER_QUERY_REQUEST"
    USAGE_LEDGER_QUERY_RESPONSE = "USAGE_LEDGER_QUERY_RESPONSE"
    TASK_COST_UPDATE = "TASK_COST_UPDATE"
    # Master build S3: Task Ledger (Mission Control) read model.
    # TASK_LEDGER_QUERY_REQUEST payload: {"task_id": str,
    # "session_id": str | None (thread hint for the checkpoint leg)};
    # TASK_LEDGER_QUERY_RESPONSE payload: the joined per-task summary
    # {"task_id", "found": bool, "session_id", "prompt", "state",
    # "outcome", "delegations", "tool_calls", "tests", "approvals",
    # "files_touched", "models", "costs", "errors", "transitions",
    # "blackboard_briefing", "coverage"} — "found" is False (with
    # empty sections) for a task no writer knows, never fabricated.
    # TASK_LEDGER_LIST_REQUEST payload: {"limit": int | None};
    # TASK_LEDGER_LIST_RESPONSE payload: {"tasks": [ {task_id,
    # session_id, prompt, summary, state, timestamp, sources,
    # cost_usd} ], "count": int}.
    TASK_LEDGER_QUERY_REQUEST = "TASK_LEDGER_QUERY_REQUEST"
    TASK_LEDGER_QUERY_RESPONSE = "TASK_LEDGER_QUERY_RESPONSE"
    TASK_LEDGER_LIST_REQUEST = "TASK_LEDGER_LIST_REQUEST"
    TASK_LEDGER_LIST_RESPONSE = "TASK_LEDGER_LIST_RESPONSE"
    # Wave 6 (D-1 option B): manual resume of a restart-interrupted
    # task from the ledger, gated by a pre-resume audit. Resume is
    # manual-only — these two requests are the only trigger; nothing
    # resumes at startup.
    # TASK_RESUME_AUDIT_REQUEST payload: {"task_id": str,
    # "session_id": str | None (thread hint)};
    # TASK_RESUME_AUDIT_RESPONSE payload: {"success": bool,
    # "task_id": str, "audit": {task_id, session_id, prompt, state,
    # verdict ("ready" | "uncertain" | "refused"), refusal_reason,
    # completed_steps, in_flight, files, pending_approvals,
    # coverage} | None, "error": str | None}.
    # TASK_RESUME_REQUEST payload: {"task_id": str,
    # "session_id": str | None, "confirm_uncertain": bool — the
    # owner's explicit confirmation, required when the audit's
    # verdict is "uncertain"};
    # TASK_RESUME_RESPONSE payload: {"success": bool, "task_id": str,
    # "session_id": str | None, "error": str | None,
    # "requires_confirmation": bool, "audit": {...} | None} — on
    # success the resumed task's own events stream on the same
    # socket exactly as an approval resume's do.
    TASK_RESUME_AUDIT_REQUEST = "TASK_RESUME_AUDIT_REQUEST"
    TASK_RESUME_AUDIT_RESPONSE = "TASK_RESUME_AUDIT_RESPONSE"
    TASK_RESUME_REQUEST = "TASK_RESUME_REQUEST"
    TASK_RESUME_RESPONSE = "TASK_RESUME_RESPONSE"
    # Full Furnish W-9: Workflows — a named, saved, ordered list of
    # task prompts, run in order as ordinary tasks (each step's
    # result carried into the next step's prompt as a bounded
    # excerpt). Definitions and run records persist in the workflow
    # store; runs are driven in-process and are NEVER auto-resumed
    # after an engine restart (D-1 discipline: a run left active by
    # a restart is closed as "interrupted", visible, not continued).
    # A workflow object is {"id", "name", "description",
    # "steps": [{"title", "prompt"}], "created_at", "updated_at"}.
    # A run object is {"run_id", "workflow_id", "session_id",
    # "status", "current_step", "steps": [{"title", "status",
    # "task_id"}]}; run status is one of "running" |
    # "awaiting_approval" | "completed" | "failed" | "stopped" |
    # "interrupted"; step status is one of "pending" | "running" |
    # "completed" | "failed" | "cancelled" | "skipped".
    # WORKFLOW_LIST_REQUEST payload: {}; WORKFLOW_LIST_RESPONSE
    # payload: {"workflows": [workflow...], "error": str (only on
    # failure, with an empty list)}.
    # WORKFLOW_SAVE_REQUEST payload: {"workflow": {"id": str | None
    # (absent/None = create), "name", "description",
    # "steps": [{"title", "prompt"}]}}; WORKFLOW_SAVE_RESPONSE
    # payload: {"success": bool, "workflow": workflow | None,
    # "error": str | None} — validation refusals and unknown-id
    # updates are success=False with the reason, never silent.
    # WORKFLOW_DELETE_REQUEST payload: {"workflow_id": str};
    # WORKFLOW_DELETE_RESPONSE payload: {"success": bool,
    # "error": str | None} — deleting a missing id is
    # success=False ("nothing was deleted"), never a fake success.
    # WORKFLOW_RUN_REQUEST payload: {"workflow_id": str};
    # WORKFLOW_RUN_RESPONSE payload: {"success": bool,
    # "run": run | None, "error": str | None} — on success the
    # run's step tasks stream on the same socket as ordinary tasks
    # in the run's session.
    # WORKFLOW_RUN_STOP_REQUEST payload: {"run_id": str};
    # WORKFLOW_RUN_STOP_RESPONSE payload: {"success": bool,
    # "error": str | None}.
    # WORKFLOW_RUN_LIST_REQUEST payload: {};
    # WORKFLOW_RUN_LIST_RESPONSE payload: {"runs": [run...]
    # (newest first), "error": str (only on failure)}.
    # WORKFLOW_RUN_STATUS is an outbound event emitted on every
    # run/step transition; its payload is the full run object.
    WORKFLOW_LIST_REQUEST = "WORKFLOW_LIST_REQUEST"
    WORKFLOW_LIST_RESPONSE = "WORKFLOW_LIST_RESPONSE"
    WORKFLOW_SAVE_REQUEST = "WORKFLOW_SAVE_REQUEST"
    WORKFLOW_SAVE_RESPONSE = "WORKFLOW_SAVE_RESPONSE"
    WORKFLOW_DELETE_REQUEST = "WORKFLOW_DELETE_REQUEST"
    WORKFLOW_DELETE_RESPONSE = "WORKFLOW_DELETE_RESPONSE"
    WORKFLOW_RUN_REQUEST = "WORKFLOW_RUN_REQUEST"
    WORKFLOW_RUN_RESPONSE = "WORKFLOW_RUN_RESPONSE"
    WORKFLOW_RUN_STOP_REQUEST = "WORKFLOW_RUN_STOP_REQUEST"
    WORKFLOW_RUN_STOP_RESPONSE = "WORKFLOW_RUN_STOP_RESPONSE"
    WORKFLOW_RUN_LIST_REQUEST = "WORKFLOW_RUN_LIST_REQUEST"
    WORKFLOW_RUN_LIST_RESPONSE = "WORKFLOW_RUN_LIST_RESPONSE"
    WORKFLOW_RUN_STATUS = "WORKFLOW_RUN_STATUS"
    # Epic 8, E8-A8.1b: context breakdown + daily budget alerting.
    # CONTEXT_BREAKDOWN_REQUEST payload: {"session_id": str, "task_id": str | None, "model": str | None}.
    # CONTEXT_BREAKDOWN_RESPONSE payload: {"model": str, "context_limit": int, "total_tokens": int,
    # "percent_used": float, "components": [{"name": str, "tokens": int, "share": float}]}.
    # DAILY_BUDGET_SET_REQUEST payload: {"daily_budget_usd": float | None}.
    # DAILY_BUDGET_SET_RESPONSE payload: {"daily_budget_usd": float | None, "ok": bool}.
    # DAILY_BUDGET_EXCEEDED payload: {"daily_budget_usd": float, "today_spend_usd": float} (soft alert push).
    CONTEXT_BREAKDOWN_REQUEST = "CONTEXT_BREAKDOWN_REQUEST"
    CONTEXT_BREAKDOWN_RESPONSE = "CONTEXT_BREAKDOWN_RESPONSE"
    DAILY_BUDGET_SET_REQUEST = "DAILY_BUDGET_SET_REQUEST"
    DAILY_BUDGET_SET_RESPONSE = "DAILY_BUDGET_SET_RESPONSE"
    DAILY_BUDGET_EXCEEDED = "DAILY_BUDGET_EXCEEDED"
    # Epic 8, E8-4.2: Media index + Library section
    MEDIA_LIST_REQUEST = "MEDIA_LIST_REQUEST"
    MEDIA_LIST_RESPONSE = "MEDIA_LIST_RESPONSE"
    # OP12: MEDIA_DELETE_REQUEST payload: {"media_id": str,
    # "delete_underlying_file": bool (optional, default False)} —
    # False removes only the library index entry; True also unlinks
    # the file from disk. MEDIA_DELETE_RESPONSE payload:
    # {"deleted": bool, "media_id": str, "file_deleted": bool} —
    # file_deleted is False when the unlink was not requested, the
    # file was already gone, or it failed; a client that requested it
    # must surface that shortfall honestly.
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
    # Epic 8, E8-4.3/N07: Library search served by the backend recall
    # path (lexical + semantic over summary/tags/notes/filename/
    # transcript). REQUEST payload: {"query", "collection", "limit"};
    # RESPONSE payload: {"query", "media": [item, ...], "degraded"} —
    # degraded=True means embeddings were unavailable and the results
    # are lexical-only (honest recall, never fabricated matches).
    MEDIA_SEARCH_REQUEST = "MEDIA_SEARCH_REQUEST"
    MEDIA_SEARCH_RESPONSE = "MEDIA_SEARCH_RESPONSE"
    # Calendar layer C4 (14 Nov 2026): a due reminder pushed by the
    # scheduler, outside any task — payload: {"event_id", "title",
    # "start_ts", "start_text", "kind": "lead" | "day_before"}. The
    # desktop turns it into an OS notification when unfocused
    # (electron/notify.cjs) and an in-app surface when focused.
    REMINDER_DUE = "REMINDER_DUE"
    # Calendar layer C5: the Settings surface's read of the diary.
    # REQUEST payload: {"limit": int (optional, default 20)};
    # RESPONSE payload: {"success": bool, "events": [event dicts as
    # in calendar/store.to_dict], "error": str (only on failure)}.
    CALENDAR_LIST_REQUEST = "CALENDAR_LIST_REQUEST"
    CALENDAR_LIST_RESPONSE = "CALENDAR_LIST_RESPONSE"
    # Full Furnish SPEC-6B C3 (FF5-06): the panel's direct create /
    # cancel — not via chat extraction. CREATE REQUEST payload:
    # {"title": str, "start_ts": epoch seconds (or "start": ISO
    # 8601), "duration_s": number | None, "accept_conflicts": bool
    # (optional)}; RESPONSE payload: {"success": bool, "saved":
    # bool, "event": event dict (calendar/store.to_dict) | None,
    # "conflicts": [{"event_id", "title", "start", "end"}...],
    # "error": str (only on failure)} — a non-empty conflicts list
    # on an unaccepted create is a WARNING (saved=False, nothing
    # stored), never an error; the same call with
    # accept_conflicts=True stores it. CANCEL REQUEST payload:
    # {"event_id": str}; RESPONSE: {"success": bool, "cancelled":
    # event_id | None, "error": str (only on failure)} — a missing
    # id is an honest not-found (success=False), never a fake
    # success; a successful cancel is a store-level delete, durable
    # across restart.
    CALENDAR_CREATE_REQUEST = "CALENDAR_CREATE_REQUEST"
    CALENDAR_CREATE_RESPONSE = "CALENDAR_CREATE_RESPONSE"
    CALENDAR_CANCEL_REQUEST = "CALENDAR_CANCEL_REQUEST"
    CALENDAR_CANCEL_RESPONSE = "CALENDAR_CANCEL_RESPONSE"
    # Epic 8, E8-5.1: streaming voice conversation. START_REQUEST
    # payload: {"session_id": str | None} — the engine creates a
    # session when omitted and answers {"ok": bool, "session_id": str,
    # "webrtc_config": dict | None, "error": str | None}; a refused
    # start is always ok=False with an honest "voice unavailable: ..."
    # error and never opens an audio device. STOP_REQUEST payload:
    # {"session_id": str}; STOP_RESPONSE: {"ok": bool}.
    # S15 (degradation ladder): the START_RESPONSE additionally
    # carries "voice_profile" ("full" | "degraded" | "text_only";
    # null when refused) and "notices": [str] — one sentence per
    # deviation from the full cascade the ladder planned (speech
    # output off, energy endpointing, a load-fallback STT model,
    # a measured-slow STT warning). VOICE_SESSION_STATUS is the
    # engine -> renderer push for MID-session transitions:
    # {"session_id", "profile": str, "notice": str (on a live
    # transition), "reason": str (only when profile is
    # "unavailable" — the session has ended, with the specific
    # stage + error as the reason)}. A session only ever steps
    # DOWN the ladder mid-flight, never back up.
    # Wave 4: the START_RESPONSE additionally carries the
    # EFFECTIVE speech profile — "tts_voice" (the voice the
    # session speaks with), "stt_model" (the STT model in effect
    # after any start-time ladder decision) and
    # "stt_configured_model" (the configured model it may have
    # stepped down from); all three are null on a refused start.
    # VOICE_SESSION_STATUS payloads likewise carry "stt_model"
    # (the model in effect after the transition) whenever the
    # session has a degradation tracker.
    # VOICE_PARTIAL_TRANSCRIPT is an outbound push for the user's
    # speech: {"session_id", "speaker": "user", "text": str,
    # "is_final": bool} — partials are provisional display only; the
    # is_final text is the one committed to session history.
    # VOICE_OUTPUT_CHUNK is an outbound push for the talker's reply:
    # {"session_id", "speaker": "assistant", "text_delta": str,
    # "audio_chunk": str | None (base64 audio), "timestamp": float}.
    # A chunk carrying audio also carries "chunk_seq": a
    # session-monotonic int identifying the chunk in emission
    # order (N09).
    # Announcement chunks (N10) additionally carry "announcement_id"
    # (stable per announced event, derived from the child task and
    # the event kind) and "announcement_kind" ("approval" |
    # "completed" | "failed"); a completed announcement also carries
    # "message_id", the announced message's own id in the session
    # store. Announcement text is never a reply turn: it must not be
    # committed as a new assistant message — the announced task's
    # own message already stands in chat.
    # VOICE_BARGE_IN (E8-5.1b behaviour; the contract lands with the
    # pipeline's interrupt seam in E8-5.1a): {"session_id",
    # "stopped_at_char": int, "committed_text": str} — committed
    # history keeps only the assistant words actually heard. When
    # the interrupted output was an announcement, the payload also
    # carries its "announcement_id" and the committed_text is NOT a
    # new message (see above).
    VOICE_SESSION_START_REQUEST = "VOICE_SESSION_START_REQUEST"
    VOICE_SESSION_START_RESPONSE = "VOICE_SESSION_START_RESPONSE"
    VOICE_SESSION_STOP_REQUEST = "VOICE_SESSION_STOP_REQUEST"
    VOICE_SESSION_STOP_RESPONSE = "VOICE_SESSION_STOP_RESPONSE"
    VOICE_PARTIAL_TRANSCRIPT = "VOICE_PARTIAL_TRANSCRIPT"
    VOICE_OUTPUT_CHUNK = "VOICE_OUTPUT_CHUNK"
    VOICE_BARGE_IN = "VOICE_BARGE_IN"
    VOICE_SESSION_STATUS = "VOICE_SESSION_STATUS"
    # N09: VOICE_PLAYBACK_PROGRESS is the renderer -> engine report
    # of playback position: {"session_id", "played_seq": int} —
    # the highest chunk_seq the renderer has FINISHED playing
    # (-1 = reporting live, nothing finished yet). Heard-word
    # accounting is derived from these reports, never from the
    # engine's emission count: emitted-but-unplayed audio was not
    # heard. Fire-and-forget: there is no response event, and a
    # stale or malformed report is ignored, never fatal.
    VOICE_PLAYBACK_PROGRESS = "VOICE_PLAYBACK_PROGRESS"
    # F-12a: RTC negotiation for the streaming voice session rides
    # this same socket. The renderer holds the microphone, so it is
    # the offerer. OFFER_REQUEST payload: {"session_id", "sdp":
    # {"type": "offer", "sdp": str}}; OFFER_RESPONSE: {"ok": bool,
    # "session_id", "answer": {"type": "answer", "sdp": str} | None,
    # "error": str | None} — a refused negotiation (no live session,
    # malformed offer) is ok=False with the honest reason.
    # ICE_REQUEST payload: {"session_id", "candidate": {"candidate":
    # str, "sdpMid": str | None, "sdpMLineIndex": int | None} |
    # None} — a null candidate is the end-of-candidates marker;
    # ICE_RESPONSE: {"ok": bool, "session_id", "error": str | None}.
    # VOICE_RTC_ICE is the engine -> renderer push carrying the
    # engine's own gathered candidates in the same shape:
    # {"session_id", "candidate": {...} | None}.
    VOICE_RTC_OFFER_REQUEST = "VOICE_RTC_OFFER_REQUEST"
    VOICE_RTC_OFFER_RESPONSE = "VOICE_RTC_OFFER_RESPONSE"
    VOICE_RTC_ICE_REQUEST = "VOICE_RTC_ICE_REQUEST"
    VOICE_RTC_ICE_RESPONSE = "VOICE_RTC_ICE_RESPONSE"
    VOICE_RTC_ICE = "VOICE_RTC_ICE"
    # Epic 8, E8-5.3: push-to-talk dictation. START_REQUEST payload:
    # {"session_id": str | None}; START_RESPONSE: {"ok": bool, "error": str | None}.
    # CHUNK_REQUEST payload: {"session_id": str | None, "audio": str,
    # "sample_rate": int, "is_final": bool}; CHUNK_RESPONSE: {"ok": bool,
    # "text": str, "is_final": bool, "error": str | None}.
    # STOP_REQUEST payload: {"session_id": str | None, "audio": str | None,
    # "sample_rate": int}; STOP_RESPONSE: {"ok": bool, "text": str, "error": str | None}.
    DICTATION_START_REQUEST = "DICTATION_START_REQUEST"
    DICTATION_START_RESPONSE = "DICTATION_START_RESPONSE"
    DICTATION_CHUNK_REQUEST = "DICTATION_CHUNK_REQUEST"
    DICTATION_CHUNK_RESPONSE = "DICTATION_CHUNK_RESPONSE"
    DICTATION_STOP_REQUEST = "DICTATION_STOP_REQUEST"
    DICTATION_STOP_RESPONSE = "DICTATION_STOP_RESPONSE"
    # Epic 8, E8-5.3: assistant message read-aloud (TTS). START_REQUEST
    # payload: {"message_id": str | None, "text": str, "voice": str | None};
    # START_RESPONSE: {"ok": bool, "message_id": str | None, "error": str | None}.
    # READ_ALOUD_CHUNK is an outbound push for synthesized speech:
    # {"message_id": str | None, "audio_chunk": str | None, "is_final": bool}.
    # STOP_REQUEST payload: {"message_id": str | None};
    # STOP_RESPONSE: {"ok": bool, "message_id": str | None}.
    READ_ALOUD_START_REQUEST = "READ_ALOUD_START_REQUEST"
    READ_ALOUD_START_RESPONSE = "READ_ALOUD_START_RESPONSE"
    READ_ALOUD_CHUNK = "READ_ALOUD_CHUNK"
    READ_ALOUD_STOP_REQUEST = "READ_ALOUD_STOP_REQUEST"
    READ_ALOUD_STOP_RESPONSE = "READ_ALOUD_STOP_RESPONSE"
    # Wave 4: Settings voice preview. REQUEST payload is either
    # {"probe": true} — answer availability only, synthesising
    # nothing — or {"voice": str} — synthesise the ONE fixed
    # preview sentence (engine-side constant; the request never
    # carries text). RESPONSE: {"ok": bool, "voice": str | None,
    # "audio_chunks": [str] (base64 WAV, in play order; empty
    # for a probe), "error": str | None} — a refused preview is
    # ok=False with the honest "voice unavailable: ..." reason.
    VOICE_PREVIEW_REQUEST = "VOICE_PREVIEW_REQUEST"
    VOICE_PREVIEW_RESPONSE = "VOICE_PREVIEW_RESPONSE"
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
    TOOL_ENABLED_SET_REQUEST = "TOOL_ENABLED_SET_REQUEST"
    TOOL_ENABLED_SET_RESPONSE = "TOOL_ENABLED_SET_RESPONSE"

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
    # MCP update check (Wave 5, FF4-04): for a server imported from a
    # registry entry, ask the engine to compare its recorded version
    # against the registry's latest published version. Explicit and
    # per-server — the list answer itself never touches the network.
    MCP_SERVER_UPDATE_CHECK_REQUEST = "MCP_SERVER_UPDATE_CHECK_REQUEST"
    MCP_SERVER_UPDATE_CHECK_RESPONSE = "MCP_SERVER_UPDATE_CHECK_RESPONSE"
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
    # Wave 5 (FF4-04 plugins half): re-check one installed plugin
    # against its recorded install source via a fresh prepare; the
    # reply carries the version/drift comparison (a view only —
    # nothing is updated by the check itself).
    PLUGIN_UPDATE_CHECK_REQUEST = "PLUGIN_UPDATE_CHECK_REQUEST"
    PLUGIN_UPDATE_CHECK_RESPONSE = "PLUGIN_UPDATE_CHECK_RESPONSE"

    # Skills & Slash Commands
    SLASH_COMMAND_LIST_REQUEST = "SLASH_COMMAND_LIST_REQUEST"
    SLASH_COMMAND_LIST_RESPONSE = "SLASH_COMMAND_LIST_RESPONSE"
    SKILL_LIST_REQUEST = "SKILL_LIST_REQUEST"
    SKILL_LIST_RESPONSE = "SKILL_LIST_RESPONSE"
    SKILL_TOGGLE_REQUEST = "SKILL_TOGGLE_REQUEST"
    SKILL_TOGGLE_RESPONSE = "SKILL_TOGGLE_RESPONSE"
    # Wave 5 (D4-6): the skills registry workflow (Epic 7's CLI
    # engine) over the socket — search a NAMED source (there is no
    # default registry), prepare one skill (fetch + hash, nothing
    # written) so the install card can show the pin, then install
    # against that reviewed hash. A fresh install lands disabled;
    # enabling stays the separate SKILL_TOGGLE act.
    SKILL_SEARCH_REQUEST = "SKILL_SEARCH_REQUEST"
    SKILL_SEARCH_RESPONSE = "SKILL_SEARCH_RESPONSE"
    SKILL_INSTALL_PREPARE_REQUEST = "SKILL_INSTALL_PREPARE_REQUEST"
    SKILL_INSTALL_PREPARE_RESPONSE = "SKILL_INSTALL_PREPARE_RESPONSE"
    SKILL_INSTALL_REQUEST = "SKILL_INSTALL_REQUEST"
    SKILL_INSTALL_RESPONSE = "SKILL_INSTALL_RESPONSE"

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
    # Master build S9: the Settings memory-backend picker —
    # the status report for ALL four backends (states + reasons,
    # memory/backend_status.py). Selecting a backend rides the
    # existing CONFIG_UPDATE_REQUEST; there is no separate
    # set message.
    MEMORY_BACKENDS_REQUEST = "MEMORY_BACKENDS_REQUEST"
    MEMORY_BACKENDS_RESPONSE = "MEMORY_BACKENDS_RESPONSE"
    # Full Furnish SPEC-6B C1 (FF5-03): the Profile panel's read of
    # the facade's user_profile() view, paginated per section.
    # REQUEST payload: {"sections": {"user"|"people"|"preferences":
    # {"limit": int | None, "offset": int}}} (a flat "limit"/
    # "offset" applies to all three sections); RESPONSE payload:
    # {"user": {"items": [...], "total": int}, "people": {"items":
    # [...] (flattened in grouped order, each item carrying its
    # entity_name), "total": int}, "preferences": {"items": [...],
    # "total": int}, "backend": str} — or {"profile": None,
    # "error": str} when the backend is unavailable: the explicit
    # unavailable state, never an empty profile dressed as success.
    # Every item carries "source_label" ("Set by you" / "From
    # chat · 17 Nov"), derived at the facade from provenance +
    # timestamp. Edit/Delete reuse MEMORY_FACT_UPDATE /
    # MEMORY_FACT_DELETE above. (The facade keeps its grouped
    # shape for in-engine consumers; the wire shape above is the
    # panel's — integrator reconciliation, Wave 6.)
    MEMORY_PROFILE_REQUEST = "MEMORY_PROFILE_REQUEST"
    MEMORY_PROFILE_RESPONSE = "MEMORY_PROFILE_RESPONSE"
    # Full Furnish SPEC-6B C2 (FF5-05): the Forget centre's four
    # calls. CANDIDATES REQUEST payload: {} — RESPONSE:
    # {"candidates": [{"topic", "fact_count", "episode_count",
    # "last_touched"}...] (distinct topics ranked by item count),
    # "backend": str, "error": str (only on failure)}. EXECUTE
    # REQUEST: {"topic": str} — RESPONSE: {"success": bool,
    # "topic": str, "removed": [{"fact_id", "summary"}...,
    # {"episode_id", "summary"}...], "counts": {"facts",
    # "episodes", "lessons"}, "tombstone_id": str | None,
    # "undo_supported": bool, "error": str (only on failure)} —
    # the removed list is EXACTLY what was deleted. UNDO REQUEST:
    # {} — RESPONSE: {"success": bool, "undone": bool, "topic":
    # str, "counts": {...}, "restored": {...}, "error": str (only
    # when a restore was refused)} — nothing to undo is
    # undone=False with success=True: an honest empty result, not
    # an error. HISTORY REQUEST: {"limit": int, "offset": int} —
    # RESPONSE: {"history": [{"tombstone_id", "topic",
    # "fact_count", "episode_count", "forgotten_at",
    # "undoable"}...], "total": int, "backend": str, "error": str
    # (only on failure)}; undoable is true only for the latest
    # unconsumed tombstone.
    MEMORY_FORGET_CANDIDATES_REQUEST = "MEMORY_FORGET_CANDIDATES_REQUEST"
    MEMORY_FORGET_CANDIDATES_RESPONSE = "MEMORY_FORGET_CANDIDATES_RESPONSE"
    MEMORY_FORGET_EXECUTE_REQUEST = "MEMORY_FORGET_EXECUTE_REQUEST"
    MEMORY_FORGET_EXECUTE_RESPONSE = "MEMORY_FORGET_EXECUTE_RESPONSE"
    MEMORY_FORGET_UNDO_REQUEST = "MEMORY_FORGET_UNDO_REQUEST"
    MEMORY_FORGET_UNDO_RESPONSE = "MEMORY_FORGET_UNDO_RESPONSE"
    MEMORY_FORGET_HISTORY_REQUEST = "MEMORY_FORGET_HISTORY_REQUEST"
    MEMORY_FORGET_HISTORY_RESPONSE = "MEMORY_FORGET_HISTORY_RESPONSE"
    # Full Furnish Wave 6 (SPEC-6B C4/C5): the Settings → Memory
    # controls read — store counts + decay read-out from the
    # facade's memory_stats() — and the graph neighbourhood read
    # (co-occurrence derived at read time, D-4). Both are plain
    # request/response reads over the facade, answered off the
    # receive loop like the other MEMORY_* reads.
    MEMORY_STATS_REQUEST = "MEMORY_STATS_REQUEST"
    MEMORY_STATS_RESPONSE = "MEMORY_STATS_RESPONSE"
    MEMORY_GRAPH_NEIGHBOURHOOD_REQUEST = "MEMORY_GRAPH_NEIGHBOURHOOD_REQUEST"
    MEMORY_GRAPH_NEIGHBOURHOOD_RESPONSE = "MEMORY_GRAPH_NEIGHBOURHOOD_RESPONSE"
    # Master build S10: the Settings "This machine" card —
    # the hardware probe + capability matrix report
    # (machine_capability.py — the MACHINE, not the LLM
    # benchmarks in the capability/ package). The payload
    # may carry {"force": true} to bypass the report cache
    # (the card's Re-probe action).
    MACHINE_CAPABILITY_REQUEST = "MACHINE_CAPABILITY_REQUEST"
    MACHINE_CAPABILITY_RESPONSE = "MACHINE_CAPABILITY_RESPONSE"
    # Master build S14: the Settings → Agent Orchestration
    # "Model routing" card — the per-agent effective-model
    # report (langgraph/model_routing.effective_models_report):
    # for each agent, the model that would serve it, from where
    # (pin / auto / run default) and why in one sentence.
    AGENT_ROUTING_REQUEST = "AGENT_ROUTING_REQUEST"
    AGENT_ROUTING_RESPONSE = "AGENT_ROUTING_RESPONSE"

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

    # Project config layer (Epic 1, E1-14 layers; the editor is the
    # Epic 6 handoff, E6-F05): view/edit the active workspace's
    # `.aether/config.json` — the layer config_layers merges over the
    # global config. GET answers with the file's raw text plus its
    # parsed form (or the parse error); UPDATE validates and writes
    # it. Roots are confined to the configured workspace_roots.
    PROJECT_CONFIG_GET_REQUEST = "PROJECT_CONFIG_GET_REQUEST"
    PROJECT_CONFIG_GET_RESPONSE = "PROJECT_CONFIG_GET_RESPONSE"
    PROJECT_CONFIG_UPDATE_REQUEST = "PROJECT_CONFIG_UPDATE_REQUEST"
    PROJECT_CONFIG_UPDATE_RESPONSE = "PROJECT_CONFIG_UPDATE_RESPONSE"

    # Workspace RAG prose index (Epic 8, E8-3.2, E8-3.3): consent-first
    # indexing lifecycle. START without confirm answers with the
    # estimate + embedder + destination (the consent preview); with
    # confirm it starts indexing and pushes PROGRESS events.
    # PAUSE toggles incremental watching; CONFIG switches embedder
    # and requires full re-index.
    WORKSPACE_INDEX_START_REQUEST = "WORKSPACE_INDEX_START_REQUEST"
    WORKSPACE_INDEX_START_RESPONSE = "WORKSPACE_INDEX_START_RESPONSE"
    WORKSPACE_INDEX_STATUS_REQUEST = "WORKSPACE_INDEX_STATUS_REQUEST"
    WORKSPACE_INDEX_STATUS_RESPONSE = "WORKSPACE_INDEX_STATUS_RESPONSE"
    WORKSPACE_INDEX_PROGRESS = "WORKSPACE_INDEX_PROGRESS"
    WORKSPACE_INDEX_PAUSE_REQUEST = "WORKSPACE_INDEX_PAUSE_REQUEST"
    WORKSPACE_INDEX_PAUSE_RESPONSE = "WORKSPACE_INDEX_PAUSE_RESPONSE"
    WORKSPACE_INDEX_CONFIG_REQUEST = "WORKSPACE_INDEX_CONFIG_REQUEST"
    WORKSPACE_INDEX_CONFIG_RESPONSE = "WORKSPACE_INDEX_CONFIG_RESPONSE"

    # Browser core (Epic 8, E8-6.1): dedicated-profile agent
    # browser. BROWSER_TASK_PAUSE_REQUEST is the browser control
    # channel — payload {"session_id": str | None,
    # "chat_session_id": str | None, "action": "pause" | "resume" |
    # "take_control" | "return_control" | "stop"}. The engine
    # resolves the target across the ID spaces (review finding
    # M04): the browser session id, then the session bound to the
    # chat session id, then a chat session id or task id carried
    # in session_id; with no identifier it targets the active
    # session, and an identifier that matches nothing is an honest
    # error — never a silent fallback to the active session.
    # BROWSER_TASK_PAUSE_RESPONSE payload: {"success": bool,
    # "session_id": str | None, "task_id": str | None,
    # "chat_session_id": str | None, "control_state": "agent" |
    # "paused" | "user" | "waiting_user" | "stopped" | "none",
    # "current_url": str, "challenge": dict | None,
    # "error": str (only when success is False)}.
    # BROWSER_CHALLENGE_DETECTED is an outbound push when a CAPTCHA
    # or 2FA page parks the agent: payload {"session_id": str,
    # "task_id": str | None, "chat_session_id": str | None,
    # "url": str, "challenge": "captcha" | "two_factor"} — the
    # agent waits for the user and never attempts the challenge
    # itself.
    BROWSER_TASK_PAUSE_REQUEST = "BROWSER_TASK_PAUSE_REQUEST"
    BROWSER_TASK_PAUSE_RESPONSE = "BROWSER_TASK_PAUSE_RESPONSE"
    BROWSER_CHALLENGE_DETECTED = "BROWSER_CHALLENGE_DETECTED"

    # Browser confirmation policy (Epic 8, E8-6.2): the Settings →
    # Browser Agent Policy surface. GET_RESPONSE payload:
    # {"enabled": bool, "vault_autofill": bool,
    # "envelopes_enabled": bool, "categories": {"payments": str,
    # "sends_publishes": str, "deletes": str,
    # "account_changes": str}} where each mode is "always_confirm" |
    # "allow_with_envelope" | "allow_freely". SET_REQUEST is a
    # partial update — any of the same fields, absent/None meaning
    # "unchanged". SET_RESPONSE payload: {"success": bool,
    # "policy": <the full policy dict>, "error": str (only when
    # success is False)}; a rejected update changes nothing.
    BROWSER_POLICY_GET_REQUEST = "BROWSER_POLICY_GET_REQUEST"
    BROWSER_POLICY_GET_RESPONSE = "BROWSER_POLICY_GET_RESPONSE"
    BROWSER_POLICY_SET_REQUEST = "BROWSER_POLICY_SET_REQUEST"
    BROWSER_POLICY_SET_RESPONSE = "BROWSER_POLICY_SET_RESPONSE"

    # Desktop control (master build S20): the live control lane
    # for Computer Use actuation — the BrowserControlState design
    # generalised to the desktop (desktop/control.py). DESKTOP_
    # CONTROL_REQUEST payload: {"action": "pause" | "resume" |
    # "take_control" | "return_control" | "stop",
    # "task_id": str | None, "chat_session_id": str | None} —
    # "stop" names a task (or stops the task currently driving)
    # and refuses that task's further desktop actions WITHOUT
    # killing the task. DESKTOP_CONTROL_RESPONSE payload:
    # {"success": bool, "control_state": "agent" | "paused" |
    # "user", "stopped_tasks": [str], "driving_task": str | None,
    # "stopped_task": str | None, "error": str (only when success
    # is False)}. Only the user drives this channel; no agent
    # path can pause, resume, or return control.
    DESKTOP_CONTROL_REQUEST = "DESKTOP_CONTROL_REQUEST"
    DESKTOP_CONTROL_RESPONSE = "DESKTOP_CONTROL_RESPONSE"

    # Desktop remembered grants (master build S20): the durable
    # (app, category) "don't ask again" grants the confirmation
    # card can create, reviewed in Settings → Computer Use.
    # DESKTOP_GRANTS_REQUEST payload: {"action": "list"} or
    # {"action": "revoke", "grant_id": int}. DESKTOP_GRANTS_
    # RESPONSE payload: {"success": bool, "grants": [{"id": int,
    # "app": str, "category": str, "granted_at": float}],
    # "error": str (only when success is False)}. Grants never
    # exist for the hard-confirm categories (close_or_quit,
    # system) — the store refuses them at write time.
    DESKTOP_GRANTS_REQUEST = "DESKTOP_GRANTS_REQUEST"
    DESKTOP_GRANTS_RESPONSE = "DESKTOP_GRANTS_RESPONSE"

    # Credential vault (Epic 8 — E8-6.3). Entries live encrypted in
    # ~/.aether/vault.db (the DPAPI / keychain BaseProtector, same as
    # provider keys) and are bound to their site. Listing is METADATA
    # ONLY — a password is never in any payload; retrieval is
    # mediated by the engine's fill service (vault -> form field),
    # never to the model. LIST_RESPONSE payload: {"entries":
    # [{"id", "site", "username", "ask_every_time", "created_at": int,
    # "created_by": "user"|"agent", "last_used_at": int|None}]}.
    # SAVE_REQUEST payload: {"id": str|None, "site": str,
    # "username": str, "password": str, "ask_every_time": bool,
    # "totp_secret": str (optional; the owner's own manual use only —
    # agents never receive TOTP secrets)}. SAVE_RESPONSE /
    # DELETE_RESPONSE payloads: {"success": bool, "id": str}.
    # VAULT_FILL_APPROVAL_REQUEST is an outbound push for an
    # ask-every-time entry: {"request_id", "entry_id", "site",
    # "username", "task_id"}; the desktop answers with
    # VAULT_FILL_APPROVAL_RESPONSE {"request_id", "approved"}.
    VAULT_ENTRY_LIST_REQUEST = "VAULT_ENTRY_LIST_REQUEST"
    VAULT_ENTRY_LIST_RESPONSE = "VAULT_ENTRY_LIST_RESPONSE"
    VAULT_ENTRY_SAVE_REQUEST = "VAULT_ENTRY_SAVE_REQUEST"
    VAULT_ENTRY_SAVE_RESPONSE = "VAULT_ENTRY_SAVE_RESPONSE"
    VAULT_ENTRY_DELETE_REQUEST = "VAULT_ENTRY_DELETE_REQUEST"
    VAULT_ENTRY_DELETE_RESPONSE = "VAULT_ENTRY_DELETE_RESPONSE"
    VAULT_FILL_APPROVAL_REQUEST = "VAULT_FILL_APPROVAL_REQUEST"
    VAULT_FILL_APPROVAL_RESPONSE = "VAULT_FILL_APPROVAL_RESPONSE"
    # Authenticator view (PRD Open Point 14, decided YES by the
    # owner 1 Nov 2026): the vault doubles as the owner's
    # authenticator app. Standalone TOTP entries enrol from the
    # Settings vault panel — TOTP_SAVE_REQUEST payload: {"id":
    # str|None, "label": str, "issuer": str, "secret": str} (the
    # Base32 setup key shown beside a site's QR code; blank on
    # update keeps the stored secret). The engine validates the
    # secret and stores it normalised + encrypted; a malformed
    # secret is refused with {"success": False, "id", "error"}.
    # TOTP_LIST_RESPONSE {"entries": [{"id", "label", "issuer",
    # "created_at", "updated_at"}]} is METADATA ONLY, like the
    # login listing. TOTP_CODES_RESPONSE is the ONLY payload that
    # ever carries a code: {"generated_at": int, "codes":
    # [{"entry_id", "kind": "authenticator"|"login", "label",
    # "issuer", "username" (login kind only), "code": str,
    # "period": 30, "seconds_remaining": int}]} — codes computed
    # engine-side (RFC 6238) from secrets that never leave the
    # engine, served only on the owner's Settings channel. No
    # agent tool or task event can request or receive any of
    # these payloads; viewing codes is audited VAULT_TOTP_ACCESSED.
    VAULT_TOTP_LIST_REQUEST = "VAULT_TOTP_LIST_REQUEST"
    VAULT_TOTP_LIST_RESPONSE = "VAULT_TOTP_LIST_RESPONSE"
    VAULT_TOTP_SAVE_REQUEST = "VAULT_TOTP_SAVE_REQUEST"
    VAULT_TOTP_SAVE_RESPONSE = "VAULT_TOTP_SAVE_RESPONSE"
    VAULT_TOTP_DELETE_REQUEST = "VAULT_TOTP_DELETE_REQUEST"
    VAULT_TOTP_DELETE_RESPONSE = "VAULT_TOTP_DELETE_RESPONSE"
    VAULT_TOTP_CODES_REQUEST = "VAULT_TOTP_CODES_REQUEST"
    VAULT_TOTP_CODES_RESPONSE = "VAULT_TOTP_CODES_RESPONSE"

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
    # SPEC-6A: the engine process died (or was killed) while the
    # task was executing. Written only by the startup orphan
    # sweep, never by a live executor — a live task ends as
    # SUCCEEDED / FAILED / CANCELLED.
    INTERRUPTED = "INTERRUPTED"


TERMINAL_STATES: set[TaskState] = {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED, TaskState.INTERRUPTED}

LEGAL_TRANSITIONS: dict[TaskState, set[TaskState]] = {
    TaskState.PENDING:   {TaskState.RUNNING, TaskState.PRE_FLIGHT_CHECK, TaskState.CANCELLED},
    TaskState.PRE_FLIGHT_CHECK: {TaskState.RUNNING, TaskState.PENDING_APPROVAL, TaskState.FAILED, TaskState.CANCELLED, TaskState.INTERRUPTED},
    TaskState.PENDING_APPROVAL: {TaskState.RUNNING, TaskState.EXECUTING_WORKER, TaskState.FAILED, TaskState.CANCELLED},
    TaskState.EXECUTING_WORKER: {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED, TaskState.INTERRUPTED},
    TaskState.RUNNING:   {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED, TaskState.PRE_FLIGHT_CHECK, TaskState.PENDING_APPROVAL, TaskState.EXECUTING_WORKER, TaskState.INTERRUPTED},
    TaskState.SUCCEEDED: set(),
    TaskState.FAILED:    set(),
    TaskState.CANCELLED: set(),
    TaskState.INTERRUPTED: set(),
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

        # E8-04: a frame whose top level is not a JSON object (a
        # scalar, an array, null) is a contract violation, not an
        # AttributeError — consumers guard on the typed errors below
        # and answer with an ERROR reply; anything else escaping
        # tears the connection down.
        if not isinstance(data, dict):
            raise InvalidEventPayloadError(
                f"Event frame must be a JSON object, got {type(data).__name__}"
            )

        ver = data.get("schema_version")
        if ver != SCHEMA_VERSION:
            raise SchemaVersionMismatchError(
                f"Schema version mismatch: event has {ver}, expected {SCHEMA_VERSION}"
            )

        try:
            ev_type = EventType(data["type"])
        except (KeyError, ValueError) as e:
            raise InvalidEventPayloadError(f"Invalid or missing event type: {e}")

        # E8-04: the payload is part of the contract — every consumer
        # treats it as a dict, so a non-object payload (including an
        # explicit null) is rejected here instead of surfacing later
        # as an AttributeError inside a handler.
        payload = data.get("payload", {})
        if not isinstance(payload, dict):
            raise InvalidEventPayloadError(
                f"Event payload must be a JSON object, got {type(payload).__name__}"
            )

        req_id = data.get("request_id") or data.get("event_id")
        ev_id = data.get("event_id") or req_id

        return cls(
            type=ev_type,
            payload=payload,
            schema_version=ver,
            ts=data.get("ts", time.time()),
            request_id=req_id,
            event_id=ev_id,
        )
