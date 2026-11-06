"""Wave 4 / E9 remainder — benchmarks/voice_latency_bench.py (E9-V2, V3).

E9-V3: format_report's final else-branch printed
"[NATIVE HARDWARE RUNTIMES DETECTED]" for is_fake=False with no
environment block. Grep-proof of unreachability: BenchmarkResult is
constructed in exactly two production sites — the fake bench
(is_fake=True, always) and the real bench (is_fake=False WITH
environment=collect_environment(...), never None) — plus tests, all
either is_fake=True or environment-carrying. The dead branch is
deleted; a hand-built result in that state now gets no label at all
rather than a fabricated one.

E9-V2: a turn that never produced audio killed the fake bench with a
bare TimeoutError — no report, no stage attribution, against the
harness's stated rule. It now fails as a VoiceUnavailableError
naming the stage (TTS first-audio), exactly the honest-failure idiom
real mode already uses, and the CLI reports it the same way.
"""
import asyncio
import sys

import pytest

import benchmarks.voice_latency_bench as vbench
from aether_engine.voice import VoiceUnavailableError
from benchmarks.voice_latency_bench import BenchmarkResult, StageTimings


def _timings():
    return [StageTimings(vad_ms=10, stt_ms=10, llm_ttft_ms=10,
                         tts_first_audio_ms=10, e2e_ms=40)]


# ---------------------------------------------------------------------------
# E9-V3 — the dead branch stays dead
# ---------------------------------------------------------------------------

def test_v3_no_native_label_for_unreachable_state():
    # is_fake=False with environment=None: no production constructor
    # produces this, and it must not wear a hardware label.
    res = BenchmarkResult(samples=_timings(), is_fake=False)
    report = res.format_report()
    assert "NATIVE HARDWARE RUNTIMES DETECTED" not in report


def test_v3_real_and_fake_labels_survive():
    real = BenchmarkResult(
        samples=_timings(), is_fake=False, llm_measured=False,
        environment={"platform": "test", "cpu_count": 1,
                     "whisper_model": "standin", "kokoro_voice": "v",
                     "runs": 1, "samples": 1})
    assert "REAL — MEASURED ON REAL MODELS" in real.format_report()

    fake = BenchmarkResult(samples=_timings(), is_fake=True,
                           fake_reasons=["simulated stages"])
    assert "FAKES DETECTED" in fake.format_report()


# ---------------------------------------------------------------------------
# E9-V2 — a silent turn fails with stage attribution, not a traceback
# ---------------------------------------------------------------------------

def test_v2_no_first_audio_raises_attributed_failure(monkeypatch):
    monkeypatch.setattr(vbench, "TURN_TIMEOUT_S", 1.0)
    frames = vbench.generate_test_audio_sample(speech_ms=320, silence_ms=500)
    with pytest.raises(VoiceUnavailableError, match="first audio"):
        asyncio.run(vbench.run_single_turn_measurement(
            frames,
            stt_latency_ms=10,
            llm_ttft_ms=10,
            tts_latency_ms=60_000,  # TTS never yields within the window
            silence_duration_ms=100,
        ))


def test_v2_cli_reports_the_failure_and_exits_nonzero(monkeypatch, capsys):
    def _boom(**_kwargs):
        raise VoiceUnavailableError("turn produced no first audio (TTS first-audio)")

    monkeypatch.setattr(vbench, "run_voice_latency_bench", _boom)
    monkeypatch.setattr(sys, "argv", ["voice_latency_bench.py"])
    with pytest.raises(SystemExit) as excinfo:
        vbench.main()
    assert excinfo.value.code != 0
    captured = capsys.readouterr()
    assert "first audio" in captured.err
    assert "VOICE PIPELINE LATENCY BENCHMARK REPORT" not in captured.out
