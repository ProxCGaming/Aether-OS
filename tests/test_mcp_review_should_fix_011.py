"""Issue 011: fix the whole-branch review SHOULD FIX items (S1–S6).

TDD red first: these tests fail until the fixes land in client.py,
registry.py, installer.py and the websocket REMOVE handler.

- S1: a server that stalls on a session call must fail within the
  server timeout (real stalled server, not a disconnected client),
  and the engine must stay responsive afterwards.
- S2: a stdio server must not see the engine's secrets — only the
  config ``env`` plus a minimal allow-list reaches the child.
- S3: a server-supplied prompt's assistant turn must be tagged
  ``[MCP prompt from server 'x']`` in chat, never shown as the
  model's own words.
- S4: a plugin manifest ``file_path`` of ``../x.json`` (or absolute)
  must be rejected with a named error, in both the dir and zip branches.
- S5: removing a server while the config file is corrupt must return
  ``success: false`` + error over the websocket, not drop the session.
- S6: ``remove_server_sync`` must refuse while a server is connected
  (loop running) instead of leaving a live session behind.
"""
import json
import os
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from aether_common.contracts import Event, EventType
from aether_engine.app import app, engine_state
from aether_engine.mcp.client import MCPClientError
from aether_engine.mcp.registry import MCPConfigError, MCPRegistry
from tests.fake_mcp_http import start_fake_http_server

FAKE_STDIO = Path(__file__).parent / "fake_stdio_server.py"
STALL_STDIO = Path(__file__).parent / "fake_stall_stdio_server.py"

PROMPT_TAG = "[MCP prompt from server 'fake']"


@pytest.fixture()
def manager():
    from aether_engine.mcp.client import MCPClientManager

    return MCPClientManager()


def _stdio_config(**overrides):
    config = {
        "transport": "stdio",
        "command": sys.executable,
        "args": [str(FAKE_STDIO)],
        "timeout": 10,
    }
    config.update(overrides)
    return config


def _stall_config(method, timeout=2):
    return {
        "transport": "stdio",
        "command": sys.executable,
        "args": [str(STALL_STDIO), method],
        "timeout": timeout,
    }


# -- S1: session-call timeouts -------------------------------------------

async def test_s1_stalled_list_tools_fails_within_timeout(manager):
    """A server that answers initialize but never answers tools/list:
    connect must fail within the 2s timeout, not hang."""
    start = time.monotonic()
    result = await manager.connect("stall", _stall_config("tools/list", timeout=2))
    elapsed = time.monotonic() - start
    assert result["ok"] is False, result
    assert "timed out" in result["error"].lower() or "timeout" in result["error"].lower(), result
    assert elapsed < 8.0, f"connect took {elapsed:.1f}s — no timeout on list_tools"
    assert manager.get_status("stall")["status"] == "failed"
    assert not manager.is_connected("stall")
    await manager.disconnect("stall")


async def test_s1_stalled_list_resources_fails_and_marks_failed(manager):
    """Connect succeeds (tools/list answered), then resources/list
    stalls: the call must time out, raise MCPClientError, and mark
    the server failed — and the manager must still work after."""
    assert (await manager.connect("stall", _stall_config("resources/list", timeout=2)))["ok"] is True
    start = time.monotonic()
    with pytest.raises(MCPClientError, match="Timed out"):
        await manager.list_resources("stall")
    assert time.monotonic() - start < 8.0
    assert manager.get_status("stall")["status"] == "failed"
    await manager.disconnect("stall")
    # The engine is still responsive: a good server still connects.
    assert (await manager.connect("fake", _stdio_config()))["ok"] is True
    assert (await manager.call_tool("fake", "add", {"a": 1, "b": 2})).strip() == "3"
    await manager.disconnect("fake")


async def test_s1_test_connection_times_out_on_stalled_server(manager):
    start = time.monotonic()
    result = await manager.test_connection("stall", _stall_config("tools/list", timeout=2))
    elapsed = time.monotonic() - start
    assert result["ok"] is False
    assert elapsed < 8.0, f"test_connection took {elapsed:.1f}s"
    assert manager.get_status("stall")["status"] == "disconnected"


# -- S2: stdio env allow-list ---------------------------------------------

async def test_s2_stdio_child_does_not_see_engine_secrets(manager, monkeypatch):
    """A secret in the engine's environment must NOT reach the stdio
    child; config env and the allow-list (PATH) still must."""
    monkeypatch.setenv("AETHER_TEST_SECRET_XYZ", "topsecret-do-not-leak")
    config = _stdio_config(env={"AETHER_STDIO_MARKER": "hello-stdio"})
    assert (await manager.connect("fake", config))["ok"] is True
    assert (await manager.call_tool("fake", "echo_env", {"name": "AETHER_STDIO_MARKER"})).strip() == "hello-stdio"
    assert (await manager.call_tool("fake", "echo_env", {"name": "AETHER_TEST_SECRET_XYZ"})).strip() == ""
    assert (await manager.call_tool("fake", "echo_env", {"name": "PATH"})).strip() != ""
    # Proxy config still reaches the child: npx/npm servers need it to
    # resolve packages (verified: without it the filesystem server
    # never answers). Only asserted where the engine actually has one.
    if "HTTPS_PROXY" in os.environ:
        assert (await manager.call_tool("fake", "echo_env", {"name": "HTTPS_PROXY"})).strip() == os.environ["HTTPS_PROXY"]
    await manager.disconnect("fake")


