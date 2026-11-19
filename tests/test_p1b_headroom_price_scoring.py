"""P1b — price + quota-headroom scoring.

The plan's contract, pinned:

* headroom is learned from rate-limit evidence only (hot inside
  60 min or before a stated reset, warm inside 6 h, else cool) —
  never an invented percentage;
* run-level ``RoutingPolicy._pick`` orders within a priority tier
  by headroom -> static price -> measured TTFT, never crosses a
  tier, never touches the explicit paths (requested model,
  vision override, user default pin), and is byte-identical to
  the pre-P1b selection when no evidence exists anywhere;
* S14's ``_rank_key`` demotes hot providers alongside failure
  demotion (warm is a tie-break only) and replaces the infinite
  cold-start cost with pricing's labelled static estimate —
  the recorded average outranks the estimate once >= 3 calls
  exist;
* a store failure degrades every consumer to the pre-P1b
  behaviour (fail-open);
* the fallback ladder template is a suggestion only, and chain
  candidates for a multi-model provider are picked by the same
  scoring instead of first-in-registry order.
"""
import time

from aether_engine.config import UserConfig
from aether_engine.cost.pricing import static_price_estimate
from aether_engine.langgraph.model_routing import (
    Candidate,
    History,
    TaskSignals,
    collect_history,
    route,
)
from aether_engine.providers.base import NetworkError, RateLimitError
from aether_engine.routing import headroom
from aether_engine.routing.fallback import (
    get_configured_fallback_candidates,
    suggested_fallback_chain,
)
from aether_engine.routing.headroom import (
    RateLimitStore,
    extract_reset_at,
    heat_info,
    heat_state_for,
    note_rate_limit,
    record_rate_limit,
)
from aether_engine.routing.health import ProviderHealthManager
from aether_engine.routing.latency import LatencyStore
from aether_engine.routing.policy import RoutingPolicy
from aether_engine.routing.registry import ModelEntry, ModelRegistry

NOW = 1_700_000_000.0


def _entry(provider, model, priority=1, caps=("chat",)):
    return ModelEntry(provider=provider, id=model, label=model,
                      capabilities=list(caps), priority=priority)


def _policy(tmp_path, models, *, with_latency=True):
    return RoutingPolicy(
        registry=ModelRegistry(models),
        latency_store=LatencyStore(tmp_path / "latency.db") if with_latency else None,
        headroom_store=RateLimitStore(tmp_path / "rate_limits.db"),
    )


# ── Part 1: the evidence store + heat derivation ─────────────────


