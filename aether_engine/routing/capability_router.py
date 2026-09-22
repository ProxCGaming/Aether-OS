"""Transparent capability-based auto-router for task dispatching.

Analyzes task prompt intent (coding, multi-step reasoning, vision, or general conversation),
queries reachable model capabilities across healthy configured providers, and routes
to the most optimal candidate model.
"""
from dataclasses import dataclass, field
import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from aether_engine.capability.benchmark_cache import get_benchmark_for_model
from aether_engine.routing.health import ProviderHealthManager
from aether_engine.routing.registry import GLOBAL_MODEL_REGISTRY, ModelEntry, ModelRegistry

logger = logging.getLogger("aether_engine.routing.capability_router")


@dataclass
class TaskCapabilityProfile:
    required_tags: Set[str] = field(default_factory=set)
    preferred_tier: str = "balanced"  # "fast", "balanced", "premium"
    has_images: bool = False
    prefer_local: bool = False
    intent: str = "general"  # "code", "reasoning", "vision", "chat", "general"


# Keyword banks for fast, transparent intent detection
_CODE_PATTERNS = [
    r"```[a-zA-Z0-9_\-\+]*",
    r"\bdef\s+[a-zA-Z_]",
    r"\bclass\s+[a-zA-Z_]",
    r"\bimport\s+[a-zA-Z_]",
    r"\bfunction\b",
    r"\basync\s+def\b",
    r"\bjson\b",
    r"\bsql\b",
    r"\bapi\b",
    r"\bendpoint\b",
    r"\btraceback\b",
    r"\bexception\b",
    r"\brefactor\b",
    r"\bwrite\s+(a\s+)?(python|javascript|typescript|c\+\+|rust|go|bash|powershell|sql)\b",
]

_REASONING_PATTERNS = [
    r"\bstep\s+by\s+step\b",
    r"\bprove\b",
    r"\bmathematical(ly)?\b",
    r"\bderivation\b",
    r"\bdeduce\b",
    r"\blogic\s+puzzle\b",
    r"\bdeep\s+think\b",
    r"\bcalculate\b",
    r"\bmultiplied\s+by\b",
    r"\bdivide(d)?\s+by\b",
]


def infer_capability_profile(
    prompt: str,
    has_images: bool = False,
    prefer_local: bool = False,
) -> TaskCapabilityProfile:
    """Analyze a user prompt to determine required capability tags and preferred tier."""
    lower = prompt.lower()
    required_tags: Set[str] = {"chat"}
    intent = "general"
    tier = "balanced"

    if has_images:
        required_tags.add("vision")
        intent = "vision"

    is_code = any(re.search(pat, lower, re.IGNORECASE) for pat in _CODE_PATTERNS)
    is_reasoning = any(re.search(pat, lower, re.IGNORECASE) for pat in _REASONING_PATTERNS)

    if is_code:
        required_tags.add("code")
        intent = "code"
        tier = "balanced"

    if is_reasoning:
        required_tags.add("reasoning")
        intent = "reasoning"
        tier = "premium"

    if not is_code and not is_reasoning and not has_images:
        if len(prompt.split()) < 20:
            required_tags.add("fast")
            tier = "fast"
            intent = "chat"

    if prefer_local:
        required_tags.add("local")

    return TaskCapabilityProfile(
        required_tags=required_tags,
        preferred_tier=tier,
        has_images=has_images,
        prefer_local=prefer_local,
        intent=intent,
    )


