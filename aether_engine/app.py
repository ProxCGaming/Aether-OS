"""FastAPI Engine application with authenticated loopback WebSocket, provider health, intelligent routing, safe fallbacks, local model task downloads, and capability check scheduling."""
import asyncio
import logging

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from aether_common.contracts import SCHEMA_VERSION
from aether_engine.config import (
    anchor_default_model,
    custom_provider_flavor,
    save_config,
)
from aether_engine.models.llama_presets import DAILY_MODEL_IDLE_TTL_S
from aether_engine.providers.discovery import fetch_available_models
from aether_engine.routes.websocket import router as ws_router
from aether_engine.secrets.storage import SecretStore
from aether_engine.state import engine_state
from aether_engine.version import APP_VERSION

logger = logging.getLogger("aether_engine.app")

# Wave 1 (runtime/UX fix programme): handle for the background startup
# discovery task, so shutdown can cancel it cleanly. Discovery used to
# run inline in the lifespan, delaying /health by up to 10s per
# configured provider; it now runs detached, exactly like the episodic
# and media backfills below.
_startup_discovery_task = None

# N-35: handle for the startup episodic backfill task, so shutdown can
# cancel and await it with the same discipline as discovery. It used
# to be fired with no handle at all — unreferenced (exposed to the
# asyncio GC pitfall) and never stopped, so shutdown could cut the
# backfill mid-write.
_episodic_backfill_task = None

# Wave 8 follow-up: handle for the llama.cpp tier-policy loop, with
# the same handle discipline as the two tasks above — shutdown
# cancels and awaits it, so no pass can outlive the engine.
_tier_policy_task = None

# How often the tier-policy loop runs a pass. The policy's binding
# clock is the daily tier's idle TTL (DAILY_MODEL_IDLE_TTL_S =
# 600s): a pass every TTL/10 bounds a stale daily model's extra
# idle time to ~10% of its TTL, while each pass costs one /models
# read (plus any load/unload it decides on) against a local router.
TIER_POLICY_INTERVAL_S = DAILY_MODEL_IDLE_TTL_S / 10


async def _discover_startup_models():
    """Startup model discovery for configured providers.

    Runs detached from the lifespan: its config effects (provider model
    lists + save_config) land exactly as the old inline loop's did, but
    engine startup no longer waits on the slowest provider. Failures
    are logged, never propagated — discovery must not take the engine
    down.
    """
    try:
        secret_store = SecretStore()
        stored = set(secret_store.list_providers())
        cfg = engine_state.user_config
        all_providers = set(stored)
        for p in cfg.custom_provider_types:
            if cfg.custom_provider_types[p] == "openai_compatible":
                all_providers.add(p)
        all_providers.add("custom_openai")

        for pname in all_providers:
            try:
                key = secret_store.load_provider(pname) if pname in stored else ""
                is_custom = pname == "custom_openai" or cfg.custom_provider_types.get(pname) == "openai_compatible"
                if key or is_custom:
                    base_url = cfg.custom_base_urls.get(pname)
                    disc = await fetch_available_models(
                        pname, key, base_url=base_url,
                        provider_flavor=custom_provider_flavor(pname, cfg))
                    if disc.models:
                        # B10: keep the configured default anchored in
                        # the replaced list (anchor_default_model).
                        models = anchor_default_model(cfg, pname, disc.models)
                        cfg.provider_models[pname] = models
                        cfg.provider_excluded_models[pname] = disc.excluded
                        engine_state.model_registry.update_provider_models(pname, models)
                        # F-10 / ADR 0033: the persisted default wins.
                        # Discovery registers candidates only — it must
                        # never overwrite UserConfig.default_model with
                        # disc.models[0] (that resurrected the 404 model
                        # gemini-2.5-flash as the effective default).
                        # Startup validation (routing/startup.py) already
                        # handles a genuinely unavailable default without
                        # rewriting the persisted config.
                        save_config(cfg)
            except Exception as e:  # noqa: BLE001 — per-provider isolation: one provider's discovery failure must not abort startup discovery for the rest
                logger.warning(f"Failed startup model discovery for {pname}: {e}")
    except Exception as e:  # noqa: BLE001 — startup boundary: background discovery is best-effort and must never take the engine down
        logger.warning(f"Startup model discovery failed: {e}")