class TestEvidenceStore:
    def test_write_read_round_trip(self, tmp_path):
        store = RateLimitStore(tmp_path / "rl.db")
        assert record_rate_limit("prov-a", "m1", timestamp=NOW, store=store)
        info = heat_info("prov-a", now=NOW + 10, store=store)
        assert info.state == "hot"
        assert info.last_event_at == NOW

    def test_no_events_is_cool(self, tmp_path):
        store = RateLimitStore(tmp_path / "rl.db")
        assert heat_info("never-seen", now=NOW, store=store).state == "cool"

    def test_store_is_bounded(self, tmp_path, monkeypatch):
        monkeypatch.setattr(headroom, "EVENT_ROW_CAP", 3)
        store = RateLimitStore(tmp_path / "rl.db")
        for i in range(6):
            record_rate_limit("prov-a", timestamp=NOW + i, store=store)
        import sqlite3
        with sqlite3.connect(tmp_path / "rl.db") as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM rate_limit_events").fetchone()[0]
        assert count == 3

    def test_window_edges(self):
        # Hot: inside 60 min. Warm: inside 6 h. Cool: beyond.
        assert heat_state_for(NOW - 3599, None, now=NOW) == "hot"
        assert heat_state_for(NOW - 3601, None, now=NOW) == "warm"
        assert heat_state_for(NOW - 6 * 3600 + 1, None, now=NOW) == "warm"
        assert heat_state_for(NOW - 6 * 3600 - 1, None, now=NOW) == "cool"

    def test_reset_time_honoured_over_window(self):
        # An old event whose stated reset is still ahead is hot;
        # once the reset passes, only the windows speak.
        assert heat_state_for(NOW - 10 * 3600, NOW + 600, now=NOW) == "hot"
        assert heat_state_for(NOW - 10 * 3600, NOW - 60, now=NOW) == "cool"
        assert heat_state_for(NOW - 1800, NOW - 60, now=NOW) == "hot"

    def test_extract_reset_at(self):
        assert extract_reset_at("Please retry in 30.5s", now=NOW) == NOW + 30.5
        assert extract_reset_at('{"retryDelay": "12s"}', now=NOW) == NOW + 12
        assert extract_reset_at("Retry-After: 45", now=NOW) == NOW + 45
        assert extract_reset_at("quota will reset in 5 minutes", now=NOW) == NOW + 300
        assert extract_reset_at("some unrelated failure", now=NOW) is None
        assert extract_reset_at(None, now=NOW) is None

    def test_note_rate_limit_records_only_rate_limits(self, tmp_path):
        store = RateLimitStore(tmp_path / "rl.db")
        assert note_rate_limit(
            RateLimitError("Rate limit exceeded: 429"), "prov-a", store=store)
        assert heat_info("prov-a", store=store).state == "hot"
        assert not note_rate_limit(
            NetworkError("connection reset"), "prov-b", store=store)
        assert heat_info("prov-b", store=store).state == "cool"
        # A 429 buried in a longer digit run is not a status code.
        assert not note_rate_limit("request 14290 failed", "prov-c", store=store)
        assert note_rate_limit(
            "HTTP 429 Too Many Requests", "prov-c", store=store)

    def test_record_success_clears_heat(self, tmp_path, monkeypatch):
        store = RateLimitStore(tmp_path / "rl.db")
        monkeypatch.setattr(headroom, "GLOBAL_RATE_LIMIT_STORE", store)
        record_rate_limit("prov-a", store=store)
        assert heat_info("prov-a", store=store).state == "hot"
        ProviderHealthManager().record_success("prov-a", 42.0)
        assert heat_info("prov-a", store=store).state == "cool"

    def test_corrupt_store_fails_open(self, tmp_path):
        broken = RateLimitStore(tmp_path)  # a directory, not a file
        assert heat_info("prov-a", store=broken).state == "cool"
        assert record_rate_limit("prov-a", store=broken) is False
        assert note_rate_limit("HTTP 429", "prov-a", store=broken) is False


# ── Part 2a: RoutingPolicy._pick scoring ─────────────────────────


