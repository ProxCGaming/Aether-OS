"""S16: speculative read-only work on partial transcripts + its guard.

Pins the S16 contract from the master-build plan (§4 S16, R20):

* THE GUARD IS STRUCTURAL: a SpeculativePreparer holds exactly
  ONE callable — the S7 briefing builder — and nothing else (no
  talker, no emitter, no store, no tool surface). It can only run
  that read-only builder on a text and hold the returned string.
* A speculation starts only on a STABLE partial: two consecutive
  partial transcription passes over a GROWN audio snapshot
  producing the identical text (the honest proxy — the Smart
  Turn endpointer exposes no stability value to the delivery
  half; the first pass is the one the endpointer observed).
* ADOPTION IS MATCH-GATED: the prepared briefing serves a turn
  only when the final transcript matches the speculated text
  under speculation_match (normalised equality, or token-overlap
  coefficient >= 0.8 with >= 3 tokens on the shorter side).
  Mismatch, barge-in, a new utterance, an escalated turn, an
  empty final, or session end discards it silently; take() never
  blocks the turn.
* ONE BUILDER: the speculation runs fast_chat_memory_briefing
  itself, and run_fast_chat adopts its output through
  prepared_memory_briefing instead of recomputing — typed chat
  and unprepared voice turns keep the pre-S16 composition byte
  for byte.
* THE TOGGLE DEFAULTS OFF (voice_speculation_enabled): the
  hybrid recall leg embeds the query through LiteLLM (a cloud
  embedder is a supported config) and the KG/episodic legs
  lazily backfill embedding rows on the read path — effects the
  guard does not allow on text the user has not finished
  saying. Opt-in is the honest default; see
  aether_engine/voice/speculation.py.

All stages are the voice suites' scripted fakes: no audio
devices, no models, no network.
"""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from test_voice_pipeline import Emitter, FakeStt, FakeTalker, FakeTts, frame

from aether_common.contracts import EventType
from aether_engine.config import UserConfig
from aether_engine.config_updates import validate_handed_off_updates
from aether_engine.langgraph import fast_chat as fast_chat_module
from aether_engine.langgraph.fast_chat import run_fast_chat
from aether_engine.providers.base import StreamChunk
from aether_engine.voice.delivery import VoiceDelivery
from aether_engine.voice.pipeline import fast_chat_talker
from aether_engine.voice.speculation import (
    MATCH_OVERLAP_THRESHOLD,
    SpeculativePreparer,
    speculation_match,
)

PARTIAL = "what is the weather"
FINAL = "what is the weather today"
BRIEFING = "PREPARED BRIEFING"


async def wait_until(predicate, timeout=5.0):
    for _ in range(int(timeout / 0.005)):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not reached in time")


class RecordingBuilder:
    """The ONE allowed callable, as a fake: records every text it
    was asked to prepare, returns a fixed briefing."""

    def __init__(self, result=BRIEFING, *, error=None, gate=None, delay=0.0):
        self.result = result
        self.error = error
        self.gate = gate          # asyncio.Event: block until set
        self.delay = delay
        self.calls = []
        self.cancelled = False

    async def __call__(self, text):
        self.calls.append(text)
        try:
            if self.gate is not None:
                await self.gate.wait()
            if self.delay:
                await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.error is not None:
            raise self.error
        return self.result


def make_preparer(builder=None, **kwargs):
    return SpeculativePreparer(
        prepare_briefing=builder or RecordingBuilder(), **kwargs)


# ---------------------------------------------------------------------------
# The match rule (pure)
# ---------------------------------------------------------------------------

