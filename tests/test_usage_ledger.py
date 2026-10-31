"""Epic 8, E8-A8.1a — cost ledger + pricing + provider recording hook.

The ledger is the persistent per-call record every cost surface reads:
one row per completed model call (cloud and local), priced via
LiteLLM's completion_cost() with a fallback price table, unpriced
models recorded honestly as 'unpriced' (never silently $0), and local
calls at $0.00 with is_local = 1. The header popover and the live
TASK_COST_UPDATE pushes must provably match these rows.
"""

from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace

import litellm
import pytest

from aether_common.contracts import EventType
from aether_engine.cost import ledger as ledger_module
from aether_engine.cost import pricing
from aether_engine.cost.ledger import UsageLedger, usage_context
from aether_engine.event_bus import EventBus
from aether_engine.providers.base import RateLimitError
from aether_engine.providers.litellm_provider import FallbackModel, LiteLLMProvider

# ---------------------------------------------------------------------------
# Fakes / fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ledger(tmp_path):
    return UsageLedger(tmp_path / "usage.db")


@pytest.fixture
def global_ledger(tmp_path, monkeypatch):
    """Point the process-wide ledger (the one the provider hook uses)
    at a throwaway database."""
    led = UsageLedger(tmp_path / "usage.db")
    monkeypatch.setattr(ledger_module, "GLOBAL_USAGE_LEDGER", led)
    return led


def _chunk(text=None, finish=None, usage=None):
    delta = SimpleNamespace(content=text, reasoning_content=None, thinking=None, tool_calls=None)
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


async def _collect(provider, messages, node_name="coder"):
    return [c async for c in provider.call_stream(messages, node_name=node_name)]


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------


class TestPricing:
    def test_local_ollama_call_is_free(self):
        result = pricing.price_call("llama3.1", "ollama", 1000, 500)
        assert result.is_local is True
        assert result.cost_usd == 0.0
        assert result.status == "priced"

    def test_local_litellm_model_prefix_is_free(self):
        result = pricing.price_call("ollama/llama3.1", "custom_openai", 1000, 500)
        assert result.is_local is True
        assert result.cost_usd == 0.0

    def test_gemini_35_flash_priced_via_fallback_table(self):
        # Verified against litellm 1.86: 'gemini/gemini-3.5-flash' is
        # NOT in litellm's cost map (completion_cost raises), so the
        # fallback table is what prices the default model.
        result = pricing.price_call("gemini/gemini-3.5-flash", "google_gemini", 1_000_000, 1_000_000)
        in_rate, out_rate = pricing.FALLBACK_PRICE_TABLE["gemini-3.5-flash"]
        assert result.is_local is False
        assert result.status == "priced"
        assert result.cost_usd == pytest.approx(in_rate + out_rate)

    def test_completion_cost_path_is_used_when_litellm_knows_the_model(self, monkeypatch):
        monkeypatch.setattr(litellm, "completion_cost", lambda **kw: 0.4242)
        result = pricing.price_call("acme/turbo-9", "custom_openai", 10, 10)
        assert result.status == "priced"
        assert result.cost_usd == pytest.approx(0.4242)

    def test_unknown_model_is_unpriced_never_silently_zero(self, monkeypatch):
        def _raise(**kw):
            raise litellm.exceptions.NotFoundError("no price", "acme", "acme/turbo-9")

        monkeypatch.setattr(litellm, "completion_cost", _raise)
        result = pricing.price_call("acme/turbo-9", "custom_openai", 100, 50)
        assert result.status == "unpriced"
        assert result.cost_usd == 0.0
        assert result.is_local is False

    def test_estimated_tokens_price_as_estimated(self):
        result = pricing.price_call(
            "gemini/gemini-3.5-flash", "google_gemini", 1000, 500, estimated=True)
        in_rate, out_rate = pricing.FALLBACK_PRICE_TABLE["gemini-3.5-flash"]
        assert result.status == "estimated"
        assert result.cost_usd == pytest.approx(
            1000 / 1_000_000 * in_rate + 500 / 1_000_000 * out_rate)


