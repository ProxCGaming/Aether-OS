"""Per-connection context for the task websocket (Wave 5 split).

Every handler inside ``ws_tasks`` used to be a closure over the
endpoint's locals — the send-locked socket, the audit logger, the
background-task registry and the per-name lock maps. ``WsConnection``
gathers exactly those per-connection pieces into one object, so the
handler families can move out of the endpoint into their own
modules and be handed this context instead of closing over the
endpoint. It holds no engine state of its own: engine-wide state
stays on the ``engine_state`` singleton, and one context is created
per connection, exactly as the closure locals were.

Slice 2 grows the context with the remaining per-connection state
the handler families closed over — the connection's secret store,
its voice-session state, the voice/dictation chain tails and the
voice-start lock — and with the reply discipline (``send_event`` /
``reply``) the ``_send_*`` helpers used to re-type per handler
(deslop C5): build the payload off the loop, turn a failure into
the family's error payload (never silence), send the correlated
Event, and swallow a dead socket with a debug log.

Slice 3 grows it with the connection's task state — the runner,
its session, the chained launch tail, the active downloads, the
current task id, the plugin installs this connection prepared —
which the task pump and the START_TASK launch (now module-level
in ``routes/ws_task_exec.py``) share with the receive loop and
its dispatch handlers.
"""
import asyncio
import logging
from typing import Any

from fastapi import WebSocket

from aether_common.contracts import Event

logger = logging.getLogger("aether_engine.routes.ws_connection")


