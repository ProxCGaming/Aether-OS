"""Provider/model replies for the task websocket (Wave 5 split).

The provider handler family of ``ws_tasks``: Test-key validation,
the provider-list update + push, the deferred half of a provider
save's discovery, the models refresh, and the capability check.
Each used to be a closure inside the endpoint; they are
module-level functions here, handed the connection's
:class:`~aether_engine.routes.ws_connection.WsConnection`.

``fetch_available_models`` and ``save_config`` resolve through
the websocket module at call time (the codebase's lazy-import
idiom): the test suites patch them on
``aether_engine.routes.websocket``, and a module-level binding
here would silently stop those patches intercepting.
``CAPABILITY_CHECK_RUN_BUDGET_SECONDS`` stays defined on the
websocket module for the same reason and is read the same way.

Reply discipline (deslop C5): validation answers through
``WsConnection.reply`` — its failure shape is the canonical
``{success: False, ...}`` error payload — and the capability
check's tail send goes through ``send_event``. The list push,
list update and refresh keep their bespoke bodies: a failed
*build* there is deliberately not an error reply (the cached
list already went out; a failed refresh is log-only), so the
uniform shape does not fit them.
"""
import asyncio
import logging
import time
import uuid
from typing import Any

from aether_common.contracts import Event, EventType
from aether_engine.config import anchor_default_model, custom_provider_flavor
from aether_engine.orchestration.core import (
    _build_provider_list_async,
    clear_discovery_failure,
    discover_pending_provider_models,
    discovery_recently_failed,
    record_discovery_failure,
    validate_provider_key_shared,
)
from aether_engine.state import engine_state

logger = logging.getLogger("aether_engine.routes.ws_providers")


async def send_validation_result(conn, resp_type, req_id_, pname_, key_, base_url_):
    """Answer a provider Test request from a background task.

    Validation (discovery + pings) used to be awaited inline in
    the message loop, so one slow Test wedged the whole socket —
    chat, cancellations, everything queued behind it, and a
    double-click doubled the freeze. Now the loop stays free;
    duplicate clicks attach to the same in-flight run via
    validate_provider_key_shared and each requester still gets
    its own response.
    """
    async def _build_payload():
        result = await validate_provider_key_shared(pname_, key_, base_url=base_url_, persist_models=False)
        result["provider"] = pname_
        result["success"] = result.get("status") == "connected"
        return result

    await conn.reply(
        req_id_, resp_type, _build_payload,
        error_payload=lambda e: {
            "provider": pname_,
            "status": "network_error",
            "message": f"Validation failed unexpectedly: {e}",
            "success": False,
        },
        send_failure_message=f"Could not send validation result for {pname_}",
        warn=lambda e: f"Provider validation for {pname_} raised: {e}",
    )

async def send_provider_list_update(conn, req_id_):
    """Background half of PROVIDER_LIST_REQUEST (RUX-B-02).

    The handler answers immediately from the model cache; this
    task runs discovery for providers whose cache is still empty
    and, if any model list changed, pushes a refreshed
    PROVIDER_LIST_RESPONSE — the same message the renderer already
    applies for list updates. Discovery of one dead provider costs
    a full DISCOVERY_TIMEOUT, so it must never run on the message
    loop (it fires on every connect and after save/remove/reset).
    """
    try:
        changed = await discover_pending_provider_models(conn.secret_store)
    except Exception as e:  # noqa: BLE001 — discovery is best-effort; the cached list already went out
        logger.warning(f"Provider discovery for list update failed: {e}")
        return
    if not changed:
        return
    try:
        providers = await _build_provider_list_async(conn.secret_store, discover=False)
        cfg = engine_state.user_config
        await conn.ws.send_text(Event(
            type=EventType.PROVIDER_LIST_RESPONSE,
            request_id=req_id_,
            payload={"providers": providers, "default_provider": cfg.default_provider, "default_model": cfg.default_model, "health": engine_state.health_manager.get_all_statuses()},
        ).to_json())
    except Exception as e:  # noqa: BLE001 — the client may have disconnected mid-discovery; nothing else to do
        logger.debug(f"Could not send provider list update: {e}")

