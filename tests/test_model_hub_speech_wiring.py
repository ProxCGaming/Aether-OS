"""Model hub, H3: speech wiring.

The speech STT/TTS choices become real config, read at the three
construction sites that used to hard-default to ``small`` /
``af_heart``:

- ``speech_stt_model`` — shared by push-to-talk dictation (the
  websocket factory + per-connection cache) and voice mode (the
  pipeline built at VOICE_SESSION_START).
- ``transcription_stt_model`` — the media analyser's ALONE (grill
  P6(c)): background video transcription gets its own setting and
  must not follow the speech picker.
- ``speech_tts_voice`` — the Kokoro voice; a per-sentence call
  parameter, so it rides the pipeline construction into every
  ``synthesize`` call.

Validation is strict and offline (config_updates, E6-F05 pattern):
faster-whisper accepts ANY string as an HF repo id, so an
unvalidated Settings typo would become a surprise multi-GB
download at first voice use. The write path refuses anything that
is not a faster-whisper size name or an HF repo the hub labeller
marks in-app for speech-to-text.

Pins: the dictation cache is keyed by the configured model (the
pre-H3 cache never re-read config — a settings change silently
left dictation on the old model until reconnect), and a
mid-session config change applies to the NEXT voice session only
(grill P3(a)) — the live pipeline is never reconstructed.
"""
from __future__ import annotations

import asyncio
import base64
from unittest.mock import AsyncMock

import pytest
from starlette.testclient import TestClient

from aether_common.contracts import Event, EventType
from aether_engine import config as config_module
from aether_engine import state as state_module
from aether_engine.app import app, engine_state
from aether_engine.config import UserConfig, load_config, save_config
from aether_engine.config_updates import (
    HANDED_OFF_FIELDS,
    validate_handed_off_updates,
)
from aether_engine.media.analyzer import MediaAnalyzer
from aether_engine.media.store import MediaStore
from aether_engine.memory import session_store
from aether_engine.routes import websocket as ws_module
from aether_engine.routing.health import ProviderHealthManager
from aether_engine.routing.registry import ModelRegistry
from aether_engine.secrets.dpapi import BaseProtector, SecretDecryptionError
from aether_engine.secrets.storage import SecretStore
from aether_engine.voice.stt import FasterWhisperStt

# ---------------------------------------------------------------------------
# Fixtures (mirroring the E6-F05 settings fixture: audit path set
# BEFORE initialize_auth per the fixture law; config in a tmp
# CONFIG_PATH file because initialize() reloads user_config).
# ---------------------------------------------------------------------------


class DummyProtector(BaseProtector):
    def protect(self, data: bytes) -> bytes:
        return b"ENC:" + data

    def unprotect(self, data: bytes) -> bytes:
        if not data.startswith(b"ENC:"):
            raise SecretDecryptionError("Invalid header")
        return data[4:]