class TestMatchRule:
    @pytest.mark.parametrize("speculated,final", [
        ("what is the weather", "what is the weather today"),
        ("What is the weather?", "what is the weather"),
        ("what is the weather today", "what is the weather"),
        # Exactly at the boundary: 4 of the shorter side's 5
        # tokens survive -> 0.8 >= MATCH_OVERLAP_THRESHOLD.
        ("weather in delhi today please", "weather in delhi today now"),
        ("yes", "Yes!"),  # below the token floor, but normalised-equal
    ])
    def test_matches(self, speculated, final):
        assert speculation_match(speculated, final) is True

    @pytest.mark.parametrize("speculated,final", [
        ("what is the weather", "delete all my files please"),
        # Just below the boundary: 3 of 4 tokens -> 0.75 < 0.8.
        ("weather in delhi today", "weather in delhi now"),
        ("yes", "yes please"),       # below the floor, not equal
        ("", "anything at all here"),
        ("what is the weather", ""),
        ("the meeting is moved", "the dentist is moved"),
    ])
    def test_non_matches(self, speculated, final):
        assert speculation_match(speculated, final) is False

    def test_threshold_constant_is_the_documented_boundary(self):
        assert MATCH_OVERLAP_THRESHOLD == 0.8


# ---------------------------------------------------------------------------
# The guard, by construction
# ---------------------------------------------------------------------------

class TestGuardByConstruction:
    def test_allowlist_is_exactly_the_briefing_builder(self):
        preparer = make_preparer()
        assert preparer.allowed_callables == ("prepare_briefing",)
        assert SpeculativePreparer.ALLOWLIST == ("prepare_briefing",)
        # The instance's only callable store holds exactly that
        # one entry — there is no second callable to reach for.
        assert set(preparer._callables) == {"prepare_briefing"}
        # No effect surface exists on the object at all.
        for forbidden in ("emit", "talker", "execute", "tool_registry",
                          "on_user_final", "on_assistant_final", "store"):
            assert not hasattr(preparer, forbidden)

    @pytest.mark.asyncio
    async def test_a_full_cycle_invokes_only_the_builder(self):
        builder = RecordingBuilder()
        emitter = Emitter()
        talker = FakeTalker()
        preparer = make_preparer(builder)
        preparer.note_stable_partial(PARTIAL)
        await wait_until(lambda: preparer._pending is not None
                         and preparer._pending["task"].done())
        assert preparer.take(FINAL) == BRIEFING
        # The builder saw the partial text exactly once; nothing
        # else in the world was touched — no event was emitted,
        # no talker ran, nothing was committed anywhere.
        assert builder.calls == [PARTIAL]
        assert emitter.events == []
        assert talker.prompts == []
        assert preparer.counters == {"started": 1, "used": 1, "discarded": 0}


# ---------------------------------------------------------------------------
# Preparer behaviour
# ---------------------------------------------------------------------------

