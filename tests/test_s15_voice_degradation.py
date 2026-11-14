"""Master build S15: voice cascading degradation + latency step-down.

The cascade used to be binary (audit R14): any unavailable
stage refused the whole session, and a slow stage just made
every turn slow. These suites pin the ladder that replaced it:

* the pure planner (degradation.plan_voice_session) over matrix
  fixtures x stage availability — full / degraded / text_only /
  unavailable, with the refusal still naming every missing
  stage, and the S10 matrix gate refusing up front when stages
  are individually fine;
* the persisted per-stage latency store (the measurement the
  S10 voice verdict said S15 would supply);
* the session tracker: latency window -> exactly the announced
  step-downs, no oscillation (the chain only walks down),
  hysteresis at the smallest model, the consecutive-failure
  ladder (drop -> swap -> end), TTS failure -> text-only once;
* the energy VAD fallback's probability shape;
* the factory + pipeline integration: text-only sessions speak
  nothing but still converse, energy endpointing runs a whole
  session, the STT load-failure retry adopts a smaller model,
  a mid-session step-down swaps engines, a TTS death flips the
  session text-only and stops synthesis attempts, repeated STT
  failure ends the session with the stage named;
* the WS start response carrying the profile + notices.

All stages are the scripted fakes from test_voice_pipeline —
no audio devices, no models, no network.
"""
import asyncio
import struct

import pytest
from test_voice_pipeline import (
    Emitter,
    FakeStt,
    FakeTalker,
    FakeTransport,
    FakeTts,
    FakeVad,
    run_until,
    speech_then_silence,
)

from aether_common.contracts import EventType
from aether_engine.voice import VoiceUnavailableError
from aether_engine.voice import degradation as degradation_module
from aether_engine.voice.degradation import (
    SessionDegradation,
    VoiceLatencyStore,
    VoicePlan,
    next_smaller_stt_model,
    plan_voice_session,
    record_voice_stage_sample,
    stt_fallback_chain,
)
from aether_engine.voice.pipeline import VoicePipeline, create_voice_pipeline
from aether_engine.voice.vad import EnergyVad

ALL_OK = {"vad": None, "stt": None, "tts": None, "transport": None}


def matrix(status: str, reason: str = "matrix reason") -> dict:
    return {
        "features": [
            {"id": "local_voice", "status": status, "reason": reason},
            {"id": "local_models", "status": "available", "reason": "n/a"},
        ]
    }


def plan(model: str = "small", **overrides) -> VoicePlan:
    base = {"profile": "full", "stt_model": model}
    base.update(overrides)
    return VoicePlan(**base)


# ---------------------------------------------------------------------------
# The model ladder
# ---------------------------------------------------------------------------

class TestModelLadder:
    def test_chain_stays_inside_the_family(self):
        assert next_smaller_stt_model("large-v3") == "medium"
        assert next_smaller_stt_model("medium") == "small"
        assert next_smaller_stt_model("small") == "base"
        assert next_smaller_stt_model("base") == "tiny"
        assert next_smaller_stt_model("small.en") == "base.en"
        assert next_smaller_stt_model("distil-large-v3") == "distil-medium.en"
        assert next_smaller_stt_model("distil-small.en") == "base.en"

    def test_smallest_and_unknown_have_no_step(self):
        assert next_smaller_stt_model("tiny") is None
        assert next_smaller_stt_model("tiny.en") is None
        # An HF repo id is not on the ladder: no guessed step.
        assert next_smaller_stt_model("someone/faster-whisper-custom") is None
        assert next_smaller_stt_model(None) is None

    def test_fallback_chain_walks_to_the_bottom(self):
        assert stt_fallback_chain("medium") == ["small", "base", "tiny"]
        assert stt_fallback_chain("tiny") == []


# ---------------------------------------------------------------------------
# The planner (pure)
# ---------------------------------------------------------------------------