class CapabilityRouter:
    """Routes incoming prompts to the best healthy, reachable model based on capability matching."""

    def __init__(self, registry: Optional[ModelRegistry] = None):
        self.registry = registry or GLOBAL_MODEL_REGISTRY

    def route_task(
        self,
        prompt: str,
        configured_providers: List[str],
        health_manager: ProviderHealthManager,
        user_default_provider: str,
        user_default_model: str,
        has_images: bool = False,
        prefer_local: bool = False,
        requested_model: Optional[str] = None,
        capabilities_db_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Select optimal provider and model based on capability matching."""
        profile = infer_capability_profile(prompt, has_images=has_images, prefer_local=prefer_local)

        # 1. If explicit model requested and its provider is healthy, honor explicit request
        if requested_model:
            model_entry = self.registry.get_model(requested_model)
            if model_entry and model_entry.provider in configured_providers:
                if health_manager.is_provider_available(model_entry.provider):
                    return {
                        "provider": model_entry.provider,
                        "model": model_entry.id,
                        "reason": f"Explicitly selected {model_entry.label}",
                        "capabilities": list(model_entry.capabilities),
                        "intent": profile.intent,
                        "routed_by": "explicit",
                    }
            elif not model_entry:
                # Custom / ad-hoc model string
                target_prov = user_default_provider if user_default_provider in configured_providers else (configured_providers[0] if configured_providers else "google_gemini")
                if "/" in requested_model and "openrouter" in configured_providers:
                    target_prov = "openrouter"
                return {
                    "provider": target_prov,
                    "model": requested_model,
                    "reason": f"User requested {requested_model}",
                    "capabilities": list(profile.required_tags),
                    "intent": profile.intent,
                    "routed_by": "explicit",
                }

        # 2. Gather healthy candidate models across all configured providers
        candidates: List[Tuple[str, str, List[str], str]] = []  # (provider, model_id, capabilities, tier)

        for p in configured_providers:
            if not health_manager.is_provider_available(p):
                continue
            models = self.registry.get_models_for_provider(p)
            for m in models:
                bench = get_benchmark_for_model(m.id) or {}
                caps = list(set(list(m.capabilities) + bench.get("capabilities", [])))
                tier = bench.get("tier", "balanced")
                candidates.append((p, m.id, caps, tier))

        if not candidates:
            # Fallback to user default
            return {
                "provider": user_default_provider,
                "model": user_default_model,
                "reason": "Default provider fallback (no healthy candidates found)",
                "capabilities": ["chat"],
                "intent": profile.intent,
                "routed_by": "fallback",
            }

        # 3. Score candidate models by capability match and tier affinity
        def _score(candidate: Tuple[str, str, List[str], str]) -> float:
            p, mid, caps, tier = candidate
            score = 0.0
            cap_set = set(caps)

            # Match required tags
            matched_tags = profile.required_tags.intersection(cap_set)
            score += len(matched_tags) * 10.0

            # Missing required tag penalty
            missing_tags = profile.required_tags - cap_set
            score -= len(missing_tags) * 15.0

            # Tier bonus
            if tier == profile.preferred_tier:
                score += 5.0
            elif profile.preferred_tier == "premium" and tier == "balanced":
                score += 2.0

            # User default model strong preference bonus
            if p == user_default_provider and mid == user_default_model:
                score += 100.0

            return score

        candidates.sort(key=_score, reverse=True)
        best = candidates[0]
        p_best, m_best, caps_best, tier_best = best

        return {
            "provider": p_best,
            "model": m_best,
            "reason": f"Auto-routed for {profile.intent.upper()} intent matching [{', '.join(sorted(profile.required_tags))}]",
            "capabilities": caps_best,
            "intent": profile.intent,
            "tier": tier_best,
            "routed_by": "capability_matcher",
        }

    async def select(self, intent: str = "general") -> tuple[str, str, str]:
        """Select a model string (provider/model) for a given intent. 
        Used by LangGraph nodes.
        Returns (litellm_model_string, api_key, api_base)"""
        from aether_engine.config import load_config
        from aether_engine.providers.litellm_provider import resolve_litellm_model
        from aether_engine.secrets.storage import SecretStore
        cfg = load_config()
        provider = cfg.default_provider
        model_str = resolve_litellm_model(cfg.default_model, provider)
        
        try:
            api_key = SecretStore().load_provider(provider)
        except Exception:
            api_key = ""
            
        base_url = cfg.custom_base_urls.get(provider, "")
        return model_str, api_key, base_url


# Global capability router singleton
GLOBAL_CAPABILITY_ROUTER = CapabilityRouter()