# ---------------------------------------------------------------------------
# Ledger storage + queries
# ---------------------------------------------------------------------------


class TestLedger:
    def test_schema_version_is_1(self, ledger, tmp_path):
        ledger.record_call(
            model_name="gemini-3.5-flash", provider_name="google_gemini",
            input_tokens=1, output_tokens=1, cost_usd=0.0,
            is_local=False, pricing_status="priced")
        with sqlite3.connect(tmp_path / "usage.db") as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == 1

    def test_record_and_query_round_trip(self, ledger):
        row = ledger.record_call(
            model_name="gemini-3.5-flash", provider_name="google_gemini",
            input_tokens=1000, output_tokens=500, cost_usd=0.00155,
            is_local=False, pricing_status="priced",
            session_id="s1", task_id="t1", agent_name="coder",
            task_class="graph")
        assert row["id"]
        assert row["total_tokens"] == 1500

        result = ledger.query(session_id="s1")
        assert len(result["entries"]) == 1
        entry = result["entries"][0]
        assert entry["model_name"] == "gemini-3.5-flash"
        assert entry["session_id"] == "s1"
        assert entry["task_id"] == "t1"
        assert entry["agent_name"] == "coder"
        totals = result["totals"]
        assert totals["total_cost_usd"] == pytest.approx(0.00155)
        assert totals["input_tokens"] == 1000
        assert totals["output_tokens"] == 500
        assert totals["by_model"]["gemini-3.5-flash"]["cost_usd"] == pytest.approx(0.00155)
        assert totals["by_model"]["gemini-3.5-flash"]["calls"] == 1
        assert totals["by_agent"]["coder"]["cost_usd"] == pytest.approx(0.00155)

    def test_totals_match_entry_sums_across_models_and_agents(self, ledger):
        ledger.record_call(model_name="gemini-3.5-flash", provider_name="google_gemini",
                           input_tokens=100, output_tokens=50, cost_usd=0.10,
                           is_local=False, pricing_status="priced",
                           session_id="s1", task_id="t1", agent_name="coder")
        ledger.record_call(model_name="gpt-4o-mini", provider_name="openai",
                           input_tokens=200, output_tokens=80, cost_usd=0.20,
                           is_local=False, pricing_status="priced",
                           session_id="s1", task_id="t1", agent_name="planner")
        ledger.record_call(model_name="llama3.1", provider_name="ollama",
                           input_tokens=300, output_tokens=90, cost_usd=0.0,
                           is_local=True, pricing_status="priced",
                           session_id="s1", task_id="t2", agent_name="coder")
        ledger.record_call(model_name="gemini-3.5-flash", provider_name="google_gemini",
                           input_tokens=999, output_tokens=999, cost_usd=9.99,
                           is_local=False, pricing_status="priced",
                           session_id="other-session", task_id="t9", agent_name="coder")

        result = ledger.query(session_id="s1")
        entries = result["entries"]
        totals = result["totals"]
        assert len(entries) == 3
        assert totals["total_cost_usd"] == pytest.approx(sum(e["cost_usd"] for e in entries))
        assert totals["input_tokens"] == sum(e["input_tokens"] for e in entries)
        assert totals["output_tokens"] == sum(e["output_tokens"] for e in entries)
        assert totals["by_model"]["llama3.1"]["cost_usd"] == 0.0
        assert totals["by_agent"]["coder"]["calls"] == 2

        by_task = ledger.query(task_id="t1")
        assert len(by_task["entries"]) == 2
        assert ledger.task_total("t1") == pytest.approx(0.30)
        assert ledger.session_total("s1") == pytest.approx(0.30)

    def test_time_window_filter(self, ledger):
        ledger.record_call(model_name="m", provider_name="p", input_tokens=1,
                           output_tokens=1, cost_usd=1.0, is_local=False,
                           pricing_status="priced", timestamp=1000)
        ledger.record_call(model_name="m", provider_name="p", input_tokens=1,
                           output_tokens=1, cost_usd=2.0, is_local=False,
                           pricing_status="priced", timestamp=2000)
        result = ledger.query(start_ts=1500, end_ts=2500)
        assert [e["cost_usd"] for e in result["entries"]] == [2.0]

    def test_local_and_unpriced_rows_are_recorded_honestly(self, ledger):
        ledger.record_call(model_name="llama3.1", provider_name="ollama",
                           input_tokens=10, output_tokens=5, cost_usd=0.0,
                           is_local=True, pricing_status="priced",
                           session_id="s1", task_id="t1", agent_name="fast_chat")
        ledger.record_call(model_name="acme/turbo-9", provider_name="custom_openai",
                           input_tokens=10, output_tokens=5, cost_usd=0.0,
                           is_local=False, pricing_status="unpriced",
                           session_id="s1", task_id="t1", agent_name="fast_chat")
        entries = ledger.query(session_id="s1")["entries"]
        local_row = next(e for e in entries if e["model_name"] == "llama3.1")
        unpriced_row = next(e for e in entries if e["model_name"] == "acme/turbo-9")
        assert local_row["is_local"] == 1
        assert local_row["cost_usd"] == 0.0
        assert unpriced_row["is_local"] == 0
        assert unpriced_row["pricing_status"] == "unpriced"


