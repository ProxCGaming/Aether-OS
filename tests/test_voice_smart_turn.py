"""S4-M16: Smart Turn endpointing — turn-completion policy behind Endpointer.

Before this fix the Endpointer ended every utterance on trailing
silence alone (700 ms default), so a mid-thought pause ("I was
thinking about …") was cut off and a finished short answer waited
the full silence window. These tests pin:

* a TurnCompletionPolicy seam on the Endpointer (observe_transcript),
* the deterministic local heuristic policy (no model, no cloud),
* dynamic required silence: COMPLETE releases earlier (never below
  the ~120 ms floor), INCOMPLETE holds longer (bounded max),
* no transcript / no policy / sensitivity 0 == legacy behaviour
  byte-for-byte,
* min_speech discard + max_utterance cap preserved,
* smart_turn_sensitivity persistence (UserConfig, E8-5.2 pattern)
  and EndpointerConfig wiring.

All inputs are scripted probabilities at 32 ms frames, as in
tests/test_voice_pipeline.py — no audio, no models, no network.
"""

from __future__ import annotations

import pytest

from aether_engine.config import UserConfig
from aether_engine.voice.vad import (
    EARLY_RELEASE_FLOOR_MS,
    MAX_HOLD_MS,
    Endpointer,
    EndpointerConfig,
    HeuristicTurnCompletionPolicy,
    TurnContext,
    TurnVerdict,
)

FRAME_MS = 32


def _feed_speech(ep: Endpointer, frames: int = 10) -> list:
    """Feed `frames` of speech (320 ms at the default count)."""
    events = [ep.feed(0.9, FRAME_MS) for _ in range(frames)]
    assert events[0] == "speech_started"
    return events


def _silence_until_event(ep: Endpointer, max_frames: int = 200):
    """Feed silence; return (event, silence_ms_at_event or None)."""
    for i in range(1, max_frames + 1):
        ev = ep.feed(0.1, FRAME_MS)
        if ev is not None:
            return ev, i * FRAME_MS
    return None, None


def _events_for_script(ep: Endpointer, probs: list[float]) -> list:
    return [ep.feed(p, FRAME_MS) for p in probs]


# ---------------------------------------------------------------------------
# Policy unit tests: complete vs incomplete battery
# ---------------------------------------------------------------------------

POLICY = HeuristicTurnCompletionPolicy()


def _judge(text: str, speech_ms: int = 2000) -> TurnVerdict:
    return POLICY.judge(TurnContext(transcript=text, speech_ms=speech_ms))


COMPLETE_SAMPLES = [
    "Yes, do it.",
    "Yes.",
    "No, thank you.",
    "Turn off the lights.",
    "Turn off the lights",
    "The weather is nice today.",
    "Please open the window!",
    "What time is it?",
    "I need some help with this",
    "Thank you very much.",
    "Set a timer for five minutes.",
    "That sounds good to me.",
]

INCOMPLETE_SAMPLES = [
    "I was thinking about",
    "I was thinking about the",
    "Turn off the",
    "I need",
    "And then I",
    "Because I was",
    "The",
    "Um, I was thinking about…",
    "So I went to the store and",
    "Can you help me with",
    "I was going to",
    "Wait, let me think about",
    "It depends on",
    "She said that",
    "If you want to",
    "Hmm…",
    "I mean, you know",
]


@pytest.mark.parametrize("text", COMPLETE_SAMPLES)
def test_policy_classifies_complete_samples(text):
    assert _judge(text) == TurnVerdict.COMPLETE, text


@pytest.mark.parametrize("text", INCOMPLETE_SAMPLES)
def test_policy_classifies_incomplete_samples(text):
    assert _judge(text) == TurnVerdict.INCOMPLETE, text


def test_policy_empty_transcript_is_unknown():
    assert _judge("") == TurnVerdict.UNKNOWN
    assert _judge("   ") == TurnVerdict.UNKNOWN


def test_policy_trailing_off_markers_are_incomplete():
    assert _judge("I was about to say...") == TurnVerdict.INCOMPLETE
    assert _judge("Well, I thought that…") == TurnVerdict.INCOMPLETE


