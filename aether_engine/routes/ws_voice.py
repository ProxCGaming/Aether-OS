"""Voice session, dictation + speech engines for the task websocket (Wave 5 split).

The voice family of ``ws_tasks`` (RUX-B-09): voice-session
start/stop, the chained dictation handlers, the per-connection
speech-engine cache, the voice pipeline's lifecycle helpers, the
escalated background-job runner, and the STT/TTS factories. The
family's per-connection state — session state, chain tails,
start lock — lives on the
:class:`~aether_engine.routes.ws_connection.WsConnection` it is
handed.

Patch surface: the suites setattr ``create_stt_engine`` /
``create_tts_engine`` / ``_create_provider_instance`` /
``classify_intent`` / ``run_langgraph_task`` /
``build_tool_registry`` on
``aether_engine.routes.websocket``. The factories are defined
here and re-exported by the websocket module, and every call
site resolves them back through that module at call time (the
codebase's lazy-import idiom), so the patches keep
intercepting. The escalated background job drives the task pump
through ``conn.execute_task_generator`` — the pump itself stays
a ``ws_tasks`` closure until slice 3.

Reply discipline (deslop C5): the session-stop, dictation and
session-start replies send through ``WsConnection.send_event``;
their payload construction stays bespoke (the session start's
fail-closed staging, the dictation handlers' reason/error
shapes do not fit the canonical error-payload form).
"""
import asyncio
import base64
import logging
import uuid
from pathlib import Path
from typing import Any

from aether_common.contracts import Event, EventType
from aether_engine.cost.ledger import reset_usage_context, set_usage_context
from aether_engine.memory import session_store
from aether_engine.routes.ws_sessions import _create_bound_session
from aether_engine.routing.policy import infer_task_requirements
from aether_engine.secrets.dpapi import SecretDecryptionError
from aether_engine.secrets.storage import ProviderNotFoundError
from aether_engine.state import engine_state

logger = logging.getLogger("aether_engine.routes.ws_voice")


def create_stt_engine():
    """Factory for local STT engine (E8-5.3 push-to-talk dictation).

    Model hub, H3: the engine is built with the configured speech
    STT model (``speech_stt_model``), read fresh at construction —
    the per-connection cache below keys on the same value, so a
    Settings change rebuilds on next use.
    """
    from aether_engine.voice.stt import FasterWhisperStt
    return FasterWhisperStt(model=engine_state.user_config.speech_stt_model)


def create_tts_engine():
    """Factory for local TTS engine (E8-5.3 assistant read-aloud)."""
    from aether_engine.voice.tts import KokoroTts
    return KokoroTts()



# ------------------------------------------------------------------
# RUX-B-09: the voice family, off the receive loop.
#
# Session start awaited opening the VAD/STT/TTS stages inline
# (first start loads the speech models); each dictation chunk /
# stop awaited that chunk's transcription inline. Session start
# now runs in a background task (serialised against other starts
# by a lock, since two starts would fight over voice_state).
# Dictation runs in tasks too, but CHAINED: each chunk's work
# waits for the previous chunk's task, so transcriptions are
# applied — and answered — in arrival order even though their
# durations vary.
# ------------------------------------------------------------------