class TestPlanner:
    def test_all_stages_ok_is_full_and_silent(self):
        result = plan_voice_session(
            stage_reasons=ALL_OK, configured_stt_model="small",
            matrix_voice={"status": "available", "reason": "gpu"})
        assert result.profile == "full"
        assert result.tts_enabled is True
        assert result.vad_fallback is False
        assert result.notices == ()
        assert result.unavailable_reason is None

    def test_missing_tts_is_text_only_with_a_notice(self):
        result = plan_voice_session(
            stage_reasons={**ALL_OK, "tts": "kokoro is not installed (local TTS)"},
            configured_stt_model="small")
        assert result.profile == "text_only"
        assert result.tts_enabled is False
        assert result.unavailable_reason is None
        assert any("text only" in n for n in result.notices)
        assert any("kokoro is not installed" in n for n in result.notices)

    def test_missing_vad_is_energy_fallback_when_replaceable(self):
        result = plan_voice_session(
            stage_reasons={**ALL_OK, "vad": "onnxruntime is not installed"},
            configured_stt_model="small", vad_replaceable=True)
        assert result.profile == "degraded"
        assert result.vad_fallback is True
        assert any("reduced" in n for n in result.notices)

    def test_missing_vad_refuses_when_not_replaceable(self):
        result = plan_voice_session(
            stage_reasons={**ALL_OK, "vad": "onnxruntime is not installed"},
            configured_stt_model="small", vad_replaceable=False)
        assert result.profile == "unavailable"
        assert "onnxruntime is not installed" in result.unavailable_reason

    def test_missing_stt_refuses_and_names_every_missing_stage(self):
        result = plan_voice_session(
            stage_reasons={
                **ALL_OK,
                "stt": "faster-whisper is not installed (local STT)",
                "tts": "kokoro is not installed (local TTS)",
            },
            configured_stt_model="small")
        assert result.profile == "unavailable"
        # The pre-ladder contract: a refusal names EVERY missing
        # stage, not just the fatal one.
        assert result.unavailable_reason.startswith("voice unavailable:")
        assert "faster-whisper is not installed" in result.unavailable_reason
        assert "kokoro is not installed" in result.unavailable_reason

    def test_missing_transport_refuses(self):
        result = plan_voice_session(
            stage_reasons={**ALL_OK, "transport": "aiortc is not installed"},
            configured_stt_model="small")
        assert result.profile == "unavailable"
        assert "aiortc is not installed" in result.unavailable_reason

    def test_matrix_unavailable_refuses_up_front(self):
        result = plan_voice_session(
            stage_reasons=ALL_OK, configured_stt_model="small",
            matrix_voice={
                "status": "unavailable",
                "reason": "This machine measured 1 GB of RAM — under "
                          "the 2 GB the local speech models need.",
            })
        assert result.profile == "unavailable"
        assert result.unavailable_reason.startswith("voice unavailable:")
        assert "1 GB of RAM" in result.unavailable_reason

    def test_matrix_degraded_alone_does_not_degrade_the_plan(self):
        # The matrix's CPU-speech "degraded" is generic advice;
        # the ladder's rungs are decided by stage facts and
        # measured latency, not by the generic verdict.
        result = plan_voice_session(
            stage_reasons=ALL_OK, configured_stt_model="small",
            matrix_voice={"status": "degraded", "reason": "cpu only"})
        assert result.profile == "full"

    def test_measured_slow_model_warns_and_names_the_step(self):
        result = plan_voice_session(
            stage_reasons=ALL_OK, configured_stt_model="small",
            stt_p95_by_model={"small": 5556.0})
        assert result.profile == "degraded"
        # Starts on the configured model (the user's Settings
        # choice wins at start); the step-down is the warning +
        # the in-session automation.
        assert result.stt_model == "small"
        notice = result.notices[-1]
        assert "5.6" in notice and "'base'" in notice

    def test_measured_slow_at_the_smallest_model_warns_without_a_step(self):
        result = plan_voice_session(
            stage_reasons=ALL_OK, configured_stt_model="tiny",
            stt_p95_by_model={"tiny": 4000.0})
        assert result.profile == "degraded"
        assert any("no smaller" in n for n in result.notices)

    def test_measured_fast_model_is_full(self):
        result = plan_voice_session(
            stage_reasons=ALL_OK, configured_stt_model="small",
            stt_p95_by_model={"small": 300.0})
        assert result.profile == "full"
        assert result.notices == ()


# ---------------------------------------------------------------------------
# The latency store
# ---------------------------------------------------------------------------