async def _init_knowledge_graph_at_startup():
    """Initialise the knowledge-graph store once, during startup.

    Belt-and-braces alongside knowledge_graph._ensure_wal_mode (the
    fix that made a concurrent first open safe): creating the schema
    and landing the file's one-time WAL journal transition here —
    before task execution begins, while startup is still effectively
    single-threaded — means the extraction hot path never pays
    first-open costs and a concurrent first use becomes the
    exception, not the norm. Runs off the event loop (sync SQLite,
    N-11) and is best-effort: a failure is logged and startup
    continues, because the store still initialises lazily on first
    use, exactly as it did before this existed.
    """
    import asyncio

    from aether_engine.memory import knowledge_graph

    try:
        await asyncio.to_thread(knowledge_graph.init_kg_db)
    except Exception as e:  # noqa: BLE001 — startup boundary: KG init is best-effort; lazy init on first use remains as the fallback
        logger.warning(f"Startup knowledge-graph initialisation failed: {e}")


async def _ensure_embedder_then_backfill():
    """Embedder lifecycle gate for the startup episodic backfill.

    The backfill embeds through the configured embedder, a local
    Ollama model — and until now nothing checked that the model (or
    Ollama itself) exists before the backfill paid the 30s embedding
    timeout against a dead endpoint (the owner's PC: Ollama off,
    ~30s burned at every launch, embeddings silently absent).
    ensure_embedder decides in ~2s when Ollama is off, pulls the
    model when the daemon is up but the model is missing, and the
    verdict is recorded in episodic's embedder status (surfaced to
    the Memory settings via embedding_stats). An unavailable
    embedder skips this launch's backfill with ONE clear warning
    instead of a queue of timed-out attempts, and arms the
    embedding circuit breaker so per-chat attempts fail fast too.
    """
    from aether_engine.memory import episodic
    from aether_engine.models.local_models import ensure_embedder

    try:
        model = episodic.resolve_embedder(engine_state.user_config)
        status = await ensure_embedder(engine_state.ollama_manager, model)
    except Exception as e:  # noqa: BLE001 — startup boundary: the gate is best-effort; a failure here must not lose the backfill's own failure reporting or surface as an unretrieved task exception
        logger.warning(f"Embedder ensure failed to run: {e}")
        await episodic.backfill_episodic_embeddings(limit=200)
        return
    if status["state"] in ("ollama_off", "pull_failed"):
        episodic.note_embedder_unavailable(status)
        logger.warning(
            f"Embedder unavailable ({status['state']}): episodic backfill "
            f"skipped this launch — model {model}: {status['detail']}")
        return
    episodic.set_embedder_status(status)
    await episodic.backfill_episodic_embeddings(limit=200)


async def _run_tier_policy_pass() -> None:
    """One llama.cpp tier-policy pass; never raises.

    Reads the enable flag from the live user config on every pass,
    so toggling llamacpp_enabled takes effect on the next pass in
    either direction — no engine restart, and while disabled the
    shared manager is never even resolved. The pass runs on the
    ONE shared manager (EngineState.get_llama_router_manager):
    its bookkeeping is per-instance, and the executor's tier hooks
    write to that same instance — a manager built here would see
    an empty history and judge every model unused.

    A router that is down is a quiet no-op, by the manager's own
    contract (list_models reads an absent router as an empty,
    error-free list, so the plan is empty). Per-model errors from
    a pass are logged once, here — never once per model — and a
    pass that itself fails is logged and swallowed: the loop's
    next pass is the retry.
    """
    try:
        if not getattr(engine_state.user_config,
                       "llamacpp_enabled", False):
            return
        manager = engine_state.get_llama_router_manager()
        result = await manager.apply_tier_policy()
    except Exception as e:  # noqa: BLE001 — a policy pass is best-effort; it must never take the loop (or the engine) down
        logger.warning(f"Tier policy pass failed: {e}")
        return
    if result.get("load") or result.get("unload"):
        logger.info(
            f"Tier policy: load={result['load']} "
            f"unload={result['unload']}")
    if result.get("errors"):
        logger.warning(
            f"Tier policy pass had per-model errors: {result['errors']}")


