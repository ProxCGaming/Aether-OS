"""E5-F03: a model ID shared by two providers must resolve by
provider-qualified identity, never by registry registration order.

Two distinct custom providers can legitimately offer the same bare
model ID (the same open model served by several hosts). The registry
used to key its lookup by the bare ID alone, so whichever provider
registered last silently won — an explicit request for that model,
with the OTHER provider configured as the user's default, routed to
the wrong endpoint (audit repro: custom_local then custom_work, both
offering ``llama3:8b``, explicit request routed to custom_work with
reason "User selected Llama3:8b").

Resolution rule pinned by this suite:
  1. An explicit provider (``provider:model`` selection, auxiliary
     override) always wins — lookups are provider-qualified.
  2. A bare model ID in routing prefers the entry belonging to the
     user's configured default provider, then providers in
     configured order.
  3. The context-free bare ``get_model`` is deterministic and
     registration-order independent: lowest priority, then provider
     name.
"""
import time

from aether_engine.routing.health import ProviderHealthManager
from aether_engine.routing.policy import RoutingPolicy
from aether_engine.routing.registry import ModelEntry, ModelRegistry

SHARED_ID = "llama3:8b"


def _entry(provider, *, label="Llama3:8b", capabilities=None, priority=1):
    return ModelEntry(
        provider=provider,
        id=SHARED_ID,
        label=label,
        capabilities=capabilities if capabilities is not None else ["chat", "tools"],
        priority=priority,
    )


def _registry_registered(*providers):
    """Registry built by registering the shared model once per
    provider, in the given order (the production discovery shape)."""
    registry = ModelRegistry(models=[])
    for provider in providers:
        registry.register_model(_entry(provider))
    return registry


# ---------------------------------------------------------------------------
# Registry storage: both providers' entries survive
# ---------------------------------------------------------------------------

class TestRegistryStorage:
    def test_register_model_keeps_both_providers_entries(self):
        registry = _registry_registered("custom_local", "custom_work")
        entries = [m for m in registry.list_all_models() if m.id == SHARED_ID]
        assert {m.provider for m in entries} == {"custom_local", "custom_work"}

    def test_register_model_keeps_both_in_reverse_order(self):
        registry = _registry_registered("custom_work", "custom_local")
        entries = [m for m in registry.list_all_models() if m.id == SHARED_ID]
        assert {m.provider for m in entries} == {"custom_local", "custom_work"}

    def test_get_models_by_id_returns_every_provider(self):
        registry = _registry_registered("custom_local", "custom_work")
        entries = registry.get_models_by_id(SHARED_ID)
        assert {m.provider for m in entries} == {"custom_local", "custom_work"}

    def test_update_provider_models_keeps_both_providers_entries(self):
        registry = ModelRegistry(models=[])
        registry.update_provider_models("custom_local", [SHARED_ID])
        registry.update_provider_models("custom_work", [SHARED_ID])
        entries = [m for m in registry.list_all_models() if m.id == SHARED_ID]
        assert {m.provider for m in entries} == {"custom_local", "custom_work"}

    def test_reregister_same_provider_refreshes_only_its_own_entry(self):
        registry = _registry_registered("custom_local", "custom_work")
        registry.register_model(_entry("custom_local", label="Llama3:8b v2"))
        entries = [m for m in registry.list_all_models() if m.id == SHARED_ID]
        assert len(entries) == 2
        local = next(m for m in entries if m.provider == "custom_local")
        work = next(m for m in entries if m.provider == "custom_work")
        assert local.label == "Llama3:8b v2"
        assert work.label == "Llama3:8b"


# ---------------------------------------------------------------------------
# Lookup: provider-qualified wins; bare lookup is deterministic
# ---------------------------------------------------------------------------

class TestRegistryLookup:
    def test_provider_qualified_lookup_ignores_registration_order(self):
        for order in (("custom_local", "custom_work"), ("custom_work", "custom_local")):
            registry = _registry_registered(*order)
            assert registry.get_model(SHARED_ID, provider="custom_local").provider == "custom_local"
            assert registry.get_model(SHARED_ID, provider="custom_work").provider == "custom_work"

    def test_provider_qualified_lookup_unknown_provider_is_none(self):
        registry = _registry_registered("custom_local", "custom_work")
        assert registry.get_model(SHARED_ID, provider="openai") is None

    def test_agent_style_provider_model_selection_resolves_to_chosen_provider(self):
        # The per-agent shape: nodes parse "provider:model" and must
        # get THAT provider's entry even when the other provider
        # registered its identical model ID last.
        registry = _registry_registered("custom_local", "custom_work")
        override = "custom_local:llama3:8b"  # the stored agent_models shape
        provider, model = override.split(":", 1)
        entry = registry.get_model(model, provider=provider)
        assert entry is not None and entry.provider == "custom_local"

    def test_bare_lookup_is_registration_order_independent(self):
        # Via discovery (update_provider_models): both entries get the
        # same derived priority, so the deterministic tiebreak decides
        # identically whichever provider was discovered last.
        winners = set()
        for order in (("custom_local", "custom_work"), ("custom_work", "custom_local")):
            registry = ModelRegistry(models=[])
            for provider in order:
                registry.update_provider_models(provider, [SHARED_ID])
            winners.add(registry.get_model(SHARED_ID).provider)
        assert len(winners) == 1

    def test_bare_lookup_prefers_lowest_priority(self):
        for order in (("custom_local", "custom_work"), ("custom_work", "custom_local")):
            registry = ModelRegistry(models=[])
            for provider in order:
                registry.register_model(_entry(provider, priority=5 if provider == "custom_local" else 2))
            assert registry.get_model(SHARED_ID).provider == "custom_work"

    def test_unique_model_bare_lookup_unchanged(self):
        # Control: a model offered by exactly one provider resolves
        # exactly as before.
        registry = ModelRegistry()
        entry = registry.get_model("gpt-4o")
        assert entry is not None and entry.provider == "openai"


