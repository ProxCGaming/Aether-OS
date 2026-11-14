"""S11: EQ module — conversation signals + response adaptation.

Pins the S11 contract from the master-build plan (§4 S11, R5):

* the detector (aether_engine/eq.py) is local and deterministic:
  weighted signals over the recent user turns (+ caller-supplied
  outcomes / voice turn contexts), a small honest state set,
  confidence from accumulated weight, and full evidence on the
  read — hard to trigger accidentally (a single "no", one
  correction, one weak signal change nothing);
* the briefing is BEHAVIOUR guidance only — never the state
  label, never the evidence, never a claim about the user's
  feelings — and empty for neutral or low-confidence reads;
* both prompt paths consume the same module: fast chat (history
  it already holds) and the supervisor routing prompt (session
  history + this task's outcomes), beside the S4 delegation
  briefing;
* the toggle (UserConfig.eq_enabled) off = the pre-S11 prompt
  byte for byte on BOTH paths — with the module OFF, replies
  must not adapt (plan §4 S11 A/B);
* the read is transient: the EQ path never touches the memory
  facade (patched to explode here), so nothing about mood can
  reach long-term memory;
* voice: the talker forwards each turn's read to the session
  owner, and VoicePipeline.note_affect_read records it on the
  session while applying NO prosody (the current TTS has no
  prosody controls — the hook says so rather than faking it).
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aether_common.contracts import Event, EventType
from aether_engine.config import UserConfig
from aether_engine.config_updates import validate_handed_off_updates
from aether_engine.eq import (
    EQ_BRIEFING_MAX_CHARS,
    AffectRead,
    detect_affect,
    eq_briefing,
)
from aether_engine.langgraph.fast_chat import (
    _SYSTEM_PROMPT,
    _eq_read_for_turn,
    run_fast_chat,
)
from aether_engine.providers.base import StreamChunk

# ----------------------------------------------------------------------
# Detector: signals fire on their fixtures
# ----------------------------------------------------------------------

def _signal_names(read):
    return [hit.name for hit in read.signals]


def test_explicit_frustration_fires_strong():
    read = detect_affect([
        "can you fix the export?",
        "this is so frustrating, it still isn't working",
    ])
    assert "explicit_frustration" in _signal_names(read)
    assert read.state == "frustrated"
    assert read.confidence in ("medium", "high")
    # The evidence is explainable: it names the signal and the
    # message it fired on.
    hit = next(h for h in read.signals
               if h.name == "explicit_frustration")
    assert "message 2" in hit.detail


def test_profanity_corroborates_frustration():
    read = detect_affect(["this damn thing broke again"])
    assert "explicit_frustration" in _signal_names(read)
    assert read.state == "frustrated"


def test_single_correction_is_low_confidence_and_no_briefing():
    read = detect_affect(["no, I meant the blue one"])
    assert "correction" in _signal_names(read)
    assert read.state == "frustrated"
    assert read.confidence == "low"
    # Low confidence adapts nothing.
    assert eq_briefing(read) == ""


def test_repeated_corrections_reach_medium():
    read = detect_affect([
        "no, I meant the export folder",
        "that's wrong, I said the other file",
    ])
    assert _signal_names(read).count("correction") == 2
    assert read.state == "frustrated"
    assert read.confidence == "medium"
    assert eq_briefing(read) != ""


def test_repetition_fires_on_near_verbatim_reask():
    read = detect_affect([
        "what is my favourite morning drink?",
        "what is my favourite morning drink?",
    ])
    assert "repetition" in _signal_names(read)


def test_repetition_does_not_fire_when_new_info_is_added():
    read = detect_affect([
        "can you check why the export keeps failing at the render step",
        ("can you check why the export keeps failing at the render step "
         "because it worked yesterday on the same project files"),
    ])
    assert "repetition" not in _signal_names(read)


def test_failure_streak_needs_two_and_grows():
    # A full-sentence reply, so the terse-after-error support
    # signal stays out of this measurement.
    msg = "go ahead and try the other approach please"
    one = detect_affect([msg], outcomes=["succeeded", "failed"])
    assert "failure_streak" not in _signal_names(one)

    two = detect_affect([msg], outcomes=["failed", "failed"])
    assert "failure_streak" in _signal_names(two)
    assert two.confidence == "low"

    three = detect_affect(
        [msg], outcomes=["failed", "failed", "failed"])
    assert "failure_streak" in _signal_names(three)
    assert three.confidence == "medium"

    # A success at the end breaks the streak.
    broken = detect_affect(
        [msg], outcomes=["failed", "failed", "succeeded"])
    assert "failure_streak" not in _signal_names(broken)


def test_caps_emphasis_is_weak_only():
    read = detect_affect(["WHY IS THIS TAKING SO LONG"])
    assert "caps_emphasis" in _signal_names(read)
    # One weak signal alone can never name a state.
    assert read.state == "neutral"
    assert eq_briefing(read) == ""


def test_terse_reply_after_error_is_weak_support():
    alone = detect_affect(["ok"], outcomes=["failed"])
    assert "terse_after_error" in _signal_names(alone)
    assert alone.state == "neutral"

    combined = detect_affect(["ok"], outcomes=["failed", "failed"])
    assert "terse_after_error" in _signal_names(combined)
    assert combined.state == "frustrated"
    assert combined.confidence == "medium"

    # Without a failed outcome, a short reply is just a short reply.
    no_error = detect_affect(["ok"], outcomes=["succeeded"])
    assert "terse_after_error" not in _signal_names(no_error)


def test_confusion_phrases_read_confused():
    read = detect_affect(["I don't understand what that means"])
    assert "confusion" in _signal_names(read)
    assert read.state == "confused"
    assert read.confidence == "low"

    stronger = detect_affect([
        "I don't understand what that means",
        "what does that mean in plain words?",
    ])
    assert stronger.state == "confused"
    assert stronger.confidence == "medium"
    assert "one step at a time" in eq_briefing(stronger)


def test_urgency_reads_rushed():
    read = detect_affect([
        "I need the summary in the next 10 minutes, this is urgent",
    ])
    assert "urgency" in _signal_names(read)
    assert read.state == "rushed"
    briefing = eq_briefing(read)
    assert "lead with the result" in briefing


def test_sustained_praise_reads_engaged():
    read = detect_affect([
        "that's perfect, thanks",
        "great, exactly what I needed",
    ])
    assert read.state == "engaged"
    assert read.confidence == "medium"
    assert "going well" in eq_briefing(read)


def test_frustration_outranks_praise_in_the_same_window():
    read = detect_affect([
        "thanks for the draft",
        "this is frustrating, it still isn't working",
    ])
    assert read.state == "frustrated"


def test_voice_long_pauses_support_confusion_weakly():
    contexts = [{"silence_ms": 5200}, {"silence_ms": 6100},
                {"silence_ms": 300}]
    alone = detect_affect(["the second option"], turn_contexts=contexts)
    assert "long_pauses" in _signal_names(alone)
    assert alone.state == "neutral"  # weak support only, never alone

    combined = detect_affect(
        ["I don't understand the second option"],
        turn_contexts=contexts)
    assert combined.state == "confused"
    assert combined.confidence == "medium"


# ----------------------------------------------------------------------
# Detector: conservative non-triggers stay neutral
# ----------------------------------------------------------------------

def test_single_no_is_not_frustration():
    read = detect_affect(["no"])
    assert read.state == "neutral"
    assert eq_briefing(read) == ""


def test_ordinary_conversation_stays_neutral():
    read = detect_affect([
        "can you summarise this article for me?",
        "make it a bit shorter",
        "now turn it into three bullet points",
    ])
    assert read.state == "neutral"
    assert read.signals == ()
    assert eq_briefing(read) == ""


def test_empty_and_blank_inputs_are_neutral():
    assert detect_affect([]).state == "neutral"
    assert detect_affect(None).state == "neutral"
    assert detect_affect(["", "   "]).state == "neutral"


def test_old_signals_fall_out_of_the_window():
    messages = ["this is so frustrating, it still isn't working"]
    messages += [f"question number {i} about topic {i}" for i in range(6)]
    read = detect_affect(messages)
    assert read.state == "neutral"
    assert "explicit_frustration" not in _signal_names(read)


def test_detector_is_deterministic():
    messages = ["no, I meant the other one", "this is frustrating"]
    assert detect_affect(messages) == detect_affect(messages)


# ----------------------------------------------------------------------
# Briefing: behaviour mapping, never a label
# ----------------------------------------------------------------------

def test_briefing_mapping_per_state():
    frustrated = eq_briefing(detect_affect([
        "no, I meant the export folder",
        "that's wrong, I said the other file",
    ]))
    assert "simplest working path" in frustrated
    assert "already failed" in frustrated

    high = eq_briefing(detect_affect([
        "this is so frustrating, it still isn't working",
        "no, I meant the export, that's not what I asked for",
    ]))
    assert "stopping point" in high
    assert "what will be different" in high

    rushed = eq_briefing(detect_affect([
        "urgent — I need it in the next 5 minutes, hurry",
    ]))
    assert "lead with the result" in rushed


def test_briefing_never_labels_or_claims_feelings():
    reads = [
        detect_affect(["this is so frustrating, it still isn't working",
                       "no, I meant the export, that's not what I asked"]),
        detect_affect(["I don't understand", "what does that mean"]),
        detect_affect(["urgent, in the next 5 minutes, hurry"]),
        detect_affect(["that's perfect, thanks", "great work"]),
    ]
    for read in reads:
        briefing = eq_briefing(read)
        assert len(briefing) <= EQ_BRIEFING_MAX_CHARS
        lowered = briefing.casefold()
        # Guidance only: no state label, no diagnosis, no claim
        # to know or share the user's feelings.
        assert "frustrat" not in lowered
        assert "you seem" not in lowered
        assert "you are feeling" not in lowered
        assert "i understand how you feel" not in lowered
        # And it instructs the model to keep the note invisible.
        assert "do not mention this note" in lowered


def test_briefing_empty_for_neutral_and_low():
    assert eq_briefing(None) == ""
    assert eq_briefing(AffectRead()) == ""
    assert eq_briefing(detect_affect(["hello there"])) == ""
    assert eq_briefing(detect_affect(["no, I meant the blue one"])) == ""


# ----------------------------------------------------------------------
# No persistence: the EQ path never touches the memory facade
# ----------------------------------------------------------------------

def test_eq_path_never_touches_memory_facade():
    from aether_engine.memory import context_assembler, facade

    boom = AssertionError("EQ path touched the memory facade")
    history = [
        {"role": "user", "content": "why does the export keep failing"},
        {"role": "assistant", "content": "I tried the usual fix"},
        {"role": "user",
         "content": "this is so frustrating, it still isn't working"},
    ]
    with patch.object(facade, "backend_or_none", side_effect=boom), \
            patch.object(context_assembler, "assemble_memory_context",
                         new=AsyncMock(side_effect=boom)):
        read = _eq_read_for_turn("try again", history, UserConfig())
        assert read is not None and read.state == "frustrated"

        from aether_engine.langgraph.supervisor_model import (
            _eq_briefing_for_state,
        )
        state = {
            "original_prompt": "try again",
            "session_id": "s-eq-nofacade",
            "messages": [],
            "delegation_log": [],
        }
        with patch(
                "aether_engine.langgraph.fast_chat.load_session_history",
                return_value=history):
            section = _eq_briefing_for_state(
                state, {"configurable": {"user_config": UserConfig()}})
        assert "simplest working path" in section


# ----------------------------------------------------------------------
# Fast chat wiring
# ----------------------------------------------------------------------

class ChunkProvider:
    """Records the messages it was shown; yields fixed chunks
    (the test_epic1_fast_chat / S7 pattern)."""

    def __init__(self, chunks=("answer",)):
        self.chunks = list(chunks)
        self.calls = []

    async def call_stream(self, messages, tools=None, node_name=None,
                          **kwargs):
        self.calls.append([dict(m) for m in messages])
        for text in self.chunks:
            yield StreamChunk(text=text)
        yield StreamChunk(finish_reason="stop")


async def _collect(agen):
    return [event async for event in agen]


_FRUSTRATED_HISTORY = [
    {"role": "user", "content": "why does the export keep failing"},
    {"role": "assistant", "content": "I tried the usual fix"},
    {"role": "user",
     "content": "this is so frustrating, it still isn't working"},
]
_NEUTRAL_HISTORY = [
    {"role": "user", "content": "can you summarise this article?"},
    {"role": "assistant", "content": "Here is a summary"},
    {"role": "user", "content": "make it shorter"},
]


def _config(**kwargs):
    # Memory briefing off so the EQ section is the only variable
    # (S7 has its own A/B pins); the calendar in the test env is
    # empty, as in the S7 suite.
    kwargs.setdefault("fast_chat_memory_enabled", False)
    return UserConfig(**kwargs)


@pytest.mark.asyncio
async def test_fast_chat_carries_eq_guidance_when_frustrated():
    provider = ChunkProvider()
    reads = []
    events = await _collect(run_fast_chat(
        "try again", provider, list(_FRUSTRATED_HISTORY),
        user_config=_config(), on_affect_read=reads.append))

    assert events[-1].type == EventType.TASK_COMPLETED
    messages = provider.calls[0]
    eq_sections = [m for m in messages if m["role"] == "system"
                   and "How to handle this reply" in m["content"]]
    assert len(eq_sections) == 1
    assert "simplest working path" in eq_sections[0]["content"]
    # The guidance rides BEFORE the user turn it adapts.
    assert messages.index(eq_sections[0]) < len(messages) - 1
    assert messages[-1] == {"role": "user", "content": "try again"}
    # The read left the runner through the callback (voice seam).
    assert len(reads) == 1
    assert reads[0].state == "frustrated"


@pytest.mark.asyncio
async def test_fast_chat_toggle_off_is_byte_identical():
    prompt = "try again"
    off_provider = ChunkProvider()
    off_reads = []
    await _collect(run_fast_chat(
        prompt, off_provider, list(_FRUSTRATED_HISTORY),
        user_config=_config(eq_enabled=False),
        on_affect_read=off_reads.append))

    # Plan §4 S11 A/B: with the module OFF, the prompt is the
    # pre-S11 composition byte for byte — persona + history +
    # the user turn, no EQ section, nothing else moved (memory
    # briefing disabled in _config, calendar empty in this env,
    # and "try again" triggers neither the C2 nor the S8 note).
    assert off_provider.calls[0] == [
        {"role": "system", "content": _SYSTEM_PROMPT},
        *_FRUSTRATED_HISTORY,
        {"role": "user", "content": prompt},
    ]
    assert off_reads == []  # off means no read is even computed

    # And ON with a neutral history composes exactly the same
    # shape — the section appears only when there is a read
    # worth adapting to.
    neutral_provider = ChunkProvider()
    await _collect(run_fast_chat(
        "make it shorter", neutral_provider, list(_NEUTRAL_HISTORY),
        user_config=_config(eq_enabled=True)))
    assert neutral_provider.calls[0] == [
        {"role": "system", "content": _SYSTEM_PROMPT},
        *_NEUTRAL_HISTORY,
        {"role": "user", "content": "make it shorter"},
    ]


@pytest.mark.asyncio
async def test_fast_chat_neutral_conversation_is_pre_s11_composition():
    provider = ChunkProvider()
    await _collect(run_fast_chat(
        "make it shorter", provider, list(_NEUTRAL_HISTORY),
        user_config=_config()))
    messages = provider.calls[0]
    assert not any("How to handle this reply" in m["content"]
                   for m in messages if m["role"] == "system")


# ----------------------------------------------------------------------
# Supervisor wiring (routing prompt, beside the S4 read-back)
# ----------------------------------------------------------------------

def _routing_system_text(state, config, history):
    from aether_engine.langgraph import supervisor_model

    with patch.object(supervisor_model, "get_config",
                      return_value=config), \
            patch("aether_engine.langgraph.fast_chat.load_session_history",
                  return_value=history):
        prompt = supervisor_model.supervisor_routing_prompt(state)
    return prompt[0].content


def _supervisor_state():
    return {
        "original_prompt": "just fix it",
        "plan": "p",
        "messages": [],
        "session_id": "s-eq-supervisor",
        "delegation_log": [],
    }


def test_supervisor_prompt_carries_eq_guidance():
    config = {"configurable": {"user_config": UserConfig()}}
    text = _routing_system_text(
        _supervisor_state(), config, _FRUSTRATED_HISTORY)
    assert "How to handle this reply" in text
    assert "simplest working path" in text
    assert "you seem" not in text.casefold()


def test_supervisor_toggle_off_is_byte_identical():
    state = _supervisor_state()
    off = _routing_system_text(
        state,
        {"configurable": {"user_config": UserConfig(eq_enabled=False)}},
        _FRUSTRATED_HISTORY)
    neutral_on = _routing_system_text(
        state,
        {"configurable": {"user_config": UserConfig(eq_enabled=True)}},
        _NEUTRAL_HISTORY)
    # Plan §4 S11 A/B: with the module OFF, the prompt must not
    # adapt — identical to the same run with nothing to adapt to.
    assert off == neutral_on
    assert "How to handle this reply" not in off


def test_supervisor_failure_streak_from_task_outcomes():
    from aether_engine.langgraph.supervisor_model import (
        _eq_briefing_for_state,
    )
    state = _supervisor_state()
    state["messages"] = [
        {"role": "tool", "content": "boom", "status": "error"},
        {"role": "tool", "content": "boom again", "status": "error"},
        {"role": "tool", "content": "boom a third time",
         "status": "error"},
    ]
    with patch("aether_engine.langgraph.fast_chat.load_session_history",
               return_value=[]):
        section = _eq_briefing_for_state(
            state, {"configurable": {"user_config": UserConfig()}})
    assert "simplest working path" in section


# ----------------------------------------------------------------------
# Voice: talker forwarding + pipeline session state (no prosody)
# ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_talker_forwards_on_affect_read():
    from aether_engine.voice.pipeline import fast_chat_talker

    captured = {}

    async def fake_run_fast_chat(text, provider, history, **kwargs):
        captured.update(kwargs)
        yield Event(
            type=EventType.TASK_PROGRESS,
            payload={"text_delta": "hi", "full_text": "hi"})

    callback = MagicMock()
    talker = fast_chat_talker(
        MagicMock(), session_id="s-eq-voice", on_affect_read=callback)
    with patch("aether_engine.langgraph.fast_chat.run_fast_chat",
               fake_run_fast_chat), \
            patch("aether_engine.langgraph.fast_chat.load_session_history",
                  return_value=[]):
        chunks = [chunk async for chunk in talker("hello")]
    assert chunks == ["hi"]
    assert captured["on_affect_read"] is callback
    assert captured["session_id"] == "s-eq-voice"


def test_pipeline_records_affect_read_and_applies_no_prosody():
    from aether_engine.eq import detect_affect
    from aether_engine.voice.pipeline import VoicePipeline

    pipeline = VoicePipeline(
        session_id="s-eq-pipeline",
        vad=MagicMock(), stt=MagicMock(), tts=MagicMock(),
        transport=MagicMock(), talker=MagicMock(), emit=MagicMock())
    assert pipeline.current_affect is None

    read = detect_affect(["this is so frustrating, it isn't working"])
    pipeline.note_affect_read(read)
    assert pipeline.current_affect is read

    # The hook is a recording no-op for synthesis: the TTS stage
    # was never asked to do anything with the read.
    assert pipeline._tts.synthesize.call_count == 0


# ----------------------------------------------------------------------
# Config surface
# ----------------------------------------------------------------------

def test_config_defaults_and_round_trip():
    config = UserConfig()
    assert config.eq_enabled is True

    custom = UserConfig(eq_enabled=False)
    restored = UserConfig.from_dict(custom.to_dict())
    assert restored.eq_enabled is False
    assert restored.to_dict()["eq_enabled"] is False


def test_config_update_validates_eq_enabled_strictly():
    assert validate_handed_off_updates({"eq_enabled": False}) == {
        "eq_enabled": False}
    with pytest.raises(ValueError):
        validate_handed_off_updates({"eq_enabled": "yes"})