class TestPreparer:
    @pytest.mark.asyncio
    async def test_match_adopts_and_counts(self):
        preparer = make_preparer()
        preparer.note_stable_partial(PARTIAL)
        await wait_until(lambda: preparer._pending["task"].done())
        assert preparer.take(FINAL) == BRIEFING
        assert preparer.counters == {"started": 1, "used": 1, "discarded": 0}
        # Single-use: a second take finds an empty slot.
        assert preparer.take(FINAL) is None
        assert preparer.counters["used"] == 1

    @pytest.mark.asyncio
    async def test_empty_briefing_is_a_real_result(self):
        # The builder legitimately recalled nothing: "" is adopted
        # (and distinguishable from "no speculation" = None), so
        # the turn skips a recompute that would also find nothing.
        preparer = make_preparer(RecordingBuilder(result=""))
        preparer.note_stable_partial(PARTIAL)
        await wait_until(lambda: preparer._pending["task"].done())
        adopted = preparer.take(FINAL)
        assert adopted == "" and adopted is not None
        assert preparer.counters["used"] == 1

    @pytest.mark.asyncio
    async def test_mismatch_discards_and_recompute_is_the_turns_job(self):
        preparer = make_preparer()
        preparer.note_stable_partial(PARTIAL)
        await wait_until(lambda: preparer._pending["task"].done())
        assert preparer.take("book me a flight to delhi") is None
        assert preparer.counters == {"started": 1, "used": 0, "discarded": 1}

    @pytest.mark.asyncio
    async def test_take_never_waits_for_a_running_speculation(self):
        gate = asyncio.Event()
        builder = RecordingBuilder(gate=gate)
        preparer = make_preparer(builder)
        preparer.note_stable_partial(PARTIAL)
        await wait_until(lambda: builder.calls == [PARTIAL])
        # The final lands while preparation is still running: the
        # turn proceeds without it, and the orphan is cancelled.
        assert preparer.take(FINAL) is None
        assert preparer.counters == {"started": 1, "used": 0, "discarded": 1}
        await wait_until(lambda: builder.cancelled)

    @pytest.mark.asyncio
    async def test_newer_stable_partial_supersedes(self):
        gate = asyncio.Event()
        builder = RecordingBuilder(gate=gate)
        preparer = make_preparer(builder)
        preparer.note_stable_partial("what is the weather")
        await wait_until(lambda: len(builder.calls) == 1)
        preparer.note_stable_partial("what is the traffic")
        assert preparer.counters == {"started": 2, "used": 0, "discarded": 1}
        gate.set()
        await wait_until(lambda: preparer._pending["task"].done())
        assert preparer.take("what is the traffic downtown") == BRIEFING
        assert preparer.counters == {"started": 2, "used": 1, "discarded": 1}

    @pytest.mark.asyncio
    async def test_same_text_noted_twice_starts_once(self):
        preparer = make_preparer()
        preparer.note_stable_partial(PARTIAL)
        preparer.note_stable_partial(PARTIAL)
        await wait_until(lambda: preparer._pending["task"].done())
        preparer.note_stable_partial(PARTIAL)  # completed, still same text
        assert preparer.counters["started"] == 1

    @pytest.mark.asyncio
    async def test_budget_drops_a_slow_preparation(self):
        builder = RecordingBuilder(delay=10.0)
        preparer = make_preparer(builder, budget_s=0.05)
        preparer.note_stable_partial(PARTIAL)
        await wait_until(lambda: preparer._pending["task"].done())
        assert preparer.take(FINAL) is None
        assert preparer.counters == {"started": 1, "used": 0, "discarded": 1}

    @pytest.mark.asyncio
    async def test_expired_speculation_is_not_adopted(self):
        preparer = make_preparer(budget_s=0.05)
        preparer.note_stable_partial(PARTIAL)
        await wait_until(lambda: preparer._pending["task"].done())
        await asyncio.sleep(0.08)  # past the budget, though it finished
        assert preparer.take(FINAL) is None
        assert preparer.counters["discarded"] == 1

    @pytest.mark.asyncio
    async def test_builder_failure_is_swallowed(self):
        preparer = make_preparer(RecordingBuilder(error=RuntimeError("boom")))
        preparer.note_stable_partial(PARTIAL)
        await wait_until(lambda: preparer._pending["task"].done())
        assert preparer.take(FINAL) is None
        assert preparer.counters == {"started": 1, "used": 0, "discarded": 1}

    @pytest.mark.asyncio
    async def test_short_partial_never_starts(self):
        preparer = make_preparer()
        preparer.note_stable_partial("yes please")  # 2 distinct tokens
        preparer.note_stable_partial("okay")
        await asyncio.sleep(0.02)
        assert preparer.counters == {"started": 0, "used": 0, "discarded": 0}
        assert preparer.take("yes please do") is None

    @pytest.mark.asyncio
    async def test_disabled_preparer_is_inert(self):
        builder = RecordingBuilder()
        preparer = make_preparer(builder, enabled=False)
        preparer.note_stable_partial(PARTIAL)
        await asyncio.sleep(0.02)
        assert builder.calls == []
        assert preparer.take(FINAL) is None
        preparer.discard("whatever")  # no pending: a no-op, not a count
        assert preparer.counters == {"started": 0, "used": 0, "discarded": 0}


# ---------------------------------------------------------------------------
# Delivery integration: stability, adoption, boundaries
# ---------------------------------------------------------------------------

