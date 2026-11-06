"""Media, local models, hub, memory, diagnostics + version (Wave 5 split).

The read-path family of ``ws_tasks`` (RUX-B-08): media search,
Ollama local models, the model hub, the memory export / episodes
/ graph reads, the diagnostics export and the version check.
Module-level functions here, handed the connection's
:class:`~aether_engine.routes.ws_connection.WsConnection`.

The diagnostics build resolves ``SecretStore`` through the
websocket module at call time — the suites patch it there, and
the diagnostics payload must list the providers the patched
store reports. ``backend_or_none`` resolves the same way in
the memory handlers: the suites patch it on the websocket
module too. The version check keeps its ``import httpx``
inside the handler for the same reason (the suites patch the
httpx module's client).

Reply discipline (deslop C5): the local-model, hub, memory-read,
diagnostics and version handlers share the canonical failure
shape — an error payload, never silence — so they answer
through ``WsConnection.reply`` (the memory handlers keep their
synchronous payload builders, now wrapped in the thread hop the
originals ran inline). Media search keeps its bespoke body: a
failed recall is answered as a *degraded success*, not an error
payload. The memory export resolves its backend outside the
builder, exactly as the closure did — ``backend_or_none`` can
raise, and that still propagates rather than becoming a reply.
"""
import asyncio
import logging

from aether_common.contracts import EventType
from aether_engine.state import engine_state
from aether_engine.version import APP_VERSION

logger = logging.getLogger("aether_engine.routes.ws_memory_media")


# ------------------------------------------------------------------
# RUX-B-08: read-path lookups + exports, answered from background
# tasks. Each of these branches used to await external HTTP (or
# run synchronous bulk work) inline in the message loop — seconds
# per call, and the two exports scale with memory size. The async
# lookups now run in tasks like the Wave 1 helpers; the sync
# exports run on worker threads. Response shapes are unchanged.
# ------------------------------------------------------------------


async def send_media_search_result(conn, req_id_, query_, collection_, limit_):
    """Answer a MEDIA_SEARCH_REQUEST from a background task (RUX-B-08)."""
    from aether_engine.tools.media_recall import search_media_entries
    error = None
    if query_:
        try:
            recall = await search_media_entries(
                query_, limit=limit_, collection=collection_)
            results = recall["results"]
            degraded = bool(recall["degraded"])
        except Exception as e:  # noqa: BLE001 — a failed recall must not kill the connection; the loop staying alive is the point of the move
            # E1-03: staying alive is not enough — the requester
            # must be ANSWERED. A failed recall is a degraded,
            # empty result carrying the error, in the same shape
            # the renderer already renders.
            logger.warning(f"Media search for {query_!r} raised: {e}")
            results = []
            degraded = True
            error = str(e)
    else:
        results = []
        degraded = False
    payload = {
        "query": query_,
        "media": results,
        "degraded": degraded,
    }
    if error is not None:
        payload["error"] = error
    await conn.send_event(
        req_id_, EventType.MEDIA_SEARCH_RESPONSE, payload,
        "Could not send media search response")

async def send_local_model_list_result(conn, req_id_):
    """Answer a LOCAL_MODEL_LIST_REQUEST from a background task (RUX-B-08)."""

    async def _build_payload():
        return {"models": await engine_state.ollama_manager.list_models()}

    # E1-03: a dead daemon answers with an empty list + the error
    # instead of leaving the requester waiting on a reply that
    # never comes.
    await conn.reply(
        req_id_, EventType.LOCAL_MODEL_LIST_RESPONSE, _build_payload,
        error_payload=lambda e: {"models": [], "error": str(e)},
        send_failure_message="Could not send local model list response",
        warn=lambda e: f"Local model list raised: {e}")


async def send_local_model_delete_result(conn, req_id_, model_name_):
    """Answer a LOCAL_MODEL_DELETE_REQUEST from a background task (RUX-B-08)."""

    async def _build_payload():
        return {
            "model": model_name_,
            "deleted": await engine_state.ollama_manager.delete_model(model_name_),
        }

    # E1-03: answer with deleted=False + the error instead of
    # leaving the requester waiting on a reply that never comes.
    await conn.reply(
        req_id_, EventType.LOCAL_MODEL_DELETE_RESPONSE, _build_payload,
        error_payload=lambda e: {"model": model_name_, "deleted": False, "error": str(e)},
        send_failure_message="Could not send local model delete response",
        warn=lambda e: f"Local model delete for {model_name_} raised: {e}")


