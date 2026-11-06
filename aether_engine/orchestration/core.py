"""Orchestration facade — thin re-export shim (Wave 5 craft split).

This module used to hold provider discovery, key validation and
provider construction in one 545-line file. The implementations
now live in focused modules — ``orchestration.discovery``,
``orchestration.validation`` and ``orchestration.factory`` — and
this module re-exports every name it has ever provided: ~15 test
files, ``routes/websocket.py`` and the langgraph nodes import (or
patch) names on this namespace, so it stays as the package's
stable surface. New code should import from the focused modules
directly.

The patch surface is contractual, not incidental: tests patch
``fetch_available_models``, ``validate_api_key``, ``save_config``,
``_validate_provider_key``, ``_validate_provider_key_inner`` and
``_create_provider_instance`` on THIS namespace, and the focused
modules resolve those names back through here at call time.
"""

import asyncio
import logging
import time
from typing import Any

from aether_engine.config import (
    CLOUD_PROVIDERS,
    anchor_default_model,
    custom_provider_flavor,
    save_config,
)
from aether_engine.orchestration.discovery import (
    DISCOVERY_FAILURE_TTL_SECONDS,
    _apply_discovery,
    _build_provider_list_async,
    _discover_cloud_provider_models,
    _discover_custom_provider_models,
    _discovery_failures,
    clear_discovery_failure,
    discover_pending_provider_models,
    discovery_recently_failed,
    record_discovery_failure,
)
from aether_engine.orchestration.factory import (
    _create_provider_instance,
    _load_provider_key,
    create_tiny_local_aux_provider,
)
from aether_engine.orchestration.validation import (
    MAX_VALIDATION_PINGS,
    _inflight_validations,
    _validate_provider_key,
    _validate_provider_key_inner,
    validate_provider_key_shared,
)
from aether_engine.providers.discovery import fetch_available_models
from aether_engine.providers.litellm_provider import LiteLLMProvider, validate_api_key
from aether_engine.routing.fallback import (
    get_configured_fallback_candidates,
)
from aether_engine.routing.registry import is_local_provider
from aether_engine.routing.startup import is_keyless_custom_provider
from aether_engine.secrets.storage import ProviderNotFoundError, SecretStore
from aether_engine.state import engine_state

logger = logging.getLogger(__name__)

__all__ = [
    "CLOUD_PROVIDERS",
    "DISCOVERY_FAILURE_TTL_SECONDS",
    "MAX_VALIDATION_PINGS",
    "Any",
    "LiteLLMProvider",
    "ProviderNotFoundError",
    "SecretStore",
    "_apply_discovery",
    "_build_provider_list_async",
    "_create_provider_instance",
    "_discover_cloud_provider_models",
    "_discover_custom_provider_models",
    "_discovery_failures",
    "_inflight_validations",
    "_load_provider_key",
    "_validate_provider_key",
    "_validate_provider_key_inner",
    "anchor_default_model",
    "asyncio",
    "clear_discovery_failure",
    "create_tiny_local_aux_provider",
    "custom_provider_flavor",
    "discover_pending_provider_models",
    "discovery_recently_failed",
    "engine_state",
    "fetch_available_models",
    "get_configured_fallback_candidates",
    "is_keyless_custom_provider",
    "is_local_provider",
    "logger",
    "logging",
    "record_discovery_failure",
    "save_config",
    "time",
    "validate_api_key",
    "validate_provider_key_shared",
]