# ---------------------------------------------------------------------------
# Endpointer: incomplete held, complete released early
# ---------------------------------------------------------------------------


def test_incomplete_utterance_held_past_classic_silence():
    """'I was thinking about' must NOT end at the classic 700 ms."""
    ep = Endpointer(EndpointerConfig(silence_duration_ms=700))
    _feed_speech(ep)
    ep.observe_transcript("I was thinking about")
    # 700 ms of silence = 22 frames (704 ms): legacy would end here.
    for _ in range(22):
        assert ep.feed(0.1, FRAME_MS) is None
    assert ep.in_speech is True
    # It does end, at the bounded extended hold (total silence =
    # the 704 ms already fed + what the helper feeds from here).
    ev, extra_ms = _silence_until_event(ep)
    assert ev == "utterance_end"
    total_ms = 22 * FRAME_MS + (extra_ms or 0)
    assert 700 < total_ms <= MAX_HOLD_MS + FRAME_MS


def test_complete_utterance_released_faster_than_classic_silence():
    """'Yes, do it.' ends strictly before 700 ms, at/after the floor."""
    ep = Endpointer(EndpointerConfig(silence_duration_ms=700))
    _feed_speech(ep)
    ep.observe_transcript("Yes, do it.")
    ev, silence_ms = _silence_until_event(ep)
    assert ev == "utterance_end"
    assert silence_ms is not None
    assert EARLY_RELEASE_FLOOR_MS <= silence_ms < 700


def test_complete_release_never_below_floor_even_at_max_sensitivity():
    ep = Endpointer(EndpointerConfig(
        silence_duration_ms=700, smart_turn_sensitivity=1.0))
    _feed_speech(ep)
    ep.observe_transcript("Yes, do it.")
    ev, silence_ms = _silence_until_event(ep)
    assert ev == "utterance_end"
    assert silence_ms is not None and silence_ms >= EARLY_RELEASE_FLOOR_MS


def test_incomplete_hold_is_bounded():
    ep = Endpointer(EndpointerConfig(
        silence_duration_ms=700, smart_turn_sensitivity=1.0))
    _feed_speech(ep)
    ep.observe_transcript("I was thinking about")
    ev, silence_ms = _silence_until_event(ep)
    assert ev == "utterance_end"
    # The required window is capped at MAX_HOLD_MS; the event fires
    # on the first frame at/after it, so accrued silence can
    # overshoot by less than one frame (as legacy 700 -> 704 does).
    assert silence_ms is not None and silence_ms <= MAX_HOLD_MS + FRAME_MS


def test_resumed_speech_cancels_hold_and_continues_utterance():
    """Speech resuming during the extended hold keeps the turn alive."""
    ep = Endpointer(EndpointerConfig(silence_duration_ms=700))
    _feed_speech(ep)
    ep.observe_transcript("I was thinking about")
    for _ in range(22):  # classic point, still held
        assert ep.feed(0.1, FRAME_MS) is None
    # User resumes: no boundary event, still the same utterance.
    assert ep.feed(0.9, FRAME_MS) is None
    assert ep.in_speech is True


# ---------------------------------------------------------------------------
# Legacy equivalence: no transcript / no policy / sensitivity 0
# ---------------------------------------------------------------------------

SCRIPT = [0.9] * 10 + [0.1] * 40 + [0.9] * 10 + [0.1] * 40


def test_no_transcript_behaviour_identical_to_legacy():
    legacy = Endpointer(EndpointerConfig(
        silence_duration_ms=700, smart_turn_sensitivity=0.0))
    smart = Endpointer(EndpointerConfig(silence_duration_ms=700))
    # `smart` has a policy + default sensitivity but never observes
    # a transcript, so its event sequence must match legacy exactly.
    assert _events_for_script(smart, SCRIPT) == _events_for_script(legacy, SCRIPT)


def test_no_policy_behaviour_identical_to_legacy():
    legacy = Endpointer(EndpointerConfig(
        silence_duration_ms=700, smart_turn_sensitivity=0.0))
    no_policy = Endpointer(
        EndpointerConfig(silence_duration_ms=700, smart_turn_sensitivity=0.0),
        policy=None,
    )
    no_policy.observe_transcript("Yes, do it.")
    assert _events_for_script(no_policy, SCRIPT) == _events_for_script(legacy, SCRIPT)


