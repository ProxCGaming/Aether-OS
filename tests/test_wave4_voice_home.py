"""Wave 4, Lane A: the Voice home's engine side.

Covers the four new speech config fields (TTS speed preset, STT
language, microphone device id, dictation mode) under the strict
write / tolerant load contract, the wiring of speed + language
into the speech engines and their per-connection caches, the
Settings voice-preview route (one fixed sentence, honest
unavailability), and the effective-STT-model surface the overlay
names.
"""
import base64
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from starlette.testclient import TestClient

from aether_common.contracts import Event, EventType
from aether_engine.app import app, engine_state
from aether_engine.config import UserConfig
from aether_engine.config_updates import (
    HANDED_OFF_FIELDS,
    validate_handed_off_updates,
)
from aether_engine.memory import session_store
from aether_engine.routes import websocket as ws_module
from aether_engine.routes import ws_voice
from aether_engine.voice.degradation import (
    VoiceLatencyStore,
    record_voice_stage_sample,
)
from aether_engine.voice.pipeline import create_voice_pipeline
from aether_engine.voice.stt import stt_language_or_none
from aether_engine.voice.tts import (
    VOICE_PREVIEW_TEXT,
    KokoroTts,
    tts_speed_factor,
)


# ---------------------------------------------------------------------------
# Config fields: defaults, round-trip, tolerant load
# ---------------------------------------------------------------------------

class TestVoiceHomeConfigFields:
    def test_defaults(self):
        cfg = UserConfig()
        assert cfg.speech_tts_speed == "normal"
        assert cfg.speech_stt_language == "auto"
        assert cfg.speech_input_device_id == ""
        assert cfg.dictation_mode == "hold"

    def test_round_trip_through_dict(self):
        cfg = UserConfig(
            speech_tts_speed="fast", speech_stt_language="hi",
            speech_input_device_id="dev-123", dictation_mode="toggle")
        back = UserConfig.from_dict(cfg.to_dict())
        assert back.speech_tts_speed == "fast"
        assert back.speech_stt_language == "hi"
        assert back.speech_input_device_id == "dev-123"
        assert back.dictation_mode == "toggle"

    def test_tolerant_load_falls_back_on_garbage(self):
        cfg = UserConfig.from_dict({
            "speech_tts_speed": "ludicrous",
            "speech_stt_language": "xx",
            "speech_input_device_id": 42,
            "dictation_mode": "press",
        })
        assert cfg.speech_tts_speed == "normal"
        assert cfg.speech_stt_language == "auto"
        assert cfg.speech_input_device_id == ""
        assert cfg.dictation_mode == "hold"


# ---------------------------------------------------------------------------
# Strict validators (the Settings write path)
# ---------------------------------------------------------------------------

class TestVoiceHomeValidators:
    def test_fields_are_handed_off(self):
        for field in ("speech_tts_speed", "speech_stt_language",
                      "speech_input_device_id", "dictation_mode"):
            assert field in HANDED_OFF_FIELDS

    def test_speed_accepts_the_presets(self):
        for preset in ("slow", "normal", "fast"):
            cleaned = validate_handed_off_updates(
                {"speech_tts_speed": preset})
            assert cleaned["speech_tts_speed"] == preset

    @pytest.mark.parametrize("value", ["ludicrous", "1.25", 1.25, None, ""])
    def test_speed_refuses_everything_else(self, value):
        with pytest.raises(ValueError, match="speech_tts_speed"):
            validate_handed_off_updates({"speech_tts_speed": value})

    def test_language_accepts_auto_and_offered_codes(self):
        for code in ("auto", "en", "hi", "bn", "zh"):
            cleaned = validate_handed_off_updates(
                {"speech_stt_language": code})
            assert cleaned["speech_stt_language"] == code

    @pytest.mark.parametrize("value", ["xx", "english", "EN", 7, None])
    def test_language_refuses_unlisted_values(self, value):
        with pytest.raises(ValueError, match="speech_stt_language"):
            validate_handed_off_updates({"speech_stt_language": value})

    def test_device_id_accepts_empty_and_opaque_ids(self):
        for value in ("", "a1b2c3", "x" * 512):
            cleaned = validate_handed_off_updates(
                {"speech_input_device_id": value})
            assert cleaned["speech_input_device_id"] == value

    @pytest.mark.parametrize("value", [42, None, "x" * 513])
    def test_device_id_refuses_non_strings_and_overlong(self, value):
        with pytest.raises(ValueError, match="speech_input_device_id"):
            validate_handed_off_updates({"speech_input_device_id": value})

    def test_dictation_mode_accepts_both_behaviours(self):
        for mode in ("hold", "toggle"):
            cleaned = validate_handed_off_updates({"dictation_mode": mode})
            assert cleaned["dictation_mode"] == mode

    @pytest.mark.parametrize("value", ["press", "HOLD", "", None])
    def test_dictation_mode_refuses_everything_else(self, value):
        with pytest.raises(ValueError, match="dictation_mode"):
            validate_handed_off_updates({"dictation_mode": value})