async def send_model_hub_search_result(conn, req_id_, source_, query_, family_):
    """Answer a MODEL_HUB_SEARCH_REQUEST from a background task (RUX-B-08)."""

    async def _build_payload():
        return await engine_state.model_hub.search(
            source=source_, query=query_, family=family_)

    # E1-03: answer in the hub's own {models, error} shape
    # instead of leaving the requester waiting forever.
    await conn.reply(
        req_id_, EventType.MODEL_HUB_SEARCH_RESPONSE, _build_payload,
        error_payload=lambda e: {"models": [], "error": str(e)},
        send_failure_message="Could not send model hub search response",
        warn=lambda e: f"Model hub search raised: {e}")


async def send_model_hub_detail_result(conn, req_id_, source_, repo_id_):
    """Answer a MODEL_HUB_DETAIL_REQUEST from a background task (RUX-B-08)."""

    async def _build_payload():
        return await engine_state.model_hub.detail(
            source=source_, repo_id=repo_id_)

    # E1-03: answer in the hub's own {detail, error} shape
    # instead of leaving the requester waiting forever.
    await conn.reply(
        req_id_, EventType.MODEL_HUB_DETAIL_RESPONSE, _build_payload,
        error_payload=lambda e: {"detail": None, "error": str(e)},
        send_failure_message="Could not send model hub detail response",
        warn=lambda e: f"Model hub detail raised: {e}")


async def send_model_hub_delete_result(conn, req_id_, source_, model_id_):
    """Answer a MODEL_HUB_DELETE_REQUEST from a background task (RUX-B-08)."""

    async def _build_payload():
        return await engine_state.hub_downloads.delete(
            source=source_, model_id=model_id_)

    # E1-03: answer in the delete result's own
    # {success, deleted, error} shape instead of leaving the
    # requester waiting forever.
    await conn.reply(
        req_id_, EventType.MODEL_HUB_DELETE_RESPONSE, _build_payload,
        error_payload=lambda e: {"success": False, "deleted": False, "error": str(e)},
        send_failure_message="Could not send model hub delete response",
        warn=lambda e: f"Model hub delete raised: {e}")


async def send_memory_export_result(conn, req_id_):
    """Answer a MEMORY_EXPORT_REQUEST off the loop (RUX-B-08).

    backend.export() is synchronous and scales with memory size
    (up to 100k facts + 100k episodes), so it runs on a worker
    thread — the pattern BACKUP_CREATE already uses.
    """
    from aether_engine.routes import websocket as _websocket

    backend = _websocket.backend_or_none(engine_state.user_config, context="memory export")
    if backend is None:
        await conn.send_event(
            req_id_, EventType.MEMORY_EXPORT_RESPONSE,
            {"success": False, "error": "The configured memory backend is unavailable (see engine log)."},
            "Could not send memory export response")
        return

    async def _build_payload():
        return {"success": True, "export": await asyncio.to_thread(backend.export)}

    await conn.reply(
        req_id_, EventType.MEMORY_EXPORT_RESPONSE, _build_payload,
        error_payload=lambda e: {"success": False, "error": str(e)},
        send_failure_message="Could not send memory export response")


async def send_memory_episodes_result(conn, req_id_, limit_):
    """Answer a MEMORY_EPISODES_REQUEST off the loop (E1-11).

    The backend reads (episode listing + embedding stats) are
    synchronous store calls that scale with memory size; run
    inline in the handler they stalled the receive loop behind
    one Memory-screen refresh. Same RUX-B-08 pattern as the
    memory export above.
    """
    from aether_engine.routes import websocket as _websocket

    def _build_payload():
        backend = _websocket.backend_or_none(engine_state.user_config, context="memory episodes list")
        if backend is None:
            return {"episodes": [], "error": "The configured memory backend is unavailable (see engine log)."}
        return {
            "episodes": backend.list_episodes(limit=limit_),
            "backend": backend.name,
            "embedding_stats": backend.embedding_stats(),
        }

    async def _build():
        return await asyncio.to_thread(_build_payload)

    await conn.reply(
        req_id_, EventType.MEMORY_EPISODES_RESPONSE, _build,
        error_payload=lambda e: {"episodes": [], "error": str(e)},
        send_failure_message="Could not send memory episodes response")