async def send_voice_session_start_result(conn, req_id_, payload_):
    """Answer a VOICE_SESSION_START_REQUEST from a background task (RUX-B-09)."""
    from aether_engine.routes import websocket as _websocket

    async with conn.voice_start_lock:
        # Fail-closed start: an unavailable stage (missing
        # local STT/TTS/VAD/transport) or an unconfigured
        # chat model answers ok=False with the honest reason
        # and opens nothing — no device, no model, no peer.
        await stop_voice_session(conn)
        voice_session_id = payload_.get("session_id")
        if not voice_session_id:
            voice_session_id, _binding = await _create_bound_session(
                "Voice conversation")
        cfg = engine_state.user_config
        voice_error: str | None = None
        voice_pipeline = None
        webrtc_config = None
        try:
            voice_provider = _websocket._create_provider_instance(
                cfg.default_provider, cfg.default_model, conn.secret_store)
        except (ProviderNotFoundError, SecretDecryptionError) as e:
            voice_provider = None
            voice_error = (
                f"voice unavailable: chat model {cfg.default_provider}/"
                f"{cfg.default_model} is not configured ({e})")
        if voice_provider is not None:
            from aether_engine.voice import VoiceUnavailableError
            from aether_engine.voice.pipeline import (
                create_voice_pipeline,
                fast_chat_talker,
            )
            from aether_engine.voice.vad import EndpointerConfig

            async def _persist_voice_user(text: str, _sid: str = voice_session_id) -> None:
                try:
                    await asyncio.to_thread(
                        session_store.add_message,
                        _sid, "user", text)
                except Exception as e:  # noqa: BLE001 — best-effort persistence, as START_TASK: a save failure must not lose the turn
                    logger.error(f"Failed to save voice user message: {e}")

            async def _persist_voice_assistant(text: str, _sid: str = voice_session_id) -> None:
                try:
                    await asyncio.to_thread(
                        session_store.add_message,
                        _sid, "assistant", text)
                except Exception as e:  # noqa: BLE001 — best-effort persistence, as task completion: a save failure must not lose the turn
                    logger.error(f"Failed to save voice assistant message: {e}")

            async def _voice_turn_error(message: str) -> None:
                await emit_voice_event(conn, Event(
                    type=EventType.ERROR,
                    payload={"message": f"Voice reply failed: {message}"},
                ))

            async def _voice_should_escalate(text: str, _provider=voice_provider) -> bool:
                """E8-5.1b: the same E1-04 split the typed
                path uses — classify_intent decides; chat
                stays with the talker, work escalates to
                the supervisor graph."""
                try:
                    intent = await _websocket.classify_intent(text, provider=_provider)
                except Exception:  # a routing failure keeps the turn with the talker
                    logger.debug("voice intent classification failed", exc_info=True)
                    return False
                return not intent.is_chat

            async def _voice_escalation_launch(text: str, _sid: str = voice_session_id, _provider=voice_provider) -> None:
                """Launch (never await) the background job:
                the task lane is a separate execution, so
                the talker stays free while it runs."""
                task = asyncio.ensure_future(
                    run_voice_background_task(conn, text, _sid, _provider))
                background = conn.voice_state.setdefault("background_tasks", set())
                background.add(task)
                task.add_done_callback(background.discard)

            def _note_voice_affect(read, _conn=conn) -> None:
                """S11: record each turn's EQ read on the pipeline.

                Late-bound on purpose: the pipeline is created
                below with this talker inside it, so the lookup
                happens per turn, once voice_state holds it.
                Recording only — note_affect_read applies no
                prosody (the current TTS has none to apply).
                """
                pipeline = _conn.voice_state.get("pipeline")
                if pipeline is not None:
                    pipeline.note_affect_read(read)

            voice_pipeline, voice_error = create_voice_pipeline(
                session_id=voice_session_id,
                talker=fast_chat_talker(
                    voice_provider, session_id=voice_session_id,
                    on_affect_read=_note_voice_affect),
                emit=lambda event: emit_voice_event(conn, event),
                on_user_final=_persist_voice_user,
                on_assistant_final=_persist_voice_assistant,
                on_turn_error=_voice_turn_error,
                escalation_classifier=_voice_should_escalate,
                on_escalation=_voice_escalation_launch,
                # H3: the speech config is read HERE, at
                # session start — a mid-session change is
                # picked up by the NEXT session only (P3(a)).
                stt_model=cfg.speech_stt_model,
                tts_voice=cfg.speech_tts_voice,
                endpointer_config=EndpointerConfig(
                    speech_threshold=cfg.vad_threshold,
                    silence_duration_ms=cfg.silence_duration_ms,
                    smart_turn_sensitivity=cfg.smart_turn_sensitivity,
                ),
            )
            if voice_pipeline is not None:
                try:
                    webrtc_config = await voice_pipeline.start()
                except VoiceUnavailableError as e:
                    voice_error = str(e)
                    voice_pipeline = None
        if voice_pipeline is not None:
            conn.voice_state["pipeline"] = voice_pipeline
            conn.voice_state["task"] = asyncio.ensure_future(
                run_voice_pipeline(conn, voice_pipeline))

            # F-12a: the engine's own ICE candidates trickle
            # back to the renderer as VOICE_RTC_ICE pushes.
            async def _push_rtc_ice(candidate, _sid=voice_session_id):
                await emit_voice_event(conn, Event(
                    type=EventType.VOICE_RTC_ICE,
                    payload={
                        "session_id": _sid,
                        "candidate": candidate,
                    },
                ))

            voice_pipeline.set_rtc_ice_listener(_push_rtc_ice)
        await conn.send_event(
            req_id_, EventType.VOICE_SESSION_START_RESPONSE,
            {
                "ok": voice_pipeline is not None,
                "session_id": voice_session_id,
                "webrtc_config": webrtc_config,
                "error": voice_error,
            },
            "Could not send voice session start response")