class TestLatencyStore:
    def test_empty_store_reads_as_unmeasured(self, tmp_path):
        store = VoiceLatencyStore(tmp_path / "voice_latency.db")
        assert store.summary("stt", "small") is None

    def test_percentiles_and_key_isolation(self, tmp_path):
        store = VoiceLatencyStore(tmp_path / "voice_latency.db")
        for duration in (100.0, 200.0, 300.0):
            store.record_sample(stage="stt", model="small", duration_ms=duration)
        store.record_sample(stage="stt", model="base", duration_ms=900.0)
        store.record_sample(stage="tts", model="af_heart", duration_ms=50.0)
        summary = store.summary("stt", "small")
        assert summary is not None
        assert summary.samples == 3
        assert summary.p50_ms == 200.0
        assert summary.mean_ms == 200.0
        assert store.summary("stt", "base").p50_ms == 900.0
        assert store.summary("tts", "af_heart").samples == 1
        assert store.summary("talker", "") is None

    def test_recording_never_raises(self, tmp_path):
        blocker = tmp_path / "a-file"
        blocker.write_text("not a directory")
        store = VoiceLatencyStore(blocker / "voice_latency.db")
        assert record_voice_stage_sample(
            "stt", "small", 100.0, store=store) is False


# ---------------------------------------------------------------------------
# The session tracker (pure decisions)
# ---------------------------------------------------------------------------

class TestSessionDegradation:
    def tracker(self, model="small", **plan_overrides):
        return SessionDegradation(plan(model, **plan_overrides))

    def test_step_down_trips_on_a_full_slow_window(self):
        tracker = self.tracker()
        for _ in range(4):
            assert tracker.note_stt_success(3000.0) is None
        step = tracker.note_stt_success(3000.0)
        assert step is not None
        notice, new_model = step
        assert new_model == "base"
        assert "slow" in notice and "'base'" in notice
        assert tracker.stt_model == "base"
        assert tracker.profile == "degraded"

    def test_step_down_walks_down_and_never_back_up(self):
        tracker = self.tracker()
        for _ in range(5):
            step = tracker.note_stt_success(3000.0)
        assert step is not None and step[1] == "base"
        # The new model must earn its own step-down with a
        # fresh window — and the direction is still only down.
        for _ in range(4):
            assert tracker.note_stt_success(3000.0) is None
        step = tracker.note_stt_success(3000.0)
        assert step is not None and step[1] == "tiny"
        # Nothing below tiny: exhausted, quiet forever.
        for _ in range(8):
            assert tracker.note_stt_success(3000.0) is None

    def test_fast_window_never_trips(self):
        tracker = self.tracker()
        for _ in range(10):
            assert tracker.note_stt_success(150.0) is None

    def test_samples_are_recorded_under_the_current_model(self):
        recorded = []
        tracker = SessionDegradation(
            plan("small"),
            recorder=lambda s, m, d: recorded.append((s, m, d)) or True)
        tracker.note_stt_success(120.0)
        for _ in range(5):
            tracker.note_stt_success(3000.0)
        assert recorded[0] == ("stt", "small", 120.0)
        # The sample that trips the step-down is recorded under
        # the model that produced it; samples after the swap are
        # recorded under the new model.
        assert [m for _s, m, _d in recorded] == ["small"] * 5 + ["base"]

    def test_failure_ladder_drop_swap_end(self):
        tracker = self.tracker()
        assert tracker.note_stt_failure("boom") == ("drop", None)
        assert tracker.note_stt_failure("boom") == ("drop", None)
        action, payload = tracker.note_stt_failure("boom")
        assert (action, payload) == ("swap", "base")
        assert tracker.stt_model == "base"
        # The swap reset the counter; three more failures end it.
        assert tracker.note_stt_failure("boom2") == ("drop", None)
        assert tracker.note_stt_failure("boom2") == ("drop", None)
        action, reason = tracker.note_stt_failure("boom2")
        assert action == "end"
        assert "speech-to-text" in reason and "boom2" in reason
        assert "'base'" in reason

    def test_failure_at_the_smallest_model_ends_without_a_swap(self):
        tracker = self.tracker(model="tiny")
        tracker.note_stt_failure("x")
        tracker.note_stt_failure("x")
        action, _reason = tracker.note_stt_failure("x")
        assert action == "end"

    def test_success_resets_the_failure_count(self):
        tracker = self.tracker()
        tracker.note_stt_failure("x")
        tracker.note_stt_failure("x")
        tracker.note_stt_success(100.0)
        assert tracker.note_stt_failure("x") == ("drop", None)
        assert tracker.note_stt_failure("x") == ("drop", None)

    def test_tts_failure_flips_text_only_exactly_once(self):
        tracker = self.tracker()
        notice = tracker.note_tts_failure()
        assert notice is not None and "text only" in notice
        assert tracker.tts_enabled is False
        assert tracker.profile == "text_only"
        assert tracker.note_tts_failure() is None