async def push_provider_list(conn, req_id_, discovery_provider_=None, discovery_error_=None):
    """Push a PROVIDER_LIST_RESPONSE built from the current cache.

    This is the send half of send_provider_list_update, shared
    with the deferred PROVIDER_SAVE discovery below (RUX-B-06):
    the list is the message the renderer already applies for
    provider updates, and a discovery error rides its provider
    entry exactly as REFRESH_MODELS_RESPONSE entries carry it.
    """
    try:
        providers = await _build_provider_list_async(conn.secret_store, discover=False)
        if discovery_provider_ is not None and discovery_error_ is not None:
            for prov in providers:
                if prov.get("name") == discovery_provider_:
                    prov["discovery_error"] = discovery_error_
                    break
        cfg = engine_state.user_config
        await conn.ws.send_text(Event(
            type=EventType.PROVIDER_LIST_RESPONSE,
            request_id=req_id_,
            payload={"providers": providers, "default_provider": cfg.default_provider, "default_model": cfg.default_model, "health": engine_state.health_manager.get_all_statuses()},
        ).to_json())
    except Exception as e:  # noqa: BLE001 — the client may have disconnected mid-push; nothing else to do
        logger.debug(f"Could not push provider list: {e}")

async def complete_provider_save_discovery(
        conn, req_id_, pname_, key_, base_url_, resolve_default_):
    """Deferred discovery half of PROVIDER_SAVE_REQUEST (RUX-B-06).

    The save branch persists the key/config and answers at once;
    only the model discovery moves here. It applies exactly what
    the old inline fetch applied — models + excluded models to
    config and registry, and, for an is_default save, the default
    model resolution the branch deferred — then pushes the
    refreshed provider list. A discovery failure is recorded in
    the negative cache (RUX-B-02) and reported on the provider's
    list entry instead of blocking the save response.
    """
    from aether_engine.routes import websocket as _websocket

    cfg = engine_state.user_config
    discovery_error = None
    try:
        disc = await _websocket.fetch_available_models(
            pname_, key_, base_url=base_url_,
            provider_flavor=custom_provider_flavor(pname_, cfg))
    except Exception as e:  # noqa: BLE001 — discovery is best-effort beside a completed save; surface it on the list entry
        logger.warning(f"Deferred discovery for saved provider {pname_} raised: {e}")
        disc = None
        discovery_error = {"code": None, "message": str(e)}
    if disc is not None:
        if disc.ok:
            clear_discovery_failure(pname_)
        else:
            record_discovery_failure(pname_)
        if disc.error is not None:
            discovery_error = disc.error.to_dict()
        elif disc.excluded:
            cfg.provider_excluded_models[pname_] = disc.excluded
        if disc.models:
            # B10: keep the configured default anchored in the
            # replaced list — except while resolve_default_ is
            # pending: then this provider has just become the
            # default and its model is resolved from this very
            # discovery below, so cfg.default_model still names
            # the *previous* provider's model and must not be
            # pinned into this provider's list.
            models = (
                disc.models
                if resolve_default_
                else anchor_default_model(cfg, pname_, disc.models)
            )
            cfg.provider_models[pname_] = models
            engine_state.model_registry.update_provider_models(pname_, models)
    if resolve_default_ and cfg.default_provider == pname_:
        # The save branch set this provider as the default but
        # deferred choosing its model until discovery landed —
        # the same registry-priority resolution (never raw list
        # position) the inline path used (F-10 residual).
        default_entry = engine_state.model_registry.get_default_model_for_provider(pname_)
        resolved_default = default_entry.id if default_entry else ""
        if not resolved_default:
            cur_models = cfg.provider_models.get(pname_, [])
            resolved_default = cur_models[0] if cur_models else ""
        if resolved_default:
            cfg.default_model = resolved_default
            engine_state.active_provider = pname_
            engine_state.active_model = resolved_default
    try:
        _websocket.save_config(cfg)
    except Exception as e:  # noqa: BLE001 — the in-memory state still landed; the list push below reports it
        logger.warning(f"Could not persist deferred discovery for {pname_}: {e}")
    await push_provider_list(conn, req_id_, pname_, discovery_error)

