"""Intelligent routing policy mapping task requirements and health state to model candidates."""
from dataclasses import dataclass, field
from typing import Any

from aether_engine.routing.health import ProviderHealthManager
from aether_engine.routing.latency import get_latency_store
from aether_engine.routing.registry import (
    GLOBAL_MODEL_REGISTRY,
    ModelEntry,
    ModelRegistry,
    is_local_provider,
)


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
    capabilities: list[str] = field(default_factory=list)
    # Finding M17: True when this route is pinned to a local provider
    # by the owner's explicit configuration (the auxiliary vision
    # override). Every fallback layer must honour it — a local-only
    # route fails honestly rather than re-routing to a cloud model.
    local_only: bool = False


_CODE_KEYWORDS = {
    "code", "sql", "html", "css", "function", "def ", "class ", "bug", "refactor",
    "algorithm", "script", "regex", "api", "endpoint", "syntax", "compile", "traceback"
}

_REASONING_KEYWORDS = {
    "step by step", "deep think", "reasoning", "prove", "math proof", "derivation",
    "complex analysis", "solve riddle", "logic puzzle", "benchmark comparison"
}


def infer_task_requirements(
    prompt: str,
    images: list[Any] | None = None,
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


def parse_vision_override(
    auxiliary_models: dict[str, str] | None,
) -> tuple[str, str] | None:
    """Parse the explicit auxiliary vision override, if one is set.

    Stored as "provider:::model"; a "provider/model" string or a bare
    model name are also accepted (the same shapes the media analyzer
    resolves). Returns ``(provider, model)`` — the provider is "" for
    a bare model name — or None when the override is absent/"auto".
    """
    vision_override = (auxiliary_models or {}).get("vision", "auto")
    if not vision_override or vision_override == "auto":
        return None
    if ":::" in vision_override:
        provider, _, model = vision_override.partition(":::")
    elif "/" in vision_override and not vision_override.startswith("models/"):
        provider, model = vision_override.split("/", 1)
    else:
        provider, model = "", vision_override
    return provider, model


def local_only_from_vision_override(
    auxiliary_models: dict[str, str] | None,
) -> bool:
    """True when the explicit vision override pins image work local.

    This is exactly the derivation ``RoutingPolicy.select_route``
    stamps onto the RoutingDecision as ``local_only`` (finding M17).
    Paths that never see the original decision — above all the
    approval-resume path, which rebuilds providers long after the
    decision was made — re-derive the constraint from the same
    source so it cannot silently lapse across a pause.
    """
    override = parse_vision_override(auxiliary_models)
    return bool(override) and is_local_provider(override[0])


class RoutingPolicy:
    """Evaluates task requirements, provider availability, and user preferences to select models."""

    def __init__(
        self,
        registry: ModelRegistry | None = None,
        latency_store=None,
    ):
        self.registry = registry or GLOBAL_MODEL_REGISTRY
        # E5-F11: the measured-latency store consulted by the
        # tie-break in _pick. None resolves the process-global store
        # lazily at selection time; tests inject a tmp store.
        self._latency_store = latency_store

    def _pick(self, candidates: list[ModelEntry]) -> tuple[ModelEntry, bool]:
        """Choose from an already-ordered candidate list.

        Returns ``(chosen, latency_decided)``. The default is
        exactly today's behaviour — the first candidate.

        E5-F11 measured-latency tie-break: within one priority tier,
        candidates WITH a measured TTFT percentile are re-ordered
        among their own slots by p50 TTFT (fastest first). This is
        deliberately the weakest rule that honours "routing uses
        measured latency":

        * it never crosses a priority tier;
        * it never displaces a candidate with no measurements —
          unmeasured models keep their exact slots, so cold start
          (no measurements anywhere) is byte-identical to the
          pre-E5-F11 selection;
        * it is consulted only where the existing order picks from
          a candidate list — never on the explicit-request path,
          the vision-override path, or the user's default-model
          pin, and health eligibility has already filtered the list
          before this runs;
        * any store failure degrades to today's order (fail-open).
        """
        if len(candidates) < 2:
            return candidates[0], False
        try:
            store = (
                self._latency_store
                if self._latency_store is not None
                else get_latency_store()
            )
            scores: dict[int, float] = {}
            for idx, entry in enumerate(candidates):
                value = store.ttft_p50_ms(entry.provider, entry.id)
                if value is not None:
                    scores[idx] = value
        except Exception:  # noqa: BLE001 — measured latency is an input, never a gate.
            return candidates[0], False
        if len(scores) < 2:
            return candidates[0], False
        slots_by_priority: dict[int, list[int]] = {}
        for idx, entry in enumerate(candidates):
            slots_by_priority.setdefault(entry.priority, []).append(idx)
        ordered = list(candidates)
        for slots in slots_by_priority.values():
            measured = [i for i in slots if i in scores]
            if len(measured) < 2:
                continue
            ranked = sorted(measured, key=lambda i: (scores[i], i))
            for slot, source in zip(measured, ranked, strict=True):
                ordered[slot] = candidates[source]
        return ordered[0], ordered[0] is not candidates[0]

    def select_route(
        self,
        prompt: str,
        configured_providers: list[str],
        health_manager: ProviderHealthManager,
        user_default_provider: str = "google_gemini",
        user_default_model: str = "gemini-3.5-flash",
        requirements: TaskRequirements | None = None,
        requested_model: str | None = None,
        auxiliary_models: dict[str, str] | None = None,
        images: list[Any] | None = None,
    ) -> RoutingDecision:
        reqs = requirements or infer_task_requirements(prompt, images=images)

        # If user explicitly requested a specific model and its provider is healthy
        if requested_model:
            # E5-F03: a bare model ID can be offered by several
            # providers at once, so no single registry entry decides
            # the route. Resolution is by configuration, never by
            # registry registration order: the entry from the user's
            # configured default provider wins first, then providers
            # in configured order; the first candidate in that
            # precedence whose provider is available serves.
            model_entries = self.registry.get_models_by_id(requested_model)
            if model_entries:
                offering = [
                    e for e in model_entries
                    if e.provider in configured_providers
                ]

                def _precedence(entry: ModelEntry) -> tuple[int, int]:
                    if entry.provider == user_default_provider:
                        return (0, 0)
                    return (1, configured_providers.index(entry.provider))

                for model_entry in sorted(offering, key=_precedence):
                    if health_manager.is_provider_available(model_entry.provider):
                        return RoutingDecision(
                            provider=model_entry.provider,
                            model=model_entry.id,
                            reason=f"User selected {model_entry.label}",
                            capabilities=list(model_entry.capabilities),
                        )
            else:
                # Infer provider from model naming patterns if not in registry
                inferred_prov = None
                if ("/" in requested_model and not requested_model.startswith("models/")) and "openrouter" in configured_providers:
                    inferred_prov = "openrouter"
                elif (requested_model.startswith(("gemini", "models/gemini"))) and "google_gemini" in configured_providers:
                    inferred_prov = "google_gemini"
                elif requested_model.startswith("claude") and "anthropic" in configured_providers:
                    inferred_prov = "anthropic"
                elif requested_model.startswith("deepseek") and "deepseek" in configured_providers:
                    inferred_prov = "deepseek"
                elif (requested_model.startswith(("gpt-", "o1", "o3"))) and "openai" in configured_providers:
                    inferred_prov = "openai"

                target_prov = inferred_prov or (user_default_provider if user_default_provider in configured_providers else (configured_providers[0] if configured_providers else "google_gemini"))
                if health_manager.is_provider_available(target_prov):
                    return RoutingDecision(
                        provider=target_prov,
                        model=requested_model,
                        reason=f"User selected {requested_model}",
                        capabilities=["chat", "fast", "tools", "vision"],
                    )

        # Phase 9, B1 + finding M17: an explicit auxiliary vision
        # override is resolved HERE — before the model-discovery
        # fallback below. Previously the empty-discovery default was
        # returned first, so an explicit LOCAL vision override
        # silently lost to the cloud default whenever discovery found
        # no healthy model. Acceptance mirrors the historical rule:
        # the local provider (ollama) is always accepted; any other
        # provider must be configured and healthy. An override that is
        # not accepted falls through to the heuristics, as before.
        if reqs.has_images:
            vision_override = parse_vision_override(auxiliary_models)
            if vision_override is not None:
                o_provider, o_model = vision_override
                if o_provider == "ollama" or (
                    o_provider in configured_providers
                    and health_manager.is_provider_available(o_provider)
                ):
                    # E5-F03: provider-qualified — the override names
                    # its provider explicitly, so only THAT provider's
                    # entry for the model may supply the capabilities.
                    o_entry = self.registry.get_model(o_model, provider=o_provider or None)
                    if o_entry and o_entry.provider == o_provider:
                        return RoutingDecision(
                            provider=o_provider,
                            model=o_entry.id,
                            reason=f"User auxiliary override (vision) → {o_entry.label}",
                            capabilities=list(o_entry.capabilities),
                            local_only=is_local_provider(o_provider),
                        )
                    return RoutingDecision(
                        provider=o_provider,
                        model=o_model,
                        reason=f"User auxiliary override (vision) → {o_model}",
                        capabilities=["chat", "vision"],
                        local_only=is_local_provider(o_provider),
                    )

        # Filter all models for configured providers that are currently healthy/available
        available_models: list[ModelEntry] = []
        for p in configured_providers:
            if health_manager.is_provider_available(p):
                available_models.extend(self.registry.get_models_for_provider(p))

        # Only consider models with chat capabilities (filter out media generation endpoints)
        available_models = [m for m in available_models if m.capabilities and "chat" in m.capabilities]

        if not available_models:
            # Fallback to configured default even if in cooldown or unkeyed
            return RoutingDecision(
                provider=user_default_provider,
                model=user_default_model,
                reason="Default provider selected (no alternative healthy providers available)",
                capabilities=["chat"],
            )

        # Identify user's preferred default model entry if available
        user_def_entry = next(
            (m for m in available_models if m.provider == user_default_provider and m.id == user_default_model),
            None
        )

        # Check for specialized requirements
        if reqs.has_images:
            # An explicit vision override was already resolved (and
            # returned) above; reaching here means there is none, or
            # it named an unavailable non-local provider.
            if user_def_entry and "vision" in user_def_entry.capabilities:
                return RoutingDecision(
                    provider=user_def_entry.provider,
                    model=user_def_entry.id,
                    reason=f"Routed to {user_def_entry.label} — vision & image processing",
                    capabilities=list(user_def_entry.capabilities),
                )
            vision_models = [m for m in available_models if "vision" in m.capabilities]
            if vision_models:
                best, by_latency = self._pick(vision_models)
                reason = f"Routed to {best.label} — vision & image processing"
                if by_latency:
                    reason += " (fastest measured TTFT among equal-priority candidates)"
                return RoutingDecision(
                    provider=best.provider,
                    model=best.id,
                    reason=reason,
                    capabilities=list(best.capabilities),
                )

        if reqs.wants_reasoning:
            if user_def_entry and "reasoning" in user_def_entry.capabilities:
                return RoutingDecision(
                    provider=user_def_entry.provider,
                    model=user_def_entry.id,
                    reason=f"Routed to {user_def_entry.label} — deep reasoning & complex logic",
                    capabilities=list(user_def_entry.capabilities),
                )
            reasoning_models = [m for m in available_models if "reasoning" in m.capabilities]
            if reasoning_models:
                best, by_latency = self._pick(reasoning_models)
                reason = f"Routed to {best.label} — deep reasoning & complex logic"
                if by_latency:
                    reason += " (fastest measured TTFT among equal-priority candidates)"
                return RoutingDecision(
                    provider=best.provider,
                    model=best.id,
                    reason=reason,
                    capabilities=list(best.capabilities),
                )

        if reqs.wants_code:
            if user_def_entry and "code" in user_def_entry.capabilities:
                return RoutingDecision(
                    provider=user_def_entry.provider,
                    model=user_def_entry.id,
                    reason=f"Routed to {user_def_entry.label} — code generation & tool capabilities",
                    capabilities=list(user_def_entry.capabilities),
                )
            code_models = [m for m in available_models if "code" in m.capabilities]
            if code_models:
                best, by_latency = self._pick(code_models)
                reason = f"Routed to {best.label} — code generation & tool capabilities"
                if by_latency:
                    reason += " (fastest measured TTFT among equal-priority candidates)"
                return RoutingDecision(
                    provider=best.provider,
                    model=best.id,
                    reason=reason,
                    capabilities=list(best.capabilities),
                )

        # Standard / Fast chat: Check if user's default model is available
        if user_def_entry:
            return RoutingDecision(
                provider=user_def_entry.provider,
                model=user_def_entry.id,
                reason=f"Routed to {user_def_entry.label} — fast chat + tool support",
                capabilities=list(user_def_entry.capabilities),
            )

        # Otherwise pick the highest priority available model for user default provider or next provider
        fallback_model, by_latency = self._pick(available_models)
        reason = f"Routed to {fallback_model.label} — optimal available model"
        if fallback_model.provider != user_default_provider:
            reason = f"Routed to {fallback_model.label} — fallback from {user_default_provider}"
        if by_latency:
            reason += " (fastest measured TTFT among equal-priority candidates)"

        return RoutingDecision(
            provider=fallback_model.provider,
            model=fallback_model.id,
            reason=reason,
            capabilities=list(fallback_model.capabilities),
        )


GLOBAL_ROUTING_POLICY = RoutingPolicy()
