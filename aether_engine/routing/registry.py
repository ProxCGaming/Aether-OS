"""Dynamic and curated model registry with capabilities and priority tiers."""
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass(frozen=True)
class ModelEntry:
    provider: str
    id: str
    label: str
    capabilities: List[str] = field(default_factory=list)
    priority: int = 1


# ---------------------------------------------------------------------------
# Initial curated model registry (seed models)
# ---------------------------------------------------------------------------
CURATED_MODELS: List[ModelEntry] = [
    # Google Gemini
    ModelEntry(
        provider="google_gemini",
        id="gemini-2.5-flash",
        label="Gemini 2.5 Flash",
        capabilities=["chat", "fast", "tools", "vision", "code"],
        priority=1,
    ),
    ModelEntry(
        provider="google_gemini",
        id="gemini-2.0-flash",
        label="Gemini 2.0 Flash",
        capabilities=["chat", "fast", "tools", "vision"],
        priority=2,
    ),
    ModelEntry(
        provider="google_gemini",
        id="gemini-2.5-pro",
        label="Gemini 2.5 Pro",
        capabilities=["chat", "tools", "vision", "code", "reasoning"],
        priority=3,
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


def _infer_capabilities(model_id: str) -> List[str]:
    """Infer baseline capabilities based on model name substrings."""
    lower = model_id.lower()
    caps = ["chat"]
    if "flash" in lower or "mini" in lower or "haiku" in lower or "small" in lower:
        caps.append("fast")
    if "pro" in lower or "plus" in lower or "sonnet" in lower or "opus" in lower or "gpt-4" in lower or "gemini" in lower:
        caps.extend(["tools", "vision"])
    if "coder" in lower or "code" in lower or "pro" in lower or "gpt-4" in lower or "sonnet" in lower:
        caps.append("code")
    if "reason" in lower or "r1" in lower or "o1" in lower or "o3" in lower:
        caps.append("reasoning")
    return list(dict.fromkeys(caps))


class ModelRegistry:
    """Central access point for model metadata and capability matching with dynamic discovery support."""

    def __init__(self, models: Optional[List[ModelEntry]] = None):
        self._models: List[ModelEntry] = list(models) if models is not None else list(CURATED_MODELS)
        self._by_id: Dict[str, ModelEntry] = {m.id: m for m in self._models}

    def get_model(self, model_id: str) -> Optional[ModelEntry]:
        return self._by_id.get(model_id)

    def get_models_for_provider(self, provider: str) -> List[ModelEntry]:
        matches = [m for m in self._models if m.provider == provider]
        return sorted(matches, key=lambda m: m.priority)

    def get_default_model_for_provider(self, provider: str) -> Optional[ModelEntry]:
        models = self.get_models_for_provider(provider)
        return models[0] if models else None

    def list_all_models(self) -> List[ModelEntry]:
        return list(self._models)

    def register_model(self, entry: ModelEntry) -> None:
        """Register or update a model entry in the registry."""
        self._by_id[entry.id] = entry
        self._models = [m for m in self._models if m.id != entry.id] + [entry]

    def update_provider_models(self, provider: str, model_ids: List[str]) -> None:
        """Dynamically replace or update discovered models for a provider."""
        if not model_ids:
            return

        # Remove existing models for this provider if we have a fresh discovered list
        self._models = [m for m in self._models if m.provider != provider]

        for i, mid in enumerate(model_ids):
            entry = ModelEntry(
                provider=provider,
                id=mid,
                label=_format_model_label(mid),
                capabilities=_infer_capabilities(mid),
                priority=i + 1,
            )
            self._models.append(entry)
            self._by_id[mid] = entry

    def remove_provider_models(self, provider: str) -> None:
        """Drop all model entries for a provider (used when a provider is disconnected)."""
        self._models = [m for m in self._models if m.provider != provider]
        self._by_id = {m.id: m for m in self._models}


GLOBAL_MODEL_REGISTRY = ModelRegistry()