async def send_dictation_chunk_result(conn, req_id_, reason_, raw_b64_, sample_rate_, is_final_):
    """Answer one DICTATION_CHUNK_REQUEST (RUX-B-09, chained by enqueue_dictation)."""
    if reason_ is not None:
        payload = {"ok": False, "text": "", "is_final": False, "error": f"voice unavailable: {reason_}"}
    else:
        try:
            audio_bytes = base64.b64decode(raw_b64_)
            stt = await open_dictation_stt(conn)
            text = await stt.transcribe(audio_bytes, sample_rate=sample_rate_)
            payload = {"ok": True, "text": text, "is_final": is_final_, "error": None}
        except Exception as e:  # noqa: BLE001
            payload = {"ok": False, "text": "", "is_final": is_final_, "error": str(e)}
    await conn.send_event(
        req_id_, EventType.DICTATION_CHUNK_RESPONSE, payload,
        "Could not send dictation chunk response")

async def send_dictation_stop_result(conn, req_id_, raw_b64_, sample_rate_):
    """Answer a DICTATION_STOP_REQUEST (RUX-B-09, chained by enqueue_dictation)."""
    text = ""
    stop_error: str | None = None
    if raw_b64_:
        try:
            stt = await open_dictation_stt(conn)
            audio_bytes = base64.b64decode(raw_b64_)
            text = await stt.transcribe(audio_bytes, sample_rate=sample_rate_)
        except Exception as e:  # noqa: BLE001 — S4-M7: a failed final transcription must be distinguishable from silence
            logger.warning(f"Dictation stop transcribe error: {e}")
            stop_error = str(e)
    await conn.send_event(
        req_id_, EventType.DICTATION_STOP_RESPONSE,
        {"ok": stop_error is None, "text": text if stop_error is None else "", "error": stop_error},
        "Could not send dictation stop response")

async def send_voice_session_stop_result(conn, req_id_):
    """Answer a VOICE_SESSION_STOP_REQUEST (RUX-B-09, chained by enqueue_voice_op)."""
    await stop_voice_session(conn)
    await conn.send_event(
        req_id_, EventType.VOICE_SESSION_STOP_RESPONSE, {"ok": True},
        "Could not send voice session stop response")

def enqueue_voice_op(conn, work_):
    """Chain one voice start/stop operation after all prior ones.

    Same shield-and-await construction as enqueue_dictation:
    operation order is arrival order, and a failed or cancelled
    operation never strands the ones behind it.
    """
    previous = conn.voice_op_tail

    async def _chained():
        if previous is not None:
            try:
                await asyncio.shield(previous)
            except asyncio.CancelledError:
                if not previous.cancelled():
                    raise
            except Exception as e:  # noqa: BLE001 — a failed earlier operation is answered by its own link; later operations still run
                logger.debug(f"Earlier voice operation ended with an error before a chained operation: {e}")
        await work_

    conn.voice_op_tail = asyncio.create_task(_chained())

def enqueue_dictation(conn, work_):
    """Chain one dictation work coroutine after all prior ones.

    The chain — not task scheduling — is what guarantees order:
    each link shields and awaits the previous link before its
    own transcription starts, so a fast chunk can never overtake
    a slow earlier one. A cancelled/failed link never strands
    the rest of the chain.
    """
    previous = conn.dictation_tail

    async def _chained():
        if previous is not None:
            try:
                await asyncio.shield(previous)
            except asyncio.CancelledError:
                if not previous.cancelled():
                    raise
            except Exception as e:  # noqa: BLE001 — a failed earlier chunk is answered by its own link; later chunks still run
                logger.debug(f"Earlier dictation chunk ended with an error before a chained chunk: {e}")
        await work_

    conn.dictation_tail = asyncio.create_task(_chained())


