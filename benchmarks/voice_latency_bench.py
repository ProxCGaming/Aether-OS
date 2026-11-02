"""Voice Pipeline Latency Benchmark (Epic 8, E8-5.2).

Measures per-stage latency across test audio samples:
  1. VAD endpointing delay (silence detection + inference)
  2. STT transcription (utterance audio -> transcript text)
  3. LLM TTFT (time-to-first-token / first text delta from talker)
  4. TTS first-audio (time from first sentence ready to first audio chunk emitted)
and computes the end-to-end median turn against the <800 ms target.

TTS first-audio is tracked as its own stage: E8-5.1b's drain-then-emit change
(buffering all sentence chunks before emitting the first chunk to enable exact
heard-word tracking) is suspected of costing first-audio latency, and this bench
is where that shows up.

Any shortfall is reported WITH stage attribution (which stage blew the budget),
never as a bare pass/fail.

Honesty about what is measured:
In its DEFAULT mode this harness ALWAYS runs against injectable
fake/simulated engines with calibrated delays — it never loads a real
model, whether or not the voice runtime dependencies (faster-whisper,
kokoro, silero-vad/onnxruntime, aiortc) are installed. The default-mode
report therefore always labels the run as running on fakes: synthetic
timings must never be presented as measured hardware latency.
(Dependency presence is still probed and reported, so a missing-runtime
environment is called out by name.)

Real-stages mode (``--real``, S4-M14) is the exception, and it is
opt-in: it loads the REAL SileroVad, the real Endpointer (with Smart
Turn at its default sensitivity), the real FasterWhisperStt and the
real KokoroTts, synthesises its fixture speech with that same Kokoro
TTS (a known sentence, so the transcript is known without a
microphone), and measures the local stages for real. It carries
``is_fake=False`` and a REAL label only when every reported stage
genuinely ran on a real model; if any stage cannot load it fails with
the precise reason and emits no report at all — it never falls back
to the fakes. There is no LLM provider in the bench environment, so
real mode does NOT measure (and never simulates) LLM TTFT: its
end-to-end figure is the sum of the measured local stages only, is
labelled as such, and its verdict against the 800 ms target applies
to that local-stage subtotal, not to a full conversational turn.

Usage:
  uv run python benchmarks/voice_latency_bench.py [--runs 5]
  uv run python benchmarks/voice_latency_bench.py --real [--runs 3]
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import io
import os
import platform
import statistics
import struct
import sys
import time
import wave
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from aether_engine.voice import VoiceUnavailableError
from aether_engine.voice.pipeline import VoicePipeline, VoiceTransport
from aether_engine.voice.stt import FasterWhisperStt, SttEngine
from aether_engine.voice.tts import (
    DEFAULT_VOICE,
    KOKORO_SAMPLE_RATE,
    KokoroTts,
    TtsEngine,
)
from aether_engine.voice.vad import (
    SILERO_FRAME_SAMPLES,
    Endpointer,
    EndpointerConfig,
    SileroVad,
    VadEngine,
)

# Reference per-stage guideline budgets (summing to 750 ms < 800 ms target)
TARGET_E2E_MS = 800.0

STAGE_BUDGETS_MS = {
    "VAD": 200.0,
    "STT": 200.0,
    "LLM TTFT": 200.0,
    "TTS first-audio": 150.0,
}


@dataclass
class StageTimings:
    """Recorded latency figures for one conversational voice turn."""
    vad_ms: float
    stt_ms: float
    llm_ttft_ms: float
    tts_first_audio_ms: float
    e2e_ms: float

    def to_dict(self) -> dict[str, float]:
        return {
            "vad_ms": round(self.vad_ms, 2),
            "stt_ms": round(self.stt_ms, 2),
            "llm_ttft_ms": round(self.llm_ttft_ms, 2),
            "tts_first_audio_ms": round(self.tts_first_audio_ms, 2),
            "e2e_ms": round(self.e2e_ms, 2),
        }


@dataclass
class BenchmarkResult:
    """Aggregated benchmark metrics across multiple runs."""
    samples: list[StageTimings]
    is_fake: bool
    fake_reasons: list[str] = field(default_factory=list)
    target_ms: float = TARGET_E2E_MS
    # Real-stages mode (S4-M14). The defaults preserve the fake
    # mode's behaviour and labelling exactly: fake runs measure a
    # (simulated) LLM TTFT and carry no environment block. A real
    # run sets llm_measured=False (no provider in the bench env —
    # the e2e figure is then the local-stage subtotal) and carries
    # the environment it measured on.
    llm_measured: bool = True
    environment: dict[str, Any] | None = None
    fixture_text: str = ""
    stt_transcript: str = ""

    @property
    def sample_count(self) -> int:
        return len(self.samples)

    @property
    def median_vad_ms(self) -> float:
        return statistics.median(s.vad_ms for s in self.samples) if self.samples else 0.0

    @property
    def median_stt_ms(self) -> float:
        return statistics.median(s.stt_ms for s in self.samples) if self.samples else 0.0

    @property
    def median_llm_ttft_ms(self) -> float:
        return statistics.median(s.llm_ttft_ms for s in self.samples) if self.samples else 0.0

    @property
    def median_tts_first_audio_ms(self) -> float:
        return statistics.median(s.tts_first_audio_ms for s in self.samples) if self.samples else 0.0

    @property
    def median_e2e_ms(self) -> float:
        return statistics.median(s.e2e_ms for s in self.samples) if self.samples else 0.0

    @property
    def meets_target(self) -> bool:
        return self.median_e2e_ms < self.target_ms

    def format_report(self) -> str:
        shortfall = attribute_shortfall(self)
        lines = [
            "================================================================================",
            "                 VOICE PIPELINE LATENCY BENCHMARK REPORT (E8-5.2)                ",
            "================================================================================",
        ]

        is_real = not self.is_fake and self.environment is not None

        if self.is_fake:
            lines.extend([
                "",
                "[FAKES DETECTED / RUNNING ON FAKES]",
                "This run measured simulated stages, not real models:",
                *[f"  - {r}" for r in self.fake_reasons],
                "Timings reflect synthetic/injected stage behavior, NOT measured hardware latency.",
                "Real-hardware verification requires Windows QA environment with native runtimes.",
                "--------------------------------------------------------------------------------",
            ])
        elif is_real:
            env = self.environment or {}
            lines.extend([
                "",
                "[REAL — MEASURED ON REAL MODELS]",
                "Every stage below ran on a real local model: Silero VAD +",
                "Endpointer (Smart Turn at default sensitivity), Faster-Whisper",
                "STT, and Kokoro TTS. Fixture speech was synthesised by the real",
                "Kokoro TTS, so no microphone was involved.",
                "",
                "Environment:",
                f"  Platform:       {env.get('platform', 'unknown')}",
                f"  OS:             {env.get('os', 'unknown')}",
                f"  CPU count:      {env.get('cpu_count', 'unknown')}",
                f"  Whisper model:  {env.get('whisper_model', 'unknown')}",
                f"  Kokoro voice:   {env.get('kokoro_voice', 'unknown')}",
                f"  Runs:           {env.get('runs', self.sample_count)}  (samples: {env.get('samples', self.sample_count)})",
            ])
            if self.fixture_text:
                lines.append(f"  Fixture text:   {self.fixture_text!r}")
            if self.stt_transcript:
                lines.append(f"  STT transcript: {self.stt_transcript!r}")
            lines.append("--------------------------------------------------------------------------------")
        else:
            lines.extend([
                "",
                "[NATIVE HARDWARE RUNTIMES DETECTED]",
                "Running with local Silero VAD, Faster-Whisper STT, and Kokoro TTS on local device.",
                "--------------------------------------------------------------------------------",
            ])

        lines.extend([
            f"Samples collected: {self.sample_count}",
            f"Target Turn Latency: < {self.target_ms:.0f} ms",
            "",
            "Stage Breakdown (Medians):",
            "  Stage             | Guideline  | Measured  | Status",
            "  ------------------+------------+-----------+--------------------",
        ])

        stage_data = [
            ("VAD", STAGE_BUDGETS_MS["VAD"], self.median_vad_ms),
            ("STT", STAGE_BUDGETS_MS["STT"], self.median_stt_ms),
        ]
        if self.llm_measured:
            stage_data.append(
                ("LLM TTFT", STAGE_BUDGETS_MS["LLM TTFT"], self.median_llm_ttft_ms))
        stage_data.append(
            ("TTS first-audio", STAGE_BUDGETS_MS["TTS first-audio"], self.median_tts_first_audio_ms))

        for name, budget, measured in stage_data:
            diff = measured - budget
            if diff <= 0:
                status = f"OK ({diff:+.1f} ms)"
            else:
                status = f"OVER BUDGET (+{diff:.1f} ms)"
            lines.append(f"  {name:<17} | {budget:>6.1f} ms  | {measured:>6.1f} ms | {status}")

        if not self.llm_measured:
            lines.append(
                f"  {'LLM TTFT':<17} | {STAGE_BUDGETS_MS['LLM TTFT']:>6.1f} ms  | "
                "     —     | NOT MEASURED")

        if is_real:
            lines.extend([
                "  ------------------+------------+-----------+--------------------",
                f"  End-to-End (local) | {self.target_ms:>6.1f} ms  | {self.median_e2e_ms:>6.1f} ms | "
                + ("PASSED (<800 ms)" if self.meets_target else "SHORTFALL (>=800 ms)"),
                "",
                f"Median turn: {self.median_e2e_ms:.1f} ms (local stages only)",
                "End-to-end is the sum of the measured LOCAL stages only — LLM TTFT not measured (no provider configured in the bench env).",
                "The verdict against the 800 ms target applies to this local-stage subtotal, not to a full conversational turn.",
            ])
        else:
            lines.extend([
                "  ------------------+------------+-----------+--------------------",
                f"  End-to-End Turn   | {self.target_ms:>6.1f} ms  | {self.median_e2e_ms:>6.1f} ms | "
                + ("PASSED (<800 ms)" if self.meets_target else "SHORTFALL (>=800 ms)"),
                "",
                f"Median turn: {self.median_e2e_ms:.1f} ms",
            ])

        if shortfall:
            lines.extend([
                "",
                "Shortfall Analysis (Stage Attribution):",
                f"  Primary Offender: {shortfall['offending_stage']}",
                f"  Stage Overrun:    +{shortfall['stage_delta_ms']:.1f} ms (measured {shortfall['stage_median_ms']:.1f} ms vs budget {shortfall['stage_budget_ms']:.1f} ms)",
                f"  Attribution Note: {shortfall['explanation']}",
            ])
            if shortfall.get("other_overages"):
                lines.append("  Other Stages Over Budget:")
                for s_name, s_delta in shortfall["other_overages"].items():
                    lines.append(f"    - {s_name}: +{s_delta:.1f} ms")
        else:
            lines.extend([
                "",
                "Turn Latency Result: PASSED target (<800 ms).",
            ])

        lines.append("================================================================================")
        return "\n".join(lines)


def detect_runtime_deps() -> tuple[bool, list[str]]:
    """Check whether native voice libraries are installed."""
    reasons: list[str] = []
    if importlib.util.find_spec("onnxruntime") is None:
        reasons.append("onnxruntime is not installed (Silero VAD)")
    if importlib.util.find_spec("faster_whisper") is None:
        reasons.append("faster_whisper is not installed (Whisper STT)")
    if importlib.util.find_spec("kokoro") is None:
        reasons.append("kokoro is not installed (Kokoro TTS)")
    if importlib.util.find_spec("aiortc") is None:
        reasons.append("aiortc is not installed (SmallWebRTC transport)")

    vad_reason = SileroVad().unavailable_reason()
    if vad_reason and vad_reason not in reasons:
        reasons.append(vad_reason)
    stt_reason = FasterWhisperStt().unavailable_reason()
    if stt_reason and stt_reason not in reasons:
        reasons.append(stt_reason)
    tts_reason = KokoroTts().unavailable_reason()
    if tts_reason and tts_reason not in reasons:
        reasons.append(tts_reason)

    is_fake = len(reasons) > 0
    return is_fake, reasons


def attribute_shortfall(result: BenchmarkResult) -> dict[str, Any] | None:
    """Attribute latency shortfall to the stage(s) that exceeded guideline budgets.

    Returns None if end-to-end median turn achieves the <800 ms target.
    Otherwise returns a dictionary with the primary offending stage, overages,
    and architectural explanations.
    """
    if result.meets_target:
        return None

    medians = {
        "VAD": result.median_vad_ms,
        "STT": result.median_stt_ms,
        "TTS first-audio": result.median_tts_first_audio_ms,
    }
    if result.llm_measured:
        # Fake mode measures a (simulated) LLM TTFT, so it takes
        # part in attribution exactly as before. Real mode never
        # measured it (llm_measured=False): blaming or crediting an
        # unmeasured stage would be a fabricated attribution.
        medians["LLM TTFT"] = result.median_llm_ttft_ms

    deltas = {stage: medians[stage] - STAGE_BUDGETS_MS[stage] for stage in medians}
    overages = {stage: delta for stage, delta in deltas.items() if delta > 0}

    if not overages:
        # None individual stage exceeded its guideline, but cumulative total exceeded 800ms
        primary_stage = max(medians, key=medians.get)  # type: ignore[arg-type]
        delta_val = medians[primary_stage] - STAGE_BUDGETS_MS[primary_stage]
        explanation = (
            f"End-to-end turn exceeded {TARGET_E2E_MS} ms due to cumulative latency across stages, "
            f"with {primary_stage} taking the largest fraction ({medians[primary_stage]:.1f} ms)."
        )
    else:
        primary_stage = max(overages, key=overages.get)  # type: ignore[arg-type]
        delta_val = overages[primary_stage]
        if primary_stage == "TTS first-audio":
            explanation = (
                f"TTS first-audio exceeded its guideline by {delta_val:.1f} ms. "
                "E8-5.1b's drain-then-emit change buffers the entire sentence before emitting "
                "the first audio chunk (for exact heard-word tracking), directly adding sentence synthesis "
                "time to first-audio latency."
            )
        elif primary_stage == "VAD":
            explanation = (
                f"VAD endpointing exceeded its guideline by {delta_val:.1f} ms. "
                "Trailing silence duration threshold in config delays utterance completion; "
                "tune silence_duration_ms in Settings to lower this stage."
            )
        elif primary_stage == "STT":
            explanation = (
                f"STT transcription exceeded its guideline by {delta_val:.1f} ms. "
                "Audio transcription duration on current CPU/device exceeded the 200 ms budget."
            )
        else:
            explanation = (
                f"LLM TTFT exceeded its guideline by {delta_val:.1f} ms. "
                "Initial model response token was delayed by provider network round-trip or model queue."
            )

    other_overages = {s: d for s, d in overages.items() if s != primary_stage}

    return {
        "offending_stage": primary_stage,
        "stage_median_ms": medians[primary_stage],
        "stage_budget_ms": STAGE_BUDGETS_MS[primary_stage],
        "stage_delta_ms": delta_val,
        "other_overages": other_overages,
        "explanation": explanation,
    }


# ---------------------------------------------------------------------------
# Test Audio & Synthetic Pipeline for Benchmarking
# ---------------------------------------------------------------------------


def make_pcm16_frame(marker: int, sample_rate: int = 16000) -> bytes:
    """Create a 32 ms PCM16 mono frame (512 samples = 1024 bytes)."""
    samples = 512
    return bytes([marker, 0]) * samples


def generate_test_audio_sample(speech_ms: int = 480, silence_ms: int = 500) -> list[bytes]:
    """Generate a test utterance: speech frames followed by silence frames."""
    frame_ms = 32
    speech_count = max(1, speech_ms // frame_ms)
    silence_count = max(1, silence_ms // frame_ms)
    return [make_pcm16_frame(1)] * speech_count + [make_pcm16_frame(0)] * silence_count


TEST_SAMPLES = [
    {"name": "Short command ('Turn on the desk lamp')", "speech_ms": 320, "silence_ms": 500, "text": "Turn on the desk lamp."},
    {"name": "Medium query ('What is the current system memory status?')", "speech_ms": 640, "silence_ms": 500, "text": "What is the current system memory status?"},
    {"name": "Conversational turn ('Can you explain the difference between the two modules?')", "speech_ms": 960, "silence_ms": 500, "text": "Can you explain the difference between the two modules?"},
]


class BenchSimulatedVad:
    """Simulated VAD engine with realistic calibrated inference delay."""

    def __init__(self, step_delay_s: float = 0.001):
        self.step_delay_s = step_delay_s

    def unavailable_reason(self) -> str | None:
        return None

    async def open(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def probability(self, frame: bytes, sample_rate: int) -> float:
        if self.step_delay_s > 0:
            await asyncio.sleep(self.step_delay_s)
        # Marker byte 1 = speech (prob 0.92); 0 = silence (prob 0.05)
        return 0.92 if frame[0] == 1 else 0.05


class BenchSimulatedStt:
    """Simulated STT engine with calibrated transcription latency."""

    def __init__(self, transcription_delay_ms: float = 160.0):
        self.delay_s = transcription_delay_ms / 1000.0

    def unavailable_reason(self) -> str | None:
        return None

    async def open(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def transcribe(self, audio: bytes, *, sample_rate: int) -> str:
        if self.delay_s > 0:
            await asyncio.sleep(self.delay_s)
        return "the red bicycle"


class BenchSimulatedTts:
    """Simulated TTS engine reflecting sentence synthesis latency and chunk output."""

    def __init__(self, synthesis_delay_ms: float = 120.0):
        self.delay_s = synthesis_delay_ms / 1000.0

    def unavailable_reason(self) -> str | None:
        return None

    async def open(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def synthesize(self, text: str, *, voice: str):
        # Time required to synthesize the sentence audio
        if self.delay_s > 0:
            await asyncio.sleep(self.delay_s)
        # Emits 2 chunks for the sentence
        yield b"CHUNK_1_" + text.encode()
        yield b"CHUNK_2_" + text.encode()


class BenchSimulatedTransport:
    """Feed pre-scripted audio frames."""

    def __init__(self, frames: list[bytes], frame_interval_s: float = 0.002):
        self.frames_list = list(frames)
        self.interval_s = frame_interval_s
        self.closed = False

    def unavailable_reason(self) -> str | None:
        return None

    async def open(self) -> dict | None:
        return {"iceServers": []}

    async def close(self) -> None:
        self.closed = True

    async def frames(self):
        for f in self.frames_list:
            if self.closed:
                break
            yield f
            if self.interval_s > 0:
                await asyncio.sleep(self.interval_s)
        # Idle until stop() lands
        while not self.closed:
            await asyncio.sleep(0.005)


async def run_single_turn_measurement(
    frames: list[bytes],
    *,
    vad_inference_ms: float = 1.0,
    stt_latency_ms: float = 150.0,
    llm_ttft_ms: float = 150.0,
    tts_latency_ms: float = 120.0,
    silence_duration_ms: int = 400,
    vad_threshold: float = 0.5,
) -> StageTimings:
    """Run one voice turn through the actual VoicePipeline and measure each stage.

    Stages measured:
      1. VAD: Time from last speech frame to STT invocation / utterance_end boundary
      2. STT: Time spent in STT transcription
      3. LLM TTFT: Time from prompt received to first yielded text delta
      4. TTS first-audio: Time from first sentence ready to first audio chunk emitted
      5. E2E: Wall-clock time from end of user speech to first audio chunk emitted
    """
    timestamps: dict[str, float] = {}
    last_speech_time = 0.0
    turn_finished_event = asyncio.Event()

    class MeasuredTransport(VoiceTransport):
        def unavailable_reason(self) -> str | None:
            return None

        async def open(self) -> dict | None:
            return {}

        async def close(self) -> None:
            pass

        async def frames(self):
            nonlocal last_speech_time
            for f in frames:
                if f[0] == 1:
                    last_speech_time = time.perf_counter()
                yield f
                await asyncio.sleep(0.001)
            # Stay idle until pipeline stops
            while not turn_finished_event.is_set():
                await asyncio.sleep(0.005)

    class MeasuredVad(VadEngine):
        def unavailable_reason(self) -> str | None:
            return None

        async def open(self) -> None:
            pass

        async def close(self) -> None:
            pass

        async def probability(self, frame: bytes, sample_rate: int) -> float:
            if vad_inference_ms > 0:
                await asyncio.sleep(vad_inference_ms / 1000.0)
            return 0.92 if frame[0] == 1 else 0.05

    class MeasuredStt(SttEngine):
        def unavailable_reason(self) -> str | None:
            return None

        async def open(self) -> None:
            pass

        async def close(self) -> None:
            pass

        async def transcribe(self, audio: bytes, *, sample_rate: int) -> str:
            timestamps["stt_start"] = time.perf_counter()
            if stt_latency_ms > 0:
                await asyncio.sleep(stt_latency_ms / 1000.0)
            timestamps["stt_end"] = time.perf_counter()
            return "The red bicycle is outside."

    class MeasuredTts(TtsEngine):
        def unavailable_reason(self) -> str | None:
            return None

        async def open(self) -> None:
            pass

        async def close(self) -> None:
            pass

        async def synthesize(self, text: str, *, voice: str):
            timestamps["tts_synth_start"] = time.perf_counter()
            if tts_latency_ms > 0:
                await asyncio.sleep(tts_latency_ms / 1000.0)
            timestamps["tts_synth_end"] = time.perf_counter()
            yield b"AUDIO_CHUNK_1"
            yield b"AUDIO_CHUNK_2"

    async def measured_talker(prompt: str):
        timestamps["talker_start"] = time.perf_counter()
        if llm_ttft_ms > 0:
            await asyncio.sleep(llm_ttft_ms / 1000.0)
        timestamps["talker_first_token"] = time.perf_counter()
        yield "The red "
        yield "bicycle is outside. "

    async def measured_emit(event):
        if event.type == "VOICE_OUTPUT_CHUNK" and event.payload.get("audio_chunk") and "first_audio" not in timestamps:
            timestamps["first_audio"] = time.perf_counter()
            turn_finished_event.set()

    pipeline = VoicePipeline(
        session_id="bench-session",
        vad=MeasuredVad(),
        stt=MeasuredStt(),
        tts=MeasuredTts(),
        transport=MeasuredTransport(),
        talker=measured_talker,
        emit=measured_emit,
        endpointer=Endpointer(EndpointerConfig(
            speech_threshold=vad_threshold,
            silence_duration_ms=silence_duration_ms,
        )),
    )

    await pipeline.start()
    run_task = asyncio.ensure_future(pipeline.run())

    try:
        await asyncio.wait_for(turn_finished_event.wait(), timeout=6.0)
    finally:
        await pipeline.stop()
        try:
            await asyncio.wait_for(run_task, timeout=1.0)
        except (TimeoutError, asyncio.CancelledError, Exception):  # noqa: S110, BLE001
            pass

    # Compute individual stage durations
    stt_start = timestamps.get("stt_start", last_speech_time)
    vad_duration_ms = max(0.0, (stt_start - last_speech_time) * 1000.0)
    stt_duration_ms = max(0.0, (timestamps.get("stt_end", stt_start) - stt_start) * 1000.0)
    talker_start = timestamps.get("talker_start", timestamps.get("stt_end", stt_start))
    first_token = timestamps.get("talker_first_token", talker_start)
    llm_ttft_duration_ms = max(0.0, (first_token - talker_start) * 1000.0)
    first_audio = timestamps.get("first_audio", first_token)
    tts_first_audio_ms = max(0.0, (first_audio - first_token) * 1000.0)
    total_e2e_ms = max(0.0, (first_audio - last_speech_time) * 1000.0)

    return StageTimings(
        vad_ms=vad_duration_ms,
        stt_ms=stt_duration_ms,
        llm_ttft_ms=llm_ttft_duration_ms,
        tts_first_audio_ms=tts_first_audio_ms,
        e2e_ms=total_e2e_ms,
    )


def run_voice_latency_bench(
    runs: int = 3,
    *,
    vad_ms: float = 1.0,
    stt_ms: float = 150.0,
    ttft_ms: float = 150.0,
    tts_ms: float = 120.0,
    silence_ms: int = 350,
    vad_threshold: float = 0.5,
) -> BenchmarkResult:
    """Execute the benchmark over all test audio samples across the specified runs."""
    deps_missing, reasons = detect_runtime_deps()
    if not deps_missing:
        # The deps are installed, but the measured stages below are
        # still the simulated Measured* engines with calibrated
        # delays — labelling this "native hardware" would be a
        # placebo. The run stays labelled fake, with the reason
        # saying exactly why.
        reasons = [
            (
                "runtime dependencies are installed, but this harness "
                "measures simulated stages with calibrated delays and "
                "never loads a real model"
            )
        ]
    samples: list[StageTimings] = []

    for _ in range(runs):
        for sample in TEST_SAMPLES:
            frames = generate_test_audio_sample(
                speech_ms=sample["speech_ms"],
                silence_ms=sample["silence_ms"] + 300,
            )
            timing = asyncio.run(run_single_turn_measurement(
                frames,
                vad_inference_ms=vad_ms,
                stt_latency_ms=stt_ms,
                llm_ttft_ms=ttft_ms,
                tts_latency_ms=tts_ms,
                silence_duration_ms=silence_ms,
                vad_threshold=vad_threshold,
            ))
            samples.append(timing)

    return BenchmarkResult(
        samples=samples,
        # Always True: the measured stages are simulated by
        # construction (see the deps_missing note above).
        is_fake=True,
        fake_reasons=reasons,
        target_ms=TARGET_E2E_MS,
    )


# ---------------------------------------------------------------------------
# Real-stages mode (S4-M14)
#
# Everything above stays the fake bench. Everything below loads the
# REAL engines — SileroVad, Endpointer (Smart Turn at default
# sensitivity), FasterWhisperStt, KokoroTts — and measures them.
# The seams (engine factories, fixture generator) exist so tests can
# drive this exact code path with lightweight stand-ins instead of
# downloading models; production callers use the defaults, which are
# the real engines.
# ---------------------------------------------------------------------------

#: The canonical fixture sentence (the fake bench's reply text):
#: known transcript, synthesised by the real Kokoro TTS.
REAL_FIXTURE_SENTENCE = "The red bicycle is outside."

#: Fixture speech must reach the STT stage at the pipeline's rate.
FIXTURE_SAMPLE_RATE = 16000

# Type aliases for the injection seams.
VadFactory = Callable[[], VadEngine]
SttFactory = Callable[[], SttEngine]
TtsFactory = Callable[[], TtsEngine]
# A fixture generator turns the (opened) TTS engine into raw 16 kHz
# PCM16 speech bytes for the fixture sentence.
FixtureGenerator = Callable[..., Awaitable[bytes]]


def collect_environment(
    *,
    whisper_model: str,
    kokoro_voice: str,
    runs: int,
    samples: int,
) -> dict[str, Any]:
    """The environment block a REAL report must carry."""
    return {
        "platform": platform.platform(),
        "os": f"{platform.system()} {platform.release()}",
        "cpu_count": os.cpu_count() or 0,
        "whisper_model": whisper_model,
        "kokoro_voice": kokoro_voice,
        "runs": runs,
        "samples": samples,
    }


def resample_pcm16(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Linear-interpolation resample of PCM16 mono audio."""
    if src_rate == dst_rate or not pcm:
        return pcm
    count = len(pcm) // 2
    src = struct.unpack(f"<{count}h", pcm[: count * 2])
    out_count = round(count * dst_rate / src_rate)
    out: list[int] = []
    for i in range(out_count):
        pos = i * src_rate / dst_rate
        lo = int(pos)
        hi = min(lo + 1, count - 1)
        frac = pos - lo
        value = src[lo] * (1.0 - frac) + src[hi] * frac
        out.append(round(value))
    return struct.pack(f"<{len(out)}h", *out)


