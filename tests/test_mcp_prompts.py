"""Issue 007: run an MCP prompt template from the MCP tab.

TDD red first: these tests fail until list_prompts / get_prompt
exist in the client, registry, and websocket handlers.

The fake HTTP server (tests/fake_mcp_http.py) exposes three prompts:
``greet`` (one required arg ``name``), ``plan`` (list form, one arg
``topic``), and ``intro`` (no arguments, for the slash-command form).
Running a prompt inserts its messages into the current chat — the
same pattern as issue 006 resources, but with the prompt's own roles
("user" / "assistant"), never auto-run on connect.
"""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

from aether_common.contracts import Event, EventType
from aether_engine.app import app, engine_state
from aether_engine.mcp.registry import MCPRegistry
from tests.fake_mcp_http import start_fake_http_server


@pytest.fixture(scope="module")
def fake_url():
    url, stop = start_fake_http_server()
    yield url
    stop()


@pytest.fixture()
def manager():
    from aether_engine.mcp.client import MCPClientManager

    return MCPClientManager()


async def test_list_and_get_prompt(fake_url, manager):
    config = {"transport": "streamable_http", "url": fake_url, "timeout": 10}
    result = await manager.connect("fake", config)
    assert result["ok"] is True, result

    prompts = await manager.list_prompts("fake")
    by_name = {p["name"]: p for p in prompts}
    assert "greet" in by_name
    assert "intro" in by_name
    assert "plan" in by_name
    greet = by_name["greet"]
    assert greet["description"]
    assert any(a["name"] == "name" and a["required"] for a in greet["arguments"])

    messages = await manager.get_prompt("fake", "greet", {"name": "Arindam"})
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert "Arindam" in messages[0]["content"]

    plan = await manager.get_prompt("fake", "plan", {"topic": "editing"})
    assert plan[0]["role"] == "user"
    assert "editing" in plan[0]["content"]

    await manager.disconnect("fake")


async def test_get_prompt_validates_arguments(fake_url, manager):
    from aether_engine.mcp.client import MCPClientError

    config = {"transport": "streamable_http", "url": fake_url, "timeout": 10}
    await manager.connect("fake", config)
    with pytest.raises(MCPClientError):
        await manager.get_prompt("fake", "greet", ["not", "a", "dict"])
    with pytest.raises(MCPClientError):
        await manager.get_prompt("fake", "greet", {"name": 42})
    with pytest.raises(MCPClientError):
        await manager.get_prompt("fake", "greet", {"name": None})
    await manager.disconnect("fake")


async def test_prompts_on_disconnected_server_fail_gracefully(manager):
    from aether_engine.mcp.client import MCPClientError

    with pytest.raises(MCPClientError):
        await manager.list_prompts("ghost")
    with pytest.raises(MCPClientError):
        await manager.get_prompt("ghost", "greet", {"name": "Arindam"})


async def test_registry_lists_and_gets_prompt(fake_url):
    with tempfile.TemporaryDirectory() as tmp:
        registry = MCPRegistry(config_path=Path(tmp) / "mcp_servers.json")
        registry.add_server(
            "fake",
            {"transport": "streamable_http", "url": fake_url, "timeout": 10},
        )
        result = await registry.connect_server("fake")
        assert result["ok"] is True, result
        prompts = await registry.list_prompts("fake")
        assert "greet" in [p["name"] for p in prompts]
        messages = await registry.get_prompt("fake", "greet", {"name": "Arindam"})
        assert "Arindam" in messages[0]["content"]
        await registry.disconnect_server("fake")


