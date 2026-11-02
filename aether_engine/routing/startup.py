"""Startup consistency validation across config.json, SecretStore, and model registry."""
import logging
from dataclasses import dataclass

from aether_engine.config import CLOUD_PROVIDERS, UserConfig
from aether_engine.routing.health import ProviderHealthManager
from aether_engine.routing.registry import (
    GLOBAL_MODEL_REGISTRY,
    ModelRegistry,
)
from aether_engine.secrets.storage import SecretStore

logger = logging.getLogger("aether_engine.routing.startup")


@dataclass
class StartupValidationResult:
    active_provider: str
    active_model: str
    configured_providers: list[str]
    was_fallback: bool
    fallback_reason: str | None = None


_CLOUD_PROVIDER_NAMES = {p.name for p in CLOUD_PROVIDERS}


def is_keyless_custom_provider(provider_name: str, user_config: UserConfig) -> bool:
    """True when *provider_name* is a custom OpenAI-compatible provider
    whose saved configuration is complete WITHOUT a secret (ADR 0013).

    Self-hosted OpenAI-compatible servers are first-class and
    legitimately keyless: PROVIDER_SAVE accepts them with no key, and
    a saved base URL is the act of configuration. The provider's
    saved SHAPE decides — never the name alone:

    - a non-empty saved base URL is required (without one there is
      no endpoint to talk to, so the configuration is incomplete);
    - the provider must be an OpenAI-compatible custom: a recorded
      ``custom_provider_types`` entry is authoritative (the anthropic
      flavour still requires its key — its dispatch is E5-F05's
      territory); with no recorded type, the legacy ``custom_openai``
      slot qualifies, and so does any dynamic provider absent from
      CLOUD_PROVIDERS (dynamic customs default to openai_compatible,
      the same default the provider-list builders use). Built-in
      cloud providers always require a key.
    """
    if provider_name == "ollama":
        # The local daemon's keylessness is the factory's own rule;
        # it is not a custom provider and never decided by this shape.
        return False
    base_url = (user_config.custom_base_urls.get(provider_name) or "").strip()
    if not base_url:
        return False
    provider_type = user_config.custom_provider_types.get(provider_name)
    if provider_type is not None:
        return provider_type == "openai_compatible"
    if provider_name == "custom_openai":
        return True
    return provider_name not in _CLOUD_PROVIDER_NAMES


def derive_configured_providers(
    user_config: UserConfig,
    secret_store: SecretStore,
    registry: ModelRegistry | None = None,
) -> list[str]:
    """The single derivation of "configured" (E5-F04).

    A provider is configured when it has a stored secret OR it is a
    keyless custom provider whose saved configuration is complete
    (:func:`is_keyless_custom_provider`). Catalog presence is
    enforced by iterating the registry — a provider with no known
    models cannot be routed to and is not listed.

    Startup validation, engine state initialisation and the START
    handler all derive from here, so "configured" means one thing
    everywhere. Previously each site re-derived it from secrets
    alone, and a saved keyless custom provider — accepted by
    PROVIDER_SAVE and shown as configured on its provider card —
    was invisible to all of them.
    """
    reg = registry or GLOBAL_MODEL_REGISTRY
    stored_names = set(secret_store.list_providers())
    configured_providers: list[str] = []
    for m in reg.list_all_models():
        if m.provider in configured_providers:
            continue
        if m.provider in stored_names or is_keyless_custom_provider(m.provider, user_config):
            configured_providers.append(m.provider)
    return configured_providers


def validate_startup_configuration(
    user_config: UserConfig,
    secret_store: SecretStore,
    health_manager: ProviderHealthManager,
    registry: ModelRegistry | None = None,
) -> StartupValidationResult:
    """
    Validate config/secret/health consistency on Engine startup.

    Consistency rules:
    1. Config references provider with no secret -> Mark unconfigured, fallback to next keyed provider.
    2. Secret exists for unknown provider -> Ignore.
    3. Model missing from provider -> Fallback to provider's default model.
    4. Default provider offline -> Select next healthy configured provider.
    5. Does NOT overwrite config.json.
    """
    reg = registry or GLOBAL_MODEL_REGISTRY

    # 1. Discover all configured providers (stored secrets, plus
    # keyless custom providers with a complete saved configuration)
    # — the single shared derivation (E5-F04).
    configured_providers = derive_configured_providers(user_config, secret_store, reg)

    desired_provider = user_config.default_provider
    desired_model = user_config.default_model

    # Check if desired provider is configured
    if desired_provider not in configured_providers:
        if configured_providers:
            fallback_provider = configured_providers[0]
            fallback_model_entry = reg.get_default_model_for_provider(fallback_provider)
            fallback_model = fallback_model_entry.id if fallback_model_entry else "default"
            reason = f"Configured provider '{desired_provider}' has no stored API key; falling back to '{fallback_provider}'."
            logger.warning(reason)
            return StartupValidationResult(
                active_provider=fallback_provider,
                active_model=fallback_model,
                configured_providers=configured_providers,
                was_fallback=True,
                fallback_reason=reason,
            )
        else:
            # No providers have keys configured yet
            default_entry = reg.get_default_model_for_provider(desired_provider)
            fallback_model = default_entry.id if default_entry else desired_model
            return StartupValidationResult(
                active_provider=desired_provider,
                active_model=fallback_model,
                configured_providers=[],
                was_fallback=False,
                fallback_reason=None,
            )

    # Provider is configured; check if provider is healthy
    if not health_manager.is_provider_available(desired_provider):
        # Look for alternative healthy configured provider
        for alt_provider in configured_providers:
            if alt_provider != desired_provider and health_manager.is_provider_available(alt_provider):
                alt_model_entry = reg.get_default_model_for_provider(alt_provider)
                alt_model = alt_model_entry.id if alt_model_entry else "default"
                reason = f"Default provider '{desired_provider}' is offline; active model set to '{alt_model}' ({alt_provider})."
                logger.warning(reason)
                return StartupValidationResult(
                    active_provider=alt_provider,
                    active_model=alt_model,
                    configured_providers=configured_providers,
                    was_fallback=True,
                    fallback_reason=reason,
                )

    # Provider is healthy; verify model exists for this provider
    provider_models = reg.get_models_for_provider(desired_provider)
    valid_model_ids = {m.id for m in provider_models}
    if desired_model not in valid_model_ids:
        default_model_entry = reg.get_default_model_for_provider(desired_provider)
        corrected_model = default_model_entry.id if default_model_entry else desired_model
        reason = f"Model '{desired_model}' not found for provider '{desired_provider}'; using default '{corrected_model}'."
        logger.info(reason)
        return StartupValidationResult(
            active_provider=desired_provider,
            active_model=corrected_model,
            configured_providers=configured_providers,
            was_fallback=True,
            fallback_reason=reason,
        )

    # Everything is consistent
    return StartupValidationResult(
        active_provider=desired_provider,
        active_model=desired_model,
        configured_providers=configured_providers,
        was_fallback=False,
        fallback_reason=None,
    )
