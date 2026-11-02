"""Dynamic and curated model registry with capabilities and priority tiers."""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ModelEntry:
    provider: str
    id: str
    label: str
    capabilities: list[str] = field(default_factory=list)
    priority: int = 1


#: Providers whose models run entirely on the user's own machine.
#: A route pinned to a local provider — above all an explicit local
#: vision override (Board E8-4.1: zero external network) — must never
#: be silently re-routed to a cloud provider by any fallback layer
#: (finding M17). Every fallback construction site consults this set
#: through :func:`is_local_provider`.
LOCAL_PROVIDERS: frozenset[str] = frozenset({"ollama"})


def is_local_provider(provider: str) -> bool:
    """True when *provider*'s models execute locally (no data leaves
    the machine)."""
    return provider in LOCAL_PROVIDERS


# ---------------------------------------------------------------------------
# Initial curated model registry (seed models)
# ---------------------------------------------------------------------------
CURATED_MODELS: list[ModelEntry] = [
    # Google Gemini
    # Epic 5, E5-03: gemini-3.5-flash is the verified working default
    # (gemini-2.5-flash returns 404 on the owner's key); it leads the
    # curated list so the registry agrees with the routing defaults.
    ModelEntry(
        provider="google_gemini",
        id="gemini-3.5-flash",
        label="Gemini 3.5 Flash",
        capabilities=["chat", "fast", "tools", "vision", "code"],
        priority=1,
    ),
    ModelEntry(
        provider="google_gemini",
        id="gemini-2.5-flash",
        label="Gemini 2.5 Flash",
        capabilities=["chat", "fast", "tools", "vision", "code"],
        priority=2,
    ),
    ModelEntry(
        provider="google_gemini",
        id="gemini-2.0-flash",
        label="Gemini 2.0 Flash",
        capabilities=["chat", "fast", "tools", "vision"],
        priority=3,
    ),
    ModelEntry(
        provider="google_gemini",
        id="gemini-2.5-pro",
        label="Gemini 2.5 Pro",
        capabilities=["chat", "tools", "vision", "code", "reasoning"],
        priority=4,
    ),
    # OpenAI
    ModelEntry(
        provider="openai",
        id="gpt-4o-mini",
        label="GPT-4o Mini",
        capabilities=["chat", "fast", "tools", "vision"],
        priority=1,
    ),
    ModelEntry(
        provider="openai",
        id="gpt-4o",
        label="GPT-4o",
        capabilities=["chat", "tools", "vision", "code", "reasoning"],
        priority=2,
    ),
    # Anthropic
    ModelEntry(
        provider="anthropic",
        id="claude-3-5-haiku",
        label="Claude 3.5 Haiku",
        capabilities=["chat", "fast", "tools", "vision"],
        priority=1,
    ),
    ModelEntry(
        provider="anthropic",
        id="claude-3-5-sonnet",
        label="Claude 3.5 Sonnet",
        capabilities=["chat", "tools", "vision", "code", "reasoning"],
        priority=2,
    ),
    # DeepSeek
    ModelEntry(
        provider="deepseek",
        id="deepseek-chat",
        label="DeepSeek V3",
        capabilities=["chat", "fast", "code"],
        priority=1,
    ),
    ModelEntry(
        provider="deepseek",
        id="deepseek-reasoner",
        label="DeepSeek R1",
        capabilities=["chat", "reasoning", "code"],
        priority=2,
    ),
]


def _format_model_label(model_id: str) -> str:
    """Generate a clean human-readable label from a model ID."""
    clean = model_id.replace("models/", "").replace("-", " ").replace("_", " ")
    parts = [w.capitalize() if not w.startswith("gpt") and not w.startswith("claude") else w.upper() for w in clean.split()]
    return " ".join(parts)