# ---------------------------------------------------------------------------
# The energy VAD fallback
# ---------------------------------------------------------------------------

class TestEnergyVad:
    async def test_silence_and_loud_frames(self):
        vad = EnergyVad()
        assert vad.unavailable_reason() is None
        await vad.open()
        assert await vad.probability(b"\x00" * 1024, 16000) == 0.0
        loud = struct.pack("<h", 12000) * 512
        assert await vad.probability(loud, 16000) == 1.0
        # A frame at the reference RMS lands on the default
        # speech threshold (0.5).
        reference = struct.pack("<h", 500) * 512
        assert await vad.probability(reference, 16000) == pytest.approx(0.5)
        await vad.close()


# ---------------------------------------------------------------------------
# Factory + pipeline integration
# ---------------------------------------------------------------------------

class SlowStt(FakeStt):
    """FakeStt whose transcriptions take a scripted time."""

    def __init__(self, script=(), delay=0.0, model_name="small"):
        super().__init__(script)
        self.delay = delay
        self.model_name = model_name

    async def transcribe(self, audio, *, sample_rate):
        if self.delay:
            await asyncio.sleep(self.delay)
        return await super().transcribe(audio, sample_rate=sample_rate)


class FailingStt(FakeStt):
    async def transcribe(self, audio, *, sample_rate):
        self.calls.append(len(audio))
        raise RuntimeError("whisper exploded")


class FailingTts(FakeTts):
    async def synthesize(self, text, *, voice):
        self.requests.append((text, voice))
        raise RuntimeError("kokoro exploded")
        yield b""  # pragma: no cover — makes this an async generator


def status_events(emitter):
    return emitter.of_type(EventType.VOICE_SESSION_STATUS)


class TestFactoryLadder:
    def build(self, *, vad=None, stt=None, tts=None, transport=None, **kw):
        return create_voice_pipeline(
            session_id="s15",
            talker=FakeTalker(),
            emit=kw.pop("emit", Emitter()),
            vad=vad if vad is not None else FakeVad({1: 0.9}),
            stt=stt if stt is not None else FakeStt(["hello"] * 20),
            tts=tts if tts is not None else FakeTts(),
            transport=transport if transport is not None else FakeTransport(()),
            capability_report=kw.pop("capability_report", matrix("degraded")),
            latency_store=kw.pop(
                "latency_store", VoiceLatencyStore(":memory:")),
            **kw)

    def test_matrix_unavailable_refuses_before_anything_opens(self):
        pipeline, error = self.build(
            capability_report=matrix(
                "unavailable",
                "This machine measured 1 GB of RAM — under the "
                "2 GB the local speech models need."))
        assert pipeline is None
        assert error is not None and error.startswith("voice unavailable:")
        assert "1 GB of RAM" in error

    def test_missing_stt_still_refuses(self):
        pipeline, error = self.build(
            stt=FakeStt(["hi"], reason="faster-whisper is not installed"))
        assert pipeline is None
        assert "faster-whisper is not installed" in error

    async def test_text_only_session_converses_without_speaking(self):
        emitter = Emitter()
        tts = FakeTts(reason="kokoro is not installed (local TTS)")
        transport = FakeTransport(speech_then_silence())
        pipeline, error = self.build(
            tts=tts, transport=transport, emit=emitter)
        assert error is None and pipeline is not None
        assert pipeline.voice_profile == "text_only"
        assert any("text only" in n for n in pipeline.start_notices)
        await pipeline.start()
        assert tts.opened is False  # the dead stage is never opened
        await run_until(
            transport, pipeline,
            lambda: any(e.type == EventType.VOICE_OUTPUT_CHUNK
                        for e in emitter.events))
        # The reply streamed as text; not one synthesis attempt.
        assert tts.requests == []

    async def test_energy_vad_runs_a_session_when_silero_cannot(self, monkeypatch):
        from aether_engine.voice.pipeline import SileroVad

        monkeypatch.setattr(
            SileroVad, "unavailable_reason",
            lambda self: "onnxruntime is not installed (Silero VAD runtime)")
        emitter = Emitter()
        talker = FakeTalker()
        loud = struct.pack("<h", 12000) * 512
        silence = b"\x00" * 1024
        transport = FakeTransport([loud] * 10 + [silence] * 30)
        pipeline, error = create_voice_pipeline(
            session_id="s15",
            talker=talker,
            emit=emitter,
            stt=FakeStt(["hello"] * 5),
            tts=FakeTts(),
            transport=transport,
            capability_report=matrix("degraded"),
            latency_store=VoiceLatencyStore(":memory:"),
        )
        assert error is None and pipeline is not None
        assert isinstance(pipeline._vad, EnergyVad)
        assert pipeline.voice_profile == "degraded"
        await pipeline.start()
        await run_until(
            transport, pipeline, lambda: bool(talker.prompts))
        assert talker.prompts == ["hello"]

    async def test_measured_slow_history_warns_at_start(self, tmp_path):
        store = VoiceLatencyStore(tmp_path / "voice_latency.db")
        for _ in range(5):
            store.record_sample(stage="stt", model="small", duration_ms=5556.0)
        pipeline, error = self.build(latency_store=store)
        assert error is None and pipeline is not None
        assert pipeline.voice_profile == "degraded"
        assert any("5.6" in n and "'base'" in n
                   for n in pipeline.start_notices)