def make_delivery(*, buffer, stt, talker, emitter, **kwargs):
    """VoiceDelivery in isolation (the Wave 5 split harness):
    scripted buffer providers, no transport/VAD."""
    return VoiceDelivery(
        session_id="sess-s16",
        stt=stt,
        tts=FakeTts(),
        talker=talker,
        emit=emitter,
        tts_voice="af_heart",
        escalation_ack="On it.",
        utterance_snapshot=lambda: bytes(buffer),
        utterance_extends=lambda snap: bytes(buffer).startswith(snap),
        has_utterance=lambda: len(buffer) > 0,
        observe_transcript=lambda text: None,
        user_speaking=lambda: False,
        is_started=lambda: True,
        is_stopping=lambda: False,
        **kwargs,
    )


async def run_one_partial_pass(delivery):
    delivery.request_partial()
    task = delivery._partial_task
    assert task is not None
    await task


class TestDeliverySpeculation:
    @pytest.mark.asyncio
    async def test_stable_partial_prepares_before_the_final_lands(self):
        buffer = bytearray(frame(1) * 10)
        order = []

        class OrderBuilder(RecordingBuilder):
            async def __call__(self, text):
                order.append(("prepare", text))
                return await super().__call__(text)

        builder = OrderBuilder()
        preparer = make_preparer(builder)
        taken = []

        async def talker(text):
            taken.append((text, preparer.take(text)))
            yield "The reply."

        delivery = make_delivery(
            buffer=buffer,
            stt=FakeStt([PARTIAL, PARTIAL, FINAL]),
            talker=talker, emitter=Emitter(), speculation=preparer)

        await run_one_partial_pass(delivery)      # first sighting
        assert builder.calls == []                # not yet stable
        buffer += frame(1) * 10                   # the utterance grows
        await run_one_partial_pass(delivery)      # identical text, grown audio
        await wait_until(lambda: builder.calls == [PARTIAL])
        # The assembler-equivalent ran BEFORE any final existed.
        order.append(("final",))
        await delivery.finish_utterance(bytes(buffer))
        await delivery._turn_task

        assert order == [("prepare", PARTIAL), ("final",)]
        assert taken == [(FINAL, BRIEFING)]
        assert preparer.counters == {"started": 1, "used": 1, "discarded": 0}

    @pytest.mark.asyncio
    async def test_changing_partials_never_start_speculation(self):
        buffer = bytearray(frame(1) * 10)
        builder = RecordingBuilder()
        preparer = make_preparer(builder)
        delivery = make_delivery(
            buffer=buffer,
            stt=FakeStt(["what is the", "what is the weather in delhi"]),
            talker=FakeTalker(), emitter=Emitter(), speculation=preparer)
        await run_one_partial_pass(delivery)
        buffer += frame(1) * 10
        await run_one_partial_pass(delivery)
        await asyncio.sleep(0.02)
        assert builder.calls == []
        assert preparer.counters["started"] == 0

    @pytest.mark.asyncio
    async def test_repeat_over_an_ungrown_snapshot_is_not_stability(self):
        buffer = bytearray(frame(1) * 10)
        builder = RecordingBuilder()
        preparer = make_preparer(builder)
        delivery = make_delivery(
            buffer=buffer,
            stt=FakeStt([PARTIAL, PARTIAL]),
            talker=FakeTalker(), emitter=Emitter(), speculation=preparer)
        await run_one_partial_pass(delivery)
        await run_one_partial_pass(delivery)  # same audio re-transcribed
        await asyncio.sleep(0.02)
        assert builder.calls == []
        assert preparer.counters["started"] == 0

    async def _delivery_with_pending_speculation(self):
        buffer = bytearray(frame(1) * 10)
        preparer = make_preparer()
        delivery = make_delivery(
            buffer=buffer,
            stt=FakeStt([PARTIAL, PARTIAL]),
            talker=FakeTalker(), emitter=Emitter(), speculation=preparer)
        await run_one_partial_pass(delivery)
        buffer += frame(1) * 10
        await run_one_partial_pass(delivery)
        await wait_until(lambda: preparer._pending is not None
                         and preparer._pending["task"].done())
        assert preparer.counters["started"] == 1
        return delivery, preparer

    @pytest.mark.asyncio
    async def test_barge_in_discards(self):
        delivery, preparer = await self._delivery_with_pending_speculation()
        await delivery.interrupt_output()
        assert preparer.counters == {"started": 1, "used": 0, "discarded": 1}
        assert preparer.take(FINAL) is None

    @pytest.mark.asyncio
    async def test_new_utterance_discards(self):
        delivery, preparer = await self._delivery_with_pending_speculation()
        delivery.begin_utterance()
        assert preparer.counters == {"started": 1, "used": 0, "discarded": 1}
        assert preparer.take(FINAL) is None

    @pytest.mark.asyncio
    async def test_session_end_discards(self):
        delivery, preparer = await self._delivery_with_pending_speculation()
        delivery.cancel_pending()
        assert preparer.counters == {"started": 1, "used": 0, "discarded": 1}
        assert preparer.take(FINAL) is None

    @pytest.mark.asyncio
    async def test_escalated_turn_never_consumes_speculation(self):
        buffer = bytearray(frame(1) * 10)
        preparer = make_preparer()
        talker = FakeTalker()
        launched = []

        async def on_escalation(text):
            launched.append(text)

        delivery = make_delivery(
            buffer=buffer,
            stt=FakeStt([PARTIAL, PARTIAL, FINAL]),
            talker=talker, emitter=Emitter(), speculation=preparer,
            escalation_classifier=AsyncMock(return_value=True),
            on_escalation=on_escalation)
        await run_one_partial_pass(delivery)
        buffer += frame(1) * 10
        await run_one_partial_pass(delivery)
        await wait_until(lambda: preparer._pending is not None
                         and preparer._pending["task"].done())
        await delivery.finish_utterance(bytes(buffer))
        await delivery._turn_task

        # The work launched from the FINAL only; the prepared
        # briefing was discarded unadopted and the talker (the
        # preparer's only consumer) never ran.
        assert launched == [FINAL]
        assert talker.prompts == []
        assert preparer.counters == {"started": 1, "used": 0, "discarded": 1}

    @pytest.mark.asyncio
    async def test_disabled_speculation_is_byte_identical(self):
        async def run(speculation):
            buffer = bytearray(frame(1) * 10)
            emitter = Emitter()
            delivery = make_delivery(
                buffer=buffer,
                stt=FakeStt([PARTIAL, PARTIAL, FINAL]),
                talker=FakeTalker(), emitter=emitter,
                speculation=speculation)
            await run_one_partial_pass(delivery)
            buffer += frame(1) * 10
            await run_one_partial_pass(delivery)
            await delivery.finish_utterance(bytes(buffer))
            await delivery._turn_task
            return [
                (e.type, e.payload.get("text"), e.payload.get("text_delta"),
                 e.payload.get("is_final"))
                for e in emitter.events
            ]

        builder = RecordingBuilder()
        baseline = await run(None)
        disabled = await run(make_preparer(builder, enabled=False))
        assert disabled == baseline
        assert builder.calls == []


