"""Epic 8, N09: voice heard-word accounting measures PLAYBACK,
not emission.

The E8-5.1b accounting credited words as the engine EMITTED audio
chunks — but the desktop queues playback independently (Web Audio
play head in VoiceOverlay), so at a barge-in the emitted count can
run far ahead of what the user actually heard, and history ended
up claiming unheard words. It also credited a sentence whose
synthesis produced no audio at all, and VOICE_BARGE_IN could wait
~4 s (the interrupt settle poll) before being published while the
desktop kept playing the tail.

N09's shape:

  * every audio chunk carries a session-monotonic ``chunk_seq``;
  * the renderer reports playback progress (VOICE_PLAYBACK_PROGRESS:
    the highest chunk_seq it has FINISHED playing; -1 = reporting
    live, nothing finished yet) over the same socket — fire and
    forget, never a round-trip on the live path;
  * once a session's renderer reports progress, the heard text is
    derived from those reports only: fully-played sentences plus
    the chunk-proportional word prefix of the first partially
    played one; emitted-but-unplayed sentences contribute nothing;
  * the barge-in event is published from that accounting at once
    (frozen at the cut), without waiting for the output task to
    settle;
  * a client that never reports progress keeps the pre-N09
    emission-based estimate (the harness behaviour the E8-5.1b
    suites pin);
  * a silent synthesis (zero audio chunks) is never credited as
    heard on the interrupted path.

All stages are scripted fakes, as in test_voice_pipeline /
test_voice_barge_in: no devices, no models, no network.
"""
import asyncio
import base64
from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest
from starlette.testclient import TestClient
from test_voice_barge_in import (
    ChunkDelayEmitter,
    QueueTransport,
    ScriptedTalker,
    ThreeChunkTts,
    audio_chunks,
    start_pipeline,
    stop_pipeline,
    wait_until,
)
from test_voice_pipeline import (
    Emitter,
    FakeStt,
    FakeTransport,
    FakeTts,
    FakeVad,
    frame,
    make_pipeline,
    speech_then_silence,
)

from aether_common.contracts import Event, EventType
from aether_engine.app import app, engine_state
from aether_engine.memory import session_store
from aether_engine.routes import websocket as ws_module
from aether_engine.voice.pipeline import VoicePipeline

# ---------------------------------------------------------------------------
# Extra fakes
# ---------------------------------------------------------------------------

class GatedSecondSentenceTts(FakeTts):
    """Sentence 1 synthesizes normally; sentence 2 yields one
    chunk, then blocks on a gate — the output task is stuck
    mid-synthesis until the test releases it."""

    def __init__(self):
        super().__init__()
        self.gate = asyncio.Event()
        self.second_blocked = asyncio.Event()

    async def synthesize(self, text, *, voice):
        self.requests.append((text, voice))
        yield b"AUD1:" + text.encode()
        if len(self.requests) == 2:
            self.second_blocked.set()
            await self.gate.wait()
        yield b"AUD2:" + text.encode()


class SilentFirstTts(FakeTts):
    """The first synthesize() call yields NO audio (a silent
    synthesis); later calls yield the usual two chunks."""

    async def synthesize(self, text, *, voice):
        self.requests.append((text, voice))
        if len(self.requests) == 1:
            return
            yield  # pragma: no cover — makes this an async generator
        yield b"AUD1:" + text.encode()
        yield b"AUD2:" + text.encode()


def barge_events(emitter):
    return emitter.of_type(EventType.VOICE_BARGE_IN)


# ---------------------------------------------------------------------------
# Chunk identity
# ---------------------------------------------------------------------------