async def send_refresh_models_result(conn, req_id_, payload_):
    """Answer a REFRESH_MODELS_REQUEST from a background task (RUX-B-01).

    The refresh used to be awaited inline in the message loop:
    a discovery pass over every stored provider, then a second
    pass inside _build_provider_list_async — up to ~100 s during
    which every other message on the socket (model switches,
    cancels, approvals) waited unread. Now the loop stays free
    and the REFRESH_MODELS_RESPONSE is sent when the work lands,
    exactly as send_validation_result does for provider Tests.
    """
    from aether_engine.routes import websocket as _websocket

    try:
        target_provider = payload_.get("provider")
        stored = set(conn.secret_store.list_providers())
        cfg = engine_state.user_config

        providers_to_refresh = set(stored)
        for p in cfg.custom_provider_types:
            if cfg.custom_provider_types[p] == "openai_compatible":
                providers_to_refresh.add(p)
        providers_to_refresh.add("custom_openai")

        if target_provider:
            providers_to_refresh = [target_provider]
        else:
            providers_to_refresh = list(providers_to_refresh)

        refreshed_models: dict[str, Any] = {}

        for pname in providers_to_refresh:
            try:
                payload_key = payload_.get("api_key")
                payload_url = payload_.get("base_url")

                if payload_key is not None:
                    key = payload_key
                else:
                    key = conn.secret_store.load_provider(pname) if pname in stored else ""

                if payload_url is not None:
                    base_url = payload_url
                else:
                    base_url = cfg.custom_base_urls.get(pname)

                is_custom = pname == "custom_openai" or cfg.custom_provider_types.get(pname) == "openai_compatible"
                if (key or is_custom) and not discovery_recently_failed(pname):
                    disc = await _websocket.fetch_available_models(
                        provider_name=pname,
                        api_key=key,
                        base_url=base_url,
                        filter_reachability=True,
                        force_reachability=True,
                        provider_flavor=custom_provider_flavor(pname, cfg),
                    )
                    if disc.ok:
                        clear_discovery_failure(pname)
                    else:
                        record_discovery_failure(pname)
                    # Do not persist discovery data for unsaved/template
                    # providers; models still flow back to the UI response.
                    if disc.models or disc.excluded or disc.error:
                        refreshed_models[pname] = disc
                    is_registered = (
                        pname in stored
                        or pname in cfg.custom_base_urls
                        or pname in cfg.custom_provider_types
                    )
                    # E5-F14: a targeted refresh of a REGISTERED
                    # (saved) provider applies and persists
                    # exactly like the full refresh — the old
                    # "manual click should not save" gate left
                    # config/registry/disk holding the stale
                    # catalog while the response showed the new
                    # one, so the refresh evaporated on the next
                    # listing/restart. Unsaved templates stay
                    # unpersisted via is_registered above.
                    if disc.models and is_registered:
                        # B10: keep the configured default anchored
                        # in the replaced list (anchor_default_model).
                        models = anchor_default_model(cfg, pname, disc.models)
                        cfg.provider_models[pname] = models
                        cfg.provider_excluded_models[pname] = disc.excluded
                        engine_state.model_registry.update_provider_models(pname, models)
                        _websocket.save_config(cfg)
            except Exception as ref_err:  # noqa: BLE001 — per-provider isolation: one provider's refresh failure must not abort the refresh for the rest
                record_discovery_failure(pname)
                logger.error(f"Error during refresh models for {pname}: {ref_err}")

        updated_providers = await _build_provider_list_async(conn.secret_store)
        # Surface freshly discovered models for the target provider even if
        # it is an unsaved template (whose data we intentionally don't persist).
        for prov in updated_providers:
            disc = refreshed_models.get(prov.get("name"))
            if disc is not None:
                prov["models"] = disc.models
                prov["excluded_models"] = disc.excluded
                if disc.error is not None:
                    prov["discovery_error"] = disc.error.to_dict()
        response = Event(
            type=EventType.REFRESH_MODELS_RESPONSE,
            request_id=req_id_,
            payload={
                "providers": updated_providers,
                "default_provider": cfg.default_provider,
                "default_model": cfg.default_model,
            },
        )
    except Exception as e:  # noqa: BLE001 — a refresh failure must still be logged, never kill the connection
        logger.error(f"Refresh models failed: {e}")
        return
    try:
        await conn.ws.send_text(response.to_json())
    except Exception as e:  # noqa: BLE001 — the client may have disconnected mid-refresh; nothing else to do
        logger.debug(f"Could not send refresh models response: {e}")