class TestPickScoring:
    def test_cold_start_is_byte_identical(self, tmp_path):
        models = [_entry("prov-a", "model-a"), _entry("prov-b", "model-b")]
        policy = _policy(tmp_path, models)
        chosen, signal = policy._pick_scored(models)
        assert chosen is models[0]
        assert signal is None

    def test_hot_provider_demoted_past_cool(self, tmp_path):
        models = [_entry("prov-a", "model-a"), _entry("prov-b", "model-b")]
        policy = _policy(tmp_path, models)
        record_rate_limit("prov-a", store=policy._headroom_store)
        chosen, signal = policy._pick_scored(models)
        assert chosen is models[1]
        assert "cooling down" in signal
        assert "prov-a" in signal

    def test_heat_never_crosses_a_priority_tier(self, tmp_path):
        models = [
            _entry("prov-a", "model-a", priority=1),
            _entry("prov-b", "model-b", priority=2),
        ]
        policy = _policy(tmp_path, models)
        record_rate_limit("prov-a", store=policy._headroom_store)
        chosen, _ = policy._pick_scored(models)
        assert chosen is models[0]

    def test_cheaper_static_price_wins_within_tier(self, tmp_path):
        models = [
            _entry("openai", "gpt-4o"),
            _entry("openai", "gpt-4o-mini"),
        ]
        policy = _policy(tmp_path, models)
        chosen, signal = policy._pick_scored(models)
        assert chosen.id == "gpt-4o-mini"
        assert "cheapest" in signal

    def test_local_model_prices_at_true_zero(self, tmp_path):
        models = [
            _entry("openai", "gpt-4o-mini"),
            _entry("ollama", "local-model"),
        ]
        policy = _policy(tmp_path, models)
        chosen, _ = policy._pick_scored(models)
        assert chosen.provider == "ollama"

    def test_unpriced_is_not_treated_as_free(self, tmp_path):
        # An unpriced model keeps its slot against a priced one
        # when nothing else discriminates — unknown != $0. With
        # the priced model first, it stays first; reversed, the
        # unpriced model stays first too (slot preservation).
        models = [
            _entry("openai", "gpt-4o-mini"),
            _entry("prov-b", "mystery-model"),
        ]
        policy = _policy(tmp_path, models)
        chosen, signal = policy._pick_scored(models)
        assert chosen is models[0]
        assert signal is None

    def test_ttft_still_decides_when_price_and_heat_tie(self, tmp_path):
        store = LatencyStore(tmp_path / "latency.db")
        for i, ttft in enumerate([500, 520, 480]):
            store.record_sample(provider_name="prov-a", model_name="slow",
                                ttft_ms=float(ttft), timestamp=1000.0 + i)
        for i, ttft in enumerate([50, 60, 40]):
            store.record_sample(provider_name="prov-a", model_name="fast",
                                ttft_ms=float(ttft), timestamp=1000.0 + i)
        models = [_entry("prov-a", "slow"), _entry("prov-a", "fast")]
        policy = RoutingPolicy(
            registry=ModelRegistry(models), latency_store=store,
            headroom_store=RateLimitStore(tmp_path / "rl.db"))
        chosen, signal = policy._pick_scored(models)
        assert chosen.id == "fast"
        assert signal == "fastest measured TTFT among equal-priority candidates"

    def test_broken_headroom_store_degrades_to_old_order(self, tmp_path):
        models = [_entry("prov-a", "model-a"), _entry("prov-b", "model-b")]
        policy = RoutingPolicy(
            registry=ModelRegistry(models),
            latency_store=LatencyStore(tmp_path / "latency.db"),
            headroom_store=RateLimitStore(tmp_path))  # directory: unreadable
        chosen, signal = policy._pick_scored(models)
        assert chosen is models[0]
        assert signal is None

    def test_pick_two_value_form_preserved(self, tmp_path):
        models = [_entry("prov-a", "model-a"), _entry("prov-b", "model-b")]
        policy = _policy(tmp_path, models)
        record_rate_limit("prov-a", store=policy._headroom_store)
        chosen, decided = policy._pick(models)
        assert chosen is models[1]
        assert decided is True


class TestExplicitPathsUnchanged:
    def _registry(self):
        return ModelRegistry([
            _entry("prov-a", "model-a", caps=("chat", "vision")),
            _entry("prov-b", "model-b", caps=("chat", "vision")),
        ])

    def test_requested_model_on_hot_provider_still_serves(self, tmp_path):
        policy = RoutingPolicy(
            registry=self._registry(),
            latency_store=LatencyStore(tmp_path / "latency.db"),
            headroom_store=RateLimitStore(tmp_path / "rl.db"))
        record_rate_limit("prov-a", store=policy._headroom_store)
        decision = policy.select_route(
            prompt="hello", configured_providers=["prov-a", "prov-b"],
            health_manager=ProviderHealthManager(),
            requested_model="model-a")
        assert decision.provider == "prov-a"
        assert decision.model == "model-a"

    def test_default_pin_on_hot_provider_still_serves(self, tmp_path):
        policy = RoutingPolicy(
            registry=self._registry(),
            latency_store=LatencyStore(tmp_path / "latency.db"),
            headroom_store=RateLimitStore(tmp_path / "rl.db"))
        record_rate_limit("prov-a", store=policy._headroom_store)
        decision = policy.select_route(
            prompt="hello", configured_providers=["prov-a", "prov-b"],
            health_manager=ProviderHealthManager(),
            user_default_provider="prov-a", user_default_model="model-a")
        assert decision.provider == "prov-a"
        assert decision.model == "model-a"