class TestLoadFallback:
    class LoadFailStt(FakeStt):
        def __init__(self, model_name, fail_models):
            super().__init__(["hello"] * 10)
            self.model_name = model_name
            self._fail_models = fail_models

        async def open(self):
            if self.model_name in self._fail_models:
                raise VoiceUnavailableError(
                    f"model {self.model_name} failed to load")
            self.opened = True

    async def test_start_adopts_the_smaller_model_that_loads(self):
        failing = self.LoadFailStt("small", {"small"})
        degradation = SessionDegradation(plan("small"))
        transport = FakeTransport(())
        pipeline = VoicePipeline(
            session_id="s15",
            vad=FakeVad({1: 0.9}),
            stt=failing,
            tts=FakeTts(),
            transport=transport,
            talker=FakeTalker(),
            emit=Emitter(),
            degradation=degradation,
            stt_factory=lambda m: self.LoadFailStt(m, {"small"}),
        )
        await pipeline.start()
        assert pipeline._stt.model_name == "base"
        assert pipeline._delivery._stt is pipeline._stt
        assert pipeline.voice_profile == "degraded"
        assert any("could not be loaded" in n
                   for n in pipeline.start_notices)
        await pipeline.stop()

    async def test_start_raises_when_no_candidate_loads(self):
        failing = self.LoadFailStt("small", {"small", "base", "tiny"})
        degradation = SessionDegradation(plan("small"))
        pipeline = VoicePipeline(
            session_id="s15",
            vad=FakeVad({1: 0.9}),
            stt=failing,
            tts=FakeTts(),
            transport=FakeTransport(()),
            talker=FakeTalker(),
            emit=Emitter(),
            degradation=degradation,
            stt_factory=lambda m: self.LoadFailStt(m, {"small", "base", "tiny"}),
        )
        with pytest.raises(VoiceUnavailableError):
            await pipeline.start()


