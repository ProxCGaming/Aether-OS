"""Safe automatic fallback policy and error classification."""
import logging
from typing import List, Optional, Tuple

from aether_engine.providers.base import (
    AuthenticationError,
    InvalidResponseError,
    NetworkError,
    ProviderError,
    RateLimitError,
    TimeoutError,
)
from aether_engine.routing.health import ProviderHealthManager
from aether_engine.routing.registry import GLOBAL_MODEL_REGISTRY, ModelEntry, ModelRegistry

logger = logging.getLogger("aether_engine.routing.fallback")


def classify_provider_error(exc: Exception) -> Tuple[bool, str]:
    """
    Classify an exception into (is_retriable, user_friendly_message).

    Allowed automatic fallback:
    - Timeouts
    - Connection resets / DNS failures / Network errors
    - 429 Rate limits
    - 5xx Server errors

    Forbidden automatic fallback:
    - Authentication / Invalid API key
    - Malformed requests
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

    if isinstance(exc, InvalidResponseError):
        return False, "Invalid response from provider."

    # Default unrecognized exceptions to non-retriable to avoid infinite fallback loops
    return False, f"Provider error: {err_str}"


def is_retriable_error(exc: Exception) -> bool:
    is_retriable, _ = classify_provider_error(exc)
    return is_retriable


def get_fallback_candidates(
    failed_provider: str,
    configured_providers: List[str],
    health_manager: ProviderHealthManager,
    registry: Optional[ModelRegistry] = None,
) -> List[ModelEntry]:
    """Retrieve healthy model candidates from remaining configured providers."""
    reg = registry or GLOBAL_MODEL_REGISTRY
    candidates: List[ModelEntry] = []

    for provider in configured_providers:
        if provider == failed_provider:
            continue
        if health_manager.is_provider_available(provider):
            models = reg.get_models_for_provider(provider)
            if models:
                candidates.append(models[0])

    return candidates