async def _tier_policy_loop(
        interval_s: float = TIER_POLICY_INTERVAL_S) -> None:
    """Run tier-policy passes for the life of the engine.

    Sequential by construction — a pass is awaited before the
    interval sleep, so passes can never overlap and a slow pass
    (a load can take the manager's 120s load timeout) simply
    shifts the cadence instead of piling the next pass on top of
    itself; elapsed ticks are never made up. Cancellation (the
    shutdown stop) propagates out of the pass or the sleep —
    nothing here swallows it.
    """
    while True:
        await _run_tier_policy_pass()
        await asyncio.sleep(interval_s)


def _start_tier_policy_loop(
        interval_s: float = TIER_POLICY_INTERVAL_S):
    """Start the tier-policy loop; a live loop is never doubled.

    Returns the loop task (the existing one when already
    running), so a second start — a repeated lifespan entry, a
    test harness re-entry — is a no-op rather than a second loop
    racing the first one's passes.
    """
    global _tier_policy_task
    if _tier_policy_task is not None and not _tier_policy_task.done():
        return _tier_policy_task
    _tier_policy_task = asyncio.create_task(_tier_policy_loop(interval_s))
    return _tier_policy_task


async def _stop_tier_policy_loop() -> None:
    """Cancel and await the tier-policy loop, lifespan-style.

    Same discipline as the discovery/backfill stops in the
    lifespan: the handle is cleared first, the task is cancelled
    and awaited, and a failed stop is logged — never allowed to
    prevent shutdown.
    """
    global _tier_policy_task
    task, _tier_policy_task = _tier_policy_task, None
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        logger.debug("Tier policy loop cancelled at shutdown")
    except Exception as e:  # noqa: BLE001 — shutdown cleanup boundary: a failed stop must not prevent shutdown
        logger.warning(f"Tier policy loop stop failed: {e}")