# ---------------------------------------------------------------------------
# fast_chat adoption: ONE builder, prepared input adopted verbatim
# ---------------------------------------------------------------------------

class ChunkProvider:
    """Records the messages it was shown; yields fixed chunks
    (the S7 test pattern)."""

    def __init__(self, chunks=("answer",)):
        self.chunks = list(chunks)
        self.calls = []

    async def call_stream(self, messages, tools=None, node_name=None, **kwargs):
        self.calls.append([dict(m) for m in messages])
        for text in self.chunks:
            yield StreamChunk(text=text)
        yield StreamChunk(finish_reason="stop")


async def _collect(agen):
    return [event async for event in agen]


class TestFastChatAdoption:
    @pytest.mark.asyncio
    async def test_prepared_briefing_is_adopted_not_recomputed(self):
        provider = ChunkProvider()
        builder = AsyncMock(side_effect=AssertionError("must not recompute"))
        with patch.object(fast_chat_module, "fast_chat_memory_briefing", builder):
            events = await _collect(run_fast_chat(
                "hello there friend", provider, [],
                user_config=UserConfig(),
                prepared_memory_briefing="PREPARED SECTION"))
        assert events[-1].type == EventType.TASK_COMPLETED
        builder.assert_not_called()
        system_contents = [
            m["content"] for m in provider.calls[0] if m["role"] == "system"]
        assert "PREPARED SECTION" in system_contents

    @pytest.mark.asyncio
    async def test_no_prepared_input_runs_the_builder_as_before(self):
        provider = ChunkProvider()
        builder = AsyncMock(return_value="BUILT SECTION")
        with patch.object(fast_chat_module, "fast_chat_memory_briefing", builder):
            events = await _collect(run_fast_chat(
                "hello there friend", provider, [],
                user_config=UserConfig()))
        assert events[-1].type == EventType.TASK_COMPLETED
        builder.assert_awaited_once()
        assert builder.await_args.args[0] == "hello there friend"
        system_contents = [
            m["content"] for m in provider.calls[0] if m["role"] == "system"]
        assert "BUILT SECTION" in system_contents

    @pytest.mark.asyncio
    async def test_empty_prepared_briefing_skips_the_recompute(self):
        provider = ChunkProvider()
        builder = AsyncMock(side_effect=AssertionError("must not recompute"))
        with patch.object(fast_chat_module, "fast_chat_memory_briefing", builder):
            events = await _collect(run_fast_chat(
                "hello there friend", provider, [],
                user_config=UserConfig(), prepared_memory_briefing=""))
        assert events[-1].type == EventType.TASK_COMPLETED
        builder.assert_not_called()


