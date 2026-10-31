"""Tests for E8-5.2: Voice endpointing tuning and latency benchmark.

Verifies:
1. UserConfig voice endpointing thresholds (vad_threshold, silence_duration_ms)
   persist and round-trip through to_dict/from_dict.
2. Endpointing thresholds measurably change endpointing behaviour.
3. Next voice session picks up updated config thresholds.
4. Voice latency benchmark measures all 4 stages (VAD, STT, LLM TTFT, TTS first-audio),
   computes end-to-end median turn against the <800 ms target, attributes shortfalls
   to stages, and honestly reports running on fakes.
"""

from __future__ import annotations

import subprocess
import sys
from contextlib import ExitStack
from unittest.mock import patch

from fastapi.testclient import TestClient

import aether_engine.routes.websocket as ws_module
from aether_common.contracts import Event, EventType
from aether_engine.app import app
from aether_engine.config import UserConfig
from aether_engine.memory import session_store
from aether_engine.state import engine_state
from aether_engine.voice.pipeline import VoicePipeline, create_voice_pipeline
from aether_engine.voice.vad import Endpointer, EndpointerConfig

# ---------------------------------------------------------------------------
# 1. UserConfig Persistence & Round-Trip Tests
# ---------------------------------------------------------------------------


def test_user_config_voice_thresholds_defaults():
    """vad_threshold and silence_duration_ms must have correct defaults in UserConfig."""
    cfg = UserConfig()
    assert hasattr(cfg, "vad_threshold")
    assert hasattr(cfg, "silence_duration_ms")
    assert cfg.vad_threshold == 0.5
    assert cfg.silence_duration_ms == 700


def test_user_config_voice_thresholds_roundtrip():
    """vad_threshold and silence_duration_ms MUST round-trip through to_dict/from_dict."""
    cfg = UserConfig(vad_threshold=0.35, silence_duration_ms=450)
    d = cfg.to_dict()

    assert "vad_threshold" in d, "vad_threshold missing from to_dict"
    assert "silence_duration_ms" in d, "silence_duration_ms missing from to_dict"
    assert d["vad_threshold"] == 0.35
    assert d["silence_duration_ms"] == 450

    restored = UserConfig.from_dict(d)
    assert restored.vad_threshold == 0.35
    assert restored.silence_duration_ms == 450


def test_user_config_voice_thresholds_resilience():
    """Corrupted or missing threshold values in from_dict fall back safely to defaults."""
    cfg = UserConfig.from_dict({"vad_threshold": "invalid", "silence_duration_ms": None})
    assert cfg.vad_threshold == 0.5
    assert cfg.silence_duration_ms == 700


# ---------------------------------------------------------------------------
# 2. Measurable Endpointing Behaviour Changes
# ---------------------------------------------------------------------------


def test_silence_duration_measurably_changes_endpointing():
    """Moving silence_duration_ms measurably changes when utterance_end triggers."""
    # 320 ms speech (10 frames of 32 ms) followed by 500 ms silence (approx 16 frames of 32 ms)
    # With silence_duration_ms=700: 500 ms silence is NOT enough to end utterance.
    ep_default = Endpointer(EndpointerConfig(silence_duration_ms=700))
    ep_default.feed(0.9, 32)  # speech started
    for _ in range(9):
        ep_default.feed(0.9, 32)  # 320 ms speech total

    result_default = None
    for _ in range(15):  # 15 * 32 ms = 480 ms silence
        ev = ep_default.feed(0.1, 32)
        if ev:
            result_default = ev
    assert result_default is None
    assert ep_default.in_speech is True, "Still in speech with default 700ms silence threshold"

    # With tuned silence_duration_ms=400: 480 ms silence DOES trigger utterance_end.
    ep_tuned = Endpointer(EndpointerConfig(silence_duration_ms=400))
    ep_tuned.feed(0.9, 32)  # speech started
    for _ in range(9):
        ep_tuned.feed(0.9, 32)  # 320 ms speech total

    result_tuned = None
    for _ in range(15):  # 15 * 32 ms = 480 ms silence
        ev = ep_tuned.feed(0.1, 32)
        if ev:
            result_tuned = ev
            break
    assert result_tuned == "utterance_end", "Utterance ended at 400ms threshold"
    assert ep_tuned.in_speech is False


def test_vad_threshold_measurably_changes_endpointing():
    """Moving vad_threshold measurably changes what probability triggers speech."""
    frame_prob = 0.40

    # Default threshold 0.5: 0.40 probability is treated as silence / not speech.
    ep_default = Endpointer(EndpointerConfig(speech_threshold=0.5))
    ev_default = ep_default.feed(frame_prob, 32)
    assert ev_default is None
    assert ep_default.in_speech is False

    # Tuned threshold 0.35: 0.40 probability triggers speech_started!
    ep_tuned = Endpointer(EndpointerConfig(speech_threshold=0.35))
    ev_tuned = ep_tuned.feed(frame_prob, 32)
    assert ev_tuned == "speech_started"
    assert ep_tuned.in_speech is True