async def emit_voice_event(conn, event: Event) -> None:
    try:
        await conn.ws.send_text(event.to_json())
    except Exception:  # a dead socket must not kill the pipeline mid-turn
        logger.debug("voice event send failed", exc_info=True)

async def run_voice_pipeline(conn, pipeline) -> None:
    # Finding M19: the talker and the escalation classifier make
    # real model calls from this task — outside the task pump, so
    # no pump ever sets their attribution. Carry the voice
    # session's context for the whole run; escalated background
    # tasks set their own context in the pump as before.
    usage_token = set_usage_context(session_id=pipeline.session_id)
    try:
        await pipeline.run()
    except asyncio.CancelledError:
        raise
    except Exception:  # the session is over either way; never wedge the socket
        logger.warning("voice pipeline ended with an error", exc_info=True)
    finally:
        reset_usage_context(usage_token)

async def stop_voice_session(conn) -> None:
    """Stop the connection's voice pipeline, if any. Closing the
    pipeline's transport ends its frame loop; cancelling the run
    task is the backstop."""
    pipeline = conn.voice_state.get("pipeline")
    task = conn.voice_state.get("task")
    conn.voice_state["pipeline"] = None
    conn.voice_state["task"] = None
    if pipeline is not None:
        try:
            await pipeline.stop()
        except Exception:  # teardown is best-effort by definition
            logger.debug("voice pipeline stop failed", exc_info=True)
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

async def stop_read_aloud(conn) -> None:
    """Stop any running read-aloud synthesis task (Epic 8, E8-5.3)."""
    ra_task = conn.voice_state.get("read_aloud_task")
    conn.voice_state["read_aloud_task"] = None
    conn.voice_state["read_aloud_id"] = None
    if ra_task is not None and not ra_task.done():
        ra_task.cancel()
        try:
            await ra_task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug("read-aloud task cancel failed", exc_info=True)

# ── Cached speech engines (S4-M7) ────────────────────────────────
# One STT and one TTS engine per connection, closed only at
# connection teardown — NOT on DICTATION_STOP / read-aloud end,
# or push-to-talk would reload the model on every utterance.
# H3 exception: the dictation STT slot is keyed by the configured
# speech model — a model change (not a stop) swaps the engine.

def dictation_stt(conn) -> Any:
    """The connection's cached dictation STT engine.

    Model-aware (H3): the cache is keyed by the configured
    speech STT model. Same model → the same engine, no reload.
    A changed model builds the new model's engine on this next
    use and drops the old one (for FasterWhisperStt, dropping
    the reference IS the release — its close() only drops the
    loaded model). The pre-H3 cache never re-read config, so a
    Settings change silently left dictation on the old model
    until the socket reconnected: a placebo control.
    """
    from aether_engine.routes import websocket as _websocket

    model = engine_state.user_config.speech_stt_model
    engine = conn.voice_state.get("dictation_stt")
    if engine is None or conn.voice_state.get("dictation_stt_model") != model:
        engine = _websocket.create_stt_engine()
        conn.voice_state["dictation_stt"] = engine
        conn.voice_state["dictation_stt_model"] = model
        conn.voice_state["dictation_stt_opened"] = False
    return engine

async def open_dictation_stt(conn) -> Any:
    """The cached STT engine, opened once. An open failure is
    logged, not raised — the transcribe call that follows reports
    the real error to the client (today's per-chunk tolerance),
    and a later chunk may retry the open."""
    engine = dictation_stt(conn)
    if not conn.voice_state.get("dictation_stt_opened"):
        if hasattr(engine, "open"):
            try:
                await engine.open()
                conn.voice_state["dictation_stt_opened"] = True
            except Exception:
                logger.debug("STT open error", exc_info=True)
        else:
            conn.voice_state["dictation_stt_opened"] = True
    return engine

