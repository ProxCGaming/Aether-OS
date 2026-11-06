"""MCP server ops + plugin lifecycle for the task websocket (Wave 5 split).

The MCP handler family (RUX-B-04) and the plugin lifecycle family
(RUX-B-10) of ``ws_tasks``, plus the module-global plugin-install
state they share with the dispatch loop: the pending-install
tables and their TTL sweep used to live at the top of
``routes/websocket.py``, which now imports them back from here,
so the dispatch branches, the disconnect cleanup and the test
suites keep resolving the same objects as websocket attributes.

``_run_mcp_prompt_into_chat`` deliberately stays in
``routes/ws_sessions.py``: it is a chat/session service (it
creates sessions and inserts messages) shared with the task
path's slash-command handling, not an MCP-wire handler — the
prompt-run handler below calls it exactly as the closure did.

Reply discipline (deslop C5): the plugin handlers' failure shape
is the canonical ``{"success": False, "error"}`` payload, so
they answer through ``WsConnection.reply``. The MCP handlers
keep their bespoke bodies — each maps the registry's result
dicts onto its own response fields — with only the send tail
factored into ``WsConnection.send_event``.
"""
import asyncio
import logging
import time
from typing import Any

from aether_common.contracts import EventType
from aether_engine.memory import session_store
from aether_engine.routes.ws_sessions import (
    _create_bound_session,
    _run_mcp_prompt_into_chat,
)
from aether_engine.state import engine_state

logger = logging.getLogger("aether_engine.routes.ws_mcp_plugins")


# Phase 10.1 A1: pending plugin installs awaiting the user's approval.
# Keyed by request id; each value is the prepare_install() payload
# ({"source": ..., "manifest": {...}}) for one PLUGIN_INSTALL_REQUEST.
# The install is only executed when the matching PLUGIN_APPROVAL_GRANTED
# arrives; a PLUGIN_APPROVAL_REJECTED pops (discards) the entry.
pending_plugin_installs: dict[str, dict[str, Any]] = {}

# E1-06: a prepared install the user never answers must not wait
# forever — entries expire after this many seconds (swept whenever
# a new install is prepared or a grant arrives), and each entry is
# also dropped when the connection that prepared it disconnects.
PLUGIN_INSTALL_TTL_SECONDS = 600.0
pending_plugin_install_created: dict[str, float] = {}


def prune_expired_plugin_installs() -> None:
    """Drop pending plugin installs older than the TTL (E1-06)."""
    from aether_engine.routes import websocket as _websocket

    # The TTL is re-exported by the websocket module and tests
    # patch it there; read it at call time so the patch lands.
    now = time.time()
    expired = [
        req_id for req_id, created in pending_plugin_install_created.items()
        if now - created > _websocket.PLUGIN_INSTALL_TTL_SECONDS]
    for req_id in expired:
        pending_plugin_installs.pop(req_id, None)
        pending_plugin_install_created.pop(req_id, None)


# ------------------------------------------------------------------
# RUX-B-04: the MCP handler family, answered from background tasks.
#
# Every handler in this family used to await the MCP registry
# inline in the message loop at the server's connect timeout
# (30 s default, configurable to 300 s — validate_mcp_server_config
# in mcp/registry.py), so touching one hung or dead server in
# Settings froze ALL control traffic on the socket — model
# switches, cancels, approvals — for that long, per click. Now
# each branch spawns one of the helpers below; the loop stays
# free and the response is sent when the work lands, exactly as
# send_validation_result does for provider Tests.
# Race guard: the serial loop also implicitly guaranteed one
# MCP operation per server at a time — the per-server lock that
# preserves it lives on the connection context
# (routes/ws_connection.py carries the full rationale verbatim).

async def send_mcp_server_remove_result(conn, req_id_, name_):
    """Answer an MCP_SERVER_REMOVE_REQUEST from a background task (RUX-B-04)."""
    from aether_engine.mcp.registry import (
        GLOBAL_MCP_REGISTRY,
        MCPConfigError,
    )
    ok, error = True, ""
    if name_:
        # Issue 011 (S5): wrap like ADD/UPDATE — remove_server
        # raises MCPConfigError via _save_config when the config
        # file failed to load (corrupt), and without this the
        # request would end with no response at all. A failed
        # remove answers success:false + error. remove_server
        # closes any live session inside the removal (S5).
        try:
            async with conn.mcp_server_lock(name_):
                await GLOBAL_MCP_REGISTRY.remove_server(name_)
        except (MCPConfigError, OSError) as e:
            # OSError: the config save's storage failure
            # (E3-F04) — same answer-don't-die rule as ADD.
            ok, error = False, str(e)
    await conn.send_event(
        req_id_, EventType.MCP_SERVER_REMOVE_RESPONSE,
        {"success": ok, "name": name_, "error": error},
        f"Could not send MCP server remove response for {name_}")