# ---------------------------------------------------------------------------
# Speed / language helpers + engine construction
# ---------------------------------------------------------------------------

class TestSpeedAndLanguage:
    def test_speed_factor_mapping(self):
        assert tts_speed_factor("slow") == 0.85
        assert tts_speed_factor("normal") == 1.0
        assert tts_speed_factor("fast") == 1.25
        assert tts_speed_factor("bogus") == 1.0
        assert tts_speed_factor(None) == 1.0

    def test_language_or_none(self):
        assert stt_language_or_none("auto") is None
        assert stt_language_or_none("hi") == "hi"
        assert stt_language_or_none("bogus") is None
        assert stt_language_or_none(None) is None

    async def test_kokoro_passes_its_speed_to_the_pipeline_call(self):
        calls = []

        class RecordingPipeline:
            def __call__(self, text, voice=None, speed=1.0):
                calls.append((text, voice, speed))
                yield None, None, [0.0, 0.05]

        tts = KokoroTts(speed=1.25)
        tts._pipelines = {"a": RecordingPipeline()}
        chunks = [c async for c in tts.synthesize("hi", voice="af_heart")]
        assert chunks  # audio came back
        assert calls == [("hi", "af_heart", 1.25)]

    def test_create_stt_engine_builds_config_model_and_language(
            self, monkeypatch):
        cfg = engine_state.user_config
        monkeypatch.setattr(cfg, "speech_stt_model", "base")
        monkeypatch.setattr(cfg, "speech_stt_language", "hi")
        engine = ws_voice.create_stt_engine()
        assert engine.model_name == "base"
        assert engine._language == "hi"
        monkeypatch.setattr(cfg, "speech_stt_language", "auto")
        engine = ws_voice.create_stt_engine()
        assert engine._language is None

    def test_create_tts_engine_builds_config_speed(self, monkeypatch):
        cfg = engine_state.user_config
        monkeypatch.setattr(cfg, "speech_tts_speed", "fast")
        engine = ws_voice.create_tts_engine()
        assert engine._speed == 1.25
        monkeypatch.setattr(cfg, "speech_tts_speed", "slow")
        engine = ws_voice.create_tts_engine()
        assert engine._speed == 0.85


# ---------------------------------------------------------------------------
# Per-connection engine caches follow config changes
# ---------------------------------------------------------------------------

class _FakeConn:
    def __init__(self):
        self.voice_state = {}


class TestEngineCacheKeys:
    def test_dictation_stt_rebuilds_on_language_change(
            self, monkeypatch):
        built = []

        def factory():
            engine = SimpleNamespace(tag=len(built))
            built.append(engine)
            return engine

        monkeypatch.setattr(ws_module, "create_stt_engine", factory)
        cfg = engine_state.user_config
        monkeypatch.setattr(cfg, "speech_stt_model", "small")
        monkeypatch.setattr(cfg, "speech_stt_language", "auto")
        conn = _FakeConn()
        first = ws_voice.dictation_stt(conn)
        assert ws_voice.dictation_stt(conn) is first  # cached
        monkeypatch.setattr(cfg, "speech_stt_language", "hi")
        second = ws_voice.dictation_stt(conn)
        assert second is not first  # rebuilt for the new language
        assert ws_voice.dictation_stt(conn) is second

    def test_read_aloud_tts_rebuilds_on_speed_change(self, monkeypatch):
        built = []

        def factory():
            engine = SimpleNamespace(tag=len(built))
            built.append(engine)
            return engine

        monkeypatch.setattr(ws_module, "create_tts_engine", factory)
        cfg = engine_state.user_config
        monkeypatch.setattr(cfg, "speech_tts_speed", "normal")
        conn = _FakeConn()
        first = ws_voice.read_aloud_tts(conn)
        assert ws_voice.read_aloud_tts(conn) is first
        monkeypatch.setattr(cfg, "speech_tts_speed", "fast")
        second = ws_voice.read_aloud_tts(conn)
        assert second is not first
        assert ws_voice.read_aloud_tts(conn) is second


