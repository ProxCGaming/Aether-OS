"""Safe automatic fallback policy and error classification."""
import logging

from aether_engine.providers.base import (
    AuthenticationError,
    InvalidResponseError,
    NetworkError,
    RateLimitError,
    TimeoutError,
)
from aether_engine.routing.health import ProviderHealthManager
from aether_engine.routing.registry import (
    GLOBAL_MODEL_REGISTRY,
    ModelEntry,
    ModelRegistry,
    is_local_provider,
)

logger = logging.getLogger("aether_engine.routing.fallback")


def classify_provider_error(exc: Exception) -> tuple[bool, str]:
    """
    Classify an exception into (is_retriable, user_friendly_message).

    Allowed automatic fallback:
    - Timeouts
    - Connection resets / DNS failures / Network errors
    - 429 Rate limits
    - 5xx Server errors
    - 400 Model not found / invalid model (try next fallback)

    Forbidden automatic fallback:
    - Authentication / Invalid API key
    - Malformed requests (other than model not found)
    - Policy / Safety rejections
    """
    if isinstance(exc, TimeoutError):
        return True, "Provider timed out."
    if isinstance(exc, RateLimitError):
        return True, "Rate limit exceeded (429)."
    if isinstance(exc, NetworkError):
        return True, "Network connection failure."

    err_str = str(exc)
    if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
        return True, "Rate limit exceeded."
    if "503" in err_str or "502" in err_str or "504" in err_str or "500" in err_str:
        return True, "Temporary server error."
    if "ConnectError" in err_str or "Timeout" in err_str or "RemoteDisconnected" in err_str:
        return True, "Network timeout or connection reset."

    if isinstance(exc, AuthenticationError) or "API_KEY_INVALID" in err_str or "401" in err_str or "403" in err_str:
        return False, "Authentication failed (invalid API key)."

    # 400 errors: check if it's a "model not found" / "invalid model" error - these should be retriable for fallback
    if "400" in err_str:
        model_not_found_indicators = [
            "not a valid model",
            "model not found",
            "invalid model",
            "does not exist",
            "unknown model",
            "model.*not.*support",
            "no such model",
        ]
        import re
        for pattern in model_not_found_indicators:
            if re.search(pattern, err_str, re.IGNORECASE):
                return True, f"Model not available: {err_str}"

    if isinstance(exc, InvalidResponseError):
        return False, "Invalid response from provider."

    # Default unrecognized exceptions to non-retriable to avoid infinite fallback loops
    return False, f"Provider error: {err_str}"


def is_retriable_error(exc: Exception) -> bool:
    is_retriable, _ = classify_provider_error(exc)
    return is_retriable


def get_fallback_candidates(
    failed_provider: str,
    configured_providers: list[str],
    health_manager: ProviderHealthManager,
    registry: ModelRegistry | None = None,
    *,
    local_only: bool = False,
    require_vision: bool = False,
) -> list[ModelEntry]:
    """Retrieve healthy model candidates from remaining configured providers.

    Constraints (finding M17) — both default off, preserving the
    unconstrained Epic 5 behaviour for ordinary routes:

    - ``local_only``: only providers whose models run on the user's
      machine are eligible. A route pinned local (e.g. an explicit
      local vision override) must fail honestly rather than be
      re-routed to a cloud provider.
    - ``require_vision``: only vision-capable models are eligible,
      so a request carrying images is never retried on a model that
      cannot see them (which would silently drop the image).
    """
    reg = registry or GLOBAL_MODEL_REGISTRY
    candidates: list[ModelEntry] = []

    for provider in configured_providers:
        if provider == failed_provider:
            continue
        if local_only and not is_local_provider(provider):
            continue
        if health_manager.is_provider_available(provider):
            models = reg.get_models_for_provider(provider)
            if require_vision:
                models = [m for m in models if "vision" in m.capabilities]
            if models:
                candidates.append(models[0])

    return candidates


def get_configured_fallback_candidates(
    failed_provider: str,
    config,
    configured_providers: list[str],
    health_manager: ProviderHealthManager,
    registry: ModelRegistry | None = None,
    *,
    local_only: bool = False,
    require_vision: bool = False,
) -> list[ModelEntry]:
    """The single fallback-policy authority (finding E5-F02).

    Every fallback enforcement point — per-call candidate computation
    in ``orchestration.core._create_provider_instance`` and the
    task-level retry factory in ``routes.websocket`` — derives its
    cross-provider candidates HERE, from the SAME config object, so
    the user's fallback configuration governs both points with one
    interpretation. ``config`` is whichever ``UserConfig`` is
    authoritative at the call site: the task's effective (layered)
    config for task paths, the global config elsewhere.

    Interpretation, identical at both points:

    - ``disable_fallbacks`` — no candidates, ever.
    - ``fallback_chain`` non-empty — the chain is the ordered
      authority: for each provider in chain order (skipping the
      failed provider, unavailable providers, and — under
      ``local_only`` — non-local providers), that provider's first
      chat-capable model (vision-capable too, under
      ``require_vision``).
    - No chain — the health-derived derivation
      (:func:`get_fallback_candidates`) over the configured
      providers, unchanged.
    """
    if getattr(config, "disable_fallbacks", False):
        return []
    chain = getattr(config, "fallback_chain", None) or []
    if not chain:
        return get_fallback_candidates(
            failed_provider,
            configured_providers,
            health_manager,
            registry=registry,
            local_only=local_only,
            require_vision=require_vision,
        )
    reg = registry or GLOBAL_MODEL_REGISTRY
    candidates: list[ModelEntry] = []
    for provider in chain:
        if provider == failed_provider:
            continue
        if local_only and not is_local_provider(provider):
            continue
        if not health_manager.is_provider_available(provider):
            continue
        for entry in reg.get_models_for_provider(provider):
            if "chat" not in entry.capabilities:
                continue
            if require_vision and "vision" not in entry.capabilities:
                continue
            candidates.append(entry)
            break
    return candidates