class TestChunkIdentity:
    async def test_audio_chunks_carry_session_monotonic_chunk_seq(self):
        transport = QueueTransport()
        emitter = Emitter()
        talker = ScriptedTalker([
            (["One two three four. ", "Five six seven eight."], None),
        ])
        committed = []

        async def on_assistant_final(text):
            committed.append(text)

        pipeline = make_pipeline(
            transport, stt=FakeStt(["go", "go", "go"]), talker=talker,
            emitter=emitter, on_assistant_final=on_assistant_final)
        run_task = await start_pipeline(pipeline)
        try:
            transport.push_frames(speech_then_silence())
            await wait_until(lambda: len(audio_chunks(emitter)) >= 4)
            await wait_until(lambda: len(committed) >= 1)
            seqs = [c.payload["chunk_seq"] for c in audio_chunks(emitter)]
            assert seqs == [0, 1, 2, 3]
            assert committed == ["One two three four. Five six seven eight."]
        finally:
            await stop_pipeline(pipeline, run_task)


# ---------------------------------------------------------------------------
# Playback-acknowledged accounting
# ---------------------------------------------------------------------------

class TestPlaybackAccounting:
    async def test_barge_in_credits_only_played_words(self):
        """THE N09 REPRO: every chunk of the sentence is EMITTED
        before the barge-in lands, but the player has only FINISHED
        the first chunk. Emission-based accounting credits all six
        words; playback-based accounting must credit two."""
        transport = QueueTransport()
        emitter = ChunkDelayEmitter(delay_after_audio_count=3)
        stt = FakeStt(["go", "go", "go"])
        talker = ScriptedTalker([
            (["Alpha beta gamma delta epsilon zeta. ",
              "More words follow here."], None),
        ])
        committed = []

        async def on_assistant_final(text):
            committed.append(text)

        pipeline = VoicePipeline(
            session_id="sess-1",
            vad=FakeVad({1: 0.9}),
            stt=stt,
            tts=ThreeChunkTts(),
            transport=transport,
            talker=talker,
            emit=emitter,
            on_assistant_final=on_assistant_final,
        )
        run_task = await start_pipeline(pipeline)
        try:
            # The renderer comes up reporting playback progress:
            # nothing finished yet.
            pipeline.note_playback_progress(-1)
            transport.push_frames(speech_then_silence())
            await wait_until(lambda: len(audio_chunks(emitter)) >= 3)
            # All 3 chunks are emitted; the player finished only
            # the first one.
            pipeline.note_playback_progress(0)
            transport.push_frames([frame(1)] * 10)
            await wait_until(lambda: len(barge_events(emitter)) >= 1)

            barge = barge_events(emitter)[0]
            # 1 of 3 chunks played of a 6-word sentence -> 2 words.
            assert barge.payload["committed_text"] == "Alpha beta "
            assert barge.payload["stopped_at_char"] == len("Alpha beta ")
            # The event is published at the cut; the history commit
            # lands as the output loop settles, with the same text.
            await wait_until(lambda: len(committed) >= 1)
            assert committed == ["Alpha beta"]
            # The mismatch this pins: all three chunks WERE emitted.
            assert len(audio_chunks(emitter)) == 3
        finally:
            await stop_pipeline(pipeline, run_task)

    async def test_barge_in_before_any_chunk_played_credits_nothing(self):
        transport = QueueTransport()
        emitter = ChunkDelayEmitter(delay_after_audio_count=1)
        stt = FakeStt([
            "the red", "the red bicycle", "the red bicycle",
            "never mind", "never mind", "never mind",
        ])
        talker = ScriptedTalker([
            (["Alpha beta gamma delta. ", "More words follow here."], None),
            (["Okay, dropped it."], None),
        ])
        committed = []

        async def on_assistant_final(text):
            committed.append(text)

        pipeline = VoicePipeline(
            session_id="sess-1",
            vad=FakeVad({1: 0.9}),
            stt=stt,
            tts=ThreeChunkTts(),
            transport=transport,
            talker=talker,
            emit=emitter,
            on_assistant_final=on_assistant_final,
        )
        run_task = await start_pipeline(pipeline)
        try:
            pipeline.note_playback_progress(-1)
            transport.push_frames(speech_then_silence())
            await wait_until(lambda: len(audio_chunks(emitter)) >= 1)
            # The user talks over the very first chunk: nothing has
            # finished playing, so nothing is credited as heard.
            transport.push_frames([frame(1)] * 10)
            await wait_until(lambda: len(barge_events(emitter)) >= 1)
            barge = barge_events(emitter)[0]
            assert barge.payload["committed_text"] == ""
            assert barge.payload["stopped_at_char"] == 0

            # The interrupting utterance becomes the next turn, and
            # it is the ONLY assistant message history gains.
            transport.push_frames([frame(0)] * 30)
            await wait_until(lambda: len(committed) >= 1)
            assert committed == ["Okay, dropped it."]
        finally:
            await stop_pipeline(pipeline, run_task)

    async def test_played_prefix_spans_sentences_and_skips_unplayed_tail(self):
        transport = QueueTransport()
        emitter = Emitter()
        stt = FakeStt(["go", "go", "go"])
        talker = ScriptedTalker([
            (["One two three four. ", "Five six seven eight. ",
              "Nine ten eleven twelve. ", "Trailing words here."], 3),
        ])
        committed = []

        async def on_assistant_final(text):
            committed.append(text)

        pipeline = make_pipeline(
            transport, stt=stt, talker=talker, emitter=emitter,
            on_assistant_final=on_assistant_final)
        run_task = await start_pipeline(pipeline)
        try:
            pipeline.note_playback_progress(-1)
            transport.push_frames(speech_then_silence())
            # 3 sentences x 2 chunks = seqs 0..5, all emitted; the
            # talker then gates, so the turn is still live.
            await wait_until(lambda: len(audio_chunks(emitter)) >= 6)
            # Played: sentence 1 in full (seqs 0-1) + sentence 2's
            # first chunk (seq 2). Sentence 3 is emitted, unplayed.
            pipeline.note_playback_progress(2)
            transport.push_frames([frame(1)] * 10)
            await wait_until(lambda: len(barge_events(emitter)) >= 1)

            barge = barge_events(emitter)[0]
            assert barge.payload["committed_text"] == (
                "One two three four. Five six ")
            # Release the gated delta; the interrupted turn commits
            # exactly the published text.
            talker.gates[0].set()
            await wait_until(lambda: len(committed) >= 1)
            assert committed == ["One two three four. Five six"]
        finally:
            await stop_pipeline(pipeline, run_task)

    async def test_uninterrupted_reply_commits_full_text_in_progress_mode(self):
        transport = QueueTransport()
        emitter = Emitter()
        talker = ScriptedTalker([
            (["One two three four. ", "Five six seven eight."], None),
        ])
        committed = []

        async def on_assistant_final(text):
            committed.append(text)

        pipeline = make_pipeline(
            transport, stt=FakeStt(["go", "go", "go"]), talker=talker,
            emitter=emitter, on_assistant_final=on_assistant_final)
        run_task = await start_pipeline(pipeline)
        try:
            pipeline.note_playback_progress(-1)
            transport.push_frames(speech_then_silence())
            await wait_until(lambda: len(audio_chunks(emitter)) >= 1)
            # Playback is still near the start when the reply ends:
            # with no interruption, the full reply stands.
            pipeline.note_playback_progress(0)
            await wait_until(lambda: len(committed) >= 1)
            assert committed == ["One two three four. Five six seven eight."]
        finally:
            await stop_pipeline(pipeline, run_task)