async def lifespan(_app: FastAPI):
    global _startup_discovery_task, _episodic_backfill_task
    # Epic 10, E10-04: unhandled exceptions land in ~/.aether/logs/crash.log.
    from aether_engine.diagnostics import install_crash_logging, record_crash

    install_crash_logging()
    try:
        engine_state.initialize()
    except Exception:
        # E10-F08: Uvicorn catches lifespan failures itself, so the
        # unhandled-exception hooks never see a startup failure.
        # Write the promised crash-log banner explicitly, then let
        # the failure propagate exactly as before.
        import sys

        record_crash("LIFESPAN STARTUP FAILURE", *sys.exc_info())
        raise

    # Epic 9: warm the deferred litellm import in a background thread.
    # The import costs ~4s; paying it here (overlapped with the rest
    # of startup) keeps engine start fast AND keeps the first real
    # provider call from paying it either.
    try:
        import threading

        from aether_engine.providers.litellm_provider import warm_litellm

        threading.Thread(target=warm_litellm, daemon=True, name="litellm-warmup").start()
    except Exception as e:  # noqa: BLE001 — startup boundary: warm-up is best-effort; the lazy import still happens on first use
        logger.warning(f"LiteLLM warm-up failed to start: {e}")
    logger.info(
        f"AETHER Engine initialized. Active: {engine_state.active_provider}/{engine_state.active_model}. "
        f"Auth token written to {engine_state.token_path}"
    )

    # WAL first-open belt-and-braces: initialise the knowledge-graph
    # store now, before any extraction threads exist, so its one-time
    # WAL journal transition lands in startup's quiet window (see
    # _init_knowledge_graph_at_startup). Awaited — it is one quick
    # idempotent init, not per-provider network work — but run off
    # the event loop. Native backend only: external backends own
    # their stores, the same gate the episodic backfill below uses.
    try:
        if (engine_state.user_config.memory_backend or "native") == "native":
            await _init_knowledge_graph_at_startup()
    except Exception as e:  # noqa: BLE001 — startup boundary: KG init is best-effort and must never block engine startup
        logger.warning(f"Startup knowledge-graph initialisation failed to start: {e}")

    # SPEC-6A: close the tasks the previous process left Running.
    # The orphan sweep runs before any new task can start (nothing
    # executes during lifespan), is audit-only, and is best-effort:
    # a sweep failure must never block engine startup — the orphans
    # it missed are swept on the next launch instead.
    try:
        from aether_engine.langgraph.orphan_sweep import sweep_orphaned_tasks

        await sweep_orphaned_tasks(audit_logger=engine_state.audit_logger)
    except Exception as e:  # noqa: BLE001 — startup boundary: the sweep is best-effort and must never block engine startup
        logger.warning(f"Startup orphan sweep failed: {e}")

    # Issue 004: auto-connect enabled MCP servers in the background.
    # start_auto_connect() returns immediately — it never blocks startup —
    # and each server connects in its own owner task, so one dead server
    # cannot block startup or the others. Local import so tests that
    # patch aether_engine.mcp.registry.GLOBAL_MCP_REGISTRY get the patch.
    try:
        from aether_engine.mcp.registry import GLOBAL_MCP_REGISTRY

        GLOBAL_MCP_REGISTRY.start_auto_connect()
    except Exception as e:  # noqa: BLE001 — startup boundary: MCP auto-connect is best-effort and must never block engine startup
        logger.warning(f"MCP auto-connect failed to start: {e}")

    # Epic 4, E4-04: backfill embeddings for episodes stored while
    # the embedding model was unavailable. Detached and bounded —
    # startup never waits on it, and failures are audit-logged by
    # the episodic store itself. Native backend only: external
    # backends own their embedding story. The task now runs the
    # embedder lifecycle gate first (_ensure_embedder_then_backfill):
    # the configured embedder is verified — and pulled if Ollama is
    # up but the model missing — before any embedding is attempted,
    # and an unavailable embedder skips the backfill for this launch
    # instead of burning the 30s embedding timeout per attempt.
    try:
        if (engine_state.user_config.memory_backend or "native") == "native":
            import asyncio

            _episodic_backfill_task = asyncio.create_task(
                _ensure_embedder_then_backfill())
    except Exception as e:  # noqa: BLE001 — startup boundary: backfill is best-effort and must never block engine startup
        logger.warning(f"Episodic embedding backfill failed to start: {e}")

    # Epic 8, E8-4.3: backfill analysis for media registered before
    # the analysis pipeline shipped (or left unanalysed while the
    # vision model was unavailable). Detached and best-effort, same
    # pattern as the episodic backfill above — startup never waits.
    try:
        from aether_engine.media.analyzer import schedule_media_backfill

        schedule_media_backfill()
    except Exception as e:  # noqa: BLE001 — startup boundary: media backfill is best-effort and must never block engine startup
        logger.warning(f"Media analysis backfill failed to start: {e}")

    # Run model discovery for configured providers in the background
    # on startup. Detached, same pattern as the backfills above: the
    # lifespan must reach yield (app healthy) without waiting for the
    # slowest provider; discovery's effects land when it finishes.
    try:
        import asyncio

        _startup_discovery_task = asyncio.create_task(_discover_startup_models())
    except Exception as e:  # noqa: BLE001 — startup boundary: discovery is best-effort and must never block engine startup
        logger.warning(f"Startup model discovery failed to start: {e}")

    # Wave 8 follow-up: the llama.cpp tier-policy loop. Wave 8's
    # manager runs a policy pass when the router starts, but
    # nothing re-ran it afterwards — so mid-session the daily
    # tier's idle unload never fired. The loop re-runs the pass on
    # the shared manager for the life of the engine; it starts
    # unconditionally and gates each pass on the live
    # llamacpp_enabled flag, so toggling the feature needs no
    # restart in either direction (while disabled a pass is one
    # config read — the manager is never resolved).
    try:
        _start_tier_policy_loop()
    except Exception as e:  # noqa: BLE001 — startup boundary: the policy loop is best-effort and must never block engine startup
        logger.warning(f"Tier policy loop failed to start: {e}")

    yield
    # Stop the tier-policy loop first among the background tasks:
    # it acts on live engine state (the shared router manager),
    # so no pass may race the shutdown of everything below.
    await _stop_tier_policy_loop()
    # Cancel background discovery if it is still running at shutdown,
    # so a slow provider cannot outlive the engine or leak the task.
    if _startup_discovery_task is not None:
        _startup_discovery_task.cancel()
        try:
            await _startup_discovery_task
        except asyncio.CancelledError:
            logger.debug("Startup discovery cancelled at shutdown")
        except Exception as e:  # noqa: BLE001 — shutdown cleanup boundary: discovery already logs its own failures; a failed stop must not prevent shutdown
            logger.warning(f"Startup discovery stop failed: {e}")
        _startup_discovery_task = None
    # N-35: cancel the episodic backfill with the same discipline as
    # discovery — shutdown must not cut it mid-write or let it
    # outlive the engine.
    if _episodic_backfill_task is not None:
        _episodic_backfill_task.cancel()
        try:
            await _episodic_backfill_task
        except asyncio.CancelledError:
            logger.debug("Episodic backfill cancelled at shutdown")
        except Exception as e:  # noqa: BLE001 — shutdown cleanup boundary: a failed stop must not prevent shutdown
            logger.warning(f"Episodic backfill stop failed: {e}")
        _episodic_backfill_task = None
    # N-35: media backfills and scheduled analyses are detached too —
    # stop everything the analyzer still has in flight.
    try:
        from aether_engine.media.analyzer import cancel_inflight_tasks

        await cancel_inflight_tasks()
    except Exception as e:  # noqa: BLE001 — shutdown cleanup boundary: a failed stop must not prevent shutdown
        logger.warning(f"Media analyzer stop failed: {e}")
    # Issue 004: close any owner-task MCP sessions before shutdown.
    try:
        from aether_engine.mcp.registry import GLOBAL_MCP_REGISTRY

        await GLOBAL_MCP_REGISTRY.stop_auto_connect()
    except Exception as e:  # noqa: BLE001 — shutdown cleanup boundary: a failed MCP stop must not prevent engine shutdown
        logger.warning(f"MCP auto-connect stop failed: {e}")
    engine_state.shutdown()
    engine_state.audit_logger.log_event("ENGINE_STOPPED")
    logger.info("AETHER Engine stopped.")


app = FastAPI(title="AETHER Engine", version=APP_VERSION, lifespan=lifespan)
app.include_router(ws_router)

@app.get("/health")
async def health():
    # N-44: /health is the shell's readiness probe, so it reports
    # the engine's REAL state — "ok" only once EngineState has
    # finished initialising, "starting" (HTTP 503) before that —
    # instead of an unconditional ok from a process whose state
    # may never have come up.
    initialized = bool(getattr(engine_state, "initialized", False))
    scheduler = getattr(engine_state, "scheduler_manager", None)
    scheduler_running = bool(
        getattr(scheduler, "scheduler", None) is not None
        and getattr(scheduler.scheduler, "running", False))
    return JSONResponse(
        {
            "status": "ok" if initialized else "starting",
            "schema_version": SCHEMA_VERSION,
            "initialized": initialized,
            "scheduler_running": scheduler_running,
        },
        status_code=200 if initialized else 503,
    )
