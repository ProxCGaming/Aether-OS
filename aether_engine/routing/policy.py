"""Intelligent routing policy mapping task requirements and health state to model candidates."""
from dataclasses import dataclass, field
import re
from typing import Any, List, Optional

from aether_engine.routing.health import ProviderHealthManager
from aether_engine.routing.registry import GLOBAL_MODEL_REGISTRY, ModelEntry, ModelRegistry


@dataclass
class TaskRequirements:
    wants_code: bool = False
    wants_reasoning: bool = False
    has_images: bool = False
    prefer_local: bool = False
    low_latency: bool = True


@dataclass
class RoutingDecision:
    provider: str
    model: str
    reason: str
    capabilities: List[str] = field(default_factory=list)


_CODE_KEYWORDS = {
    "code", "python", "javascript", "typescript", "rust", "c++", "c#", "java", "sql",
    "html", "css", "function", "def ", "class ", "bug", "refactor", "algorithm",
    "script", "regex", "api", "endpoint", "syntax", "compile", "traceback"
}

_REASONING_KEYWORDS = {
    "step by step", "deep think", "reasoning", "prove", "math proof", "derivation",
    "complex analysis", "solve riddle", "logic puzzle", "benchmark comparison"
}


def infer_task_requirements(
    prompt: str,
    images: Optional[List[Any]] = None,
    prefer_local: bool = False,
) -> TaskRequirements:
    """Infer task requirements by inspecting prompt tokens and attached assets."""
    has_images = bool(images)
    lower = prompt.lower()

    wants_code = any(kw in lower for kw in _CODE_KEYWORDS) or ("```" in prompt)
    wants_reasoning = any(kw in lower for kw in _REASONING_KEYWORDS)
    low_latency = not wants_reasoning

    return TaskRequirements(
        wants_code=wants_code,
        wants_reasoning=wants_reasoning,
        has_images=has_images,
        prefer_local=prefer_local,
        low_latency=low_latency,
    )


class RoutingPolicy:
    """Evaluates task requirements, provider availability, and user preferences to select models."""

    def __init__(self, registry: Optional[ModelRegistry] = None):
        self.registry = registry or GLOBAL_MODEL_REGISTRY

    def select_route(
        self,
        prompt: str,
        configured_providers: List[str],
        health_manager: ProviderHealthManager,
        user_default_provider: str = "google_gemini",
        user_default_model: str = "gemini-2.5-flash",
        requirements: Optional[TaskRequirements] = None,
        requested_model: Optional[str] = None,
    ) -> RoutingDecision:
        reqs = requirements or infer_task_requirements(prompt)

        # If user explicitly requested a specific model and its provider is healthy
        if requested_model:
            model_entry = self.registry.get_model(requested_model)
            if model_entry and (model_entry.provider in configured_providers):
                if health_manager.is_provider_available(model_entry.provider):
                    return RoutingDecision(
                        provider=model_entry.provider,
                        model=model_entry.id,
                        reason=f"User selected {model_entry.label}",
                        capabilities=list(model_entry.capabilities),
                    )
            elif not model_entry:
                # Infer provider from model naming patterns if not in registry
                inferred_prov = None
                if ("/" in requested_model and not requested_model.startswith("models/")) and "openrouter" in configured_providers:
                    inferred_prov = "openrouter"
                elif (requested_model.startswith("gemini") or requested_model.startswith("models/gemini")) and "google_gemini" in configured_providers:
                    inferred_prov = "google_gemini"
                elif requested_model.startswith("claude") and "anthropic" in configured_providers:
                    inferred_prov = "anthropic"
                elif requested_model.startswith("deepseek") and "deepseek" in configured_providers:
                    inferred_prov = "deepseek"
                elif (requested_model.startswith("gpt-") or requested_model.startswith("o1") or requested_model.startswith("o3")) and "openai" in configured_providers:
                    inferred_prov = "openai"

                target_prov = inferred_prov or (user_default_provider if user_default_provider in configured_providers else (configured_providers[0] if configured_providers else "google_gemini"))
                if health_manager.is_provider_available(target_prov):
                    return RoutingDecision(
                        provider=target_prov,
                        model=requested_model,
                        reason=f"User selected {requested_model}",
                        capabilities=["chat", "fast", "tools", "vision"],
                    )

        # Filter all models for configured providers that are currently healthy/available
        available_models: List[ModelEntry] = []
        for p in configured_providers:
            if health_manager.is_provider_available(p):
                available_models.extend(self.registry.get_models_for_provider(p))

        if not available_models:
            # Fallback to configured default even if in cooldown or unkeyed
            return RoutingDecision(
                provider=user_default_provider,
                model=user_default_model,
                reason="Default provider selected (no alternative healthy providers available)",
                capabilities=["chat"],
            )

        # Check for specialized requirements
        if reqs.has_images:
            vision_models = [m for m in available_models if "vision" in m.capabilities]
            if vision_models:
                best = vision_models[0]
                return RoutingDecision(
                    provider=best.provider,
                    model=best.id,
                    reason=f"Routed to {best.label} — vision & image processing",
                    capabilities=list(best.capabilities),
                )

        if reqs.wants_reasoning:
            reasoning_models = [m for m in available_models if "reasoning" in m.capabilities]
            if reasoning_models:
                best = reasoning_models[0]
                return RoutingDecision(
                    provider=best.provider,
                    model=best.id,
                    reason=f"Routed to {best.label} — deep reasoning & complex logic",
                    capabilities=list(best.capabilities),
                )

        if reqs.wants_code:
            code_models = [m for m in available_models if "code" in m.capabilities]
            if code_models:
                best = code_models[0]
                return RoutingDecision(
                    provider=best.provider,
                    model=best.id,
                    reason=f"Routed to {best.label} — code generation & tool capabilities",
                    capabilities=list(best.capabilities),
                )

        # Standard / Fast chat: Check if user's default model is available
        for m in available_models:
            if m.provider == user_default_provider and m.id == user_default_model:
                return RoutingDecision(
                    provider=m.provider,
                    model=m.id,
                    reason=f"Routed to {m.label} — fast chat + tool support",
                    capabilities=list(m.capabilities),
                )

        # Otherwise pick the highest priority available model for user default provider or next provider
        fallback_model = available_models[0]
        reason = f"Routed to {fallback_model.label} — optimal available model"
        if fallback_model.provider != user_default_provider:
            reason = f"Routed to {fallback_model.label} — fallback from {user_default_provider}"

        return RoutingDecision(
            provider=fallback_model.provider,
            model=fallback_model.id,
            reason=reason,
            capabilities=list(fallback_model.capabilities),
        )


GLOBAL_ROUTING_POLICY = RoutingPolicy()