class TestMCPPromptWebSocketHandlers:
    """Handler level: real /ws/tasks endpoint, real registry on a temp
    config file, session_store pointed at a temp DB (same pattern as
    test_mcp_resources.py)."""

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
        return client.websocket_connect(f"/ws/tasks?token={engine_state.auth_token}")

    def _send(self, ws, type_, payload):
        ws.send_text(Event(type=type_, payload=payload).to_json())
        return Event.from_json(ws.receive_text())

    def _add_and_connect_fake(self, ws, fake_url):
        self.registry.add_server(
            "fake",
            {"transport": "streamable_http", "url": fake_url, "timeout": 10, "auto_connect": False},
        )
        resp = self._send(ws, EventType.MCP_SERVER_CONNECT_REQUEST, {"name": "fake"})
        assert resp.payload["success"] is True, resp.payload

    def _disconnect_fake(self, ws):
        resp = self._send(ws, EventType.MCP_SERVER_DISCONNECT_REQUEST, {"name": "fake"})
        assert resp.payload["success"] is True, resp.payload

    def test_list_prompts_over_websocket(self, fake_url):
        with TestClient(app) as client:
            with self._connect(client) as ws:
                ws.receive_text()  # HELLO
                self._add_and_connect_fake(ws, fake_url)
                resp = self._send(
                    ws, EventType.MCP_PROMPT_LIST_REQUEST, {"name": "fake"}
                )
                assert resp.type == EventType.MCP_PROMPT_LIST_RESPONSE
                assert resp.payload["success"] is True
                names = [p["name"] for p in resp.payload["prompts"]]
                assert "greet" in names
                assert "intro" in names
                self._disconnect_fake(ws)

    def test_run_prompt_appends_messages_to_current_chat(self, fake_url):
        from aether_engine.memory import session_store

        with TestClient(app) as client:
            with self._connect(client) as ws:
                ws.receive_text()  # HELLO
                self._add_and_connect_fake(ws, fake_url)
                session_id = session_store.create_session("chat")
                resp = self._send(
                    ws,
                    EventType.MCP_PROMPT_RUN_REQUEST,
                    {
                        "name": "fake",
                        "prompt": "greet",
                        "arguments": {"name": "Arindam"},
                        "session_id": session_id,
                    },
                )
                assert resp.type == EventType.MCP_PROMPT_RUN_RESPONSE
                assert resp.payload["success"] is True, resp.payload
                assert resp.payload["session_id"] == session_id
                assert len(resp.payload["messages"]) == 1
                assert "Arindam" in resp.payload["messages"][0]["content"]
                # The prompt's messages are in the chat history, with
                # their own roles — ready for the user to send.
                sess = session_store.get_session(session_id)
                contents = [m["content"] for m in sess["messages"]]
                assert any("Arindam" in c for c in contents)
                roles = [m["role"] for m in sess["messages"]]
                assert "user" in roles
                self._disconnect_fake(ws)

    def test_run_prompt_without_session_creates_one(self, fake_url):
        from aether_engine.memory import session_store

        with TestClient(app) as client:
            with self._connect(client) as ws:
                ws.receive_text()  # HELLO
                self._add_and_connect_fake(ws, fake_url)
                resp = self._send(
                    ws,
                    EventType.MCP_PROMPT_RUN_REQUEST,
                    {"name": "fake", "prompt": "intro"},
                )
                assert resp.payload["success"] is True, resp.payload
                assert resp.payload["session_id"]
                sess = session_store.get_session(resp.payload["session_id"])
                assert sess is not None
                assert any("Introduce yourself" in m["content"] for m in sess["messages"])
                self._disconnect_fake(ws)

    def test_run_prompt_on_disconnected_server_fails(self, fake_url):
        self.registry.add_server(
            "fake",
            {"transport": "streamable_http", "url": fake_url, "timeout": 10, "auto_connect": False},
        )
        with TestClient(app) as client:
            with self._connect(client) as ws:
                ws.receive_text()  # HELLO
                resp = self._send(
                    ws,
                    EventType.MCP_PROMPT_RUN_REQUEST,
                    {"name": "fake", "prompt": "greet", "arguments": {"name": "Arindam"}},
                )
                assert resp.type == EventType.MCP_PROMPT_RUN_RESPONSE
                assert resp.payload["success"] is False
                assert resp.payload["error"]
                assert "not connected" in resp.payload["error"]

    def test_run_prompt_rejects_non_dict_arguments(self, fake_url):
        with TestClient(app) as client:
            with self._connect(client) as ws:
                ws.receive_text()  # HELLO
                self._add_and_connect_fake(ws, fake_url)
                resp = self._send(
                    ws,
                    EventType.MCP_PROMPT_RUN_REQUEST,
                    {"name": "fake", "prompt": "greet", "arguments": ["Arindam"]},
                )
                assert resp.payload["success"] is False
                assert "arguments" in resp.payload["error"]
                self._disconnect_fake(ws)

    def test_slash_command_runs_prompt_instead_of_a_task(self, fake_url):
        """``/mcp:fake:intro`` inserts the prompt's messages into the
        chat and starts no graph task: the reply is a prompt-run
        response, not a task event."""
        from aether_engine.memory import session_store

        with TestClient(app) as client:
            with self._connect(client) as ws:
                ws.receive_text()  # HELLO
                self._add_and_connect_fake(ws, fake_url)
                session_id = session_store.create_session("chat")
                resp = self._send(
                    ws,
                    EventType.START_TASK,
                    {"prompt": "/mcp:fake:intro", "session_id": session_id},
                )
                assert resp.type == EventType.MCP_PROMPT_RUN_RESPONSE, resp
                assert resp.payload["success"] is True, resp.payload
                assert resp.payload["session_id"] == session_id
                assert any(
                    "Introduce yourself" in m["content"]
                    for m in resp.payload["messages"]
                )
                sess = session_store.get_session(session_id)
                assert any(
                    "Introduce yourself" in m["content"] for m in sess["messages"]
                )
                self._disconnect_fake(ws)
