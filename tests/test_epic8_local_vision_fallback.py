"""Tests for finding M17 — explicit local vision must never silently
fall back to cloud processing (Board E8-4.1: zero external network for
explicitly-local vision).

Three layers are pinned here:

1. Routing policy — an explicit vision override is consulted BEFORE
   the empty-discovery default, and a decision taken under an explicit
   LOCAL vision override is marked ``local_only``.
2. Fallback candidate construction — ``get_fallback_candidates``
   honours ``local_only`` / ``require_vision`` constraints, and
   leaves unconstrained behaviour exactly as Epic 5 shipped it.
3. Provider construction — ``_create_provider_instance`` propagates
   the constraints into the per-call fallback list, so a failed local
   call cannot be retried on a cloud model inside the provider either.

The websocket task-retry factory composes layers 2+3 with the
decision's ``local_only`` flag; those compositions are what the
assertions below make safe.
"""
from unittest.mock import MagicMock

import pytest

from aether_engine.routing.fallback import get_fallback_candidates
from aether_engine.routing.health import ProviderHealthManager
from aether_engine.routing.policy import RoutingPolicy
from aether_engine.routing.registry import ModelEntry, ModelRegistry


@pytest.fixture
def health_mgr():
    return ProviderHealthManager()


@pytest.fixture
def registry():
    return ModelRegistry([
        ModelEntry(provider="google_gemini", id="gemini-3.5-flash", label="Gemini 3.5 Flash",
                   capabilities=["chat", "fast", "tools", "vision"], priority=1),
        ModelEntry(provider="openai", id="gpt-4o", label="GPT-4o",
                   capabilities=["chat", "tools", "vision"], priority=1),
        # anthropic's top-priority model cannot see; its second can.
        ModelEntry(provider="anthropic", id="claude-instant", label="Claude Instant",
                   capabilities=["chat", "fast"], priority=1),
        ModelEntry(provider="anthropic", id="claude-3-5-sonnet", label="Claude 3.5 Sonnet",
                   capabilities=["chat", "tools", "vision"], priority=2),
        # ollama's top-priority model cannot see; its second can.
        ModelEntry(provider="ollama", id="llama3.2", label="Llama 3.2",
                   capabilities=["chat", "fast"], priority=1),
        ModelEntry(provider="ollama", id="llava", label="LLaVA",
                   capabilities=["chat", "vision"], priority=2),
        # deepseek has no vision model at all.
        ModelEntry(provider="deepseek", id="deepseek-chat", label="DeepSeek Chat",
                   capabilities=["chat", "code"], priority=1),
    ])


IMAGE = [{"name": "snip.png", "mime_type": "image/png", "data": "data:image/png;base64,AAAA"}]
ALL_PROVIDERS = ["google_gemini", "openai", "anthropic", "ollama", "deepseek"]


class TestPolicyHonoursLocalVisionOverride:
    def test_empty_discovery_local_vision_override_wins_over_cloud_default(
            self, registry, health_mgr):
        """Sub-defect 2: discovery finds no healthy model, yet the
        explicit local vision override must win over the cloud
        default instead of losing to it."""
        health_mgr.record_failure("google_gemini", "down", is_retriable=False)
        policy = RoutingPolicy(registry)

        decision = policy.select_route(
            prompt="Read the text in this screenshot",
            configured_providers=["google_gemini"],
            health_manager=health_mgr,
            user_default_provider="google_gemini",
            user_default_model="gemini-3.5-flash",
            auxiliary_models={"vision": "ollama:::llava"},
            images=IMAGE,
        )

        assert decision.provider == "ollama"
        assert decision.model == "llava"
        assert decision.local_only is True

    def test_local_vision_override_decision_is_local_only(self, registry, health_mgr):
        policy = RoutingPolicy(registry)
        decision = policy.select_route(
            prompt="Describe this image",
            configured_providers=["google_gemini"],
            health_manager=health_mgr,
            user_default_provider="google_gemini",
            user_default_model="gemini-3.5-flash",
            auxiliary_models={"vision": "ollama:::llava"},
            images=IMAGE,
        )
        assert decision.provider == "ollama"
        assert decision.local_only is True

    def test_cloud_vision_override_is_not_local_only(self, registry, health_mgr):
        policy = RoutingPolicy(registry)
        decision = policy.select_route(
            prompt="Describe this image",
            configured_providers=["google_gemini", "openai"],
            health_manager=health_mgr,
            user_default_provider="google_gemini",
            user_default_model="gemini-3.5-flash",
            auxiliary_models={"vision": "google_gemini:::gemini-3.5-flash"},
            images=IMAGE,
        )
        assert decision.provider == "google_gemini"
        assert decision.local_only is False

    def test_auto_vision_route_is_not_local_only(self, registry, health_mgr):
        policy = RoutingPolicy(registry)
        decision = policy.select_route(
            prompt="Describe this image",
            configured_providers=["google_gemini"],
            health_manager=health_mgr,
            user_default_provider="google_gemini",
            user_default_model="gemini-3.5-flash",
            auxiliary_models={"vision": "auto"},
            images=IMAGE,
        )
        assert decision.provider == "google_gemini"
        assert decision.local_only is False

    def test_local_vision_override_does_not_pin_text_only_requests(
            self, registry, health_mgr):
        """The constraint guards images. A text-only request keeps the
        normal route even while a local vision override is configured."""
        policy = RoutingPolicy(registry)
        decision = policy.select_route(
            prompt="Hello there!",
            configured_providers=["google_gemini"],
            health_manager=health_mgr,
            user_default_provider="google_gemini",
            user_default_model="gemini-3.5-flash",
            auxiliary_models={"vision": "ollama:::llava"},
        )
        assert decision.provider == "google_gemini"
        assert decision.local_only is False


