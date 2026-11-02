"""E5-F11 — per-model latency percentiles + measured-latency routing.

The plan addendum (Epic 5, omp #37) promises: "Latency bench shape:
TTFT/prefill/decode percentiles recorded per model so routing
decisions use measured latency."

Before this fix the capability was absent, not mis-measured: health
kept one last-duration scalar per provider, self-tests recorded
whole-test durations and a mean, and RoutingPolicy selected via
health/capability/defaults/list order with no measured-latency input
at all (engine grep for ttft|prefill|percentile returned zero).

These tests pin the built capability:

* a real (scripted-litellm) streaming call records a latency sample
  keyed by the (provider, model) that actually served it — the
  fallback's own identity when a fallback served (E5-F03);
* percentile summaries (p50/p95, linear interpolation) are computed
  per model over a bounded recent window, isolated by provider;
* routing: among otherwise-tied candidates (same priority tier),
  the model with the lower measured TTFT p50 wins; with no
  measurements the selection is byte-identical to before; a model
  with no measurements is never displaced by a measured one; the
  tie-break never crosses priority tiers, never overrides the
  user's default or an explicit request;
* recording is observational: a failing latency store never breaks
  the call (the usage ledger's M19 discipline), and probe traffic
  (record_usage=False — R2-NEW-1) records nothing.
"""
import asyncio
import sqlite3
from types import SimpleNamespace

import litellm
import pytest

import aether_engine.routing.latency as latency_module
from aether_engine.providers.litellm_provider import FallbackModel, LiteLLMProvider
from aether_engine.routing.health import ProviderHealthManager
from aether_engine.routing.latency import LatencyStore
from aether_engine.routing.policy import RoutingPolicy
from aether_engine.routing.registry import ModelEntry, ModelRegistry


@pytest.fixture()
def store(tmp_path):
    return LatencyStore(tmp_path / "latency.db")


def _seed(store, provider, model, ttfts, *, start_ts=1_000.0):
    """Record one sample per TTFT value, timestamps increasing."""
    for i, ttft in enumerate(ttfts):
        store.record_sample(
            provider_name=provider, model_name=model,
            ttft_ms=float(ttft), total_ms=float(ttft) + 100.0,
            timestamp=start_ts + i,
        )


# ---------------------------------------------------------------------------
# Percentile store
# ---------------------------------------------------------------------------


class TestLatencyStore:
    def test_percentiles_p50_p95_linear_interpolation(self, store):
        _seed(store, "openai", "gpt-main", [100, 200, 300, 400])
        summary = store.summary("openai", "gpt-main")
        assert summary is not None
        assert summary.samples == 4
        # Linear interpolation: p50 rank = 0.5*(4-1) = 1.5 -> 250;
        # p95 rank = 0.95*3 = 2.85 -> 300 + 0.85*100 = 385.
        assert summary.ttft_p50_ms == pytest.approx(250.0)
        assert summary.ttft_p95_ms == pytest.approx(385.0)
        assert summary.total_p50_ms == pytest.approx(350.0)

    def test_unknown_model_has_no_summary(self, store):
        assert store.summary("openai", "never-called") is None
        assert store.ttft_p50_ms("openai", "never-called") is None

    def test_isolation_by_provider_and_model(self, store):
        _seed(store, "openai", "shared-id", [100, 100])
        _seed(store, "anthropic", "shared-id", [900, 900])
        _seed(store, "openai", "other-id", [500, 500])
        assert store.ttft_p50_ms("openai", "shared-id") == pytest.approx(100.0)
        assert store.ttft_p50_ms("anthropic", "shared-id") == pytest.approx(900.0)
        assert store.ttft_p50_ms("openai", "other-id") == pytest.approx(500.0)

    def test_decode_tps_derived_from_tokens_and_decode_window(self, store):
        store.record_sample(
            provider_name="openai", model_name="gpt-main",
            ttft_ms=100.0, total_ms=1100.0,
            decode_ms=1000.0, output_tokens=50, timestamp=1.0)
        store.record_sample(
            provider_name="openai", model_name="gpt-main",
            ttft_ms=100.0, total_ms=600.0,
            decode_ms=500.0, output_tokens=50, timestamp=2.0)
        summary = store.summary("openai", "gpt-main")
        # 50 tokens / 1.0 s = 50 tps; 50 / 0.5 s = 100 tps; p50 = 75.
        assert summary.decode_tps_p50 == pytest.approx(75.0)
        assert summary.decode_tps_p95 == pytest.approx(97.5)

    def test_no_decode_tps_without_reported_tokens(self, store):
        store.record_sample(
            provider_name="openai", model_name="gpt-main",
            ttft_ms=100.0, total_ms=300.0,
            decode_ms=200.0, output_tokens=None, timestamp=1.0)
        summary = store.summary("openai", "gpt-main")
        # Decode throughput is never fabricated from chunk counts.
        assert summary.decode_tps_p50 is None

    def test_total_only_sample_keeps_total_drops_ttft(self, store):
        store.record_sample(
            provider_name="openai", model_name="gpt-main",
            ttft_ms=None, total_ms=420.0, timestamp=1.0)
        summary = store.summary("openai", "gpt-main")
        assert summary.samples == 1
        assert summary.ttft_p50_ms is None
        assert summary.total_p50_ms == pytest.approx(420.0)

    def test_window_is_recent_and_retention_is_bounded(self, store, tmp_path):
        for i in range(250):
            store.record_sample(
                provider_name="openai", model_name="gpt-main",
                ttft_ms=float(i), total_ms=float(i),
                timestamp=1_000.0 + i)
        summary = store.summary("openai", "gpt-main")
        # Percentiles see only the newest 100 samples (150..249):
        # p50 = 199.5 — the older, faster half must not leak in.
        assert summary.samples == 100
        assert summary.ttft_p50_ms == pytest.approx(199.5)
        # The store itself retains at most 200 rows per model.
        conn = sqlite3.connect(tmp_path / "latency.db")
        try:
            (count,) = conn.execute(
                "SELECT COUNT(*) FROM latency_samples").fetchone()
        finally:
            conn.close()
        assert count == 200