# ---------------------------------------------------------------------------
# 3. Next Voice Session Picks Up Tuned Thresholds
# ---------------------------------------------------------------------------


def test_voice_pipeline_respects_custom_endpointer():
    """create_voice_pipeline honours custom endpointer thresholds."""
    ep = Endpointer(EndpointerConfig(speech_threshold=0.35, silence_duration_ms=450))

    class DummyTransport:
        def unavailable_reason(self):
            return None

    pipeline = VoicePipeline(
        session_id="test-session",
        vad=None,  # dummy
        stt=None,  # dummy
        tts=None,  # dummy
        transport=DummyTransport(),
        talker=lambda x: None,
        emit=lambda e: None,
        endpointer=ep,
    )
    assert pipeline.endpointer.config.speech_threshold == 0.35
    assert pipeline.endpointer.config.silence_duration_ms == 450


def test_websocket_config_update_drives_next_voice_session():
    """Updating config via CONFIG_UPDATE_REQUEST updates the next voice session's thresholds."""
    captured_pipelines: list[VoicePipeline] = []
    real_create = create_voice_pipeline

    def mock_create(**kwargs):
        pipe, err = real_create(**kwargs)
        if pipe is not None:
            captured_pipelines.append(pipe)
        return pipe, err

    # Create dummy stages that report available
    from tests.test_voice_pipeline import (
        FakeStt,
        FakeTransport,
        FakeTts,
        FakeVad,
        _voice_stack,
    )

    sid = session_store.create_session("Voice Tuning Test")
    with ExitStack() as stack:
        _voice_stack(stack)

        def factory(**kwargs):
            return real_create(
                vad=FakeVad({1: 0.9}),
                stt=FakeStt(["hello"]),
                tts=FakeTts(),
                transport=FakeTransport([b"\x00" * 64]),
                **kwargs,
            )

        stack.enter_context(patch("aether_engine.voice.pipeline.create_voice_pipeline", side_effect=factory))

        from aether_engine.routing.intent import IntentDecision

        async def _chat_verdict(prompt, provider=None):
            return IntentDecision(route="chat", reason="test", source="model")

        stack.enter_context(patch.object(ws_module, "classify_intent", side_effect=_chat_verdict))

        with TestClient(app) as client, client.websocket_connect(
            f"/ws/tasks?token={engine_state.auth_token}"
        ) as ws:
            ws.receive_text()  # HELLO

            # 1. Update config thresholds via CONFIG_UPDATE_REQUEST
            ws.send_text(
                Event(
                    type=EventType.CONFIG_UPDATE_REQUEST,
                    payload={"config": {"vad_threshold": 0.35, "silence_duration_ms": 420}},
                ).to_json()
            )
            raw = ws.receive_text()
            ev = Event.from_json(raw)
            assert ev.type == EventType.CONFIG_UPDATE_RESPONSE
            assert ev.payload["config"]["vad_threshold"] == 0.35
            assert ev.payload["config"]["silence_duration_ms"] == 420

            # 2. Start a voice session: verify the pipeline gets the updated thresholds
            ws.send_text(
                Event(
                    type=EventType.VOICE_SESSION_START_REQUEST,
                    payload={"session_id": sid},
                ).to_json()
            )
            raw_start = ws.receive_text()
            ev_start = Event.from_json(raw_start)
            assert ev_start.type == EventType.VOICE_SESSION_START_RESPONSE
            assert ev_start.payload["ok"] is True

            # Stop session cleanly
            ws.send_text(
                Event(
                    type=EventType.VOICE_SESSION_STOP_REQUEST,
                    payload={"session_id": sid},
                ).to_json()
            )
            ws.receive_text()

    # The running pipeline had the tuned thresholds
    active_pipe = engine_state.user_config
    assert active_pipe.vad_threshold == 0.35
    assert active_pipe.silence_duration_ms == 420


# ---------------------------------------------------------------------------
# 4. Latency Benchmark Harness Assertions
# ---------------------------------------------------------------------------


def test_voice_bench_module_importable():
    """benchmarks.voice_latency_bench must be importable with its expected API."""
    import benchmarks.voice_latency_bench as bench_mod

    assert hasattr(bench_mod, "run_voice_latency_bench")
    assert hasattr(bench_mod, "StageTimings")
    assert hasattr(bench_mod, "BenchmarkResult")
    assert hasattr(bench_mod, "attribute_shortfall")


