"""Wave 5 (blow-up overhaul) — the websocket split, slice 2.

§1 of the seam map: the four handler families that slice 1 left
as closures inside ``ws_tasks`` move into their own modules as
module-level handlers handed the connection's ``WsConnection``:

* ``routes/ws_providers.py`` — provider/model replies +
  capability check (RUX-B-01/02/03/06);
* ``routes/ws_mcp_plugins.py`` — MCP server ops (RUX-B-04) +
  plugin lifecycle (RUX-B-10), plus the module-global
  pending-install state that used to sit at the top of
  ``routes/websocket.py``;
* ``routes/ws_memory_media.py`` — media, local models, hub,
  memory, diagnostics, version (RUX-B-08);
* ``routes/ws_voice.py`` — voice session + dictation + pipeline
  lifecycle (RUX-B-09) and the STT/TTS factories.

The moves are only safe if the patch surface keeps working
exactly: the suites patch ``SecretStore``,
``_create_provider_instance``, ``fetch_available_models``,
``classify_intent``, ``save_config``, ``run_langgraph_task``
and the speech factories on ``aether_engine.routes.websocket``,
read the pending-install tables as websocket attributes, and
setattr ``PLUGIN_INSTALL_TTL_SECONDS`` /
``CAPABILITY_CHECK_RUN_BUDGET_SECONDS`` there. These tests pin
the re-exports by identity, pin that a patch on the websocket
module still intercepts the moved code, and pin the
``WsConnection`` growth (voice state, secret store, reply
discipline — deslop C5) the families now rely on.
"""
import asyncio
import json
import time

from aether_engine.routes import (
    websocket,
    ws_connection,
    ws_mcp_plugins,
    ws_memory_media,
    ws_providers,
    ws_voice,
)
from aether_engine.secrets.storage import SecretStore

# Handlers websocket imports back from each family module (the
# dispatch chain calls them through these names).
REEXPORTED_HANDLERS = (
    (ws_providers, (
        "send_validation_result",
        "send_provider_list_update",
        "complete_provider_save_discovery",
        "send_refresh_models_result",
        "send_capability_check_result",
    )),
    (ws_mcp_plugins, (
        "send_mcp_server_remove_result",
        "send_mcp_server_update_result",
        "send_mcp_server_connect_result",
        "send_mcp_server_disconnect_result",
        "send_mcp_server_test_result",
        "send_mcp_resource_list_result",
        "send_mcp_resource_read_result",
        "send_mcp_prompt_list_result",
        "send_mcp_prompt_run_result",
        "send_plugin_install_result",
        "send_plugin_uninstall_result",
        "send_plugin_toggle_result",
        "prune_expired_plugin_installs",
    )),
    (ws_memory_media, (
        "send_media_search_result",
        "send_local_model_list_result",
        "send_local_model_delete_result",
        "send_model_hub_search_result",
        "send_model_hub_detail_result",
        "send_model_hub_delete_result",
        "send_memory_export_result",
        "send_memory_episodes_result",
        "send_memory_graph_result",
        "send_diagnostics_export_result",
        "send_version_check_result",
    )),
    (ws_voice, (
        "send_voice_session_start_result",
        "send_voice_session_stop_result",
        "send_dictation_chunk_result",
        "send_dictation_stop_result",
        "enqueue_voice_op",
        "enqueue_dictation",
        "dictation_stt",
        "read_aloud_tts",
        "open_read_aloud_tts",
        "stop_voice_session",
        "stop_read_aloud",
        "close_speech_engines",
        "cancel_voice_background_tasks",
        "create_stt_engine",
        "create_tts_engine",
    )),
)


def test_family_handlers_are_reexported_by_identity():
    for module, names in REEXPORTED_HANDLERS:
        for name in names:
            assert getattr(websocket, name) is getattr(module, name), name


def test_plugin_install_state_is_reexported_by_identity():
    # The dispatch loop, the disconnect cleanup and the suites
    # all resolve the tables as websocket attributes; they must
    # be the very dicts the plugin family owns.
    assert websocket.pending_plugin_installs is (
        ws_mcp_plugins.pending_plugin_installs)
    assert websocket.pending_plugin_install_created is (
        ws_mcp_plugins.pending_plugin_install_created)
    assert websocket.PLUGIN_INSTALL_TTL_SECONDS == (
        ws_mcp_plugins.PLUGIN_INSTALL_TTL_SECONDS)