@pytest.fixture
def settings_env(tmp_path, monkeypatch):
    engine_state.token_path = tmp_path / "engine.token"
    engine_state.audit_logger.log_path = tmp_path / "audit.log"

    store = SecretStore(db_path=tmp_path / "secrets.db",
                        protector=DummyProtector())
    monkeypatch.setattr(state_module, "SecretStore", lambda *a, **k: store)
    monkeypatch.setattr(ws_module, "SecretStore", lambda *a, **k: store)

    ws_root = tmp_path / "wsroot"
    ws_root.mkdir()

    config_path = tmp_path / "config.json"
    monkeypatch.setattr(config_module, "CONFIG_PATH", config_path)
    save_config(UserConfig(workspace_roots=[str(ws_root)]), config_path)

    monkeypatch.setattr(engine_state, "model_registry", ModelRegistry([]))
    monkeypatch.setattr(engine_state, "health_manager", ProviderHealthManager())
    monkeypatch.setattr(session_store, "DB_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(session_store, "_initialized", False)
    monkeypatch.setattr(
        ws_module, "get_pending_approvals", AsyncMock(return_value=[]))

    engine_state.initialize_auth()
    return {"tmp_path": tmp_path, "config_path": config_path,
            "ws_root": ws_root, "store": store}


def _recv_until(ws, event_type, max_events=30):
    for _ in range(max_events):
        ev = Event.from_json(ws.receive_text())
        if ev.type == event_type:
            return ev
    raise AssertionError(f"no {event_type} received")


class _Socket:
    """TestClient + authenticated websocket, HELLO consumed."""

    def __enter__(self):
        self._client = TestClient(app)
        self._client.__enter__()
        self._cm = self._client.websocket_connect(
            "/ws/tasks",
            headers={"Authorization": f"Bearer {engine_state.auth_token}"})
        self.ws = self._cm.__enter__()
        hello = Event.from_json(self.ws.receive_text())
        assert hello.type == EventType.HELLO
        return self.ws

    def __exit__(self, *exc):
        self._cm.__exit__(*exc)
        self._client.__exit__(*exc)
        return False


def _update(ws, config):
    ws.send_text(Event(
        type=EventType.CONFIG_UPDATE_REQUEST,
        payload={"config": config}).to_json())
    return _recv_until(ws, EventType.CONFIG_UPDATE_RESPONSE)


def _get(ws):
    ws.send_text(Event(type=EventType.CONFIG_GET_REQUEST, payload={}).to_json())
    return _recv_until(ws, EventType.CONFIG_GET_RESPONSE)


# ---------------------------------------------------------------------------
# 1. Config fields: defaults, round-trip, tolerant load
# ---------------------------------------------------------------------------


class TestSpeechConfigFields:
    def test_defaults(self):
        cfg = UserConfig()
        assert cfg.speech_stt_model == "small"
        assert cfg.transcription_stt_model == "small"
        assert cfg.speech_tts_voice == "af_heart"

    def test_round_trip_through_dict(self):
        cfg = UserConfig(
            speech_stt_model="medium",
            transcription_stt_model="base",
            speech_tts_voice="bf_emma")
        data = cfg.to_dict()
        assert data["speech_stt_model"] == "medium"
        assert data["transcription_stt_model"] == "base"
        assert data["speech_tts_voice"] == "bf_emma"
        back = UserConfig.from_dict(data)
        assert back.speech_stt_model == "medium"
        assert back.transcription_stt_model == "base"
        assert back.speech_tts_voice == "bf_emma"

    def test_round_trip_through_disk(self, tmp_path):
        path = tmp_path / "config.json"
        save_config(UserConfig(
            speech_stt_model="large-v3-turbo",
            transcription_stt_model="tiny",
            speech_tts_voice="jm_kumo"), path)
        back = load_config(path)
        assert back.speech_stt_model == "large-v3-turbo"
        assert back.transcription_stt_model == "tiny"
        assert back.speech_tts_voice == "jm_kumo"

    def test_tolerant_load_keeps_valid_values(self):
        cfg = UserConfig.from_dict({
            "speech_stt_model": "Systran/faster-whisper-large-v3",
            "transcription_stt_model": "distil-large-v3",
            "speech_tts_voice": "zf_xiaobei",
        })
        assert cfg.speech_stt_model == "Systran/faster-whisper-large-v3"
        assert cfg.transcription_stt_model == "distil-large-v3"
        assert cfg.speech_tts_voice == "zf_xiaobei"

    def test_tolerant_load_falls_back_on_garbage(self):
        # The loader never bricks startup, but an unknown speech
        # value must NOT reach faster-whisper/Kokoro either — it
        # falls back to the default, exactly like the browser
        # category modes tighten back on load.
        cfg = UserConfig.from_dict({
            "speech_stt_model": "huge-v9",
            "transcription_stt_model": "openai/whisper-large-v3",
            "speech_tts_voice": "not a voice!",
        })
        assert cfg.speech_stt_model == "small"
        assert cfg.transcription_stt_model == "small"
        assert cfg.speech_tts_voice == "af_heart"

    def test_tolerant_load_falls_back_on_wrong_types(self):
        cfg = UserConfig.from_dict({
            "speech_stt_model": 42,
            "transcription_stt_model": None,
            "speech_tts_voice": ["af_heart"],
        })
        assert cfg.speech_stt_model == "small"
        assert cfg.transcription_stt_model == "small"
        assert cfg.speech_tts_voice == "af_heart"


# ---------------------------------------------------------------------------
# 2. Strict validators (validate_handed_off_updates, unit level)
# ---------------------------------------------------------------------------

_FASTER_WHISPER_SIZES = [
    "tiny", "tiny.en", "base", "base.en", "small", "small.en",
    "medium", "medium.en", "large-v1", "large-v2", "large-v3",
    "large", "large-v3-turbo", "turbo",
    "distil-small.en", "distil-medium.en", "distil-large-v2",
    "distil-large-v3", "distil-large-v3.5",
]

_STT_REPO_IDS = [
    "Systran/faster-whisper-small",
    "Systran/faster-whisper-large-v3",
    "someuser/faster-whisper-custom",
]

_STT_REFUSED = [
    "huge-v9",                          # unknown bare name
    "faster-whisper-small",             # bare leaf, not a size
    "openai/whisper-large-v3",          # not a faster-whisper build
    "hexgrad/kokoro-82m",               # a TTS repo, not STT
    "Systran/faster-whisper-small/x",   # malformed repo id
    "some user/faster-whisper-x",       # whitespace in repo id
    "",
    "   ",
    None,
    42,
    ["small"],
]

_KOKORO_PREFIXES = [
    "af", "am", "bf", "bm", "ef", "em", "ff", "fm", "hf", "hm",
    "if", "im", "jf", "jm", "pf", "pm", "zf", "zm",
]

_TTS_REFUSED = [
    "heart",        # no prefix
    "af_",          # empty name
    "xf_heart",     # unknown prefix family
    "AF_heart",     # prefixes are lowercase
    "af_Heart",     # names are lowercase
    "af_heart2",    # no digits in Kokoro voice names
    "af heart",     # whitespace inside
    "",
    None,
    7,
]


class TestSpeechValidators:
    def test_fields_are_handed_off(self):
        for field in ("speech_stt_model", "transcription_stt_model",
                      "speech_tts_voice"):
            assert field in HANDED_OFF_FIELDS

    @pytest.mark.parametrize("field", ["speech_stt_model",
                                       "transcription_stt_model"])
    @pytest.mark.parametrize("value", _FASTER_WHISPER_SIZES + _STT_REPO_IDS)
    def test_stt_accepts_sizes_and_in_app_repos(self, field, value):
        cleaned = validate_handed_off_updates({field: value})
        assert cleaned[field] == value

    @pytest.mark.parametrize("field", ["speech_stt_model",
                                       "transcription_stt_model"])
    @pytest.mark.parametrize("value", _STT_REFUSED)
    def test_stt_refuses_everything_else(self, field, value):
        with pytest.raises(ValueError) as excinfo:
            validate_handed_off_updates({field: value})
        assert field in str(excinfo.value)

    @pytest.mark.parametrize("field", ["speech_stt_model",
                                       "transcription_stt_model"])
    def test_stt_value_is_stripped(self, field):
        cleaned = validate_handed_off_updates({field: "  medium  "})
        assert cleaned[field] == "medium"

    @pytest.mark.parametrize("prefix", _KOKORO_PREFIXES)
    def test_tts_accepts_every_voice_family(self, prefix):
        cleaned = validate_handed_off_updates(
            {"speech_tts_voice": f"{prefix}_test"})
        assert cleaned["speech_tts_voice"] == f"{prefix}_test"

    @pytest.mark.parametrize("value", [
        "af_heart", "af_bella", "am_adam", "bf_emma", "jf_alpha",
        "zf_xiaobei", "hf_alpha", "pm_santa", "jm_kumo",
    ])
    def test_tts_accepts_real_kokoro_voices(self, value):
        cleaned = validate_handed_off_updates({"speech_tts_voice": value})
        assert cleaned["speech_tts_voice"] == value

    @pytest.mark.parametrize("value", _TTS_REFUSED)
    def test_tts_refuses_non_voice_ids(self, value):
        with pytest.raises(ValueError) as excinfo:
            validate_handed_off_updates({"speech_tts_voice": value})
        assert "speech_tts_voice" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 3. CONFIG_UPDATE over the socket: valid persists, invalid refused
# ---------------------------------------------------------------------------


class TestSpeechConfigUpdateSocket:
    def test_valid_update_persists(self, settings_env):
        with _Socket() as ws:
            resp = _update(ws, {
                "speech_stt_model": "large-v3",
                "transcription_stt_model": "Systran/faster-whisper-base",
                "speech_tts_voice": "bf_emma",
            })
            assert resp.payload["success"] is True
            got = _get(ws)
            cfg = got.payload["config"]
            assert cfg["speech_stt_model"] == "large-v3"
            assert cfg["transcription_stt_model"] == "Systran/faster-whisper-base"
            assert cfg["speech_tts_voice"] == "bf_emma"
        on_disk = load_config(settings_env["config_path"])
        assert on_disk.speech_stt_model == "large-v3"
        assert on_disk.transcription_stt_model == "Systran/faster-whisper-base"
        assert on_disk.speech_tts_voice == "bf_emma"

    def test_invalid_update_refused_and_old_value_intact(self, settings_env):
        with _Socket() as ws:
            resp = _update(ws, {"speech_stt_model": "medium"})
            assert resp.payload["success"] is True
            resp = _update(ws, {"speech_stt_model": "huge-v9"})
            assert resp.payload["success"] is False
            assert "speech_stt_model" in resp.payload["error"]
            got = _get(ws)
            assert got.payload["config"]["speech_stt_model"] == "medium"
        on_disk = load_config(settings_env["config_path"])
        assert on_disk.speech_stt_model == "medium"

    def test_mixed_update_with_one_bad_field_applies_nothing(
            self, settings_env):
        with _Socket() as ws:
            resp = _update(ws, {
                "speech_stt_model": "base",
                "transcription_stt_model": "bogus-model",
            })
            assert resp.payload["success"] is False
            assert "transcription_stt_model" in resp.payload["error"]
            got = _get(ws)
            # The valid sibling field was NOT applied either.
            assert got.payload["config"]["speech_stt_model"] == "small"
            assert got.payload["config"]["transcription_stt_model"] == "small"


# ---------------------------------------------------------------------------
# 4. The dictation factory + model-aware per-connection cache
# ---------------------------------------------------------------------------


class FakeStt:
    """SttEngine fake recording its own lifecycle (ptt-suite shape)."""

    def __init__(self, script=(), reason=None):
        self.script = list(script)
        self.reason = reason
        self.open_calls = 0
        self.close_calls = 0

    def unavailable_reason(self):
        return self.reason

    async def open(self):
        self.open_calls += 1

    async def close(self):
        self.close_calls += 1

    async def transcribe(self, audio: bytes, *, sample_rate: int) -> str:
        if self.script:
            return self.script.pop(0)
        return ""


class TestDictationFactoryReadsConfig:
    def test_factory_builds_the_configured_model(self, monkeypatch):
        monkeypatch.setattr(
            engine_state, "user_config",
            UserConfig(speech_stt_model="tiny"))
        engine = ws_module.create_stt_engine()
        assert isinstance(engine, FasterWhisperStt)
        assert engine._model_name == "tiny"

    def test_factory_default_is_small(self, monkeypatch):
        monkeypatch.setattr(engine_state, "user_config", UserConfig())
        engine = ws_module.create_stt_engine()
        assert engine._model_name == "small"


class TestDictationCacheModelAware:
    def test_settings_change_rebuilds_engine_on_next_use(
            self, settings_env, monkeypatch):
        built: list[FakeStt] = []

        def counting_factory():
            engine = FakeStt([f"engine-{len(built)}"])
            built.append(engine)
            return engine

        monkeypatch.setattr(ws_module, "create_stt_engine", counting_factory)

        def chunk(ws, audio):
            ws.send_text(Event(
                type=EventType.DICTATION_CHUNK_REQUEST,
                payload={
                    "session_id": "s1",
                    "audio": base64.b64encode(audio).decode("ascii"),
                    "sample_rate": 16000,
                    "is_final": False,
                }).to_json())
            return _recv_until(ws, EventType.DICTATION_CHUNK_RESPONSE)

        with _Socket() as ws:
            ws.send_text(Event(
                type=EventType.DICTATION_START_REQUEST,
                payload={"session_id": "s1"}).to_json())
            resp = _recv_until(ws, EventType.DICTATION_START_RESPONSE)
            assert resp.payload["ok"] is True
            assert len(built) == 1

            resp = chunk(ws, b"one")
            assert resp.payload["text"] == "engine-0"
            resp = chunk(ws, b"two")
            assert resp.payload["ok"] is True
            # Same model: same cached engine, opened exactly once.
            assert len(built) == 1
            assert built[0].open_calls == 1

            # The user changes the speech model in Settings,
            # mid-connection (the pre-H3 placebo trap).
            resp = _update(ws, {"speech_stt_model": "base"})
            assert resp.payload["success"] is True

            resp = chunk(ws, b"three")
            assert resp.payload["text"] == "engine-1"
            # A new engine was built for the new model and opened;
            # the old one was dropped, not re-opened.
            assert len(built) == 2
            assert built[1].open_calls == 1
            assert built[0].open_calls == 1

            resp = chunk(ws, b"four")
            assert resp.payload["ok"] is True
            assert len(built) == 2
            assert built[1].open_calls == 1
        # Teardown closes the live (current-model) engine.
        assert built[1].close_calls == 1


# ---------------------------------------------------------------------------
# 5. Voice mode: session start reads config; changes apply NEXT session
# ---------------------------------------------------------------------------


class FakePipeline:
    """Minimal pipeline double for the socket-level wiring tests."""

    def __init__(self):
        self.started = False
        self.stopped = False
        self._done = asyncio.Event()

    async def start(self):
        self.started = True

    async def run(self):
        await self._done.wait()

    async def stop(self):
        self.stopped = True
        self._done.set()

    def set_rtc_ice_listener(self, listener):
        self.ice_listener = listener


class TestVoiceSessionNextSessionSemantics:
    @pytest.fixture
    def voice_stack(self, settings_env, monkeypatch):
        calls: list[dict] = []
        pipelines: list[FakePipeline] = []

        def recording_factory(**kwargs):
            calls.append(kwargs)
            pipeline = FakePipeline()
            pipelines.append(pipeline)
            return pipeline, None

        monkeypatch.setattr(
            "aether_engine.voice.pipeline.create_voice_pipeline",
            recording_factory)
        monkeypatch.setattr(
            ws_module, "_create_provider_instance",
            lambda *a, **k: object())
        return {"calls": calls, "pipelines": pipelines}

    def _start(self, ws, sid="voice-1"):
        ws.send_text(Event(
            type=EventType.VOICE_SESSION_START_REQUEST,
            payload={"session_id": sid}).to_json())
        return _recv_until(ws, EventType.VOICE_SESSION_START_RESPONSE)

    def _stop(self, ws, sid="voice-1"):
        ws.send_text(Event(
            type=EventType.VOICE_SESSION_STOP_REQUEST,
            payload={"session_id": sid}).to_json())
        return _recv_until(ws, EventType.VOICE_SESSION_STOP_RESPONSE)

    def test_session_start_reads_config(self, voice_stack):
        with _Socket() as ws:
            resp = self._start(ws)
            assert resp.payload["ok"] is True
        assert len(voice_stack["calls"]) == 1
        kwargs = voice_stack["calls"][0]
        assert kwargs["stt_model"] == "small"
        assert kwargs["tts_voice"] == "af_heart"

    def test_mid_session_change_applies_to_next_session(self, voice_stack):
        with _Socket() as ws:
            resp = self._start(ws)
            assert resp.payload["ok"] is True
            first = voice_stack["pipelines"][0]

            resp = _update(ws, {
                "speech_stt_model": "base",
                "speech_tts_voice": "bf_emma",
            })
            assert resp.payload["success"] is True
            # P3(a): the live session is NOT rebuilt or stopped —
            # it finishes on the model it started with.
            assert len(voice_stack["calls"]) == 1
            assert first.stopped is False

            self._stop(ws)
            assert first.stopped is True

            resp = self._start(ws)
            assert resp.payload["ok"] is True
        assert len(voice_stack["calls"]) == 2
        kwargs = voice_stack["calls"][1]
        assert kwargs["stt_model"] == "base"
        assert kwargs["tts_voice"] == "bf_emma"


# ---------------------------------------------------------------------------
# 6. Pipeline factory: stt_model / tts_voice forwarding
# ---------------------------------------------------------------------------


class TestPipelineFactorySpeech:
    @pytest.fixture(autouse=True)
    def _stt_available(self, monkeypatch):
        # faster-whisper is not installed in this environment; the
        # factory's fail-closed probe would (correctly) refuse.
        # Construction is what is under test, not package presence.
        monkeypatch.setattr(
            FasterWhisperStt, "unavailable_reason", lambda self: None)

    def _build(self, **kwargs):
        from test_voice_pipeline import (
            Emitter,
            FakeTalker,
            FakeTransport,
            FakeTts,
            FakeVad,
        )

        from aether_engine.voice.pipeline import create_voice_pipeline

        return create_voice_pipeline(
            session_id="sess-h3",
            talker=FakeTalker(),
            emit=Emitter(),
            vad=FakeVad({1: 0.9}),
            tts=FakeTts(),
            transport=FakeTransport(()),
            **kwargs)

    def test_factory_forwards_stt_model_and_tts_voice(self):
        pipeline, error = self._build(stt_model="base", tts_voice="bf_emma")
        assert error is None
        assert isinstance(pipeline._stt, FasterWhisperStt)
        assert pipeline._stt._model_name == "base"
        assert pipeline._tts_voice == "bf_emma"

    def test_factory_defaults_unchanged(self):
        pipeline, error = self._build()
        assert error is None
        assert pipeline._stt._model_name == "small"
        assert pipeline._tts_voice == "af_heart"

    def test_injected_stt_wins_over_stt_model(self):
        from test_voice_pipeline import FakeStt

        fake = FakeStt(["hello"])
        pipeline, error = self._build(stt=fake, stt_model="base")
        assert error is None
        assert pipeline._stt is fake

    @pytest.mark.asyncio
    async def test_tts_voice_reaches_every_sentence(self):
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

        from aether_engine.voice.pipeline import create_voice_pipeline

        tts = FakeTts()
        transport = FakeTransport(speech_then_silence())
        pipeline, error = create_voice_pipeline(
            session_id="sess-h3",
            talker=FakeTalker(),
            emit=Emitter(),
            vad=FakeVad({1: 0.9}),
            stt=FakeStt(["the red bicycle"] * 10),
            tts=tts,
            transport=transport,
            tts_voice="bf_emma",
        )
        assert error is None
        await pipeline.start()
        await run_until(transport, pipeline, lambda: len(tts.requests) >= 1)
        assert tts.requests
        assert {voice for _text, voice in tts.requests} == {"bf_emma"}


# ---------------------------------------------------------------------------
# 7. Media analyser: transcription model, NOT the speech model (P6(c))
# ---------------------------------------------------------------------------


class TestAnalyzerTranscriptionModel:
    @pytest.fixture
    def temp_store(self, tmp_path):
        store = MediaStore(db_path=tmp_path / "test_media.db")
        store.init_db()
        return store

    def test_analyzer_builds_transcription_model_not_speech_model(
            self, temp_store):
        config = UserConfig(
            speech_stt_model="large-v3",
            transcription_stt_model="base")
        analyzer = MediaAnalyzer(store=temp_store, config=config)
        engine = analyzer.stt_engine
        assert isinstance(engine, FasterWhisperStt)
        # The separation pin: the analyser follows its OWN field.
        assert engine._model_name == "base"

    def test_analyzer_default_transcription_model(self, temp_store):
        analyzer = MediaAnalyzer(store=temp_store, config=UserConfig())
        assert analyzer.stt_engine._model_name == "small"

    def test_injected_engine_still_wins(self, temp_store):
        fake = FakeStt()
        analyzer = MediaAnalyzer(
            store=temp_store,
            config=UserConfig(transcription_stt_model="base"),
            stt_engine=fake)
        assert analyzer.stt_engine is fake