class TestS22Contract:
    """S22: a provider with a fresh 429 event is not chosen by
    auto routing while an equal-tier cool candidate exists, and
    the published reason says why."""

    def test_fresh_429_changes_the_next_route_with_reason(self, tmp_path):
        models = [
            _entry("prov-a", "model-a"),
            _entry("prov-b", "model-b"),
        ]
        policy = RoutingPolicy(
            registry=ModelRegistry(models),
            latency_store=LatencyStore(tmp_path / "latency.db"),
            headroom_store=RateLimitStore(tmp_path / "rl.db"))
        health = ProviderHealthManager()
        before = policy.select_route(
            prompt="hello", configured_providers=["prov-a", "prov-b"],
            health_manager=health,
            user_default_provider="none", user_default_model="none")
        assert before.provider == "prov-a"

        record_rate_limit("prov-a", store=policy._headroom_store)
        after = policy.select_route(
            prompt="hello", configured_providers=["prov-a", "prov-b"],
            health_manager=health,
            user_default_provider="none", user_default_model="none")
        assert after.provider == "prov-b"
        assert "cooling down" in after.reason
        assert "prov-a" in after.reason


# ── Part 2b: S14 _rank_key scoring ───────────────────────────────


def _candidate(provider, model, tier=1, incumbent=False):
    return Candidate(provider=provider, model=model, tier=tier,
                     capabilities=("chat",), is_incumbent=incumbent)


def _route(history, candidates):
    return route(
        TaskSignals(agent="coder", task_text="write a python function"),
        candidates, history)


class TestS14Scoring:
    def test_hot_provider_demoted_for_equal_tier(self):
        history = History(provider_heat={
            "prov-a": headroom.HeatInfo("prov-a", "hot", time.time(), None),
        })
        hot = _candidate("prov-a", "m-a")
        cool = _candidate("prov-b", "m-b", incumbent=True)
        choice = _route(history, [hot, cool])
        assert choice.provider == "prov-b"
        assert "cooling down" in choice.reason

    def test_cold_start_estimate_lets_cheaper_model_win(self):
        history = History(estimated_cost={
            ("prov-a", "m-a"): 0.010,
            ("prov-b", "m-b"): 0.001,
        })
        expensive = _candidate("prov-a", "m-a", incumbent=True)
        cheap = _candidate("prov-b", "m-b")
        choice = _route(history, [expensive, cheap])
        assert choice.model == "m-b"
        assert choice.cost_basis == "estimated"
        assert "estimated price" in choice.reason

    def test_recorded_cost_outranks_estimate_at_three_calls(self):
        # m-a's estimate is cheapest, but its 5 recorded calls say
        # it actually costs more than m-b's estimate — recorded wins.
        history = History(
            usage={("prov-a", "m-a"): {"calls": 5, "cost_usd": 0.25,
                                       "total_tokens": 0}},
            estimated_cost={("prov-a", "m-a"): 0.001,
                            ("prov-b", "m-b"): 0.010})
        a = _candidate("prov-a", "m-a", incumbent=True)
        b = _candidate("prov-b", "m-b")
        choice = _route(history, [a, b])
        assert choice.model == "m-b"

    def test_estimate_used_below_three_recorded_calls(self):
        # Only 2 recorded calls: not yet mature, the estimate is
        # the ranking cost — m-a wins on its cheap estimate despite
        # the two expensive recorded calls.
        history = History(
            usage={("prov-a", "m-a"): {"calls": 2, "cost_usd": 0.10,
                                       "total_tokens": 0}},
            estimated_cost={("prov-a", "m-a"): 0.001,
                            ("prov-b", "m-b"): 0.010})
        a = _candidate("prov-a", "m-a")
        b = _candidate("prov-b", "m-b", incumbent=True)
        choice = _route(history, [a, b])
        assert choice.model == "m-a"
        assert choice.cost_basis == "estimated"

    def test_unpriced_model_is_never_free(self):
        history = History(estimated_cost={("prov-b", "m-b"): 0.010})
        unknown = _candidate("prov-a", "m-a")
        priced = _candidate("prov-b", "m-b")
        choice = _route(history, [unknown, priced])
        assert choice.model == "m-b"
        assert choice.cost_basis == "estimated"

    def test_warm_is_only_a_tiebreak(self):
        # Warm provider with the cheaper estimate still wins —
        # warm never demotes a class by itself.
        history = History(
            provider_heat={"prov-a": headroom.HeatInfo(
                "prov-a", "warm", time.time() - 7200, None)},
            estimated_cost={("prov-a", "m-a"): 0.001,
                            ("prov-b", "m-b"): 0.010})
        warm_cheap = _candidate("prov-a", "m-a")
        cool_dear = _candidate("prov-b", "m-b", incumbent=True)
        choice = _route(history, [warm_cheap, cool_dear])
        assert choice.model == "m-a"

    def test_collect_history_fills_heat_and_estimates(self, tmp_path):
        store = RateLimitStore(tmp_path / "rl.db")
        record_rate_limit("prov-a", store=store)
        candidates = [_candidate("prov-a", "gpt-4o-mini"),
                      _candidate("prov-b", "mystery")]
        history = collect_history(
            candidates, latency_store=LatencyStore(tmp_path / "lat.db"),
            headroom_store=store)
        assert history.provider_heat["prov-a"].state == "hot"
        assert ("prov-a", "gpt-4o-mini") in history.estimated_cost
        assert ("prov-b", "mystery") not in history.estimated_cost


