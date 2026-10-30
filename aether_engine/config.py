"""Provider registry and persisted user config (~/.aether/config.json) with dynamic discovered models."""
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Phase 10.3 B1: the only auxiliary task classes with a real engine seam.
# routing/policy.py reads "vision"; langgraph/compaction.py reads
# "compression" (the conversation summariser — seam added by Epic 1,
# E1-06). The other four helper tasks (web, skills, approval, mcp,
# title) have no engine call site, so an override for them would be a
# placebo. Single source of truth — routes/websocket.py imports this;
# UserConfig.from_dict filters stale entries from old config files
# against it on load.
AUXILIARY_TASK_CLASSES = frozenset({"vision", "compression", "advisor", "title"})

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
    models: list[str] = field(default_factory=list)
    icon: str = "◆"
    supports_custom_url: bool = False


CLOUD_PROVIDERS: list[ProviderInfo] = [
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

PROVIDER_MAP: dict[str, ProviderInfo] = {p.name: p for p in CLOUD_PROVIDERS}

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
    provider_models: dict[str, list[str]] = field(default_factory=dict)
    # Models the provider returned but the discovery filter rejected
    # (non-chat: embeddings, image, audio...), as {"id", "kind"} dicts.
    # Shown in the provider card "View all" expander; never in chat selectors.
    provider_excluded_models: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    custom_base_urls: dict[str, str] = field(default_factory=dict)
    custom_provider_names: dict[str, str] = field(default_factory=dict)
    custom_provider_types: dict[str, str] = field(default_factory=dict)
    task_classes: dict[str, dict[str, int]] = field(default_factory=lambda: {
        "instant": {"time_cap_seconds": 30, "memory_cap_mb": 256},
        "quick": {"time_cap_seconds": 300, "memory_cap_mb": 1024},
        "standard": {"time_cap_seconds": 1800, "memory_cap_mb": 4096},
        "heavy": {"time_cap_seconds": 14400, "memory_cap_mb": 8192},
        "custom": {"time_cap_seconds": 3600, "memory_cap_mb": 2048},
    })
    tool_policies: dict[str, str] = field(default_factory=dict)
    workspace_roots: list[str] = field(default_factory=list)
    agent_models: dict[str, str] = field(default_factory=dict)
    agent_tools: dict[str, list[str]] = field(default_factory=dict)
    agent_skills: dict[str, list[str]] = field(default_factory=dict)
    # Epic 1, E1-11: supervisor spawn policy. The allow-list names the
    # specialists the supervisor may delegate to (None = all of them);
    # the delegation cap bounds handoffs per user turn (the graph
    # recursion limit still bounds total steps). Enforced in
    # supervisor_model.supervisor_post_model_hook.
    supervisor_spawn_allowlist: list[str] | None = None
    supervisor_delegation_cap: int = 8
    # Epic 1, E1-10: per-agent output schemas (agent name -> JSON-Schema
    # dict). When set, the agent's final answer must validate against
    # the schema; see aether_engine/langgraph/structured.py.
    agent_output_schemas: dict[str, Any] = field(default_factory=dict)
    # Epic 1, E1-13: agents opted into prewalk — they plan on the run
    # provider and switch to their agent_models entry (the execution
    # model) at the first executed write/execute tool.
    prewalk_agents: list[str] = field(default_factory=list)
    custom_slash_commands: dict[str, dict] = field(default_factory=dict)
    disable_fallbacks: bool = False
    fallback_chain: list[str] = field(default_factory=list)
    # Phase 9: per-task-class auxiliary model overrides, task -> "provider:::model" or "auto"
    auxiliary_models: dict[str, str] = field(default_factory=dict)
    # Phase 9: reasoning effort for reasoning-capable models ("standard" | "medium" | "high")
    reasoning_effort: str = "standard"
    # Phase 10.3 A1: memory retention. Age caps are in days; count caps are
    # hard maxima enforced by pruning (oldest first). A value <= 0 disables
    # that bound (no pruning on that axis). Defaults are documented here and
    # nowhere else — the memory modules read them from UserConfig, they do
    # not hardcode their own numbers.
    #   Episodic: 90 days / 1000 episodes (working memory: recent tasks).
    #   Knowledge graph: 365 days / 5000 facts (long-term memory: keep a year).
    #   Sessions: 180 days / 200 sessions (chat history: half a year).
    # Phase 10.3 A2: episodic semantic-similarity threshold (cosine).
    # Episodes scoring below it never enter RRF in query_episodes.
    # Default 0.3: nomic-embed-text emits L2-normalised embeddings and
    # unrelated short texts score well below 0.3 while related task
    # summaries score materially higher (same default as litellm's
    # semantic tool filter). 0.0 restores the old unthresholded behaviour.
    episodic_retention_days: int = 90
    episodic_max_episodes: int = 1000
    episodic_similarity_threshold: float = 0.3
    kg_fact_retention_days: int = 365
    kg_max_facts: int = 5000
    session_retention_days: int = 180
    session_max_sessions: int = 200
    # Epic 1, E1-06: conversation compaction thresholds. When a thread's
    # model-facing history exceeds either bound, the older turns are
    # folded into a summary (see langgraph/compaction.py); the most
    # recent compaction_keep_recent messages always stay verbatim.
    compaction_trigger_messages: int = 40
    compaction_trigger_chars: int = 24000
    compaction_keep_recent: int = 12

    def to_dict(self) -> dict[str, Any]:
        return {
            "default_provider": self.default_provider,
            "default_model": self.default_model,
            "window_width": self.window_width,
            "window_height": self.window_height,
            "provider_models": self.provider_models,
            "provider_excluded_models": self.provider_excluded_models,
            "custom_base_urls": self.custom_base_urls,
            "custom_provider_names": self.custom_provider_names,
            "custom_provider_types": self.custom_provider_types,
            "task_classes": self.task_classes,
            "tool_policies": self.tool_policies,
            "workspace_roots": self.workspace_roots,
            "agent_models": self.agent_models,
            "agent_tools": self.agent_tools,
            "agent_skills": self.agent_skills,
            "supervisor_spawn_allowlist": self.supervisor_spawn_allowlist,
            "supervisor_delegation_cap": self.supervisor_delegation_cap,
            "agent_output_schemas": self.agent_output_schemas,
            "prewalk_agents": self.prewalk_agents,
            "custom_slash_commands": self.custom_slash_commands,
            "disable_fallbacks": self.disable_fallbacks,
            "fallback_chain": self.fallback_chain,
            "auxiliary_models": self.auxiliary_models,
            "reasoning_effort": self.reasoning_effort,
            "episodic_retention_days": self.episodic_retention_days,
            "episodic_max_episodes": self.episodic_max_episodes,
            "episodic_similarity_threshold": self.episodic_similarity_threshold,
            "kg_fact_retention_days": self.kg_fact_retention_days,
            "kg_max_facts": self.kg_max_facts,
            "session_retention_days": self.session_retention_days,
            "session_max_sessions": self.session_max_sessions,
            "compaction_trigger_messages": self.compaction_trigger_messages,
            "compaction_trigger_chars": self.compaction_trigger_chars,
            "compaction_keep_recent": self.compaction_keep_recent,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "UserConfig":
        def_model = d.get("default_model", "gemini-2.0-flash") or "gemini-2.0-flash"
        if "2.5" in def_model:
            def_model = "gemini-2.0-flash"

        def _as_dict(value: Any) -> dict[str, Any]:
            # Older/corrupted config files may contain null for these maps,
            # which would otherwise crash every save/remove/list operation.
            return value if isinstance(value, dict) else {}
            
        def _as_list_dict(value: Any) -> dict[str, list[str]]:
            return value if isinstance(value, dict) else {}

        def _as_optional_str_list(value: Any) -> list[str] | None:
            # Spawn allow-list (E1-11): only a real list of names counts;
            # anything else (missing, null, corrupt) means "no
            # restriction" rather than "allow nothing".
            if isinstance(value, list):
                return [str(v) for v in value]
            return None

        def _as_int(value: Any, default: int) -> int:
            # A corrupt retention value must not nuke the whole config
            # (load_config falls back to all defaults on any from_dict
            # exception); fall back to the documented default instead.
            try:
                return int(value)
            except (TypeError, ValueError):
                return default

        def _as_float(value: Any, default: float) -> float:
            # Same guard as _as_int, for the similarity threshold.
            try:
                return float(value)
            except (TypeError, ValueError):
                return default

        return cls(
            default_provider=d.get("default_provider") or "google_gemini",
            default_model=def_model,
            window_width=int(d.get("window_width", 420)),
            window_height=int(d.get("window_height", 680)),
            provider_models=_as_dict(d.get("provider_models")),
            provider_excluded_models=_as_dict(d.get("provider_excluded_models")),
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
            agent_models=_as_dict(d.get("agent_models")),
            agent_tools=_as_list_dict(d.get("agent_tools")),
            agent_skills=_as_list_dict(d.get("agent_skills")),
            supervisor_spawn_allowlist=_as_optional_str_list(d.get("supervisor_spawn_allowlist")),
            supervisor_delegation_cap=_as_int(d.get("supervisor_delegation_cap"), 8),
            agent_output_schemas=_as_dict(d.get("agent_output_schemas")),
            prewalk_agents=d.get("prewalk_agents") if isinstance(d.get("prewalk_agents"), list) else [],
            custom_slash_commands=_as_dict(d.get("custom_slash_commands")),
            disable_fallbacks=d.get("disable_fallbacks", False),
            fallback_chain=d.get("fallback_chain", []),
            # Phase 10.3 B1: drop stale auxiliary overrides for task classes
            # with no engine call site (old config files may still carry
            # web/skills/approval/mcp/title); only AUXILIARY_TASK_CLASSES survive a load.
            auxiliary_models={k: v for k, v in _as_dict(d.get("auxiliary_models")).items() if k in AUXILIARY_TASK_CLASSES},
            reasoning_effort=d.get("reasoning_effort") or "standard",
            episodic_retention_days=_as_int(d.get("episodic_retention_days"), 90),
            episodic_max_episodes=_as_int(d.get("episodic_max_episodes"), 1000),
            episodic_similarity_threshold=_as_float(d.get("episodic_similarity_threshold"), 0.3),
            kg_fact_retention_days=_as_int(d.get("kg_fact_retention_days"), 365),
            kg_max_facts=_as_int(d.get("kg_max_facts"), 5000),
            session_retention_days=_as_int(d.get("session_retention_days"), 180),
            session_max_sessions=_as_int(d.get("session_max_sessions"), 200),
            compaction_trigger_messages=_as_int(d.get("compaction_trigger_messages"), 40),
            compaction_trigger_chars=_as_int(d.get("compaction_trigger_chars"), 24000),
            compaction_keep_recent=_as_int(d.get("compaction_keep_recent"), 12),
        )


def load_config(path: Path | None = None) -> UserConfig:
    p = path or CONFIG_PATH
    if p.exists():
        try:
            return UserConfig.from_dict(json.loads(p.read_text("utf-8")))
        except Exception:  # noqa: BLE001, S110 — a corrupt config file must never stop the engine starting; defaults are the designed fallback.
            pass
    return UserConfig()


def save_config(cfg: UserConfig, path: Path | None = None) -> None:
    p = path or CONFIG_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg.to_dict(), indent=2), encoding="utf-8")