# ---------------------------------------------------------------------------
# The voice preview route (socket level)
# ---------------------------------------------------------------------------

@pytest.fixture
def ws_env(tmp_path, monkeypatch):
    engine_state.token_path = tmp_path / "engine.token"
    engine_state.audit_logger.log_path = tmp_path / "audit.log"
    engine_state.initialize()
    monkeypatch.setattr(session_store, "DB_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(session_store, "_initialized", False)
    monkeypatch.setattr(
        ws_module, "get_pending_approvals", AsyncMock(return_value=[]))
    return {"tmp_path": tmp_path}


def _receive_until(ws, wanted, max_events=40):
    for _ in range(max_events):
        ev = Event.from_json(ws.receive_text())
        if ev.type == wanted:
            return ev
    raise AssertionError(f"{wanted} not received")


class FakePreviewTts:
    def __init__(self, reason=None):
        self.reason = reason
        self.requests = []
        self.open_calls = 0

    def unavailable_reason(self):
        return self.reason

    async def open(self):
        self.open_calls += 1

    async def close(self):
        pass

    async def synthesize(self, text, *, voice):
        self.requests.append((text, voice))
        yield b"WAVCHUNK1"
        yield b"WAVCHUNK2"


def _preview(ws_env, monkeypatch, fake_tts, payload):
    monkeypatch.setattr(ws_module, "create_tts_engine", lambda: fake_tts)
    with ExitStack(), TestClient(app) as client, client.websocket_connect(
        "/ws/tasks",
        headers={"Authorization": f"Bearer {engine_state.auth_token}"},
    ) as ws:
        ws.receive_text()  # HELLO
        ws.send_text(Event(
            type=EventType.VOICE_PREVIEW_REQUEST, payload=payload).to_json())
        return _receive_until(ws, EventType.VOICE_PREVIEW_RESPONSE).payload


class TestVoicePreviewRoute:
    def test_event_types_exist(self):
        assert EventType.VOICE_PREVIEW_REQUEST == "VOICE_PREVIEW_REQUEST"
        assert EventType.VOICE_PREVIEW_RESPONSE == "VOICE_PREVIEW_RESPONSE"

    def test_preview_speaks_the_fixed_sentence_in_the_named_voice(
            self, ws_env, monkeypatch):
        fake = FakePreviewTts()
        payload = _preview(ws_env, monkeypatch, fake, {"voice": "bf_emma"})
        assert payload["ok"] is True
        assert payload["voice"] == "bf_emma"
        assert payload["error"] is None
        # The text is the engine-side fixed sentence — the
        # request carried no text at all.
        assert fake.requests == [(VOICE_PREVIEW_TEXT, "bf_emma")]
        decoded = [base64.b64decode(c) for c in payload["audio_chunks"]]
        assert decoded == [b"WAVCHUNK1", b"WAVCHUNK2"]

    def test_probe_answers_availability_without_synthesising(
            self, ws_env, monkeypatch):
        fake = FakePreviewTts()
        payload = _preview(ws_env, monkeypatch, fake, {"probe": True})
        assert payload["ok"] is True
        assert payload["audio_chunks"] == []
        assert fake.requests == []
        assert fake.open_calls == 0

    def test_unavailable_engine_is_honest(self, ws_env, monkeypatch):
        fake = FakePreviewTts(reason="kokoro is not installed (local TTS)")
        payload = _preview(ws_env, monkeypatch, fake, {"voice": "af_heart"})
        assert payload["ok"] is False
        assert "kokoro is not installed" in payload["error"]
        probe = _preview(ws_env, monkeypatch, fake, {"probe": True})
        assert probe["ok"] is False
        assert "kokoro is not installed" in probe["error"]

    def test_unknown_voice_is_refused(self, ws_env, monkeypatch):
        fake = FakePreviewTts()
        payload = _preview(
            ws_env, monkeypatch, fake, {"voice": "not a voice"})
        assert payload["ok"] is False
        assert "unknown Kokoro voice" in payload["error"]
        assert fake.requests == []


# ---------------------------------------------------------------------------
# The effective STT model (what the overlay names)
# ---------------------------------------------------------------------------

class _FakeVad:
    def unavailable_reason(self):
        return None

    async def open(self):
        pass

    async def close(self):
        pass

    async def probability(self, data, sample_rate):
        return 0.0


class _FakeStt:
    def __init__(self, model_name="medium"):
        self._model_name = model_name

    @property
    def model_name(self):
        return self._model_name

    def unavailable_reason(self):
        return None

    async def open(self):
        pass

    async def close(self):
        pass

    async def transcribe(self, audio, *, sample_rate):
        return ""


class _FakeTts:
    def unavailable_reason(self):
        return None

    async def open(self):
        pass

    async def close(self):
        pass

    async def synthesize(self, text, *, voice):
        yield b""


class _FakeTransport:
    def unavailable_reason(self):
        return None

    async def open(self):
        return {}

    async def close(self):
        pass

    async def frames(self):
        yield b""


def _matrix(status):
    return {"features": [
        {"id": "local_voice", "status": status, "reason": "test"},
        {"id": "local_models", "status": "available", "reason": "n/a"},
    ]}


async def _silent_talker(messages, emit_transcript):
    yield "ok"


class TestEffectiveSttModel:
    def _build(self, store, stt_model="medium"):
        pipeline, error = create_voice_pipeline(
            session_id="w4",
            talker=_silent_talker,
            emit=lambda event: None,
            vad=_FakeVad(),
            stt=_FakeStt(stt_model),
            tts=_FakeTts(),
            transport=_FakeTransport(),
            stt_model=stt_model,
            capability_report=_matrix("available"),
            latency_store=store,
        )
        assert error is None and pipeline is not None
        return pipeline

    def test_unmeasured_model_is_the_effective_model(self):
        pipeline = self._build(VoiceLatencyStore(":memory:"))
        assert pipeline.effective_stt_model == "medium"
        assert pipeline.voice_profile == "full"

    def test_measured_slow_model_starts_configured_but_degraded(
            self, tmp_path):
        store = VoiceLatencyStore(tmp_path / "voice_latency.db")
        for _ in range(6):
            assert record_voice_stage_sample(
                "stt", "medium", 9000.0, store=store)
        pipeline = self._build(store)
        # The plan honours the configured model at start (the
        # step-down itself is a mid-session act), but the
        # profile is degraded and the warning names the faster
        # model — the effective model is still 'medium' here.
        assert pipeline.effective_stt_model == "medium"
        assert pipeline.voice_profile == "degraded"
        assert any("small" in n for n in pipeline.start_notices)

    async def test_session_status_names_the_stepped_down_model(self):
        # The delivery half's status push carries the tracker's
        # current model, so a mid-session step-down reaches the
        # overlay as data, not just as notice prose.
        from aether_engine.voice.degradation import (
            SessionDegradation,
            VoicePlan,
        )
        from aether_engine.voice.delivery import VoiceDelivery

        tracker = SessionDegradation(
            VoicePlan(profile="full", stt_model="small"))
        # Five slow turns trip the one-shot step-down decision
        # ('small' steps down to 'base' in the size chain).
        result = None
        for _ in range(5):
            result = tracker.note_stt_success(9000.0) or result
        assert result is not None and result[1] == "base"
        assert tracker.stt_model == "base"

        emitted = []

        async def emit(event):
            emitted.append(event)

        delivery = VoiceDelivery(
            session_id="w4",
            stt=_FakeStt(), tts=_FakeTts(), talker=_silent_talker,
            emit=emit, tts_voice="af_heart", escalation_ack="ok",
            utterance_snapshot=lambda: b"",
            utterance_extends=lambda snapshot: False,
            has_utterance=lambda: False,
            observe_transcript=lambda text: None,
            user_speaking=lambda: False,
            is_started=lambda: True,
            is_stopping=lambda: False,
            degradation=tracker,
        )
        await delivery._emit_session_status(
            profile="degraded", notice=result[0])
        assert len(emitted) == 1
        payload = emitted[0].payload
        assert payload["profile"] == "degraded"
        assert payload["stt_model"] == "base"