# ---------------------------------------------------------------------------
# The talker is the only consumer
# ---------------------------------------------------------------------------

class TestTalkerConsumption:
    def _patch_fast_chat(self, captured):
        async def fake_run_fast_chat(text, provider, history, **kwargs):
            captured.update(kwargs)
            yield type("E", (), {
                "type": EventType.TASK_PROGRESS,
                "payload": {"text_delta": "hi"}})()

        return (
            patch.object(fast_chat_module, "run_fast_chat", fake_run_fast_chat),
            patch.object(fast_chat_module, "load_session_history",
                         lambda session_id, text: []),
        )

    @pytest.mark.asyncio
    async def test_talker_hands_the_prepared_briefing_to_fast_chat(self):
        preparer = make_preparer()
        preparer.note_stable_partial(PARTIAL)
        await wait_until(lambda: preparer._pending["task"].done())
        captured = {}
        p1, p2 = self._patch_fast_chat(captured)
        with p1, p2:
            talk = fast_chat_talker(object(), session_id="s",
                                    speculation=preparer)
            deltas = [d async for d in talk(FINAL)]
        assert deltas == ["hi"]
        assert captured["prepared_memory_briefing"] == BRIEFING
        assert preparer.counters["used"] == 1

    @pytest.mark.asyncio
    async def test_talker_without_speculation_passes_none(self):
        captured = {}
        p1, p2 = self._patch_fast_chat(captured)
        with p1, p2:
            talk = fast_chat_talker(object(), session_id="s")
            deltas = [d async for d in talk(FINAL)]
        assert deltas == ["hi"]
        assert captured["prepared_memory_briefing"] is None


# ---------------------------------------------------------------------------
# Wiring + config
# ---------------------------------------------------------------------------

class TestWiringAndConfig:
    def test_pipeline_forwards_the_preparer_to_delivery(self):
        from test_voice_pipeline import FakeTransport, make_pipeline

        preparer = make_preparer()
        pipeline = make_pipeline(FakeTransport(), speculation=preparer)
        assert pipeline._delivery._speculation is preparer

    def test_toggle_defaults_off_and_round_trips(self):
        assert UserConfig().voice_speculation_enabled is False
        enabled = UserConfig(voice_speculation_enabled=True)
        assert enabled.to_dict()["voice_speculation_enabled"] is True
        assert UserConfig.from_dict(
            enabled.to_dict()).voice_speculation_enabled is True
        assert UserConfig.from_dict({}).voice_speculation_enabled is False

    def test_handed_off_update_requires_a_real_boolean(self):
        cleaned = validate_handed_off_updates(
            {"voice_speculation_enabled": True})
        assert cleaned == {"voice_speculation_enabled": True}
        with pytest.raises(ValueError):
            validate_handed_off_updates({"voice_speculation_enabled": "yes"})