def test_sensitivity_zero_is_silence_only_even_with_transcript():
    ep = Endpointer(EndpointerConfig(
        silence_duration_ms=700, smart_turn_sensitivity=0.0))
    _feed_speech(ep)
    ep.observe_transcript("Yes, do it.")
    ev, silence_ms = _silence_until_event(ep)
    assert ev == "utterance_end"
    # First frame at/after 700 ms at 32 ms granularity.
    assert silence_ms == 704


def test_observe_empty_transcript_does_not_change_behaviour():
    ep = Endpointer(EndpointerConfig(silence_duration_ms=700))
    _feed_speech(ep)
    ep.observe_transcript("")
    ev, silence_ms = _silence_until_event(ep)
    assert ev == "utterance_end"
    assert silence_ms == 704


# ---------------------------------------------------------------------------
# Sensitivity effect
# ---------------------------------------------------------------------------


def _release_ms(text: str, sensitivity: float):
    ep = Endpointer(EndpointerConfig(
        silence_duration_ms=700, smart_turn_sensitivity=sensitivity))
    _feed_speech(ep)
    ep.observe_transcript(text)
    ev, silence_ms = _silence_until_event(ep)
    assert ev == "utterance_end"
    return silence_ms


def test_higher_sensitivity_releases_complete_earlier():
    low = _release_ms("Yes, do it.", 0.25)
    default = _release_ms("Yes, do it.", 0.5)
    high = _release_ms("Yes, do it.", 1.0)
    assert high < default < low < 700


def test_higher_sensitivity_holds_incomplete_longer():
    low = _release_ms("I was thinking about", 0.25)
    default = _release_ms("I was thinking about", 0.5)
    high = _release_ms("I was thinking about", 1.0)
    assert 700 < low < default < high <= MAX_HOLD_MS + FRAME_MS


def test_config_sensitivity_is_clamped():
    assert EndpointerConfig(smart_turn_sensitivity=5.0).smart_turn_sensitivity == 1.0
    assert EndpointerConfig(smart_turn_sensitivity=-1.0).smart_turn_sensitivity == 0.0
    assert EndpointerConfig().smart_turn_sensitivity == 0.5


# ---------------------------------------------------------------------------
# Preserved semantics: min-speech discard + max-utterance cap
# ---------------------------------------------------------------------------


def test_min_speech_discard_preserved_with_smart_turn():
    ep = Endpointer(EndpointerConfig(
        silence_duration_ms=700, min_speech_ms=240))
    ep.feed(0.9, FRAME_MS)  # only 32 ms of speech — a blip
    ep.observe_transcript("Yes, do it.")
    ev, _ = _silence_until_event(ep)
    assert ev == "utterance_discarded"


def test_max_utterance_cap_preserved_with_smart_turn():
    ep = Endpointer(EndpointerConfig(
        silence_duration_ms=700, min_speech_ms=240, max_utterance_ms=320))
    _feed_speech(ep, frames=5)
    ep.observe_transcript("I was thinking about")
    # Keep speaking: the cap fires on speech, policy must not delay it.
    events = [ep.feed(0.9, FRAME_MS) for _ in range(10)]
    assert "utterance_end" in events


def test_transcript_resets_between_utterances():
    """A complete verdict from turn 1 must not speed up turn 2."""
    ep = Endpointer(EndpointerConfig(silence_duration_ms=700))
    _feed_speech(ep)
    ep.observe_transcript("Yes, do it.")
    ev, fast_ms = _silence_until_event(ep)
    assert ev == "utterance_end" and fast_ms < 700
    # Second utterance with no transcript: legacy timing.
    _feed_speech(ep)
    ev, silence_ms = _silence_until_event(ep)
    assert ev == "utterance_end"
    assert silence_ms == 704


# ---------------------------------------------------------------------------
# Custom policy seam
# ---------------------------------------------------------------------------


class _AlwaysComplete:
    def judge(self, context: TurnContext) -> TurnVerdict:
        return TurnVerdict.COMPLETE