# ---------------------------------------------------------------------------
# Cut immediacy: the event must not wait for the output task
# ---------------------------------------------------------------------------

class TestCutImmediacy:
    async def test_barge_in_published_while_output_task_is_stuck(self):
        transport = QueueTransport()
        emitter = Emitter()
        tts = GatedSecondSentenceTts()
        talker = ScriptedTalker([
            (["One two three four. ", "Five six seven eight."], None),
        ])
        committed = []

        async def on_assistant_final(text):
            committed.append(text)

        pipeline = make_pipeline(
            transport, stt=FakeStt(["go", "go", "go"]), tts=tts,
            talker=talker, emitter=emitter,
            on_assistant_final=on_assistant_final)
        run_task = await start_pipeline(pipeline)
        try:
            pipeline.note_playback_progress(-1)
            transport.push_frames(speech_then_silence())
            # Sentence 1 emitted (seqs 0-1) and fully played;
            # sentence 2's synthesis is blocked on the gate, so the
            # output task cannot settle.
            await wait_until(lambda: tts.second_blocked.is_set())
            assert len(audio_chunks(emitter)) == 2
            pipeline.note_playback_progress(1)

            interrupt_task = asyncio.ensure_future(
                pipeline.interrupt_output())
            # The cut must be published NOW — the pre-N09 code polled
            # up to ~4 s for the output task to settle first, while
            # the desktop kept playing the unheard tail.
            await wait_until(
                lambda: len(barge_events(emitter)) >= 1, timeout=1.0)
            barge = barge_events(emitter)[0]
            assert barge.payload["committed_text"] == "One two three four. "

            tts.gate.set()
            result = await asyncio.wait_for(interrupt_task, timeout=5.0)
            assert result["committed_text"] == "One two three four. "
            assert result["stopped_at_char"] == len("One two three four. ")
            await wait_until(lambda: len(committed) >= 1)
            assert committed == ["One two three four."]
        finally:
            tts.gate.set()
            await stop_pipeline(pipeline, run_task)


