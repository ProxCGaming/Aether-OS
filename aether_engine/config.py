"""Provider registry and persisted user config (~/.aether/config.json) with dynamic discovered models."""
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

CONFIG_PATH = Path.home() / ".aether" / "config.json"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"

# ---------------------------------------------------------------------------
# Base provider registry metadata (URLs, icons, display names)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ProviderInfo:
    name: str
    display_name: str
    key_url: str
    models: List[str] = field(default_factory=list)
    icon: str = "◆"
    supports_custom_url: bool = False


CLOUD_PROVIDERS: List[ProviderInfo] = [
    ProviderInfo(
        name="google_gemini", display_name="Google Gemini",
        key_url="https://aistudio.google.com/apikey",
        models=[],
        icon="◆",
    ),
    ProviderInfo(
        name="openai", display_name="OpenAI",
        key_url="https://platform.openai.com/api-keys",
        models=[],
        icon="◇",
    ),
    ProviderInfo(
        name="anthropic", display_name="Anthropic",
        key_url="https://console.anthropic.com/settings/keys",
        models=[],
        icon="◈",
    ),
    ProviderInfo(
        name="deepseek", display_name="DeepSeek",
        key_url="https://platform.deepseek.com/api_keys",
        models=[],
        icon="◉",
    ),
    ProviderInfo(
        name="openrouter", display_name="OpenRouter",
        key_url="https://openrouter.ai/settings/keys",
        models=[],
        icon="◎",
    ),
    ProviderInfo(
        name="custom_openai", display_name="Custom OpenAI Compatible",
        key_url="",
        models=[],
        icon="⊕",
        supports_custom_url=True,
    ),
]

PROVIDER_MAP: Dict[str, ProviderInfo] = {p.name: p for p in CLOUD_PROVIDERS}

LOCAL_MODELS = [
    {"name": "Qwen 3 8B", "size": "4.9 GB", "badges": ["Fast", "Code"]},
    {"name": "Llama 3.1 8B", "size": "4.7 GB", "badges": ["General", "Chat"]},
    {"name": "Gemma 3 12B", "size": "7.3 GB", "badges": ["Reasoning", "Multi-turn"]},
    {"name": "DeepSeek R1 8B", "size": "4.9 GB", "badges": ["Reasoning", "Code"]},
]


# ---------------------------------------------------------------------------
# Persisted user config (default provider + model + window dimensions + discovered models)
# ---------------------------------------------------------------------------
@dataclass
class UserConfig:
    default_provider: str = "google_gemini"
    default_model: str = "gemini-2.0-flash"
    window_width: int = 420
    window_height: int = 680
    provider_models: Dict[str, List[str]] = field(default_factory=dict)
    custom_base_urls: Dict[str, str] = field(default_factory=dict)
    custom_provider_names: Dict[str, str] = field(default_factory=dict)
    custom_provider_types: Dict[str, str] = field(default_factory=dict)
    task_classes: Dict[str, Dict[str, int]] = field(default_factory=lambda: {
        "instant": {"time_cap_seconds": 30, "memory_cap_mb": 256},
        "quick": {"time_cap_seconds": 300, "memory_cap_mb": 1024},
        "standard": {"time_cap_seconds": 1800, "memory_cap_mb": 4096},
        "heavy": {"time_cap_seconds": 14400, "memory_cap_mb": 8192},
        "custom": {"time_cap_seconds": 3600, "memory_cap_mb": 2048},
    })
    tool_policies: Dict[str, str] = field(default_factory=dict)
    workspace_roots: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "default_provider": self.default_provider,
            "default_model": self.default_model,
            "window_width": self.window_width,
            "window_height": self.window_height,
            "provider_models": self.provider_models,
            "custom_base_urls": self.custom_base_urls,
            "custom_provider_names": self.custom_provider_names,
            "custom_provider_types": self.custom_provider_types,
            "task_classes": self.task_classes,
            "tool_policies": self.tool_policies,
            "workspace_roots": self.workspace_roots,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "UserConfig":
        def_model = d.get("default_model", "gemini-2.0-flash") or "gemini-2.0-flash"
        if "2.5" in def_model:
            def_model = "gemini-2.0-flash"

        def _as_dict(value: Any) -> Dict[str, Any]:
            # Older/corrupted config files may contain null for these maps,
            # which would otherwise crash every save/remove/list operation.
            return value if isinstance(value, dict) else {}

        return cls(
            default_provider=d.get("default_provider") or "google_gemini",
            default_model=def_model,
            window_width=int(d.get("window_width", 420)),
            window_height=int(d.get("window_height", 680)),
            provider_models=_as_dict(d.get("provider_models")),
            custom_base_urls=_as_dict(d.get("custom_base_urls")),
            custom_provider_names=_as_dict(d.get("custom_provider_names")),
            custom_provider_types=_as_dict(d.get("custom_provider_types")),
            task_classes=d.get("task_classes", {
                "instant": {"time_cap_seconds": 30, "memory_cap_mb": 256},
                "quick": {"time_cap_seconds": 300, "memory_cap_mb": 1024},
                "standard": {"time_cap_seconds": 1800, "memory_cap_mb": 4096},
                "heavy": {"time_cap_seconds": 14400, "memory_cap_mb": 8192},
                "custom": {"time_cap_seconds": 3600, "memory_cap_mb": 2048},
            }),
            tool_policies=_as_dict(d.get("tool_policies")),
            workspace_roots=d.get("workspace_roots") if isinstance(d.get("workspace_roots"), list) else [],
        )


def load_config(path: Optional[Path] = None) -> UserConfig:
    p = path or CONFIG_PATH
    if p.exists():
        try:
            return UserConfig.from_dict(json.loads(p.read_text("utf-8")))
        except Exception:
            pass
    return UserConfig()


def save_config(cfg: UserConfig, path: Optional[Path] = None) -> None:
    p = path or CONFIG_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg.to_dict(), indent=2), encoding="utf-8")