async def send_capability_check_result(conn, req_id_, method_):
    """Answer a CAPABILITY_CHECK_RUN_NOW from a background task (RUX-B-03).

    The run used to be awaited inline in the message loop:
    per-provider discovery plus a live self-test per model,
    minutes at a time with NO overall budget, during which every
    other message on the socket — model switches, cancels,
    approvals — waited unread and the app looked hung. Now the
    loop stays free and the CAPABILITY_CHECK_HISTORY_RESPONSE is
    sent when the run lands, exactly as send_validation_result
    does for provider Tests.

    The run is bounded by CAPABILITY_CHECK_RUN_BUDGET_SECONDS.
    Past the budget the run is cancelled and recorded + reported
    exactly like a failed run (status FAILED, the timeout as its
    error_log) — same record shape _execute_check_job persists
    for its own failures — so the response never changes shape
    and the timed-out run still appears in history.
    """
    from aether_engine.routes import websocket as _websocket

    # The budget constant stays defined on the websocket module —
    # tests patch it there — so read it at call time.
    budget_seconds = _websocket.CAPABILITY_CHECK_RUN_BUDGET_SECONDS
    mgr = engine_state.scheduler_manager
    started_at = time.time()
    try:
        run_res = await asyncio.wait_for(
            mgr.run_now(method=method_),
            timeout=budget_seconds,
        )
    except TimeoutError:
        run_res = {
            "id": str(uuid.uuid4()),
            "method": method_ or mgr.current_method,
            "started_at": started_at,
            "finished_at": time.time(),
            "models_tested": 0,
            "status": "FAILED",
            "cost_estimate": 0.0,
            "error_log": (
                f"Capability check timed out after "
                f"{budget_seconds:g} seconds; "
                f"the run was cancelled."
            ),
        }
        logger.warning(
            f"Capability check run exceeded the "
            f"{budget_seconds:g}s budget; "
            f"recorded as a failed run.")
        try:
            # Persist it like any other failed run so the summary
            # and history below (and every later history poll)
            # include it. E1-14: through the scheduler's public
            # record_run — the routes layer no longer reaches
            # for its private _record_run.
            mgr.record_run(run_res)
        except Exception as e:  # noqa: BLE001 — the response matters more than the history row; never lose the reply over persistence
            logger.warning(f"Could not record timed-out capability run: {e}")
    except Exception as e:  # noqa: BLE001 — a failed check must still produce a response, never silence
        logger.warning(f"Capability check run raised: {e}")
        run_res = {
            "id": str(uuid.uuid4()),
            "method": method_ or mgr.current_method,
            "started_at": started_at,
            "finished_at": time.time(),
            "models_tested": 0,
            "status": "FAILED",
            "cost_estimate": 0.0,
            "error_log": str(e),
        }
    await conn.send_event(
        req_id_, EventType.CAPABILITY_CHECK_HISTORY_RESPONSE,
        {
            "latest_run": run_res,
            "summary": mgr.get_status_summary(),
            "history": mgr.get_history(),
        },
        "Could not send capability check result")