# -- S3: prompt tagging -----------------------------------------------------

@pytest.fixture(scope="module")
def fake_url():
    url, stop = start_fake_http_server()
    yield url
    stop()


async def test_s3_assistant_prompt_message_is_tagged(fake_url, manager):
    config = {"transport": "streamable_http", "url": fake_url, "timeout": 10}
    assert (await manager.connect("fake", config))["ok"] is True
    messages = await manager.get_prompt("fake", "duet", {})
    assert len(messages) == 2
    assistant = next(m for m in messages if m["role"] == "assistant")
    assert assistant["content"].startswith(PROMPT_TAG), assistant
    assert "Hello! I am the model." in assistant["content"]
    # The user turn is server-supplied too, so it carries the tag as well.
    assert messages[0]["content"].startswith(PROMPT_TAG)
    await manager.disconnect("fake")


async def test_s3_stdio_duet_prompt_is_tagged(manager):
    assert (await manager.connect("fake", _stdio_config()))["ok"] is True
    messages = await manager.get_prompt("fake", "duet", {})
    assistant = next(m for m in messages if m["role"] == "assistant")
    assert assistant["content"].startswith(PROMPT_TAG), assistant
    await manager.disconnect("fake")


class TestS3S5WebSocket:
    """Handler level: real /ws/tasks, real registry on a temp config."""

    def setup_method(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp_dir.name)
        engine_state.token_path = self.tmp_path / "engine.token"
        engine_state.audit_logger.log_path = self.tmp_path / "audit.log"
        self.token = engine_state.initialize_auth()
        self._db_patcher = patch(
            "aether_engine.memory.session_store.DB_PATH",
            self.tmp_path / "test_memory.db",
        )
        self._db_patcher.start()
        from aether_engine.memory import session_store

        session_store._initialized = False
        self.config_path = self.tmp_path / "mcp_servers.json"
        self.registry = MCPRegistry(config_path=self.config_path)
        self._patcher = patch(
            "aether_engine.mcp.registry.GLOBAL_MCP_REGISTRY", self.registry
        )
        self._patcher.start()

    def teardown_method(self):
        self._patcher.stop()
        self._db_patcher.stop()
        self.tmp_dir.cleanup()

    def _connect(self, client):
        return client.websocket_connect("/ws/tasks?token=" + engine_state.auth_token)

    def _send(self, ws, type_, payload):
        ws.send_text(Event(type=type_, payload=payload).to_json())
        return Event.from_json(ws.receive_text())

    def test_s3_prompt_run_stores_tagged_messages_in_chat(self, fake_url):
        from aether_engine.memory import session_store

        with TestClient(app) as client:
            with self._connect(client) as ws:
                ws.receive_text()  # HELLO
                self.registry.add_server(
                    "fake",
                    {"transport": "streamable_http", "url": fake_url, "timeout": 10, "auto_connect": False},
                )
                resp = self._send(ws, EventType.MCP_SERVER_CONNECT_REQUEST, {"name": "fake"})
                assert resp.payload["success"] is True, resp.payload
                session_id = session_store.create_session("chat")
                resp = self._send(
                    ws, EventType.MCP_PROMPT_RUN_REQUEST,
                    {"name": "fake", "prompt": "duet", "session_id": session_id},
                )
                assert resp.payload["success"] is True, resp.payload
                assert any(
                    m["role"] == "assistant" and m["content"].startswith(PROMPT_TAG)
                    for m in resp.payload["messages"]
                ), resp.payload["messages"]
                sess = session_store.get_session(session_id)
                assert any(
                    m["role"] == "assistant" and m["content"].startswith(PROMPT_TAG)
                    for m in sess["messages"]
                ), sess["messages"]
                resp = self._send(ws, EventType.MCP_SERVER_DISCONNECT_REQUEST, {"name": "fake"})
                assert resp.payload["success"] is True

    def test_s5_remove_with_corrupt_config_returns_error_not_disconnect(self):
        """REMOVE must answer success:false + error, and the websocket
        must stay alive for the next request, when remove_server raises
        via _save_config.

        The raise happens when a server is in memory *and* a config
        load error is recorded (the B1 state: _save_config refuses to
        overwrite a corrupt file). A corrupt file on its own leaves
        the registry empty, so removing a name is a no-op there — the
        state that actually reaches _save_config is a recorded load
        error with servers in memory, which is what is built here.
        """
        self.registry.add_server(
            "fake",
            {"transport": "streamable_http", "url": "http://127.0.0.1:9/mcp", "timeout": 2},
        )
        self.registry._config_load_error = (
            "MCP config file is corrupt and was not loaded"
        )
        with TestClient(app) as client:
            with self._connect(client) as ws:
                ws.receive_text()  # HELLO
                resp = self._send(
                    ws, EventType.MCP_SERVER_REMOVE_REQUEST, {"name": "fake"}
                )
                assert resp.type == EventType.MCP_SERVER_REMOVE_RESPONSE, resp
                assert resp.payload["success"] is False, resp.payload
                assert "corrupt" in resp.payload["error"].lower(), resp.payload
                # The websocket is still alive: the next request answers.
                resp = self._send(ws, EventType.MCP_SERVER_LIST_REQUEST, {})
                assert resp.type == EventType.MCP_SERVER_LIST_RESPONSE, resp