class TestMidSessionLadder:
    def build_session(self, *, stt, tts=None, degradation=None, factory=None,
                      emitter=None, talker=None):
        emitter = emitter or Emitter()
        transport = FakeTransport(())
        pipeline = VoicePipeline(
            session_id="s15",
            vad=FakeVad({1: 0.9}),
            stt=stt,
            tts=tts if tts is not None else FakeTts(),
            transport=transport,
            talker=talker if talker is not None else FakeTalker(),
            emit=emitter,
            degradation=degradation or SessionDegradation(plan("small")),
            stt_factory=factory,
        )
        return pipeline, transport, emitter

    async def test_latency_step_down_swaps_the_engine_and_announces(
            self, monkeypatch):
        monkeypatch.setattr(
            degradation_module, "STT_STEP_DOWN_P95_MS", 30.0)
        slow = SlowStt(["turn"] * 20, delay=0.06, model_name="small")
        built = {}

        def factory(model):
            engine = SlowStt(["turn"] * 20, delay=0.0, model_name=model)
            built[model] = engine
            return engine

        pipeline, transport, emitter = self.build_session(
            stt=slow, factory=factory)
        transport._frames = speech_then_silence() * 5
        await pipeline.start()
        await run_until(
            transport, pipeline, lambda: bool(status_events(emitter)))
        events = status_events(emitter)
        assert len(events) == 1
        payload = events[0].payload
        assert payload["profile"] == "degraded"
        assert "'base'" in payload["notice"]
        assert pipeline._stt is built["base"]
        assert pipeline._delivery._stt is built["base"]
        assert slow.closed  # the replaced engine was closed

    async def test_tts_failure_flips_text_only_and_stops_attempts(self):
        tts = FailingTts()
        talker = FakeTalker()
        pipeline, transport, emitter = self.build_session(
            stt=FakeStt(["hello"] * 5), tts=tts, talker=talker)
        transport._frames = speech_then_silence()
        await pipeline.start()
        task = asyncio.ensure_future(pipeline.run())
        try:
            for _ in range(1000):
                if status_events(emitter):
                    break
                await asyncio.sleep(0.005)
            events = status_events(emitter)
            assert len(events) == 1
            assert events[0].payload["profile"] == "text_only"
            assert "text only" in events[0].payload["notice"]
            assert pipeline.voice_profile == "text_only"
            assert len(tts.requests) == 1
            # A second turn, driven serially: the reply still
            # streams as text, and the dead stage is not called
            # again — the flip is session state, not per-turn
            # retry.
            await pipeline._delivery.finish_utterance(b"\x00" * 3200)
            for _ in range(1000):
                if len(talker.prompts) >= 2:
                    break
                await asyncio.sleep(0.005)
            await asyncio.sleep(0.1)
            assert len(talker.prompts) == 2
            assert len(tts.requests) == 1
        finally:
            await pipeline.stop()
            await asyncio.wait_for(task, timeout=5.0)

    async def test_repeated_stt_failure_swaps_then_recovers(self):
        failing = FailingStt(["never"] * 10)
        good = FakeStt(["recovered"] * 5)
        talker = FakeTalker()
        pipeline, transport, emitter = self.build_session(
            stt=failing, factory=lambda model: good, talker=talker)
        transport._frames = speech_then_silence() * 4
        await pipeline.start()
        await run_until(
            transport, pipeline, lambda: bool(talker.prompts))
        assert talker.prompts == ["recovered"]
        events = status_events(emitter)
        assert len(events) == 1
        assert "kept failing" in events[0].payload["notice"]

    async def test_repeated_stt_failure_ends_the_session_with_the_reason(self):
        failing = FailingStt(["never"] * 20)
        pipeline, transport, emitter = self.build_session(
            stt=failing, factory=lambda model: FailingStt(["never"] * 20))
        transport._frames = speech_then_silence() * 6
        await pipeline.start()
        await run_until(
            transport, pipeline,
            lambda: any(e.payload.get("profile") == "unavailable"
                        for e in status_events(emitter)))
        events = status_events(emitter)
        end = [e for e in events if e.payload.get("profile") == "unavailable"]
        assert len(end) == 1
        reason = end[0].payload["reason"]
        assert "speech-to-text" in reason and "whisper exploded" in reason
        assert pipeline.voice_profile == "unavailable"
        assert pipeline.fatal_reason == reason


# ---------------------------------------------------------------------------
# The WS start response carries the profile + notices
# ---------------------------------------------------------------------------

class TestWsStartResponse:
    async def test_start_response_reports_profile_and_notices(self, monkeypatch):
        from aether_engine.routes import websocket as ws_module
        from aether_engine.routes import ws_voice

        emitter = Emitter()
        tts = FakeTts(reason="kokoro is not installed (local TTS)")
        transport = FakeTransport(())

        def factory(**kwargs):
            return create_voice_pipeline(
                vad=FakeVad({1: 0.9}),
                stt=FakeStt(["hello"] * 5),
                tts=tts,
                transport=transport,
                capability_report=matrix("degraded"),
                latency_store=VoiceLatencyStore(":memory:"),
                **kwargs)

        monkeypatch.setattr(
            "aether_engine.voice.pipeline.create_voice_pipeline", factory)
        monkeypatch.setattr(
            ws_module, "_create_provider_instance",
            lambda *a, **k: object())

        sent = []

        class _Conn:
            def __init__(self):
                self.voice_start_lock = asyncio.Lock()
                self.voice_state = {}
                self.secret_store = object()
                self.voice_op_tail = None
                self.dictation_tail = None

            async def send_event(self, req_id, event_type, payload, _msg):
                sent.append((event_type, payload))

        conn = _Conn()
        await ws_voice.send_voice_session_start_result(
            conn, "req-1", {"session_id": "s15-ws"})
        await ws_voice.stop_voice_session(conn)

        assert len(sent) == 1
        event_type, payload = sent[0]
        assert event_type == EventType.VOICE_SESSION_START_RESPONSE
        assert payload["ok"] is True
        assert payload["voice_profile"] == "text_only"
        assert any("text only" in n for n in payload["notices"])
        assert emitter is not None  # (factory emit unused in this shape)