# ── Part 3: the fallback ladder ──────────────────────────────────


class TestFallbackLadder:
    def _config(self, chain=()):
        cfg = UserConfig()
        cfg.fallback_chain = list(chain)
        return cfg

    def test_chain_pick_uses_scoring_for_multi_model_provider(self, tmp_path):
        registry = ModelRegistry([
            _entry("prov-a", "gpt-4o"),
            _entry("prov-a", "gpt-4o-mini"),
        ])
        result = get_configured_fallback_candidates(
            "failed", self._config(chain=["prov-a"]),
            ["prov-a"], ProviderHealthManager(), registry=registry)
        assert [e.id for e in result] == ["gpt-4o-mini"]

    def test_chain_pick_without_evidence_keeps_first_model(self):
        registry = ModelRegistry([
            _entry("prov-a", "model-a"),
            _entry("prov-a", "model-b"),
        ])
        result = get_configured_fallback_candidates(
            "failed", self._config(chain=["prov-a"]),
            ["prov-a"], ProviderHealthManager(), registry=registry)
        assert [e.id for e in result] == ["model-a"]

    def test_template_orders_strong_standard_cheap_local(self):
        registry = ModelRegistry([
            _entry("strong-co", "big", caps=("chat", "reasoning")),
            _entry("plain-co", "mid", caps=("chat",)),
            _entry("fast-co", "small", caps=("chat", "fast")),
            _entry("ollama", "local", caps=("chat",)),
        ])
        suggestion = suggested_fallback_chain(
            self._config(),
            ["fast-co", "ollama", "plain-co", "strong-co"],
            registry=registry)
        assert suggestion == ["strong-co", "plain-co", "fast-co", "ollama"]

    def test_template_empty_when_chain_configured_or_single_provider(self):
        registry = ModelRegistry([
            _entry("a-co", "m1", caps=("chat", "reasoning")),
            _entry("b-co", "m2", caps=("chat",)),
        ])
        assert suggested_fallback_chain(
            self._config(chain=["a-co"]), ["a-co", "b-co"],
            registry=registry) == []
        assert suggested_fallback_chain(
            self._config(), ["a-co"], registry=registry) == []


# ── Pricing seam ─────────────────────────────────────────────────


class TestStaticPriceEstimate:
    def test_known_model_prices_and_unknown_is_none(self):
        assert static_price_estimate("gpt-4o-mini", "openai") is not None
        assert static_price_estimate("no-such-model-zzz", "prov-x") is None

    def test_local_prices_at_zero(self):
        assert static_price_estimate("anything", "ollama") == 0.0