# -- S4: plugin file_path traversal ----------------------------------------

def _plugin_source(tmp_path, name, mcp_servers, configs):
    source_dir = tmp_path / "source" / name
    source_dir.mkdir(parents=True)
    manifest = {"name": name, "version": "1.0", "mcp_servers": mcp_servers}
    (source_dir / "manifest.json").write_text(json.dumps(manifest))
    for fname, text in configs.items():
        target = source_dir / fname
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    return source_dir


@pytest.fixture
def isolated_installer(tmp_path, monkeypatch):
    import aether_engine.mcp.registry as reg_module
    from aether_engine.plugins.installer import PluginInstaller

    monkeypatch.setattr(
        reg_module, "GLOBAL_MCP_REGISTRY",
        MCPRegistry(config_path=tmp_path / "mcp_servers.json"),
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return PluginInstaller(tmp_path / "plugins")


def test_s4_dir_branch_rejects_dotdot_file_path(tmp_path, isolated_installer):
    """A manifest pointing at ../secret.json must be rejected — the
    file exists outside the package and is a valid config, so only
    the traversal guard can stop it."""
    (tmp_path / "secret.json").write_text(json.dumps({"command": "echo"}))
    source = _plugin_source(
        tmp_path, "BadPlugin",
        [{"name": "bad-srv", "file_path": "../../secret.json"}],
        {},
    )
    with pytest.raises(ValueError, match="inside the plugin|outside|traversal|\\.\\."):
        isolated_installer.prepare_install(str(source))


def test_s4_dir_branch_rejects_absolute_file_path(tmp_path, isolated_installer):
    secret = tmp_path / "secret.json"
    secret.write_text(json.dumps({"command": "echo"}))
    source = _plugin_source(
        tmp_path, "BadPlugin",
        [{"name": "bad-srv", "file_path": str(secret)}],
        {},
    )
    with pytest.raises(ValueError, match="inside the plugin|outside|traversal|absolute"):
        isolated_installer.prepare_install(str(source))


def test_s4_zip_branch_rejects_dotdot_file_path(tmp_path, isolated_installer):
    """Same traversal via a zip package: rejected before any zip read."""
    source_dir = tmp_path / "source" / "ZipBad"
    source_dir.mkdir(parents=True)
    manifest = {
        "name": "ZipBad", "version": "1.0",
        "mcp_servers": [{"name": "bad-srv", "file_path": "../secret.json"}],
    }
    zip_path = tmp_path / "ZipBad.zip"
    with zipfile.ZipFile(zip_path, "w") as z:
        z.writestr("manifest.json", json.dumps(manifest))
        z.writestr("../secret.json", json.dumps({"command": "echo"}))
    with pytest.raises(ValueError, match="inside the plugin|outside|traversal|\\.\\."):
        isolated_installer.prepare_install(str(zip_path))


def test_s4_nested_file_path_inside_package_still_works(tmp_path, isolated_installer):
    source = _plugin_source(
        tmp_path, "GoodPlugin",
        [{"name": "good-srv", "file_path": "sub/mcp.json"}],
        {"sub/mcp.json": json.dumps({"command": "echo", "args": ["hello"]})},
    )
    # _plugin_source writes "sub/mcp.json" as a flat filename; write it properly.
    data = isolated_installer.prepare_install(str(source))
    assert data["manifest"]["name"] == "GoodPlugin"


# -- S6: sync remove refuses while connected --------------------------------

async def test_s6_remove_server_sync_refuses_while_connected(manager, tmp_path, monkeypatch):
    """With a loop running and a live session, the sync remove must
    refuse — the config and the session must both survive — and after
    a proper disconnect it must remove cleanly."""
    import aether_engine.mcp.registry as reg_module

    monkeypatch.setattr(reg_module, "_MCP_CONFIG_PATH", tmp_path / "mcp_servers.json")
    registry = MCPRegistry()
    registry.add_server("fake", _stdio_config())
    assert (await registry.connect_server("fake"))["ok"] is True
    assert registry._client.is_connected("fake")
    with pytest.raises(MCPConfigError, match="connected|disconnect"):
        registry.remove_server_sync("fake")
    # Refusal leaves everything intact: config present, session live.
    assert "fake" in registry.list_servers()
    assert registry._client.is_connected("fake")
    await registry.disconnect_server("fake")
    registry.remove_server_sync("fake")
    assert "fake" not in registry.list_servers()
