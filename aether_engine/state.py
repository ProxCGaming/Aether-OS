import logging
from pathlib import Path

from aether_common.auth import (
    DEFAULT_TOKEN_PATH,
    generate_token,
    write_token,
)
from aether_engine.audit import AuditLogger
from aether_engine.config import (
    DEFAULT_GEMINI_MODEL,
    DEFAULT_LLAMACPP_ROUTER_URL,
    UserConfig,
    load_config,
)
from aether_engine.models.hub_downloads import ModelHubDownloads
from aether_engine.models.llama_router import LlamaRouterManager
from aether_engine.models.local_models import OllamaManager
from aether_engine.models.model_hub import ModelHub
from aether_engine.routing.health import (
    GLOBAL_HEALTH_MANAGER,
)
from aether_engine.routing.policy import (
    GLOBAL_ROUTING_POLICY,
)
from aether_engine.routing.registry import (
    CURATED_MODELS,
    GLOBAL_MODEL_REGISTRY,
)
from aether_engine.routing.startup import (
    validate_startup_configuration,
)
from aether_engine.scheduler.capability_jobs import CapabilitySchedulerManager
from aether_engine.secrets.storage import SecretStore

logger = logging.getLogger("aether_engine.state")

class EngineState:
    def __init__(self):
        self.token_path = DEFAULT_TOKEN_PATH
        self.audit_logger = AuditLogger()
        self.auth_token = ""
        self.user_config = UserConfig()
        self.health_manager = GLOBAL_HEALTH_MANAGER
        self.model_registry = GLOBAL_MODEL_REGISTRY
        self.routing_policy = GLOBAL_ROUTING_POLICY
        self.scheduler_manager = CapabilitySchedulerManager()
        self.ollama_manager = OllamaManager()
        # The shared llama.cpp router manager (Wave 8 tier
        # policy): built lazily by get_llama_router_manager() and
        # rebuilt when the llamacpp config fields it is built
        # from change. Unlike OllamaManager it cannot be built
        # eagerly here — it is parameterised by user config that
        # only exists after load_config().
        self.llama_router_manager: LlamaRouterManager | None = None
        self._llama_router_manager_key: tuple | None = None
        self._llama_router_secret_store: SecretStore | None = None
        self.model_hub = ModelHub()
        self.hub_downloads = ModelHubDownloads(
            hub=self.model_hub, ollama=self.ollama_manager)
        from aether_engine.plugins.installer import PluginInstaller
        self.plugin_installer = PluginInstaller(Path.home() / ".aether" / "plugins", audit_logger=self.audit_logger)
        self.active_provider: str = "google_gemini"
        self.active_model: str = DEFAULT_GEMINI_MODEL
        self.configured_providers: list[str] = []
        self.disabled_nodes: set[str] = set()
        # Providers resync_active_state() has registered config-
        # discovered models for (E8-07): resync deregisters any of
        # these that later vanish from the config, so a removed
        # provider's models cannot linger in the registry.
        self._config_registered_providers: set[str] = set()
        # Readiness for /health (N-44): True only once initialize()
        # has run to completion; shutdown() clears it again.
        self.initialized: bool = False

    def get_llama_router_manager(
            self, secret_store: SecretStore | None = None
    ) -> LlamaRouterManager:
        """The one long-lived LlamaRouterManager for the run path.

        The manager's tier bookkeeping (``_last_used`` /
        ``_job_state``) is per-instance in-memory state, so the
        executor's tier-policy hooks and any policy-facing reads
        must share ONE instance — a manager constructed per call
        would forget every note the moment it returned. (The
        websocket panel routes keep their per-connection
        construction: their operations are stateless reads and
        process control, which carry no bookkeeping to lose.)

        The instance is keyed on the config fields the manager is
        built from — router URL (which carries the port), binary
        path and models dir — and rebuilt on the next access
        after any of them changes, whichever path changed them
        (CONFIG_UPDATE replaces fields in place; CONFIG_RESET
        swaps the whole object and resyncs). The router API key
        is NOT part of the key: the manager resolves it from the
        secret store on every call, so a saved key is picked up
        without a rebuild. A caller-supplied secret store (the
        connection's own) is remembered for later rebuilds.
        """
        if secret_store is not None:
            self._llama_router_secret_store = secret_store
        cfg = self.user_config
        key = (
            getattr(cfg, "llamacpp_router_url",
                    DEFAULT_LLAMACPP_ROUTER_URL),
            getattr(cfg, "llamacpp_binary_path", ""),
            getattr(cfg, "llamacpp_models_dir", ""),
        )
        if (self.llama_router_manager is None
                or self._llama_router_manager_key != key):
            store = self._llama_router_secret_store
            if store is None:
                store = SecretStore()
                self._llama_router_secret_store = store
            self.llama_router_manager = LlamaRouterManager.from_config(
                cfg, secret_store=store)
            self._llama_router_manager_key = key
        return self.llama_router_manager

    def initialize(self) -> str:
        self.auth_token = generate_token()
        write_token(self.auth_token, self.token_path)
        self.user_config = load_config()

        self.resync_active_state()

        # Start background capability scheduler with startup misfire recovery
        self.scheduler_manager.start()

        self.audit_logger.log_event("ENGINE_STARTED", {
            "token_path": str(self.token_path),
            "active_provider": self.active_provider,
            "active_model": self.active_model,
        })
        self.initialized = True
        return self.auth_token

    def resync_active_state(self) -> None:
        """(Re)derive every live field that mirrors ``user_config``.

        ``active_provider`` / ``active_model`` / ``configured_providers``
        and the skill manager's workspace roots are independent state,
        not views onto the config — replacing the config alone leaves
        them stale. ``initialize()`` derives them once at startup;
        CONFIG_RESET replaces the config wholesale, so it must run the
        same derivation again or HELLO keeps reporting the pre-reset
        provider/model and the skill catalogue keeps serving skills
        from workspace roots the reset removed (E6-F04). Providers
        this method previously registered config-discovered models
        for are deregistered again once they vanish from the config
        (E8-07) — the registry mirrors the config in both directions.
        """
        # Register persisted discovered models into the model registry.
        # E8-07: also deregister what an earlier resync registered
        # for a provider that has since left the config (or whose
        # discovered list was emptied) — update_provider_models
        # only ever adds/replaces, so without this a removed
        # provider's models stay selectable for the whole session.
        live_providers = {
            prov_name
            for prov_name, prov_models in
            self.user_config.provider_models.items()
            if prov_models}
        for stale_provider in (
                self._config_registered_providers - live_providers):
            self.model_registry.remove_provider_models(stale_provider)
            # The registry is seeded from CURATED_MODELS at
            # construction, so restore that baseline: deregistration
            # returns the provider to its boot state — only the
            # config-discovered entries resync added are gone. (An
            # EXPLICIT provider removal goes through the
            # PROVIDER_REMOVE handler, which drops every entry and
            # discards the provider from the tracked set below, so
            # it is never resurrected by this restore.)
            for entry in CURATED_MODELS:
                if entry.provider == stale_provider:
                    self.model_registry.register_model(entry)
        self._config_registered_providers = set(live_providers)
        for prov_name, prov_models in self.user_config.provider_models.items():
            if prov_models:
                self.model_registry.update_provider_models(prov_name, prov_models)

        # Skill manager roots follow the config's workspace roots
        from aether_engine.skills.manager import GLOBAL_SKILL_MANAGER
        GLOBAL_SKILL_MANAGER.update_workspace_roots(self.user_config.workspace_roots or [])

        # Run startup consistency validation
        secret_store = SecretStore()
        val_res = validate_startup_configuration(
            user_config=self.user_config,
            secret_store=secret_store,
            health_manager=self.health_manager,
            registry=self.model_registry,
        )
        self.active_provider = val_res.active_provider
        self.active_model = val_res.active_model
        self.configured_providers = val_res.configured_providers

        if val_res.was_fallback:
            logger.warning(f"Startup fallback applied: {val_res.fallback_reason}")
            self.audit_logger.log_event("STARTUP_FALLBACK", {"reason": val_res.fallback_reason})

    def shutdown(self):
        self.initialized = False
        self.scheduler_manager.stop()

engine_state = EngineState()