def _infer_capabilities(model_id: str) -> list[str]:
    """Infer baseline capabilities based on model name substrings."""
    lower = model_id.lower()

    # Non-chat / media generation models get NO chat capabilities
    non_chat_indicators = ("image", "imagen", "transcribe", "lyria", "audio", "tts", "whisper", "embedding", "diffusion")
    if any(ind in lower for ind in non_chat_indicators):
        return []

    caps = ["chat"]
    if "flash" in lower or "mini" in lower or "haiku" in lower or "small" in lower:
        caps.append("fast")
    if "pro" in lower or "plus" in lower or "sonnet" in lower or "opus" in lower or "gpt-4" in lower or "gemini" in lower:
        caps.extend(["tools", "vision"])
    if "coder" in lower or "code" in lower or "pro" in lower or "gpt-4" in lower or "sonnet" in lower or "deepseek" in lower or "gemini-2.5" in lower or "gemini-3" in lower:
        caps.append("code")
    if "reason" in lower or "r1" in lower or "o1" in lower or "o3" in lower:
        caps.append("reasoning")
    return list(dict.fromkeys(caps))


class ModelRegistry:
    """Central access point for model metadata and capability matching with dynamic discovery support."""

    def __init__(self, models: list[ModelEntry] | None = None):
        self._models: list[ModelEntry] = list(models) if models is not None else list(CURATED_MODELS)
        self._by_id: dict[str, ModelEntry] = {m.id: m for m in self._models}

    def get_model(self, model_id: str) -> ModelEntry | None:
        return self._by_id.get(model_id)

    def get_models_for_provider(self, provider: str) -> list[ModelEntry]:
        matches = [m for m in self._models if m.provider == provider]
        return sorted(matches, key=lambda m: m.priority)

    def get_default_model_for_provider(self, provider: str) -> ModelEntry | None:
        models = self.get_models_for_provider(provider)
        return models[0] if models else None

    def list_all_models(self) -> list[ModelEntry]:
        return list(self._models)

    def register_model(self, entry: ModelEntry) -> None:
        """Register or update a model entry in the registry."""
        self._by_id[entry.id] = entry
        self._models = [m for m in self._models if m.id != entry.id] + [entry]

    def update_provider_models(self, provider: str, model_ids: list[str]) -> None:
        """Dynamically replace or update discovered models for a provider."""
        if not model_ids:
            return

        # Filter out non-chat / media generation models
        non_chat = ("image", "imagen", "transcribe", "lyria", "audio", "tts", "whisper", "embedding", "diffusion")
        sanitized_ids = [
            mid for mid in model_ids
            if not any(kw in mid.lower() for kw in non_chat)
        ]
        if not sanitized_ids:
            return

        # Remove existing models for this provider if we have a fresh discovered list
        self._models = [m for m in self._models if m.provider != provider]

        # F-10 / ADR 0033: discovery must not replace curated priority.
        # A model that has a curated entry keeps its curated priority
        # (so gemini-3.5-flash stays priority 1 and gemini-2.5-flash —
        # which 404s — can never become priority 1 by discovery alone,
        # whatever order the provider returned them in). Models with no
        # curated entry keep their discovered relative order, after the
        # curated ones.
        curated_priority = {
            m.id: m.priority for m in CURATED_MODELS if m.provider == provider
        }

        def _effective_priority(item: tuple[int, str]) -> int:
            idx, mid = item
            return curated_priority.get(mid, 100 + idx)

        ordered = sorted(
            enumerate(sanitized_ids),
            key=lambda item: (_effective_priority(item), item[0]),
        )
        for idx, mid in ordered:
            entry = ModelEntry(
                provider=provider,
                id=mid,
                label=_format_model_label(mid),
                capabilities=_infer_capabilities(mid),
                priority=_effective_priority((idx, mid)),
            )
            self._models.append(entry)
            self._by_id[mid] = entry

    def remove_provider_models(self, provider: str) -> None:
        """Drop all model entries for a provider (used when a provider is disconnected)."""
        self._models = [m for m in self._models if m.provider != provider]
        self._by_id = {m.id: m for m in self._models}


GLOBAL_MODEL_REGISTRY = ModelRegistry()