async def send_memory_graph_result(conn, req_id_, query_, limit_, include_history_):
    """Answer a MEMORY_GRAPH_REQUEST off the loop (E1-11).

    Fact listing/search is a synchronous backend read that
    scales with the graph; same off-loop treatment as the
    episodes list above.
    """
    from aether_engine.routes import websocket as _websocket

    def _build_payload():
        backend = _websocket.backend_or_none(engine_state.user_config, context="memory graph list")
        if backend is None:
            # E4-F08: report unavailability like the episodes
            # list and export handlers do — not a bare empty
            # list the UI cannot tell from "no facts".
            return {"facts": [], "error": "The configured memory backend is unavailable (see engine log)."}
        if query_:
            # Epic 4, E4-08: fact search for the Memory UI.
            return {"facts": backend.search_facts(
                str(query_), limit=limit_, include_history=include_history_)}
        return {"facts": backend.list_facts(
            limit=limit_, include_history=include_history_)}

    async def _build():
        return await asyncio.to_thread(_build_payload)

    await conn.reply(
        req_id_, EventType.MEMORY_GRAPH_RESPONSE, _build,
        error_payload=lambda e: {"facts": [], "error": str(e)},
        send_failure_message="Could not send memory graph response")

async def send_diagnostics_export_result(conn, req_id_):
    """Answer a DIAGNOSTICS_EXPORT_REQUEST off the loop (RUX-B-08).

    build_diagnostics + the two store counts are synchronous
    bulk work, so the whole payload build runs on a worker
    thread — the pattern BACKUP_CREATE already uses.
    """
    def _build_payload():
        try:
            from aether_engine.diagnostics import build_diagnostics
            from aether_engine.memory import episodic as _episodic
            from aether_engine.memory import knowledge_graph as _kg
            from aether_engine.routes import websocket as _websocket

            store = _websocket.SecretStore()
            with _kg._get_conn() as _conn:
                kg_count = _conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
            bundle = build_diagnostics(
                user_config=engine_state.user_config,
                health=engine_state.health_manager.get_all_statuses(),
                kg_count=kg_count,
                episodic_count=_episodic.embedding_stats().get("total", 0),
                audit_log_path=engine_state.audit_logger.log_path,
                providers=store.list_providers(),
            )
            return {"success": True, "bundle": bundle}
        except Exception as e:  # noqa: BLE001 — diagnostics failure must surface, not crash the handler
            return {"success": False, "error": str(e)}

    async def _build():
        return await asyncio.to_thread(_build_payload)

    await conn.reply(
        req_id_, EventType.DIAGNOSTICS_EXPORT_RESPONSE, _build,
        error_payload=lambda e: {"success": False, "error": str(e)},
        send_failure_message="Could not send diagnostics export response")

async def send_version_check_result(conn, req_id_):
    """Answer a VERSION_CHECK_REQUEST from a background task (RUX-B-08)."""

    async def _build_payload():
        import httpx

        def _parse(v: str) -> tuple:
            # E10-F09: SemVer precedence, not digit
            # stripping. Build metadata (+...) never
            # affects precedence; a prerelease precedes
            # its release; prerelease identifiers compare
            # numerically when numeric (numeric < alpha-
            # numeric), lexically otherwise, and a
            # shorter identifier list precedes a longer
            # one it prefixes. An unparseable tag raises
            # — the handler turns that into a failed
            # check, not a successful "no update".
            text = v.strip()
            if text[:1] in ("v", "V"):
                text = text[1:]
            core, _, _build = text.partition("+")
            core, dash, prerelease = core.partition("-")
            pieces = core.split(".")
            if len(pieces) > 3 or not all(p.isdigit() for p in pieces):
                raise ValueError(f"not a semantic version: {v!r}")
            nums = [int(p) for p in pieces] + [0] * (3 - len(pieces))
            if not dash:
                return (*nums, 1, ())
            pre_key = []
            for ident in prerelease.split("."):
                if ident.isdigit():
                    pre_key.append((0, int(ident), ""))
                elif ident:
                    pre_key.append((1, 0, ident))
                else:
                    raise ValueError(f"not a semantic version: {v!r}")
            return (*nums, 0, tuple(pre_key))

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://api.github.com/repos/ProxCGaming/Aether-OS/releases/latest",
                headers={"Accept": "application/vnd.github+json"},
            )
        if resp.status_code == 404:
            return {"success": False, "error": "No releases have been published yet.",
                       "current": APP_VERSION}
        elif resp.status_code != 200:
            return {"success": False, "error": f"GitHub answered {resp.status_code}.",
                       "current": APP_VERSION}
        else:
            latest = str(resp.json().get("tag_name", "")).lstrip("v")
            return {
                "success": True,
                "current": APP_VERSION,
                "latest": latest,
                "update_available": _parse(latest) > _parse(APP_VERSION),
            }

    await conn.reply(
        req_id_, EventType.VERSION_CHECK_RESPONSE, _build_payload,
        error_payload=lambda e: {"success": False, "error": str(e), "current": APP_VERSION},
        send_failure_message="Could not send version check response")
