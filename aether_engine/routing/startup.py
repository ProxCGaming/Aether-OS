"""Startup consistency validation across config.json, SecretStore, and model registry."""
from dataclasses import dataclass
import logging
from typing import List, Optional, Tuple

from aether_engine.config import UserConfig
from aether_engine.routing.health import ProviderHealthManager
from aether_engine.routing.registry import GLOBAL_MODEL_REGISTRY, ModelEntry, ModelRegistry
from aether_engine.secrets.storage import SecretStore

logger = logging.getLogger("aether_engine.routing.startup")


@dataclass
class StartupValidationResult:
    active_provider: str
    active_model: str
    configured_providers: List[str]
    was_fallback: bool
    fallback_reason: Optional[str] = None


def validate_startup_configuration(
    user_config: UserConfig,
    secret_store: SecretStore,
    health_manager: ProviderHealthManager,
    registry: Optional[ModelRegistry] = None,
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

    # 1. Discover all providers that have valid stored secrets and are in registry
    stored_names = set(secret_store.list_providers())
    configured_providers: List[str] = []
    for m in reg.list_all_models():
        if m.provider in stored_names and m.provider not in configured_providers:
            configured_providers.append(m.provider)

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