def read_aloud_tts(conn) -> Any:
    """The connection's cached read-aloud TTS engine, built once."""
    from aether_engine.routes import websocket as _websocket

    engine = conn.voice_state.get("read_aloud_tts")
    if engine is None:
        engine = _websocket.create_tts_engine()
        conn.voice_state["read_aloud_tts"] = engine
    return engine

async def open_read_aloud_tts(conn) -> Any:
    """The cached TTS engine, opened once. Same failure contract
    as ``open_dictation_stt``: logged, not raised."""
    engine = read_aloud_tts(conn)
    if not conn.voice_state.get("read_aloud_tts_opened"):
        if hasattr(engine, "open"):
            try:
                await engine.open()
                conn.voice_state["read_aloud_tts_opened"] = True
            except Exception:
                logger.debug("TTS open error", exc_info=True)
        else:
            conn.voice_state["read_aloud_tts_opened"] = True
    return engine

async def close_speech_engines(conn) -> None:
    """Close both cached speech engines (connection teardown).
    Guarded and best-effort: a failing close never wedges the
    disconnect path, and the slots are cleared regardless."""
    conn.voice_state["dictation_stt_model"] = None
    for engine_key, opened_key in (
        ("dictation_stt", "dictation_stt_opened"),
        ("read_aloud_tts", "read_aloud_tts_opened"),
    ):
        engine = conn.voice_state.get(engine_key)
        conn.voice_state[engine_key] = None
        conn.voice_state[opened_key] = False
        if engine is None:
            continue
        try:
            if hasattr(engine, "close"):
                await engine.close()
        except Exception:
            logger.debug("speech engine close failed", exc_info=True)