async def send_mcp_server_update_result(conn, req_id_, name_, auto_connect_, enabled_):
    """Answer an MCP_SERVER_UPDATE_REQUEST from a background task (RUX-B-04)."""
    from aether_engine.mcp.registry import (
        GLOBAL_MCP_REGISTRY,
        MCPConfigError,
    )
    ok, error, config = True, "", None
    if not name_:
        ok, error = False, "Server name is required."
    else:
        try:
            async with conn.mcp_server_lock(name_):
                config = await GLOBAL_MCP_REGISTRY.update_server(
                    name_,
                    auto_connect=auto_connect_,
                    enabled=enabled_,
                )
        except (MCPConfigError, OSError) as e:
            # OSError: the config save's storage failure
            # (E3-F04) — same answer-don't-die rule as ADD.
            ok, error = False, str(e)
    await conn.send_event(
        req_id_, EventType.MCP_SERVER_UPDATE_RESPONSE,
        {"success": ok, "name": name_, "config": config, "error": error},
        f"Could not send MCP server update response for {name_}")

async def send_mcp_server_connect_result(conn, req_id_, name_):
    """Answer an MCP_SERVER_CONNECT_REQUEST from a background task (RUX-B-04)."""
    from aether_engine.mcp.registry import GLOBAL_MCP_REGISTRY
    if name_:
        try:
            async with conn.mcp_server_lock(name_):
                result = await GLOBAL_MCP_REGISTRY.connect_server(name_)
        except Exception as e:  # noqa: BLE001 — as in send_validation_result: a failure must still produce a response, never silence (the registry/client convert ordinary failures to result dicts; this net covers the unexpected)
            logger.warning(f"MCP connect for {name_} raised: {e}")
            result = {"ok": False, "error": str(e)}
    else:
        result = {"ok": False, "error": "Server name is required."}
    server_status = GLOBAL_MCP_REGISTRY.get_server_status(name_) if name_ else {}
    await conn.send_event(
        req_id_, EventType.MCP_SERVER_CONNECT_RESPONSE,
        {
            "success": result["ok"], "name": name_, "error": result.get("error", ""),
            "status": server_status.get("status"), "tool_count": server_status.get("tool_count", 0),
        },
        f"Could not send MCP server connect response for {name_}")

async def send_mcp_server_disconnect_result(conn, req_id_, name_):
    """Answer an MCP_SERVER_DISCONNECT_REQUEST from a background task (RUX-B-04)."""
    from aether_engine.mcp.registry import GLOBAL_MCP_REGISTRY
    if name_:
        try:
            async with conn.mcp_server_lock(name_):
                result = await GLOBAL_MCP_REGISTRY.disconnect_server(name_)
        except Exception as e:  # noqa: BLE001 — as in send_validation_result: a failure must still produce a response, never silence
            logger.warning(f"MCP disconnect for {name_} raised: {e}")
            result = {"ok": False, "error": str(e)}
    else:
        result = {"ok": False, "error": "Server name is required."}
    server_status = GLOBAL_MCP_REGISTRY.get_server_status(name_) if name_ else {}
    await conn.send_event(
        req_id_, EventType.MCP_SERVER_DISCONNECT_RESPONSE,
        {
            "success": result["ok"], "name": name_, "error": result.get("error", ""),
            "status": server_status.get("status"), "tool_count": server_status.get("tool_count", 0),
        },
        f"Could not send MCP server disconnect response for {name_}")

async def send_mcp_server_test_result(conn, req_id_, name_):
    """Answer an MCP_SERVER_TEST_REQUEST from a background task (RUX-B-04)."""
    from aether_engine.mcp.registry import GLOBAL_MCP_REGISTRY
    if name_:
        try:
            async with conn.mcp_server_lock(name_):
                result = await GLOBAL_MCP_REGISTRY.test_connection(name_)
        except Exception as e:  # noqa: BLE001 — as in send_validation_result: a failure must still produce a response, never silence
            logger.warning(f"MCP connection test for {name_} raised: {e}")
            result = {"ok": False, "error": str(e), "tools": []}
    else:
        result = {"ok": False, "error": "Server name is required.", "tools": []}
    await conn.send_event(
        req_id_, EventType.MCP_SERVER_TEST_RESPONSE,
        {
            "success": result["ok"],
            "name": name_,
            "error": result.get("error", ""),
            "tools": result.get("tools", []),
        },
        f"Could not send MCP server test response for {name_}")