# ---------------------------------------------------------------------------
# Routing policy: explicit selection follows configuration, not registry
# ---------------------------------------------------------------------------

def _policy(registry):
    return RoutingPolicy(registry), ProviderHealthManager()


class TestExplicitSelection:
    def test_default_provider_wins_over_last_registered(self):
        # The audit repro, verbatim: custom_local registered first,
        # custom_work second, custom_local is the configured default.
        registry = _registry_registered("custom_local", "custom_work")
        policy, health = _policy(registry)
        decision = policy.select_route(
            prompt="hello",
            configured_providers=["custom_local", "custom_work"],
            health_manager=health,
            user_default_provider="custom_local",
            user_default_model=SHARED_ID,
            requested_model=SHARED_ID,
        )
        assert decision.provider == "custom_local"
        assert decision.model == SHARED_ID
        assert decision.reason == "User selected Llama3:8b"

    def test_default_provider_wins_in_reverse_registration_order(self):
        registry = _registry_registered("custom_work", "custom_local")
        policy, health = _policy(registry)
        decision = policy.select_route(
            prompt="hello",
            configured_providers=["custom_local", "custom_work"],
            health_manager=health,
            user_default_provider="custom_local",
            user_default_model=SHARED_ID,
            requested_model=SHARED_ID,
        )
        assert decision.provider == "custom_local"

    def test_configured_order_decides_when_default_does_not_offer(self):
        # Registration order says custom_local (registered last);
        # configured order says custom_work (listed first). The
        # configured order must win.
        registry = _registry_registered("custom_work", "custom_local")
        policy, health = _policy(registry)
        decision = policy.select_route(
            prompt="hello",
            configured_providers=["custom_work", "custom_local"],
            health_manager=health,
            user_default_provider="google_gemini",
            user_default_model="gemini-3.5-flash",
            requested_model=SHARED_ID,
        )
        assert decision.provider == "custom_work"

    def test_unavailable_default_provider_falls_to_next_offering(self):
        registry = _registry_registered("custom_local", "custom_work")
        policy, health = _policy(registry)
        state = health.get_state("custom_local")
        state.status = "offline"
        state.consecutive_failures = 5
        state.cooldown_until = time.time() + 3600
        decision = policy.select_route(
            prompt="hello",
            configured_providers=["custom_local", "custom_work"],
            health_manager=health,
            user_default_provider="custom_local",
            user_default_model=SHARED_ID,
            requested_model=SHARED_ID,
        )
        assert decision.provider == "custom_work"

    def test_single_provider_model_routes_as_before(self):
        # Control: a model offered by exactly one configured provider.
        registry = ModelRegistry(models=[
            _entry("custom_local"),
            ModelEntry(provider="openai", id="gpt-4o", label="GPT-4o",
                       capabilities=["chat"], priority=1),
        ])
        policy, health = _policy(registry)
        decision = policy.select_route(
            prompt="hello",
            configured_providers=["custom_local", "openai"],
            health_manager=health,
            user_default_provider="openai",
            user_default_model="gpt-4o",
            requested_model=SHARED_ID,
        )
        assert decision.provider == "custom_local"
        assert decision.model == SHARED_ID

    def test_unregistered_model_inference_unchanged(self):
        # Control: a requested model absent from the registry still
        # goes through the naming-pattern inference branch.
        registry = ModelRegistry(models=[_entry("custom_local")])
        policy, health = _policy(registry)
        decision = policy.select_route(
            prompt="hello",
            configured_providers=["custom_local", "openai"],
            health_manager=health,
            user_default_provider="custom_local",
            user_default_model=SHARED_ID,
            requested_model="gpt-4o",
        )
        assert decision.provider == "openai"
        assert decision.model == "gpt-4o"


# ---------------------------------------------------------------------------
# Vision override: the override provider's OWN entry must be used
# ---------------------------------------------------------------------------

class TestVisionOverride:
    def test_override_uses_its_own_providers_entry(self):
        registry = ModelRegistry(models=[])
        registry.register_model(_entry(
            "custom_local", capabilities=["chat", "vision", "tools"]))
        registry.register_model(_entry(
            "custom_work", capabilities=["chat"]))
        policy, health = _policy(registry)
        decision = policy.select_route(
            prompt="what is in this image?",
            configured_providers=["custom_local", "custom_work"],
            health_manager=health,
            user_default_provider="custom_work",
            user_default_model=SHARED_ID,
            auxiliary_models={"vision": "custom_local:::llama3:8b"},
            images=["img.png"],
        )
        assert decision.provider == "custom_local"
        assert decision.model == SHARED_ID
        # The override provider's real entry capabilities, not the
        # generic stand-ins used when the entry cannot be found.
        assert decision.capabilities == ["chat", "vision", "tools"]