# ---------------------------------------------------------------------------
# Recording from real provider calls
# ---------------------------------------------------------------------------


def _chunk(text=None, finish=None, usage=None):
    delta = SimpleNamespace(
        content=text, reasoning_content=None, thinking=None, tool_calls=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=finish)],
        usage=usage,
    )


class _Stream:
    def __init__(self, chunks, delay=0.01):
        self._chunks = chunks
        self._delay = delay

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for c in self._chunks:
            yield c
            await asyncio.sleep(self._delay)


def _text_stream(text, *, with_usage=False):
    usage = None
    if with_usage:
        usage = SimpleNamespace(prompt_tokens=5, completion_tokens=10)
    return _Stream([
        _chunk(text=text),
        _chunk(text=" more"),
        _chunk(finish="stop", usage=usage),
    ])


async def _drain(provider):
    return [c async for c in provider.call_stream(
        [{"role": "user", "content": "hello"}], tools=None, node_name="test")]


@pytest.fixture()
def latency_global(store, monkeypatch):
    """Point the module-level store (what the provider hook uses)
    at the tmp store."""
    monkeypatch.setattr(latency_module, "GLOBAL_LATENCY_STORE", store)
    return store


class TestProviderRecording:
    def test_streaming_call_records_sample_with_real_ttft(
            self, latency_global, monkeypatch):
        async def fake_acompletion(**kwargs):
            return _text_stream("hello", with_usage=True)

        monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
        provider = LiteLLMProvider(
            api_key="sk-openai", model="gpt-main", provider_name="openai")
        chunks = asyncio.run(_drain(provider))
        assert "".join(c.text or "" for c in chunks) == "hello more"

        summary = latency_global.summary("openai", "gpt-main")
        assert summary is not None
        assert summary.samples == 1
        assert summary.ttft_p50_ms is not None
        assert 0.0 <= summary.ttft_p50_ms <= summary.total_p50_ms
        # Usage reported 10 output tokens over a real (slept) decode
        # window, so a decode rate was honestly measurable.
        assert summary.decode_tps_p50 is not None
        assert summary.decode_tps_p50 > 0

    def test_fallback_sample_attributed_to_serving_model(
            self, latency_global, monkeypatch):
        async def fake_acompletion(**kwargs):
            if "claude" in kwargs["model"]:
                return _text_stream("fallback answer")
            raise litellm.RateLimitError(
                "rate limited", "openai", kwargs["model"])

        monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
        provider = LiteLLMProvider(
            api_key="sk-openai", model="gpt-main", provider_name="openai",
            fallback_models=[FallbackModel(
                model="anthropic/claude-main", api_key="sk-anthropic",
                provider_name="anthropic")])
        chunks = asyncio.run(_drain(provider))
        assert "".join(c.text or "" for c in chunks) == "fallback answer more"

        # The sample keys to the model that actually served, under
        # its registry (bare) ID — not the primary, not the
        # LiteLLM-prefixed string (E5-F03 identity).
        served = latency_global.summary("anthropic", "claude-main")
        assert served is not None and served.samples == 1
        assert latency_global.summary("openai", "gpt-main") is None

    def test_recording_failure_never_breaks_the_call(
            self, latency_global, monkeypatch):
        def _boom(**kwargs):
            raise RuntimeError("latency store exploded")

        monkeypatch.setattr(latency_module, "record_latency_sample", _boom)

        async def fake_acompletion(**kwargs):
            return _text_stream("untouched")

        monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
        provider = LiteLLMProvider(
            api_key="sk-openai", model="gpt-main", provider_name="openai")
        chunks = asyncio.run(_drain(provider))
        assert "".join(c.text or "" for c in chunks) == "untouched more"

    def test_broken_store_path_never_breaks_the_call(
            self, tmp_path, monkeypatch):
        broken = LatencyStore(tmp_path)  # a directory, not a file
        monkeypatch.setattr(latency_module, "GLOBAL_LATENCY_STORE", broken)

        async def fake_acompletion(**kwargs):
            return _text_stream("still fine")

        monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
        provider = LiteLLMProvider(
            api_key="sk-openai", model="gpt-main", provider_name="openai")
        chunks = asyncio.run(_drain(provider))
        assert "".join(c.text or "" for c in chunks) == "still fine more"

    def test_probe_opt_out_records_nothing(
            self, latency_global, monkeypatch):
        async def fake_acompletion(**kwargs):
            return _text_stream("probe")

        monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
        provider = LiteLLMProvider(
            api_key="sk-openai", model="gpt-main", provider_name="openai",
            record_usage=False)
        asyncio.run(_drain(provider))
        assert latency_global.summary("openai", "gpt-main") is None