# ---------------------------------------------------------------------------
# Silent synthesis
# ---------------------------------------------------------------------------

class TestSilentSynthesis:
    async def test_silent_sentence_is_not_credited_as_heard(self):
        transport = QueueTransport()
        # Sentence 1 synthesizes silently (no audio chunks);
        # sentence 2's first chunk pauses in the emit window.
        emitter = ChunkDelayEmitter(delay_after_audio_count=1)
        stt = FakeStt(["go"] * 6)
        talker = ScriptedTalker([
            (["Zero audio sentence here. ",
              "Audible words follow now."], None),
        ])
        committed = []

        async def on_assistant_final(text):
            committed.append(text)

        pipeline = make_pipeline(
            transport, stt=stt, tts=SilentFirstTts(), talker=talker,
            emitter=emitter, on_assistant_final=on_assistant_final)
        run_task = await start_pipeline(pipeline)
        try:
            transport.push_frames(speech_then_silence())
            await wait_until(lambda: len(audio_chunks(emitter)) >= 1)
            transport.push_frames([frame(1)] * 10)
            await wait_until(lambda: len(barge_events(emitter)) >= 1)

            barge = barge_events(emitter)[0]
            # Only the audible sentence's heard prefix (1 of its 2
            # chunks emitted -> 2 of its 4 words). The silent
            # sentence was never heard; the pre-N09 code credited
            # it in full.
            assert barge.payload["committed_text"] == "Audible words "
            await wait_until(lambda: len(committed) >= 1)
            assert committed == ["Audible words"]
            spoken = b"".join(
                base64.b64decode(c.payload["audio_chunk"])
                for c in audio_chunks(emitter))
            assert b"Zero audio" not in spoken
        finally:
            await stop_pipeline(pipeline, run_task)


# ---------------------------------------------------------------------------
# The websocket route: progress reports reach the live pipeline
# ---------------------------------------------------------------------------