def wav_bytes_to_pcm16(wav_bytes: bytes) -> tuple[bytes, int]:
    """Decode a WAV chunk to (raw PCM16 bytes, sample_rate).

    Non-WAV input is treated as raw PCM16 at Kokoro's sample rate
    (the only producer that could hand us headerless audio).
    """
    if wav_bytes[:4] != b"RIFF":
        return wav_bytes, KOKORO_SAMPLE_RATE
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
        rate = wav.getframerate()
        width = wav.getsampwidth()
        frames = wav.readframes(wav.getnframes())
    if width != 2:
        raise VoiceUnavailableError(
            f"real bench: expected PCM16 WAV audio, got sample width {width}")
    return frames, rate


def slice_pcm_into_frames(
    pcm: bytes, frame_samples: int = SILERO_FRAME_SAMPLES,
) -> list[bytes]:
    """Slice PCM16 audio into fixed-size frames (last one padded)."""
    frame_bytes = frame_samples * 2
    frames: list[bytes] = []
    for offset in range(0, len(pcm), frame_bytes):
        chunk = pcm[offset:offset + frame_bytes]
        if len(chunk) < frame_bytes:
            chunk = chunk + b"\x00" * (frame_bytes - len(chunk))
        frames.append(chunk)
    return frames


@dataclass
class FixtureAudio:
    """Synthesised fixture speech, framed for the VAD/endpointer."""

    text: str
    pcm: bytes  # speech only, 16 kHz PCM16 — what STT transcribes
    frames: list[bytes]  # silence + speech + silence, 512-sample frames
    speech_start_frame: int
    speech_end_frame: int  # exclusive: first trailing-silence frame
    sample_rate: int = FIXTURE_SAMPLE_RATE