def test_ttl_patch_on_websocket_governs_the_prune(monkeypatch):
    # One suite setattrs PLUGIN_INSTALL_TTL_SECONDS on the
    # websocket module to expire entries immediately; the prune
    # (now in ws_mcp_plugins) reads it there at call time.
    monkeypatch.setattr(websocket, "PLUGIN_INSTALL_TTL_SECONDS", 0)
    ws_mcp_plugins.pending_plugin_installs["req-x"] = {"source": "s"}
    ws_mcp_plugins.pending_plugin_install_created["req-x"] = (
        time.time() - 10)
    try:
        websocket.prune_expired_plugin_installs()
        assert "req-x" not in ws_mcp_plugins.pending_plugin_installs
        assert "req-x" not in ws_mcp_plugins.pending_plugin_install_created
    finally:
        ws_mcp_plugins.pending_plugin_installs.pop("req-x", None)
        ws_mcp_plugins.pending_plugin_install_created.pop("req-x", None)


def test_factory_patch_on_websocket_intercepts_moved_voice_code(monkeypatch):
    # The suites setattr create_stt_engine on the websocket
    # module; the moved dictation cache resolves it back through
    # that module at call time, so the patch must still land.
    sentinel = object()
    calls = []

    def fake_factory():
        calls.append(1)
        return sentinel

    monkeypatch.setattr(websocket, "create_stt_engine", fake_factory)
    conn = ws_connection.WsConnection(
        object(), object(), secret_store=object())
    assert ws_voice.dictation_stt(conn) is sentinel
    # Cached per connection (S4-M7): a second call does not
    # rebuild the engine.
    assert ws_voice.dictation_stt(conn) is sentinel
    assert calls == [1]


class TestWsConnectionGrowth:
    def test_secret_store_passed_through_by_identity(self):
        store = object()
        conn = ws_connection.WsConnection(object(), object(), store)
        assert conn.secret_store is store

    def test_secret_store_defaults_to_a_real_store(self):
        conn = ws_connection.WsConnection(object(), object())
        assert isinstance(conn.secret_store, SecretStore)

    def test_voice_state_defaults(self):
        conn = ws_connection.WsConnection(object(), object(), object())
        state = conn.voice_state
        assert state["pipeline"] is None
        assert state["task"] is None
        assert state["read_aloud_task"] is None
        assert state["read_aloud_id"] is None
        assert state["dictation_stt"] is None
        assert state["dictation_stt_opened"] is False
        assert state["dictation_stt_model"] is None
        assert state["read_aloud_tts"] is None
        assert state["read_aloud_tts_opened"] is False

    def test_voice_state_is_per_connection(self):
        a = ws_connection.WsConnection(object(), object(), object())
        b = ws_connection.WsConnection(object(), object(), object())
        assert a.voice_state is not b.voice_state

    def test_chain_tails_start_empty_and_start_lock_is_per_connection(self):
        a = ws_connection.WsConnection(object(), object(), object())
        b = ws_connection.WsConnection(object(), object(), object())
        assert a.dictation_tail is None
        assert a.voice_op_tail is None
        assert isinstance(a.voice_start_lock, asyncio.Lock)
        assert a.voice_start_lock is not b.voice_start_lock

    def test_execute_task_generator_slot_starts_empty(self):
        conn = ws_connection.WsConnection(object(), object(), object())
        assert conn.execute_task_generator is None


class _FakeSocket:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def send_text(self, data):
        if self.fail:
            raise RuntimeError("dead socket")
        self.sent.append(json.loads(data))


class TestReplyDiscipline:
    async def test_reply_sends_the_built_payload(self):
        ws = _FakeSocket()
        conn = ws_connection.WsConnection(ws, object(), object())

        async def build():
            return {"ok": True}

        await conn.reply(
            "req-1", "SOME_RESPONSE", build,
            error_payload=lambda e: {"success": False, "error": str(e)},
            send_failure_message="Could not send some response")
        assert len(ws.sent) == 1
        assert ws.sent[0]["type"] == "SOME_RESPONSE"
        assert ws.sent[0]["request_id"] == "req-1"
        assert ws.sent[0]["payload"] == {"ok": True}

    async def test_reply_turns_a_build_failure_into_the_error_payload(self):
        ws = _FakeSocket()
        conn = ws_connection.WsConnection(ws, object(), object())

        async def build():
            raise ValueError("boom")

        await conn.reply(
            "req-2", "SOME_RESPONSE", build,
            error_payload=lambda e: {"success": False, "error": str(e)},
            send_failure_message="Could not send some response")
        # Never silence: the failure IS the response.
        assert ws.sent[0]["payload"] == {"success": False, "error": "boom"}

    async def test_send_event_swallows_a_dead_socket(self):
        ws = _FakeSocket(fail=True)
        conn = ws_connection.WsConnection(ws, object(), object())
        # Must not raise.
        await conn.send_event(
            "req-3", "SOME_RESPONSE", {"ok": True},
            "Could not send some response")
        assert ws.sent == []