async def run_voice_background_task(conn, text: str, session_id: str, provider) -> None:
    """One voice-escalated job (Epic 8, E8-5.1b): a normal
    supervisor task, built exactly like a typed work message
    (same central registry builder, same execute_task_generator
    pump), but running FREE in the background — the talker lane
    never waits on it. Its events stream to the socket, so the
    task shows in chat with every detail exactly as tasks show
    today; an approval pauses it for the normal in-chat
    approval (the resume path is keyed by thread, not by this
    pump — and nothing in the voice path can approve). When it
    lands, the final summary is spoken through the live voice
    pipeline; if voice mode has ended by then, the summary
    simply stands in chat as the task's own message (the pump
    already persisted it).

    N10: the pause itself is announced too — a task waiting on
    the owner's approval says so, factually, and points at the
    chat where the decision belongs. Every announcement carries
    a stable identity derived from the child task + event kind,
    and a completion announcement also carries the persisted
    message's own id, so the desktop renders the spoken text as
    that message rather than as a second assistant turn."""
    from aether_engine.routes import websocket as _websocket

    cfg = engine_state.user_config
    req_id = f"voice-task-{uuid.uuid4().hex[:12]}"
    terminal: dict[str, Any] = {}
    # N10: announcement bookkeeping for this job. Identities are
    # derived from the child task's own ids, so the same pause
    # or result always maps to the same announcement — an event
    # the stream re-reports never announces twice.
    announced: set[str] = set()
    announce_tasks: set[asyncio.Task] = set()
    identity: dict[str, Any] = {}

    def _announcement_id(kind: str, *parts: Any) -> str:
        base = identity.get("task_id") or req_id
        return ":".join(
            ["voice-ann", str(base), kind]
            + [str(p) for p in parts if p])

    async def _announce(
        announcement_id: str, kind: str, text: str, *,
        message_id: str | None = None,
    ) -> None:
        if announcement_id in announced:
            return
        announced.add(announcement_id)
        pipeline = conn.voice_state.get("pipeline")
        if pipeline is None:
            return
        await pipeline.speak_announcement(
            text, announcement_id=announcement_id, kind=kind,
            message_id=message_id)

    def _schedule_announce(
        announcement_id: str, kind: str, text: str,
    ) -> None:
        # A mid-stream announcement (an approval pause) must
        # not block the pump — the approval card has to reach
        # chat promptly. The pipeline speaks at the next quiet
        # moment on its own.
        task = asyncio.ensure_future(
            _announce(announcement_id, kind, text))
        announce_tasks.add(task)
        task.add_done_callback(announce_tasks.discard)

    async def _task_message_id(response: str) -> str | None:
        """The id of the message the pump persisted for this
        job's result: the newest assistant message in the
        session with exactly this content. Best-effort — the
        announcement stays identity-tagged without it."""
        try:
            session = await asyncio.to_thread(
                session_store.get_session, session_id)
        except Exception:  # noqa: BLE001 — a lookup failure must not break the announcement
            return None
        for message in reversed((session or {}).get("messages") or []):
            if (message.get("role") == "assistant"
                    and message.get("content") == response):
                return message.get("id")
        return None

    try:
        from aether_engine.mcp.registry import GLOBAL_MCP_REGISTRY

        reqs = infer_task_requirements(text)
        tools = _websocket.build_tool_registry(
            mcp_tools=GLOBAL_MCP_REGISTRY.get_mcp_tools(
                risk_overrides=cfg.mcp_tool_risk_overrides),
            include_file_tools=reqs.wants_code,
        )
        generator = _websocket.run_langgraph_task(
            prompt=text,
            provider=provider,
            tools=tools,
            request_id=req_id,
            approval_handler=None,
            workspace_roots=cfg.workspace_roots or [str(Path.home() / "AetherWorkspace")],
            disabled_nodes=engine_state.disabled_nodes,
            session_id=session_id,
            agent_models=cfg.agent_models,
            agent_tools=cfg.agent_tools,
            secret_store=conn.secret_store,
            user_config=cfg,
            forced_agent=None,
            active_skills=[],
            plan_mode=False,
            attachments=None,
        )

        async def _observed():
            async for ev in generator:
                payload = (
                    ev.payload if isinstance(ev.payload, dict)
                    else {})
                if payload.get("task_id"):
                    identity["task_id"] = payload["task_id"]
                if ev.type == EventType.TASK_COMPLETED:
                    terminal["response"] = payload.get("response")
                elif ev.type == EventType.TASK_FAILED:
                    terminal["error"] = (
                        payload.get("error") or "unknown error")
                elif ev.type == EventType.TOOL_APPROVAL_REQUEST:
                    # N10: the task is paused on the owner.
                    # Announce it — factually, and pointing at
                    # the chat, where the decision belongs:
                    # nothing in the voice path can approve.
                    tool_name = payload.get("tool_name")
                    if tool_name:
                        notice = (
                            "That background task is paused, "
                            "waiting for your approval to use "
                            f"{tool_name}. You can review and "
                            "decide in the chat.")
                    else:
                        notice = (
                            "That background task is paused, "
                            "waiting for your approval. You can "
                            "review and decide in the chat.")
                    _schedule_announce(
                        _announcement_id(
                            "approval", payload.get("approval_key"),
                            tool_name),
                        "approval", notice)
                yield ev

        await conn.execute_task_generator(
            _observed(), cfg.default_provider, cfg.default_model,
            req_id, session_id=session_id,
            routing_reason="Voice escalation: spoken request classified as work",
        )
    except asyncio.CancelledError:
        for task in announce_tasks:
            task.cancel()
        raise
    except Exception:  # a background job must never wedge the socket
        logger.warning("voice background task failed to run", exc_info=True)
        terminal.setdefault("error", "the background task could not be started")
    pipeline = conn.voice_state.get("pipeline")
    if pipeline is None:
        return
    if terminal.get("response"):
        await _announce(
            _announcement_id("completed"), "completed",
            str(terminal["response"]),
            message_id=await _task_message_id(
                str(terminal["response"])))
    elif terminal.get("error"):
        await _announce(
            _announcement_id("failed"), "failed",
            f"That background task failed: {terminal['error']}")

async def cancel_voice_background_tasks(conn) -> None:
    """Disconnect teardown for escalated jobs: their event
    stream dies with the socket, so they stop with it. (Voice
    STOP alone does NOT cancel them — they run free; only
    their spoken summary is skipped.)"""
    tasks = conn.voice_state.get("background_tasks") or set()
    conn.voice_state["background_tasks"] = set()
    for task in tasks:
        if not task.done():
            task.cancel()
