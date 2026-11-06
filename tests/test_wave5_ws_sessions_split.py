"""Wave 5 (blow-up overhaul) — the websocket split, slice 1.

§1 of the seam map: ``routes/websocket.py`` was one 6,674-line
module whose endpoint function trapped every handler in closures.
Slice 1 is the enabling step only — no handler family moves yet:

* ``routes/ws_connection.py`` gains ``WsConnection``, the
  per-connection context (send-locked socket, audit logger,
  background-task registry, per-name lock getters) the handler
  families will be handed as they move out;
* ``routes/ws_sessions.py`` gains the already-module-level
  session-side block (workspace binding, the artifact GET service,
  message-attachment registration) that used to sit inline in
  ``routes/websocket.py``.

Both moves are only safe if the old surface keeps working exactly:
``routes/websocket.py`` is a top patch target (tests patch
``SecretStore``, ``_create_provider_instance``,
``fetch_available_models`` and ``classify_intent`` on its namespace)
and the moved names are imported from it directly, so websocket
re-exports every moved name. These tests pin that surface by
identity, and pin the ``WsConnection`` contract the later slices
build on.
"""
import asyncio

from aether_engine.routes import websocket, ws_connection, ws_sessions

# Every name the ws_sessions move took out of routes/websocket.py.
MOVED_FUNCTIONS = (
    "_resolve_workspace_binding",
    "_create_bound_session",
    "_unbind_sessions_bound_to",
    "_workspace_session_counts",
    "_run_mcp_prompt_into_chat",
    "_read_text_capped",
    "handle_artifact_get",
    "_inspect_artifact_on_disk",
    "_remove_session_attachments",
    "_register_message_attachments",
)


def test_moved_functions_are_reexported_by_identity():
    for name in MOVED_FUNCTIONS:
        assert getattr(websocket, name) is getattr(ws_sessions, name), name


def test_moved_constants_are_reexported():
    assert websocket.ARTIFACT_CONTENT_CAP_BYTES == (
        ws_sessions.ARTIFACT_CONTENT_CAP_BYTES)
    assert websocket._ARTIFACT_TRUNCATION_MARKER == (
        ws_sessions._ARTIFACT_TRUNCATION_MARKER)


def test_ws_connection_is_the_class_websocket_uses():
    assert websocket.WsConnection is ws_connection.WsConnection


def test_patch_surface_names_still_live_on_websocket():
    # The names the suite patches on aether_engine.routes.websocket
    # must keep resolving there; the moved code consumes none of
    # them, so a split that dropped one would only show up as a
    # silently un-patched real call deep inside a handler.
    for name in (
        "SecretStore",
        "_create_provider_instance",
        "fetch_available_models",
        "classify_intent",
        "pending_plugin_installs",
    ):
        assert hasattr(websocket, name), name


class TestWsConnection:
    def test_holds_socket_and_audit_logger_by_reference(self):
        ws, audit = object(), object()
        conn = ws_connection.WsConnection(ws, audit)
        assert conn.ws is ws
        assert conn.audit_logger is audit

    async def test_spawn_background_registers_and_discards(self):
        conn = ws_connection.WsConnection(object(), object())
        release = asyncio.Event()

        async def work():
            await release.wait()

        task = conn.spawn_background(work())
        assert conn.background_tasks == {task}
        release.set()
        await task
        # The done callback discards synchronously at completion,
        # but give the loop one tick so it cannot race the assert.
        await asyncio.sleep(0)
        assert conn.background_tasks == set()

    def test_named_locks_are_stable_per_name(self):
        conn = ws_connection.WsConnection(object(), object())
        assert conn.mcp_server_lock("srv") is conn.mcp_server_lock("srv")
        assert conn.mcp_server_lock("a") is not conn.mcp_server_lock("b")
        assert conn.plugin_lifecycle_lock("p") is (
            conn.plugin_lifecycle_lock("p"))

    def test_mcp_and_plugin_locks_are_separate_namespaces(self):
        # The closure originals kept two maps: an MCP server and a
        # plugin that share a name must not serialise against each
        # other.
        conn = ws_connection.WsConnection(object(), object())
        assert conn.mcp_server_lock("same") is not (
            conn.plugin_lifecycle_lock("same"))
