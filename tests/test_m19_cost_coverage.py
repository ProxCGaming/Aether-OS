"""Finding M19 — cost totals omit real model work.

The usage ledger is supposed to record EVERY model call (PRD A8,
including zero-cost local calls), but four classes of real work never
reached it:

1. ``record_provider_call`` silently DROPPED any call made without a
   usage-attribution context (its early return on an empty context).
2. Intent classification runs in the START_TASK handler BEFORE the
   task pump sets the attribution context, so the classification call
   was dropped by (1) and never attributed to the session.
3. The voice talker runs in the voice pipeline's own task, outside
   the task pump — its calls were dropped by (1) too.
4. Media-library vision analysis and embedding calls call LiteLLM
   directly, bypassing the provider recording hook entirely.

These tests drive the real recording seams (provider wrapper, intent
classifier, voice talker entry, media vision provider, embedding
path) with fake transports and assert the ledger rows exist — with
the right attribution — and that calls which recorded before still
record exactly once with unchanged pricing semantics.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import litellm
import pytest

from aether_engine.cost import ledger as ledger_module
from aether_engine.cost import pricing
from aether_engine.cost.ledger import UsageLedger, record_provider_call, usage_context
from aether_engine.providers.litellm_provider import LiteLLMProvider

# ---------------------------------------------------------------------------
# Fakes / fixtures (mirroring tests/test_usage_ledger.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def global_ledger(tmp_path, monkeypatch):
    """Point the process-wide ledger (the one the recording hook
    uses) at a throwaway database."""
    led = UsageLedger(tmp_path / "usage.db")
    monkeypatch.setattr(ledger_module, "GLOBAL_USAGE_LEDGER", led)
    return led


def _chunk(text=None, finish=None, usage=None):
    delta = SimpleNamespace(
        content=text, reasoning_content=None, thinking=None, tool_calls=None)
    chunk = SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish)])
    if usage is not None:
        chunk.usage = usage
    return chunk


def _usage(prompt_tokens, completion_tokens):
    return SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )


class _Stream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for c in self._chunks:
            yield c


def _gemini_provider(**overrides):
    kwargs = {
        "api_key": "test-key",
        "model": "gemini-3.5-flash",
        "provider_name": "google_gemini",
    }
    kwargs.update(overrides)
    return LiteLLMProvider(**kwargs)


def _fake_stream_acompletion(text="Hello world", usage=None):
    async def fake_acompletion(**kwargs):
        return _Stream([
            _chunk(text, finish="stop", usage=usage or _usage(1000, 500)),
        ])

    return fake_acompletion


async def _collect(provider, messages, node_name="coder"):
    return [c async for c in provider.call_stream(messages, node_name=node_name)]


# ---------------------------------------------------------------------------
# Path 1 — calls with no usage context were dropped by the hook
# ---------------------------------------------------------------------------


class TestNoContextCallsRecord:
    async def test_provider_call_without_context_still_records(
            self, global_ledger, monkeypatch):
        monkeypatch.setattr(litellm, "acompletion", _fake_stream_acompletion())
        provider = _gemini_provider()
        # No usage_context, no set_usage_context: exactly the state of
        # classification / voice / background calls before this fix.
        chunks = await _collect(provider, [{"role": "user", "content": "hi"}])
        assert [c.text for c in chunks if c.text] == ["Hello world"]

        entries = global_ledger.query()["entries"]
        assert len(entries) == 1
        row = entries[0]
        assert row["model_name"] == "gemini-3.5-flash"
        assert row["provider_name"] == "google_gemini"
        assert row["input_tokens"] == 1000
        assert row["output_tokens"] == 500
        # Explicit system attribution: no session/task, marked as such.
        assert row["session_id"] is None
        assert row["task_id"] is None
        assert row["task_class"] == "system"
        in_rate, out_rate = pricing.FALLBACK_PRICE_TABLE["gemini-3.5-flash"]
        assert row["cost_usd"] == pytest.approx(
            1000 / 1_000_000 * in_rate + 500 / 1_000_000 * out_rate)
        assert row["pricing_status"] == "priced"

    async def test_hook_records_unpriced_and_local_without_context(
            self, global_ledger):
        record_provider_call(
            model_name="acme/turbo-9", provider_name="custom_openai",
            input_tokens=100, output_tokens=50)
        record_provider_call(
            model_name="llama3.1", provider_name="ollama",
            input_tokens=120, output_tokens=30)

        entries = global_ledger.query()["entries"]
        assert len(entries) == 2
        unpriced = next(e for e in entries if e["model_name"] == "acme/turbo-9")
        assert unpriced["pricing_status"] == "unpriced"
        assert unpriced["cost_usd"] == 0.0
        assert unpriced["is_local"] == 0
        assert unpriced["task_class"] == "system"
        local = next(e for e in entries if e["model_name"] == "llama3.1")
        assert local["pricing_status"] == "priced"
        assert local["is_local"] == 1
        assert local["cost_usd"] == 0.0
        assert local["task_class"] == "system"


# ---------------------------------------------------------------------------
# Semantics pin — calls that recorded before are unchanged, exactly once
# ---------------------------------------------------------------------------


class TestRecordingSemanticsUnchanged:
    async def test_attributed_call_records_exactly_once(
            self, global_ledger, monkeypatch):
        monkeypatch.setattr(litellm, "acompletion", _fake_stream_acompletion())
        provider = _gemini_provider()
        with usage_context(session_id="s1", task_id="t1", task_class="graph"):
            await _collect(provider, [{"role": "user", "content": "hi"}])
        entries = global_ledger.query()["entries"]
        assert len(entries) == 1
        row = entries[0]
        assert row["session_id"] == "s1"
        assert row["task_id"] == "t1"
        assert row["task_class"] == "graph"
        assert row["agent_name"] == "coder"
        assert row["pricing_status"] == "priced"

    async def test_unattributed_call_records_exactly_once_not_twice(
            self, global_ledger, monkeypatch):
        monkeypatch.setattr(litellm, "acompletion", _fake_stream_acompletion())
        provider = _gemini_provider()
        await _collect(provider, [{"role": "user", "content": "hi"}])
        assert len(global_ledger.query()["entries"]) == 1

    def test_pricing_semantics_with_context_unchanged(self, global_ledger):
        with usage_context(session_id="s1", task_id="t1"):
            record_provider_call(
                model_name="acme/turbo-9", provider_name="custom_openai",
                input_tokens=100, output_tokens=50)
            record_provider_call(
                model_name="llama3.1", provider_name="ollama",
                input_tokens=120, output_tokens=30)
        entries = global_ledger.query()["entries"]
        assert len(entries) == 2
        unpriced = next(e for e in entries if e["model_name"] == "acme/turbo-9")
        assert unpriced["pricing_status"] == "unpriced"
        assert unpriced["cost_usd"] == 0.0
        assert unpriced["is_local"] == 0
        local = next(e for e in entries if e["model_name"] == "llama3.1")
        assert local["is_local"] == 1
        assert local["cost_usd"] == 0.0
        assert local["pricing_status"] == "priced"


# ---------------------------------------------------------------------------
# Path 2 — intent classification (runs before the task pump exists)
# ---------------------------------------------------------------------------


class TestIntentClassificationRecords:
    async def test_classification_call_records_without_context(
            self, global_ledger, monkeypatch):
        from aether_engine.routing.intent import classify_intent

        monkeypatch.setattr(
            litellm, "acompletion",
            _fake_stream_acompletion(text="CHAT", usage=_usage(50, 5)))
        provider = _gemini_provider()
        # The START_TASK handler's pre-fix call shape: no usage
        # context has been set yet (the pump sets it later).
        decision = await classify_intent(
            "tell me a story about a dragon", provider=provider)
        assert decision.source == "model"  # the model really was consulted

        entries = global_ledger.query()["entries"]
        assert len(entries) == 1
        row = entries[0]
        assert row["agent_name"] == "intent_classifier"
        assert row["input_tokens"] == 50
        assert row["task_class"] == "system"

    async def test_classification_call_attributes_to_session_when_scoped(
            self, global_ledger, monkeypatch):
        from aether_engine.routing.intent import classify_intent

        monkeypatch.setattr(
            litellm, "acompletion",
            _fake_stream_acompletion(text="CHAT", usage=_usage(50, 5)))
        provider = _gemini_provider()
        # The handler's post-fix shape: the classification runs under
        # the session's usage context.
        with usage_context(session_id="sess-42"):
            decision = await classify_intent(
                "tell me a story about a dragon", provider=provider)
        assert decision.source == "model"

        entries = global_ledger.query(session_id="sess-42")["entries"]
        assert len(entries) == 1
        assert entries[0]["agent_name"] == "intent_classifier"
        assert entries[0]["session_id"] == "sess-42"


# ---------------------------------------------------------------------------
# Path 3 — the voice talker (runs outside the task pump)
# ---------------------------------------------------------------------------


class TestVoiceTalkerRecords:
    @pytest.fixture
    def talker(self, monkeypatch):
        from aether_engine.langgraph import fast_chat as fast_chat_module
        from aether_engine.voice.pipeline import fast_chat_talker

        monkeypatch.setattr(
            fast_chat_module, "load_session_history", lambda sid, text: [])
        monkeypatch.setattr(
            fast_chat_module, "_record_message_point", AsyncMock())
        monkeypatch.setattr(
            litellm, "acompletion",
            _fake_stream_acompletion(text="A red bicycle.", usage=_usage(200, 20)))
        provider = _gemini_provider()
        return fast_chat_talker(provider, session_id="voice-s1")

    async def test_talker_call_records_without_pump_context(
            self, global_ledger, talker):
        # The pipeline task's pre-fix state: no usage context at all.
        deltas = [d async for d in talker("what do you see?")]
        assert "".join(deltas) == "A red bicycle."

        entries = global_ledger.query()["entries"]
        assert len(entries) == 1
        row = entries[0]
        assert row["agent_name"] == "fast_chat"
        assert row["input_tokens"] == 200
        assert row["task_class"] == "system"

    async def test_talker_call_attributes_to_voice_session_when_scoped(
            self, global_ledger, talker):
        # The pipeline runner's post-fix shape: the whole run carries
        # the voice session's attribution.
        with usage_context(session_id="voice-s1"):
            deltas = [d async for d in talker("what do you see?")]
        assert "".join(deltas) == "A red bicycle."

        entries = global_ledger.query(session_id="voice-s1")["entries"]
        assert len(entries) == 1
        assert entries[0]["agent_name"] == "fast_chat"
        assert entries[0]["session_id"] == "voice-s1"


# ---------------------------------------------------------------------------
# Path 4a — media-library vision analysis (direct LiteLLM, no hook)
# ---------------------------------------------------------------------------


class TestMediaVisionRecords:
    async def test_vision_analysis_call_records(
            self, global_ledger, monkeypatch, tmp_path):
        from aether_engine.config import UserConfig
        from aether_engine.media.analyzer import LiteLLMVisionProvider

        async def fake_acompletion(**kwargs):
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(
                    content='{"summary": "a red bicycle", "tags": ["bicycle"]}'))],
                usage=_usage(300, 120),
            )

        monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
        image = tmp_path / "IMG_001.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)

        provider = LiteLLMVisionProvider(config=UserConfig(
            default_provider="google_gemini",
            default_model="gemini-3.5-flash",
            auxiliary_models={},
        ))
        result = await provider.analyze_image(str(image))
        assert result["summary"] == "a red bicycle"

        entries = global_ledger.query()["entries"]
        assert len(entries) == 1
        row = entries[0]
        assert row["model_name"] == "gemini-3.5-flash"
        assert row["provider_name"] == "google_gemini"
        assert row["input_tokens"] == 300
        assert row["output_tokens"] == 120
        assert row["agent_name"] == "media_analyzer"
        assert row["task_class"] == "system"


# ---------------------------------------------------------------------------
# Path 4b — embedding calls (direct LiteLLM, no hook)
# ---------------------------------------------------------------------------


class TestEmbeddingRecords:
    async def test_embedding_call_records_local_zero_cost_row(
            self, global_ledger, monkeypatch):
        from aether_engine.config import UserConfig
        from aether_engine.memory import episodic

        async def fake_aembedding(**kwargs):
            return SimpleNamespace(
                data=[{"embedding": [0.1, 0.2, 0.3]}],
                usage=SimpleNamespace(prompt_tokens=42, total_tokens=42),
            )

        monkeypatch.setattr(litellm, "aembedding", fake_aembedding)
        monkeypatch.setattr(episodic, "_embed_unavailable_until", 0.0)

        vector = await episodic._embed_text(
            "a red bicycle", config=UserConfig())
        assert vector == [0.1, 0.2, 0.3]

        entries = global_ledger.query()["entries"]
        assert len(entries) == 1
        row = entries[0]
        assert row["model_name"] == "ollama/qwen3-embedding:0.6b"
        assert row["provider_name"] == "ollama"
        assert row["is_local"] == 1
        assert row["cost_usd"] == 0.0
        assert row["pricing_status"] == "priced"
        assert row["input_tokens"] == 42
        assert row["output_tokens"] == 0
        assert row["agent_name"] == "embedding"
        assert row["task_class"] == "system"

    async def test_failed_embedding_writes_no_row(
            self, global_ledger, monkeypatch):
        from aether_engine.config import UserConfig
        from aether_engine.memory import episodic

        async def fake_aembedding(**kwargs):
            raise ConnectionError("ollama down")

        monkeypatch.setattr(litellm, "aembedding", fake_aembedding)
        monkeypatch.setattr(episodic, "_embed_unavailable_until", 0.0)

        vector = await episodic._embed_text(
            "a red bicycle", config=UserConfig())
        assert vector is None
        assert global_ledger.query()["entries"] == []