def build_fixture_frames(
    speech_pcm_16k: bytes,
    *,
    text: str,
    prepend_silence_ms: int = 160,
    append_silence_ms: int = 1024,
    frame_samples: int = SILERO_FRAME_SAMPLES,
) -> FixtureAudio:
    """Frame fixture speech with leading/trailing silence so the
    endpointer's speech-start and trailing-silence paths both run."""
    frame_ms = frame_samples * 1000 // FIXTURE_SAMPLE_RATE
    silence_frame = b"\x00" * (frame_samples * 2)
    prepend = max(0, prepend_silence_ms // frame_ms)
    append = max(1, append_silence_ms // frame_ms)
    speech_frames = slice_pcm_into_frames(speech_pcm_16k, frame_samples)
    frames = [silence_frame] * prepend + speech_frames + [silence_frame] * append
    return FixtureAudio(
        text=text,
        pcm=speech_pcm_16k,
        frames=frames,
        speech_start_frame=prepend,
        speech_end_frame=prepend + len(speech_frames),
    )


async def synthesize_fixture_pcm(
    tts: TtsEngine, *, sentence: str, voice: str,
) -> bytes:
    """Default fixture generator: speak *sentence* with the real
    TTS engine and return its audio as 16 kHz PCM16."""
    pcm_parts: list[bytes] = []
    async for chunk in tts.synthesize(sentence, voice=voice):
        pcm, rate = wav_bytes_to_pcm16(chunk)
        if rate != FIXTURE_SAMPLE_RATE:
            pcm = resample_pcm16(pcm, rate, FIXTURE_SAMPLE_RATE)
        pcm_parts.append(pcm)
    pcm = b"".join(pcm_parts)
    if not pcm:
        raise VoiceUnavailableError(
            "real bench: TTS produced no audio for the fixture sentence")
    return pcm


def _load_real_stage(name: str, factory: Callable[[], Any]) -> Any:
    """Build one real stage, failing honestly.

    A factory (or the engine's own availability probe) that reports
    a problem becomes a VoiceUnavailableError naming the stage and
    the precise reason — never a silent fallback to a fake.
    """
    try:
        engine = factory()
    except VoiceUnavailableError:
        raise
    except Exception as e:  # re-raised honestly below
        raise VoiceUnavailableError(
            f"real bench: {name} stage failed to construct: {e}") from e
    reason = engine.unavailable_reason()
    if reason is not None:
        raise VoiceUnavailableError(f"real bench: {name} stage unavailable: {reason}")
    return engine


async def _open_real_stage(name: str, engine: Any) -> None:
    try:
        await engine.open()
    except VoiceUnavailableError:
        raise
    except Exception as e:  # e.g. a failed model download
        raise VoiceUnavailableError(
            f"real bench: {name} stage failed to load: {e}") from e


async def _measure_vad_endpointing(
    vad: VadEngine,
    endpointer: Endpointer,
    fixture: FixtureAudio,
) -> float:
    """Drive the real Endpointer with real per-frame VAD
    probabilities, frame by frame as the pipeline does, and return
    the ms from the last speech frame being fed to utterance_end.

    "Last speech frame" means the last frame whose real VAD
    probability crossed the speech threshold — the fake bench's
    convention (its transport timestamps the last marker frame) —
    not the fixture buffer's nominal end: synthesised audio may
    itself end in silence, and the acoustic end is what the
    endpointer reacts to.

    Frames are fed back-to-back (the fake bench's convention), so
    this measures the endpointing processing cost — Silero
    inference over the trailing-silence frames plus the state
    machine — not the wall-clock length of the silence window.
    The fixture transcript is observed once speech starts, exactly
    as the pipeline observes STT partials, so the Smart Turn policy
    bends the trailing-silence window for this utterance.
    """
    endpointer.reset()
    last_speech_fed_at: float | None = None
    frame_ms = round(
        len(fixture.frames[0]) / 2 / fixture.sample_rate * 1000) if fixture.frames else 32
    for frame in fixture.frames:
        probability = await vad.probability(frame, fixture.sample_rate)
        event = endpointer.feed(probability, frame_ms)
        if event == "speech_started":
            endpointer.observe_transcript(fixture.text)
        if probability >= endpointer.config.speech_threshold:
            last_speech_fed_at = time.perf_counter()
        if event == "utterance_end":
            if last_speech_fed_at is None:
                last_speech_fed_at = time.perf_counter()
            return max(0.0, (time.perf_counter() - last_speech_fed_at) * 1000.0)
    raise VoiceUnavailableError(
        "real bench: endpointer never emitted utterance_end for the "
        "fixture utterance (VAD probabilities or silence window "
        "did not produce an endpoint)")


async def _measure_stt(
    stt: SttEngine, fixture: FixtureAudio,
) -> tuple[float, str]:
    """Real transcription of the fixture utterance, timed."""
    start = time.perf_counter()
    transcript = await stt.transcribe(
        fixture.pcm, sample_rate=fixture.sample_rate)
    return max(0.0, (time.perf_counter() - start) * 1000.0), (transcript or "").strip()


async def _measure_tts_first_audio(
    tts: TtsEngine, *, text: str, voice: str,
) -> float:
    """Time from synthesize() call to the first audio chunk."""
    start = time.perf_counter()
    async for _chunk in tts.synthesize(text, voice=voice):
        return max(0.0, (time.perf_counter() - start) * 1000.0)
    raise VoiceUnavailableError(
        "real bench: TTS produced no audio chunks for the reply sentence")


async def _run_real_stages_bench_async(
    runs: int,
    *,
    whisper_model: str,
    kokoro_voice: str,
    sentence: str,
    reply_text: str,
    vad_factory: VadFactory,
    stt_factory: SttFactory,
    tts_factory: TtsFactory,
    fixture_generator: FixtureGenerator,
    endpointer_config: EndpointerConfig | None,
) -> BenchmarkResult:
    vad = _load_real_stage("vad", vad_factory)
    stt = _load_real_stage("stt", stt_factory)
    tts = _load_real_stage("tts", tts_factory)
    opened: list[Any] = []
    try:
        for name, engine in (("vad", vad), ("stt", stt), ("tts", tts)):
            await _open_real_stage(name, engine)
            opened.append(engine)

        fixture_pcm = await fixture_generator(
            tts, sentence=sentence, voice=kokoro_voice)
        if not fixture_pcm:
            raise VoiceUnavailableError(
                "real bench: fixture generator produced no audio")
        fixture = build_fixture_frames(fixture_pcm, text=sentence)
        endpointer = Endpointer(endpointer_config or EndpointerConfig())

        samples: list[StageTimings] = []
        transcript = ""
        for _ in range(runs):
            vad_ms = await _measure_vad_endpointing(vad, endpointer, fixture)
            stt_ms, transcript = await _measure_stt(stt, fixture)
            tts_ms = await _measure_tts_first_audio(
                tts, text=reply_text, voice=kokoro_voice)
            samples.append(StageTimings(
                vad_ms=vad_ms,
                stt_ms=stt_ms,
                # Not measured: no provider in the bench env. Zero
                # is a placeholder excluded from attribution and
                # from the report table (llm_measured=False).
                llm_ttft_ms=0.0,
                tts_first_audio_ms=tts_ms,
                e2e_ms=vad_ms + stt_ms + tts_ms,
            ))
    except VoiceUnavailableError:
        raise
    except Exception as e:  # a real stage crashing mid-measurement
        # is still an honest failure, never a REAL-labelled report.
        raise VoiceUnavailableError(
            f"real bench: measurement failed: {type(e).__name__}: {e}") from e
    finally:
        for engine in reversed(opened):
            try:
                await engine.close()
            except Exception:  # noqa: S110, BLE001 — teardown must not mask the result
                pass

    return BenchmarkResult(
        samples=samples,
        # False only because every stage above is a real engine
        # that loaded and ran; any failure raised before this point.
        is_fake=False,
        fake_reasons=[],
        target_ms=TARGET_E2E_MS,
        llm_measured=False,
        environment=collect_environment(
            whisper_model=whisper_model,
            kokoro_voice=kokoro_voice,
            runs=runs,
            samples=len(samples),
        ),
        fixture_text=sentence,
        stt_transcript=transcript,
    )


def run_real_stages_bench(
    runs: int = 3,
    *,
    whisper_model: str = "small",
    kokoro_voice: str = DEFAULT_VOICE,
    sentence: str = REAL_FIXTURE_SENTENCE,
    reply_text: str | None = None,
    vad_factory: VadFactory | None = None,
    stt_factory: SttFactory | None = None,
    tts_factory: TtsFactory | None = None,
    fixture_generator: FixtureGenerator | None = None,
    endpointer_config: EndpointerConfig | None = None,
) -> BenchmarkResult:
    """Measure the REAL local voice stages (S4-M14).

    Loads SileroVad, FasterWhisperStt(``whisper_model``) and
    KokoroTts for real, synthesises the fixture sentence with that
    Kokoro TTS, and per run measures VAD+endpointing, STT, and TTS
    first-audio. LLM TTFT is not measured (no provider in the bench
    environment): the e2e figure is the local-stage subtotal.

    Raises VoiceUnavailableError — with the precise reason — when
    any real stage cannot load; never falls back to the fakes and
    never returns a REAL-labelled result for a failed stage.

    The factory / fixture-generator parameters are the test seam:
    they default to the real engines.
    """
    return asyncio.run(_run_real_stages_bench_async(
        runs,
        whisper_model=whisper_model,
        kokoro_voice=kokoro_voice,
        sentence=sentence,
        reply_text=reply_text if reply_text is not None else sentence,
        vad_factory=vad_factory if vad_factory is not None else SileroVad,
        stt_factory=stt_factory if stt_factory is not None
        else (lambda: FasterWhisperStt(model=whisper_model)),
        tts_factory=tts_factory if tts_factory is not None else KokoroTts,
        fixture_generator=fixture_generator if fixture_generator is not None
        else synthesize_fixture_pcm,
        endpointer_config=endpointer_config,
    ))


def main():
    parser = argparse.ArgumentParser(description="Aether Voice Pipeline Latency Benchmark (E8-5.2)")
    parser.add_argument("--runs", type=int, default=3, help="Number of benchmark repetitions (default: 3)")
    parser.add_argument("--real", action="store_true",
                        help="Measure the REAL local stages (Silero VAD, Faster-Whisper STT, "
                             "Kokoro TTS) instead of the simulated ones (S4-M14). Fails "
                             "honestly, with no report, when a real stage cannot load.")
    parser.add_argument("--whisper-model", type=str, default="small",
                        help="Whisper model size for --real STT (default: small, the production default)")
    parser.add_argument("--kokoro-voice", type=str, default=DEFAULT_VOICE,
                        help=f"Kokoro voice for --real TTS (default: {DEFAULT_VOICE})")
    parser.add_argument("--silence-ms", type=int, default=350, help="Endpointing silence duration in ms (default: 350)")
    parser.add_argument("--vad-ms", type=float, default=1.0, help="Simulated VAD inference latency ms (default: 1.0)")
    parser.add_argument("--stt-ms", type=float, default=150.0, help="Simulated STT transcription latency ms (default: 150.0)")
    parser.add_argument("--ttft-ms", type=float, default=150.0, help="Simulated LLM TTFT latency ms (default: 150.0)")
    parser.add_argument("--tts-ms", type=float, default=120.0, help="Simulated TTS synthesis latency ms (default: 120.0)")
    args = parser.parse_args()

    if args.real:
        try:
            res = run_real_stages_bench(
                runs=args.runs,
                whisper_model=args.whisper_model,
                kokoro_voice=args.kokoro_voice,
            )
        except VoiceUnavailableError as e:
            # Honest failure: name the precise reason on stderr,
            # emit no report (and certainly no REAL-labelled one),
            # and exit nonzero.
            print(f"real voice bench failed: {e}", file=sys.stderr)
            raise SystemExit(2) from e
        print(res.format_report())
        return

    res = run_voice_latency_bench(
        runs=args.runs,
        vad_ms=args.vad_ms,
        stt_ms=args.stt_ms,
        ttft_ms=args.ttft_ms,
        tts_ms=args.tts_ms,
        silence_ms=args.silence_ms,
    )

    print(res.format_report())


if __name__ == "__main__":
    main()
