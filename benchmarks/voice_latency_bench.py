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
This harness ALWAYS runs against injectable fake/simulated engines with
calibrated delays — it never loads a real model, whether or not the voice
runtime dependencies (faster-whisper, kokoro, silero-vad/onnxruntime, aiortc)
are installed. The benchmark report therefore always labels the run as
running on fakes: synthetic timings must never be presented as measured
hardware latency. (Dependency presence is still probed and reported, so a
missing-runtime environment is called out by name.)

Usage:
  uv run python benchmarks/voice_latency_bench.py [--runs 5]
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from aether_engine.voice.pipeline import VoicePipeline, VoiceTransport
from aether_engine.voice.stt import FasterWhisperStt, SttEngine
from aether_engine.voice.tts import KokoroTts, TtsEngine
from aether_engine.voice.vad import Endpointer, EndpointerConfig, SileroVad, VadEngine

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
            ("LLM TTFT", STAGE_BUDGETS_MS["LLM TTFT"], self.median_llm_ttft_ms),
            ("TTS first-audio", STAGE_BUDGETS_MS["TTS first-audio"], self.median_tts_first_audio_ms),
        ]

        for name, budget, measured in stage_data:
            diff = measured - budget
            if diff <= 0:
                status = f"OK ({diff:+.1f} ms)"
            else:
                status = f"OVER BUDGET (+{diff:.1f} ms)"
            lines.append(f"  {name:<17} | {budget:>6.1f} ms  | {measured:>6.1f} ms | {status}")

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
        "LLM TTFT": result.median_llm_ttft_ms,
        "TTS first-audio": result.median_tts_first_audio_ms,
    }

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
            "runtime dependencies are installed, but this harness "
            "measures simulated stages with calibrated delays and "
            "never loads a real model"
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


def main():
    parser = argparse.ArgumentParser(description="Aether Voice Pipeline Latency Benchmark (E8-5.2)")
    parser.add_argument("--runs", type=int, default=3, help="Number of benchmark repetitions (default: 3)")
    parser.add_argument("--silence-ms", type=int, default=350, help="Endpointing silence duration in ms (default: 350)")
    parser.add_argument("--vad-ms", type=float, default=1.0, help="Simulated VAD inference latency ms (default: 1.0)")
    parser.add_argument("--stt-ms", type=float, default=150.0, help="Simulated STT transcription latency ms (default: 150.0)")
    parser.add_argument("--ttft-ms", type=float, default=150.0, help="Simulated LLM TTFT latency ms (default: 150.0)")
    parser.add_argument("--tts-ms", type=float, default=120.0, help="Simulated TTS synthesis latency ms (default: 120.0)")
    args = parser.parse_args()

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