async def send_mcp_resource_list_result(conn, req_id_, name_):
    """Answer an MCP_RESOURCE_LIST_REQUEST from a background task (RUX-B-04)."""
    # Issue 006: list a connected server's resources.
    from aether_engine.mcp.registry import GLOBAL_MCP_REGISTRY
    resources, ok, error = [], True, ""
    if not name_:
        ok, error = False, "Server name is required."
    else:
        try:
            async with conn.mcp_server_lock(name_):
                resources = await GLOBAL_MCP_REGISTRY.list_resources(name_)
        except Exception as e:  # noqa: BLE001 — MCP client calls raise arbitrary server/transport errors; surfaced in the response payload
            ok, error = False, str(e)
    await conn.send_event(
        req_id_, EventType.MCP_RESOURCE_LIST_RESPONSE,
        {"success": ok, "name": name_, "resources": resources, "error": error},
        f"Could not send MCP resource list response for {name_}")

async def send_mcp_resource_read_result(conn, req_id_, name_, uri_, session_id_):
    """Answer an MCP_RESOURCE_READ_REQUEST from a background task (RUX-B-04)."""
    # Issue 006: read a resource and load it into the current
    # chat as a context message. Role "system": it is context,
    # not something the user said — and the supervisor's
    # get_current_chat_history briefing reads it back to the
    # model on the next turn. If no session was given (or it
    # no longer exists), create one so the resource is never
    # lost. The UI also gets the message live in this response
    # (App.jsx appends it) — the Settings panel alone cannot
    # update the chat view.
    from aether_engine.mcp.registry import GLOBAL_MCP_REGISTRY
    ok, error, content, session_id = True, "", "", session_id_
    if not name_ or not uri_:
        ok, error = False, "Server name and resource URI are required."
    else:
        try:
            # Only the registry read takes the per-server lock:
            # the chat persistence below touches no MCP state.
            async with conn.mcp_server_lock(name_):
                content = await GLOBAL_MCP_REGISTRY.read_resource(name_, uri_)
        except Exception as e:  # noqa: BLE001 — MCP client calls raise arbitrary server/transport errors; surfaced in the response payload
            ok, error = False, str(e)
    message = ""
    truncated = content.endswith("...[resource truncated to 20000 chars]")
    if ok:
        # A chat-save failure must return an error, never kill
        # the websocket (an exception here used to take the
        # whole connection down with it).
        try:
            if session_id:
                sess = await asyncio.to_thread(session_store.get_session, session_id)
                if sess is None:
                    session_id = None
            if not session_id:
                session_id, _binding = await _create_bound_session(
                    f"MCP resource: {uri_}")
            message = f"[MCP resource from server '{name_}': {uri_}]\n{content}"
            await asyncio.to_thread(
                session_store.add_message, session_id, "system", message
            )
        except Exception as e:  # noqa: BLE001 — chat-persistence boundary: a save failure becomes an error response, never a dead websocket
            logger.error(f"Failed to save MCP resource to chat: {e}")
            ok, error = False, f"Failed to save resource to chat: {e}"
            message = ""
        if ok:
            engine_state.audit_logger.log_event("MCP_RESOURCE_READ", {
                "server": name_, "uri": uri_, "session_id": session_id,
                "truncated": truncated,
            })
    await conn.send_event(
        req_id_, EventType.MCP_RESOURCE_READ_RESPONSE,
        {
            "success": ok, "name": name_, "uri": uri_, "content": content,
            "message": message, "truncated": truncated,
            "session_id": session_id, "error": error,
        },
        f"Could not send MCP resource read response for {name_}")

async def send_mcp_prompt_list_result(conn, req_id_, name_):
    """Answer an MCP_PROMPT_LIST_REQUEST from a background task (RUX-B-04)."""
    # Issue 007: list a connected server's prompt templates.
    from aether_engine.mcp.registry import GLOBAL_MCP_REGISTRY
    prompts, ok, error = [], True, ""
    if not name_:
        ok, error = False, "Server name is required."
    else:
        try:
            async with conn.mcp_server_lock(name_):
                prompts = await GLOBAL_MCP_REGISTRY.list_prompts(name_)
        except Exception as e:  # noqa: BLE001 — MCP client calls raise arbitrary server/transport errors; surfaced in the response payload
            ok, error = False, str(e)
    await conn.send_event(
        req_id_, EventType.MCP_PROMPT_LIST_RESPONSE,
        {"success": ok, "name": name_, "prompts": prompts, "error": error},
        f"Could not send MCP prompt list response for {name_}")