# ---------------------------------------------------------------------------
# Measured-latency routing
# ---------------------------------------------------------------------------


def _registry(*, fast_priority=1):
    return ModelRegistry([
        ModelEntry(provider="openai", id="slow-model", label="Slow Model",
                   capabilities=["chat", "code"], priority=1),
        ModelEntry(provider="openai", id="fast-model", label="Fast Model",
                   capabilities=["chat", "code"], priority=fast_priority),
    ])


def _policy(store, **registry_kwargs):
    return RoutingPolicy(
        registry=_registry(**registry_kwargs), latency_store=store)


def _select(policy, **overrides):
    kwargs = {
        "prompt": "fix this function bug",
        "configured_providers": ["openai"],
        "health_manager": ProviderHealthManager(),
        "user_default_provider": "google_gemini",
        "user_default_model": "gemini-3.5-flash",
    }
    kwargs.update(overrides)
    return policy.select_route(**kwargs)


class TestLatencyRouting:
    def test_faster_measured_model_wins_a_tie(self, store):
        _seed(store, "openai", "slow-model", [500, 520, 480])
        _seed(store, "openai", "fast-model", [50, 60, 40])
        decision = _select(_policy(store))
        assert decision.model == "fast-model"
        assert "fastest measured TTFT" in decision.reason

    def test_cold_start_is_identical_to_today(self, store):
        decision = _select(_policy(store))
        assert decision.model == "slow-model"
        assert decision.reason == (
            "Routed to Slow Model — code generation & tool capabilities")

    def test_unmeasured_incumbent_is_never_displaced(self, store):
        # Only the challenger has measurements: the incumbent keeps
        # its slot — latency reorders measured models among
        # themselves, never a model with no evidence against it.
        _seed(store, "openai", "fast-model", [50, 60, 40])
        decision = _select(_policy(store))
        assert decision.model == "slow-model"

    def test_latency_never_crosses_priority_tiers(self, store):
        _seed(store, "openai", "slow-model", [500, 520, 480])
        _seed(store, "openai", "fast-model", [50, 60, 40])
        decision = _select(_policy(store, fast_priority=2))
        assert decision.model == "slow-model"

    def test_user_default_pin_beats_latency(self, store):
        _seed(store, "openai", "slow-model", [500, 520, 480])
        _seed(store, "openai", "fast-model", [50, 60, 40])
        decision = _select(
            _policy(store),
            user_default_provider="openai", user_default_model="slow-model")
        assert decision.model == "slow-model"
        assert "fastest measured TTFT" not in decision.reason

    def test_explicit_request_beats_latency(self, store):
        _seed(store, "openai", "slow-model", [500, 520, 480])
        _seed(store, "openai", "fast-model", [50, 60, 40])
        decision = _select(_policy(store), requested_model="slow-model")
        assert decision.model == "slow-model"
        assert decision.reason == "User selected Slow Model"

    def test_standard_chat_fallback_branch_uses_latency(self, store):
        _seed(store, "openai", "slow-model", [500, 520, 480])
        _seed(store, "openai", "fast-model", [50, 60, 40])
        decision = _select(_policy(store), prompt="hello there")
        assert decision.model == "fast-model"
        assert "fastest measured TTFT" in decision.reason

    def test_broken_latency_store_falls_back_to_today(self, tmp_path):
        class _BrokenStore:
            def ttft_p50_ms(self, provider, model):
                raise RuntimeError("db gone")

        policy = RoutingPolicy(registry=_registry(), latency_store=_BrokenStore())
        decision = _select(policy)
        assert decision.model == "slow-model"