class WsConnection:
    """One task-websocket connection's shared working state.

    ``ws`` is the endpoint's send-locked socket — the
    ``_SendLockedWebSocket`` proxy ``ws_tasks`` rebinds its socket
    reference to right after accept — so every send through it
    serialises through the one send lock (N-05).
    """

    def __init__(self, ws: WebSocket, audit_logger: Any,
                 secret_store: Any = None) -> None:
        self.ws = ws
        self.audit_logger = audit_logger
        # The connection's own secret store. ``ws_tasks`` passes
        # the instance it constructed through the websocket
        # module's (patchable) ``SecretStore`` name; the default
        # construction here only serves standalone contexts.
        if secret_store is None:
            from aether_engine.secrets.storage import SecretStore

            secret_store = SecretStore()
        self.secret_store = secret_store
        # N-19: every fire-and-forget helper spawned for this
        # connection registers here so disconnect cleanup can cancel
        # the work that would otherwise outlive its socket (sends
        # into a dead connection, downloads nobody is waiting for).
        self.background_tasks: set[asyncio.Task] = set()
        self._mcp_server_locks: dict[str, asyncio.Lock] = {}
        self._plugin_lifecycle_locks: dict[str, asyncio.Lock] = {}
        # Epic 8, E8-5.1a: this connection's voice session, if any. The
        # pipeline runs as its own task — the talker lane never queues
        # behind task_runner (grill Q28/Q29).
        self.voice_state: dict[str, Any] = {
            "pipeline": None,
            "task": None,
            "read_aloud_task": None,
            "read_aloud_id": None,
            # S4-M7: per-connection speech-engine cache. Building a
            # FasterWhisperStt/KokoroTts loads a model, so dictation and
            # read-aloud reuse one engine for the connection's lifetime
            # instead of constructing a fresh one per chunk/request.
            # H3: the dictation slot is keyed by the configured speech
            # STT model (dictation_stt_model) — a settings change swaps
            # the engine on next use instead of silently dictating with
            # the old model until reconnect.
            "dictation_stt": None,
            "dictation_stt_opened": False,
            "dictation_stt_model": None,
            "read_aloud_tts": None,
            "read_aloud_tts_opened": False,
        }
        # RUX-B-09: dictation chunk/stop work is chained the same way —
        # transcription results are applied in arrival order even though
        # each chunk runs off the receive loop.
        self.dictation_tail: asyncio.Task | None = None
        # RUX-B-09: voice session start/stop operations are chained too,
        # so a stop sent while a start is still opening its stages lands
        # after that start — the order the serial loop used to give —
        # instead of racing it and leaving a session running.
        self.voice_op_tail: asyncio.Task | None = None
        # Session starts serialise on this lock, since two starts
        # would fight over voice_state (RUX-B-09).
        self.voice_start_lock = asyncio.Lock()
        # Slice 3: this connection's task-execution state, shared
        # by the pump, the launch pipeline and the dispatch
        # handlers (routes/ws_task_exec.py).
        self.task_runner: asyncio.Task | None = None
        # Session of the task this connection most recently started or
        # resumed — lets STEER_TASK tell "a task is live for this session"
        # apart from "start a new task" (Epic 1, E1-07).
        self.task_session_id: str | None = None
        # RUX-B-07: START_TASK's pre-work runs in a launch task, not in
        # the receive loop. Launches are chained in receipt order (each
        # launch waits for the previous one) so the ordering the serial
        # loop used to guarantee between successive task starts — cancel
        # the old runner first, commit any staged rewind, then build the
        # new task — is preserved inside the launch pipeline.
        self.task_start_tail: asyncio.Task | None = None
        self.active_downloads: dict[str, asyncio.Task] = {}
        # N-44: the id of the task this connection is currently running
        # (set by the task pump when TASK_CREATED flows past, cleared
        # when the pump exits) — CANCEL_TASK reports the task it
        # actually cancelled instead of a hard-coded "unknown".
        self.active_task_id: str | None = None
        # E1-06: plugin installs THIS connection prepared — dropped
        # from the module-global pending table when it disconnects.
        self.own_plugin_install_ids: set[str] = set()
        # The task pump (``execute_task_generator``) lives in
        # routes/ws_task_exec.py (slice 3); this slot holds it
        # bound to this connection (a ``functools.partial`` filled
        # in by ``ws_tasks``), so the voice family's escalated
        # background jobs can drive the pump through the context,
        # as they did when it was a ``ws_tasks`` closure.
        self.execute_task_generator: Any = None

    def spawn_background(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)
        return task

    def mcp_server_lock(self, name: str) -> asyncio.Lock:
        #
        # Race guard: the serial loop also implicitly guaranteed one MCP
        # operation per server at a time. The client layer is built for
        # cross-task session ownership (disconnects are routed through
        # each session's owner task), but two operations on the SAME
        # server — connect racing remove, a resource read racing
        # disconnect — could still interleave destructively against the
        # registry's session/config state, so each helper holds a
        # per-server lock around its registry work. Operations on
        # different servers, and every other message, stay parallel. The
        # map is per-connection (like active_downloads): separate
        # connections always had separate loops, so cross-connection
        # contention remains the registry/client layer's existing
        # contract, unchanged by this slice.
        lock = self._mcp_server_locks.get(name)
        if lock is None:
            lock = self._mcp_server_locks[name] = asyncio.Lock()
        return lock

    def plugin_lifecycle_lock(self, name: str) -> asyncio.Lock:
        # A per-plugin lock preserves the serial loop's ordering
        # between lifecycle operations on the same plugin, as the
        # MCP helpers' per-server lock does (RUX-B-04).
        lock = self._plugin_lifecycle_locks.get(name)
        if lock is None:
            lock = self._plugin_lifecycle_locks[name] = asyncio.Lock()
        return lock

    async def send_event(self, req_id, event_type, payload,
                         send_failure_message) -> None:
        """Send one correlated reply Event, swallowing a dead socket.

        The tail every ``_send_*`` helper used to re-type: the send
        is the last step, and a client that disconnected mid-work
        leaves nothing else to do — the work already happened (or
        already failed honestly into the payload) — so a failed
        send is a debug line, never an exception into the caller.
        """
        try:
            await self.ws.send_text(Event(
                type=event_type, request_id=req_id, payload=payload,
            ).to_json())
        except Exception as e:  # noqa: BLE001 — the client may have disconnected mid-answer; nothing else to do
            logger.debug(f"{send_failure_message}: {e}")

    async def reply(self, req_id, event_type, payload_builder, *,
                    error_payload, send_failure_message,
                    warn=None) -> None:
        """Answer one request with the C5 reply discipline.

        ``payload_builder`` is an async callable producing the
        success payload (its heavy work already runs off the
        receive loop — the caller spawned this as a background
        task). If it raises, the answer is ``error_payload(exc)``
        — the family's own failure shape, so a failure is always a
        response, never silence — after logging ``warn(exc)`` when
        the family's original handler logged a warning there. The
        send itself goes through :meth:`send_event`.
        """
        try:
            payload = await payload_builder()
        except Exception as e:  # noqa: BLE001 — a failed build must still produce a response, never silence
            if warn is not None:
                logger.warning(warn(e))
            payload = error_payload(e)
        await self.send_event(
            req_id, event_type, payload, send_failure_message)