async def send_mcp_prompt_run_result(conn, req_id_, name_, prompt_name_, arguments_, session_id_):
    """Answer an MCP_PROMPT_RUN_REQUEST from a background task (RUX-B-04)."""
    # Issue 007: run a prompt template and insert its messages
    # into the current chat (same pattern as resources in
    # 006, but with the prompt's own roles). Never auto-run:
    # only an explicit Run / slash command reaches here. The
    # shared _run_mcp_prompt_into_chat bundles the registry call
    # with the chat insert, so the per-server lock covers the
    # whole run — the two cannot be split without splitting the
    # helper the slash path also uses.
    if name_:
        async with conn.mcp_server_lock(name_):
            result = await _run_mcp_prompt_into_chat(
                name_, prompt_name_, arguments_, session_id_
            )
    else:
        result = await _run_mcp_prompt_into_chat(
            name_, prompt_name_, arguments_, session_id_
        )
    await conn.send_event(
        req_id_, EventType.MCP_PROMPT_RUN_RESPONSE,
        {
            "success": result["ok"], "name": name_, "prompt": prompt_name_,
            "messages": result["messages"],
            "session_id": result["session_id"], "error": result["error"],
        },
        f"Could not send MCP prompt run response for {name_}")


# ------------------------------------------------------------------
# RUX-B-10: the plugin lifecycle, answered from background tasks.
#
# Install's tree copy ran synchronously in the loop on approval
# (scaling with plugin size); uninstall/toggle awaited an MCP
# disconnect per installed server (30 s default each) in the loop
# before their synchronous file work. Now the disconnects await
# in a task and the file work runs on worker threads. A
# per-plugin lock preserves the serial loop's ordering between
# lifecycle operations on the same plugin, as the MCP helpers'
# per-server lock does (RUX-B-04). Response shapes — including
# the failure payloads the renderer surfaces (E7-F05) — are
# exactly the inline handlers'.

async def send_plugin_install_result(conn, req_id_, data_):
    """Answer a PLUGIN_APPROVAL_GRANTED from a background task (RUX-B-10)."""
    plugin_name = (data_.get("manifest") or {}).get("name", "")

    async def _build_payload():
        async with conn.plugin_lifecycle_lock(plugin_name):
            await asyncio.to_thread(engine_state.plugin_installer.install, data_)
        engine_state.audit_logger.log_event("PLUGIN_INSTALLED", {"manifest": data_["manifest"]})
        return {"success": True}

    await conn.reply(
        req_id_, EventType.PLUGIN_INSTALL_RESPONSE, _build_payload,
        error_payload=lambda e: {"success": False, "error": str(e)},
        send_failure_message="Could not send plugin install response")


async def disconnect_plugin_mcp_servers(name_):
    """Disconnect a plugin's MCP servers (PLUGIN_UNINSTALL/TOGGLE).

    Disconnect the plugin's MCP servers first, in this
    (session-owner) task — remove_server_sync inside
    uninstall()/toggle() cannot close a live session from a sync
    caller (anyio cancel-scope rule, see registry.py).
    """
    from aether_engine.mcp.registry import GLOBAL_MCP_REGISTRY
    installed = engine_state.plugin_installer.get_all().get(name_, {})
    for mcp_name in installed.get("installed_mcp", []):
        await GLOBAL_MCP_REGISTRY.disconnect_server(mcp_name)

async def send_plugin_uninstall_result(conn, req_id_, name_):
    """Answer a PLUGIN_UNINSTALL_REQUEST from a background task (RUX-B-10)."""

    async def _build_payload():
        async with conn.plugin_lifecycle_lock(name_ or ""):
            await disconnect_plugin_mcp_servers(name_)
            await asyncio.to_thread(engine_state.plugin_installer.uninstall, name_)
        return {"success": True, "name": name_}

    await conn.reply(
        req_id_, EventType.PLUGIN_UNINSTALL_RESPONSE, _build_payload,
        error_payload=lambda e: {"success": False, "error": str(e)},
        send_failure_message="Could not send plugin uninstall response")


async def send_plugin_toggle_result(conn, req_id_, name_, enabled_):
    """Answer a PLUGIN_TOGGLE_REQUEST from a background task (RUX-B-10)."""

    async def _build_payload():
        async with conn.plugin_lifecycle_lock(name_ or ""):
            if not enabled_:
                # Mirrors the uninstall path (E7-F01): if a
                # disconnect fails, toggle() is never reached, so
                # the plugin stays enabled with its sessions and
                # skills untouched.
                await disconnect_plugin_mcp_servers(name_)
            await asyncio.to_thread(
                engine_state.plugin_installer.toggle, name_, enabled_)
        return {"success": True, "name": name_, "enabled": enabled_}

    await conn.reply(
        req_id_, EventType.PLUGIN_TOGGLE_RESPONSE, _build_payload,
        error_payload=lambda e: {"success": False, "error": str(e)},
        send_failure_message="Could not send plugin toggle response")