class TestFallbackCandidateConstraints:
    def test_local_only_candidates_exclude_cloud_providers(
            self, registry, health_mgr):
        candidates = get_fallback_candidates(
            failed_provider="google_gemini",
            configured_providers=ALL_PROVIDERS,
            health_manager=health_mgr,
            registry=registry,
            local_only=True,
        )
        assert candidates
        assert {c.provider for c in candidates} == {"ollama"}

    def test_local_only_with_failed_local_provider_yields_no_candidates(
            self, registry, health_mgr):
        """The M17 repro precondition: the local provider failed and
        every other configured provider is cloud — the honest outcome
        is NO candidate, never a cloud one."""
        candidates = get_fallback_candidates(
            failed_provider="ollama",
            configured_providers=ALL_PROVIDERS,
            health_manager=health_mgr,
            registry=registry,
            local_only=True,
            require_vision=True,
        )
        assert candidates == []

    def test_require_vision_picks_vision_capable_model_per_provider(
            self, registry, health_mgr):
        candidates = get_fallback_candidates(
            failed_provider="google_gemini",
            configured_providers=ALL_PROVIDERS,
            health_manager=health_mgr,
            registry=registry,
            require_vision=True,
        )
        by_provider = {c.provider: c.id for c in candidates}
        assert by_provider["anthropic"] == "claude-3-5-sonnet"
        assert by_provider["ollama"] == "llava"
        assert by_provider["openai"] == "gpt-4o"

    def test_require_vision_skips_providers_without_vision_models(
            self, registry, health_mgr):
        candidates = get_fallback_candidates(
            failed_provider="google_gemini",
            configured_providers=ALL_PROVIDERS,
            health_manager=health_mgr,
            registry=registry,
            require_vision=True,
        )
        assert "deepseek" not in {c.provider for c in candidates}

    def test_unconstrained_candidates_unchanged(self, registry, health_mgr):
        """Guard (passes before and after): Epic 5's default candidate
        construction — top-priority healthy model per provider — is
        untouched when no constraints are supplied."""
        candidates = get_fallback_candidates(
            failed_provider="google_gemini",
            configured_providers=ALL_PROVIDERS,
            health_manager=health_mgr,
            registry=registry,
        )
        by_provider = {c.provider: c.id for c in candidates}
        assert by_provider == {
            "openai": "gpt-4o",
            "anthropic": "claude-instant",
            "ollama": "llama3.2",
            "deepseek": "deepseek-chat",
        }


def _provider_instance(registry, configured, primary_provider, primary_model, **kwargs):
    from aether_engine.orchestration.core import _create_provider_instance, engine_state

    orig_registry = engine_state.model_registry
    orig_providers = engine_state.configured_providers
    orig_chain = engine_state.user_config.fallback_chain
    try:
        engine_state.model_registry = registry
        engine_state.configured_providers = configured
        engine_state.user_config.fallback_chain = kwargs.pop("fallback_chain", [])
        secret_store = MagicMock()
        secret_store.load_provider.return_value = "fake-key"
        return _create_provider_instance(primary_provider, primary_model, secret_store, **kwargs)
    finally:
        engine_state.model_registry = orig_registry
        engine_state.configured_providers = orig_providers
        engine_state.user_config.fallback_chain = orig_chain


class TestProviderInstanceConstraints:
    def test_local_only_vision_instance_has_no_fallback_models(self, registry):
        """A local vision call whose provider instance carries cloud
        fallbacks would leak the image on the first retriable error.
        Under local_only + require_vision there is no legal fallback
        here at all: the other local models cannot see, and every
        other provider is cloud."""
        inst = _provider_instance(
            registry, ALL_PROVIDERS, "ollama", "llava",
            local_only=True, require_vision=True)
        assert inst.fallback_models == []

    def test_require_vision_instance_fallbacks_are_vision_capable(self, registry):
        inst = _provider_instance(
            registry, ALL_PROVIDERS, "ollama", "llava", require_vision=True)
        assert inst.fallback_models
        assert {fm.provider_name for fm in inst.fallback_models} == {
            "google_gemini", "openai", "anthropic"}
        assert not any("llama3.2" in fm.model or "claude-instant" in fm.model
                       for fm in inst.fallback_models)

    def test_local_only_instance_ignores_cloud_fallback_chain(self, registry):
        inst = _provider_instance(
            registry, ALL_PROVIDERS, "ollama", "llava",
            local_only=True, fallback_chain=["google_gemini", "openai"])
        assert {fm.provider_name for fm in inst.fallback_models} == {"ollama"}

    def test_unconstrained_instance_keeps_cloud_fallback(self, registry):
        """Guard (passes before and after): without constraints the
        Epic 5 construction still offers cross-provider fallbacks."""
        inst = _provider_instance(registry, ALL_PROVIDERS, "ollama", "llava")
        providers = {fm.provider_name for fm in inst.fallback_models}
        assert "google_gemini" in providers