class _AlwaysUnknown:
    def judge(self, context: TurnContext) -> TurnVerdict:
        return TurnVerdict.UNKNOWN


def test_custom_policy_plugs_in_without_touching_endpointer():
    ep = Endpointer(EndpointerConfig(silence_duration_ms=700), policy=_AlwaysComplete())
    _feed_speech(ep)
    ep.observe_transcript("anything at all here")
    ev, silence_ms = _silence_until_event(ep)
    assert ev == "utterance_end"
    assert silence_ms < 700


def test_unknown_verdict_falls_back_to_silence_only():
    ep = Endpointer(EndpointerConfig(silence_duration_ms=700), policy=_AlwaysUnknown())
    _feed_speech(ep)
    ep.observe_transcript("anything at all here")
    ev, silence_ms = _silence_until_event(ep)
    assert ev == "utterance_end"
    assert silence_ms == 704


def test_policy_receives_speech_duration_context():
    seen = {}

    class _Recorder:
        def judge(self, context: TurnContext) -> TurnVerdict:
            seen["transcript"] = context.transcript
            seen["speech_ms"] = context.speech_ms
            return TurnVerdict.INCOMPLETE

    ep = Endpointer(EndpointerConfig(silence_duration_ms=700), policy=_Recorder())
    _feed_speech(ep, frames=10)  # 320 ms speech
    ep.observe_transcript("I was thinking about")
    ep.feed(0.1, FRAME_MS)
    assert seen["transcript"] == "I was thinking about"
    assert seen["speech_ms"] == 320


# ---------------------------------------------------------------------------
# Pipeline wiring: partials reach the main endpointer only
# ---------------------------------------------------------------------------


async def test_pipeline_partials_are_observed_by_main_endpointer():
    """_emit_partial must call observe_transcript on the main
    endpointer (and the barge-in endpointer must stay un-fed)."""
    from test_voice_pipeline import (
        Emitter,
        FakeTransport,
        make_pipeline,
        run_until,
        speech_then_silence,
    )

    committed = []

    async def on_user_final(text):
        committed.append(text)

    transport = FakeTransport(speech_then_silence())
    pipeline = make_pipeline(
        transport, on_user_final=on_user_final, emitter=Emitter())
    observed_main: list[str] = []
    observed_barge: list[str] = []
    real_main = pipeline._endpointer.observe_transcript
    real_barge = pipeline._barge_endpointer.observe_transcript
    pipeline._endpointer.observe_transcript = (
        lambda t: (observed_main.append(t), real_main(t))[1])
    pipeline._barge_endpointer.observe_transcript = (
        lambda t: (observed_barge.append(t), real_barge(t))[1])

    await pipeline.start()
    await run_until(transport, pipeline, lambda: len(committed) >= 1)
    assert committed == ["the red bicycle"]
    assert observed_main, "main endpointer never observed a partial"
    assert all(t == "the red bicycle" for t in observed_main)
    assert observed_barge == [], "barge-in endpointer must not observe transcripts"


# ---------------------------------------------------------------------------
# Config persistence (E8-5.2 pattern, alongside vad_threshold)
# ---------------------------------------------------------------------------


def test_user_config_smart_turn_defaults():
    cfg = UserConfig()
    assert cfg.smart_turn_sensitivity == 0.5


def test_user_config_smart_turn_roundtrip():
    cfg = UserConfig(smart_turn_sensitivity=0.8)
    d = cfg.to_dict()
    assert d["smart_turn_sensitivity"] == 0.8
    restored = UserConfig.from_dict(d)
    assert restored.smart_turn_sensitivity == 0.8


def test_user_config_smart_turn_clamped_on_load():
    assert UserConfig.from_dict({"smart_turn_sensitivity": 7}).smart_turn_sensitivity == 1.0
    assert UserConfig.from_dict({"smart_turn_sensitivity": -2}).smart_turn_sensitivity == 0.0


def test_user_config_smart_turn_resilience():
    cfg = UserConfig.from_dict({"smart_turn_sensitivity": "invalid"})
    assert cfg.smart_turn_sensitivity == 0.5
    assert UserConfig.from_dict({}).smart_turn_sensitivity == 0.5