@pytest.fixture
def ws_env(tmp_path, monkeypatch):
    """Engine state + stores isolated to tmp (mirrors
    test_voice_pipeline's fixture, incl. the N12 save/restore)."""
    saved_token_path = engine_state.token_path
    saved_log_path = engine_state.audit_logger.log_path
    saved_user_config = engine_state.user_config
    saved_auth_token = engine_state.auth_token
    engine_state.token_path = tmp_path / "engine.token"
    engine_state.audit_logger.log_path = tmp_path / "audit.log"
    engine_state.initialize_auth()
    monkeypatch.setattr(session_store, "DB_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(session_store, "_initialized", False)
    monkeypatch.setattr(
        ws_module, "get_pending_approvals", AsyncMock(return_value=[]))
    try:
        yield {"tmp_path": tmp_path}
    finally:
        engine_state.token_path = saved_token_path
        engine_state.audit_logger.log_path = saved_log_path
        engine_state.user_config = saved_user_config
        engine_state.auth_token = saved_auth_token


def _receive_until(ws, wanted, max_events=40):
    events = []
    for _ in range(max_events):
        ev = Event.from_json(ws.receive_text())
        events.append(ev)
        if ev.type == wanted:
            return events
    raise AssertionError(
        f"{wanted} not received; got {[ev.type for ev in events]}")


class TestPlaybackProgressRoute:
    def test_progress_event_reaches_live_pipeline(self, ws_env):
        from aether_engine.routing.intent import IntentDecision
        from aether_engine.voice.pipeline import create_voice_pipeline

        transport = FakeTransport(())
        created = []
        recorded = []
        real_factory = create_voice_pipeline

        def factory(**kwargs):
            pipeline, error = real_factory(
                vad=FakeVad({1: 0.9}),
                stt=FakeStt(["unused"]),
                tts=FakeTts(),
                transport=transport,
                **kwargs,
            )
            assert error is None
            original = getattr(pipeline, "note_playback_progress", None)
            if original is not None:
                pipeline.note_playback_progress = (
                    lambda seq: (recorded.append(seq), original(seq)))
            created.append(pipeline)
            return pipeline, None

        async def _chat_verdict(prompt, provider=None):
            return IntentDecision(
                route="chat", reason="test verdict", source="model")

        sid = session_store.create_session("Voice chat")
        with ExitStack() as stack:
            mock_store = stack.enter_context(
                patch.object(ws_module, "SecretStore")).return_value
            mock_store.list_providers.return_value = ["google_gemini"]
            stack.enter_context(patch.object(
                ws_module, "_create_provider_instance",
                return_value=object()))
            stack.enter_context(patch(
                "aether_engine.voice.pipeline.create_voice_pipeline",
                side_effect=factory))
            stack.enter_context(patch.object(
                ws_module, "classify_intent", side_effect=_chat_verdict))
            with TestClient(app) as client, client.websocket_connect(
                    "/ws/tasks",
                    headers={
                        "Authorization": f"Bearer {engine_state.auth_token}",
                    }) as ws:
                ws.receive_text()  # HELLO
                ws.send_text(Event(
                    type=EventType.VOICE_SESSION_START_REQUEST,
                    payload={"session_id": sid},
                ).to_json())
                events = _receive_until(
                    ws, EventType.VOICE_SESSION_START_RESPONSE)
                assert events[-1].payload["ok"] is True
                assert created, "factory never built a pipeline"

                # A well-formed report lands on the live pipeline...
                ws.send_text(Event(
                    type=EventType.VOICE_PLAYBACK_PROGRESS,
                    payload={"session_id": sid, "played_seq": 3},
                ).to_json())
                # ...a malformed one is ignored, never fatal...
                ws.send_text(Event(
                    type=EventType.VOICE_PLAYBACK_PROGRESS,
                    payload={"session_id": sid, "played_seq": "abc"},
                ).to_json())
                # ...and the socket is still healthy afterwards
                # (messages are handled in order on one connection,
                # so the stop answer proves both were processed).
                ws.send_text(Event(
                    type=EventType.VOICE_SESSION_STOP_REQUEST,
                    payload={"session_id": sid},
                ).to_json())
                events = _receive_until(
                    ws, EventType.VOICE_SESSION_STOP_RESPONSE)
                assert events[-1].payload == {"ok": True}

        # Both events were routed to the live pipeline's method;
        # the malformed value was accepted by the route and then
        # ignored by the method's own validation — the accounting
        # position reflects only the well-formed report.
        assert recorded == [3, "abc"]
        assert created[0]._played_seq == 3
        assert transport.closed