# ---------------------------------------------------------------------------
# Provider recording hook
# ---------------------------------------------------------------------------


class TestProviderHook:
    def test_completed_call_writes_one_attributed_row(self, global_ledger, monkeypatch):
        async def fake_acompletion(**kwargs):
            return _Stream([
                _chunk("Hello"),
                _chunk(" world", finish="stop", usage=_usage(1000, 500)),
            ])

        monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
        provider = _gemini_provider()
        with usage_context(session_id="s1", task_id="t1", task_class="graph"):
            chunks = asyncio.run(_collect(provider, [{"role": "user", "content": "hi"}]))
        assert [c.text for c in chunks if c.text] == ["Hello", " world"]

        entries = global_ledger.query(session_id="s1")["entries"]
        assert len(entries) == 1
        row = entries[0]
        in_rate, out_rate = pricing.FALLBACK_PRICE_TABLE["gemini-3.5-flash"]
        assert row["model_name"] == "gemini-3.5-flash"
        assert row["provider_name"] == "google_gemini"
        assert row["input_tokens"] == 1000
        assert row["output_tokens"] == 500
        assert row["cost_usd"] == pytest.approx(
            1000 / 1_000_000 * in_rate + 500 / 1_000_000 * out_rate)
        assert row["pricing_status"] == "priced"
        assert row["task_id"] == "t1"
        assert row["agent_name"] == "coder"
        assert row["task_class"] == "graph"

    def test_local_call_records_zero_cost_row(self, global_ledger, monkeypatch):
        async def fake_acompletion(**kwargs):
            return _Stream([_chunk("ok", finish="stop", usage=_usage(120, 30))])

        monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
        provider = LiteLLMProvider(api_key="ollama", model="llama3.1", provider_name="ollama")
        with usage_context(session_id="s2", task_id="t2"):
            asyncio.run(_collect(provider, [{"role": "user", "content": "hi"}],
                                 node_name="fast_chat"))
        entries = global_ledger.query(session_id="s2")["entries"]
        assert len(entries) == 1
        assert entries[0]["is_local"] == 1
        assert entries[0]["cost_usd"] == 0.0

    def test_fallback_success_records_the_fallback_model(self, global_ledger, monkeypatch):
        calls = []

        async def fake_acompletion(**kwargs):
            calls.append(kwargs["model"])
            if len(calls) == 1:
                raise litellm.RateLimitError("rate limited", "google_gemini", "gemini-3.5-flash")
            return _Stream([_chunk("hi", finish="stop", usage=_usage(100, 40))])

        monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
        provider = _gemini_provider(fallback_models=[
            FallbackModel(model="gpt-4o-mini", api_key="test-key", provider_name="openai"),
        ])
        with usage_context(session_id="s3", task_id="t3"):
            asyncio.run(_collect(provider, [{"role": "user", "content": "hi"}]))
        entries = global_ledger.query(session_id="s3")["entries"]
        assert len(entries) == 1
        assert entries[0]["model_name"] == "gpt-4o-mini"
        assert entries[0]["provider_name"] == "openai"

    def test_missing_usage_falls_back_to_labelled_estimate(self, global_ledger, monkeypatch):
        async def fake_acompletion(**kwargs):
            return _Stream([_chunk("some answer text", finish="stop")])

        monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
        monkeypatch.setattr(pricing, "estimate_token_counts", lambda **kw: (111, 22))
        provider = _gemini_provider()
        with usage_context(session_id="s4", task_id="t4"):
            asyncio.run(_collect(provider, [{"role": "user", "content": "hi"}]))
        entries = global_ledger.query(session_id="s4")["entries"]
        assert len(entries) == 1
        assert entries[0]["input_tokens"] == 111
        assert entries[0]["output_tokens"] == 22
        assert entries[0]["pricing_status"] == "estimated"

    def test_failed_call_writes_no_row(self, global_ledger, monkeypatch):
        async def fake_acompletion(**kwargs):
            raise litellm.RateLimitError("rate limited", "google_gemini", "gemini-3.5-flash")

        monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
        provider = _gemini_provider()
        with usage_context(session_id="s5", task_id="t5"), pytest.raises(RateLimitError):
            asyncio.run(_collect(provider, [{"role": "user", "content": "hi"}]))
        assert global_ledger.query(session_id="s5")["entries"] == []

    def test_task_cost_update_matches_ledger_rows(self, global_ledger, monkeypatch):
        import asyncio as aio

        bus = EventBus()
        bus._subscribers = {}
        queue = aio.Queue()
        bus.subscribe("t6", queue)

        async def fake_acompletion(**kwargs):
            return _Stream([_chunk("ok", finish="stop", usage=_usage(2000, 1000))])

        monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
        monkeypatch.setattr(ledger_module, "event_bus", bus)
        provider = _gemini_provider()
        with usage_context(session_id="s6", task_id="t6"):
            asyncio.run(_collect(provider, [{"role": "user", "content": "hi"}]))

        assert not queue.empty()
        event = queue.get_nowait()
        assert event.type == EventType.TASK_COST_UPDATE
        row = global_ledger.query(task_id="t6")["entries"][0]
        assert event.payload["session_id"] == "s6"
        assert event.payload["task_id"] == "t6"
        assert event.payload["delta_cost_usd"] == pytest.approx(row["cost_usd"])
        assert event.payload["task_total_cost_usd"] == pytest.approx(
            global_ledger.task_total("t6"))
        assert event.payload["model"] == "gemini-3.5-flash"
        assert event.payload["input_tokens"] == 2000
        assert event.payload["output_tokens"] == 1000


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------


class TestContracts:
    def test_usage_event_types_exist(self):
        assert EventType.USAGE_LEDGER_QUERY_REQUEST.value == "USAGE_LEDGER_QUERY_REQUEST"
        assert EventType.USAGE_LEDGER_QUERY_RESPONSE.value == "USAGE_LEDGER_QUERY_RESPONSE"
        assert EventType.TASK_COST_UPDATE.value == "TASK_COST_UPDATE"

    def test_query_response_shape(self, ledger):
        ledger.record_call(model_name="gemini-3.5-flash", provider_name="google_gemini",
                           input_tokens=10, output_tokens=5, cost_usd=0.01,
                           is_local=False, pricing_status="priced",
                           session_id="s1", task_id="t1", agent_name="coder")
        result = ledger.query(session_id="s1")
        assert set(result) == {"entries", "totals"}
        assert set(result["totals"]) == {
            "total_cost_usd", "input_tokens", "output_tokens", "by_model", "by_agent"}