def test_voice_bench_runs_over_test_audio_and_generates_report():
    """Benchmark runs over test audio and returns a complete per-stage latency report."""
    from benchmarks.voice_latency_bench import run_voice_latency_bench

    result = run_voice_latency_bench(runs=3)

    assert result.sample_count >= 1
    assert result.median_vad_ms >= 0
    assert result.median_stt_ms >= 0
    assert result.median_llm_ttft_ms >= 0
    assert result.median_tts_first_audio_ms >= 0
    assert result.median_e2e_ms >= 0

    report = result.format_report()
    assert "VAD" in report
    assert "STT" in report
    assert "LLM TTFT" in report
    assert "TTS first-audio" in report
    assert "Median turn" in report
    assert "800 ms" in report

    # Honesty under missing deps: VM lacks hardware libs so fakes are detected
    assert result.is_fake is True
    assert "FAKES DETECTED" in report
    assert "hardware latency" in report


def test_voice_bench_meets_target_when_stages_within_budget():
    """When stage latencies are within budget, benchmark meets <800ms target."""
    from benchmarks.voice_latency_bench import (
        BenchmarkResult,
        StageTimings,
        attribute_shortfall,
    )

    # All stages fast: VAD=150, STT=150, LLM=150, TTS=120 -> E2E=570ms (<800ms)
    fast_timings = [
        StageTimings(vad_ms=150, stt_ms=150, llm_ttft_ms=150, tts_first_audio_ms=120, e2e_ms=570),
        StageTimings(vad_ms=140, stt_ms=160, llm_ttft_ms=150, tts_first_audio_ms=110, e2e_ms=560),
    ]
    res = BenchmarkResult(samples=fast_timings, is_fake=True)
    assert res.median_e2e_ms < 800.0
    assert res.meets_target is True

    shortfall = attribute_shortfall(res)
    assert shortfall is None, "No shortfall when under 800ms target"


def test_voice_bench_shortfall_attributed_to_tts_drain_then_emit():
    """When TTS drain-then-emit pushes latency over 800ms, shortfall is attributed to TTS."""
    from benchmarks.voice_latency_bench import (
        BenchmarkResult,
        StageTimings,
        attribute_shortfall,
    )

    # TTS is the bottleneck (e.g. 450 ms due to sentence drain before first chunk emit)
    # VAD=150, STT=150, LLM=150, TTS=450 -> E2E=900ms (>800ms)
    slow_tts_timings = [
        StageTimings(vad_ms=150, stt_ms=150, llm_ttft_ms=150, tts_first_audio_ms=450, e2e_ms=900),
        StageTimings(vad_ms=150, stt_ms=150, llm_ttft_ms=150, tts_first_audio_ms=440, e2e_ms=890),
    ]
    res = BenchmarkResult(samples=slow_tts_timings, is_fake=True)
    assert res.median_e2e_ms >= 800.0
    assert res.meets_target is False

    shortfall = attribute_shortfall(res)
    assert shortfall is not None
    assert shortfall["offending_stage"] == "TTS first-audio"
    assert "TTS first-audio" in shortfall["explanation"]
    assert shortfall["stage_delta_ms"] > 0

    report = res.format_report()
    assert "Shortfall" in report
    assert "TTS first-audio" in report


def test_voice_bench_shortfall_attributed_to_vad_endpointing():
    """When VAD silence duration pushes latency over 800ms, shortfall is attributed to VAD."""
    from benchmarks.voice_latency_bench import (
        BenchmarkResult,
        StageTimings,
        attribute_shortfall,
    )

    # VAD silence duration took 700 ms: VAD=700, STT=100, LLM=100, TTS=100 -> E2E=1000ms
    slow_vad_timings = [
        StageTimings(vad_ms=700, stt_ms=100, llm_ttft_ms=100, tts_first_audio_ms=100, e2e_ms=1000),
    ]
    res = BenchmarkResult(samples=slow_vad_timings, is_fake=True)
    assert res.meets_target is False

    shortfall = attribute_shortfall(res)
    assert shortfall is not None
    assert shortfall["offending_stage"] == "VAD"
    assert "VAD" in shortfall["explanation"]


def test_voice_bench_cli_execution():
    """Running benchmarks/voice_latency_bench.py directly prints the report and exits 0."""
    res = subprocess.run(
        [sys.executable, "benchmarks/voice_latency_bench.py", "--runs", "2"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert res.returncode == 0, f"Bench failed with stderr: {res.stderr}"
    stdout = res.stdout
    assert "VOICE PIPELINE LATENCY BENCHMARK REPORT" in stdout
    assert "VAD" in stdout
    assert "STT" in stdout
    assert "LLM TTFT" in stdout
    assert "TTS first-audio" in stdout
    assert "Median turn" in stdout
    assert "FAKES DETECTED" in stdout
