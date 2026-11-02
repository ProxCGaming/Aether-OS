"""E3-F04: a failed MCP config save must not destroy the saved allow-list.

The defect (Astra full-product review, Epic 3): ``MCPRegistry._save_config``
opened the live ``mcp_servers.json`` with ``"w"`` and wrote directly,
after the in-memory mutation had already been applied. A partial-write
failure (disk full) truncated the previous valid file, the in-memory
registry kept the change the disk never took, and the websocket
ADD/UPDATE/REMOVE handlers caught only ``MCPConfigError`` — so a storage
``OSError`` killed the handler with no response to the UI.

These tests pin the fixed behaviour:

* a save is atomic (temp file + ``os.replace``, the pattern E0-01 added
  for the plugin registry): a failed save leaves the previous file
  byte-for-byte intact;
* a failed save rolls the in-memory state back, so memory, disk and a
  fresh reload all agree;
* an ordinary Settings save (the ``enabled`` / ``auto_connect`` toggle)
  preserves every other stored field exactly, and an explicit change
  applies exactly — no more, no less;
* the websocket ADD handler answers a storage failure with
  ``success: false`` + error instead of dropping the session.

TDD: the failure-injection tests fail on the pre-fix code.
"""
import contextlib
import json
import signal
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.testclient import TestClient

import aether_engine.mcp.registry as registry_module
from aether_common.contracts import Event, EventType
from aether_engine.app import app, engine_state
from aether_engine.mcp.registry import MCPRegistry


class _StubClient:
    """Minimal fake MCPClientManager: nothing is ever connected."""

    def get_status(self, name):
        return {"status": "disconnected", "error": None, "tool_count": 0}

    def is_connected(self, name):
        return False

    async def disconnect(self, name):
        return {"ok": True, "error": None}

    async def shutdown_owned(self):
        return None


@pytest.fixture()
def config_path(tmp_path):
    return tmp_path / "mcp_servers.json"


# A stored server carrying every field a save must preserve: env values,
# args, a non-default timeout, both flags, and a field the validator
# passes through untouched (plugin-installed servers carry extras).
EXISTING = {
    "transport": "stdio",
    "command": "npx",
    "args": ["-y", "some-mcp-server"],
    "env": {"API_KEY": "secret-value", "REGION": "eu"},
    "timeout": 45,
    "enabled": True,
    "auto_connect": True,
    "installed_by": "plugin:demo",
}

NEW = {"transport": "streamable_http", "url": "http://127.0.0.1:9/mcp"}


def _seed(config_path, data):
    config_path.write_text(json.dumps(data, indent=4), encoding="utf-8")
    return config_path.read_bytes()


def _partial_write_failure(monkeypatch):
    """json.dump that writes a fragment then dies, like a full disk."""
    real_dump = json.dump

    def dying_dump(data, fp, **kwargs):
        fp.write('{"existing":')
        fp.flush()
        raise OSError("SYNTHETIC_DISK_FULL")

    monkeypatch.setattr(registry_module.json, "dump", dying_dump)
    return real_dump


# -- the failed save: file, memory and reload must all keep the old state --

def test_failed_add_save_preserves_file_memory_and_reload(config_path, monkeypatch):
    original_bytes = _seed(config_path, {"existing": EXISTING})
    registry = MCPRegistry(config_path=config_path, client=_StubClient())
    assert set(registry.list_servers()) == {"existing"}

    _partial_write_failure(monkeypatch)
    with pytest.raises(OSError, match="SYNTHETIC_DISK_FULL"):
        registry.add_server("new", NEW)

    # The previous valid file is byte-for-byte intact.
    assert config_path.read_bytes() == original_bytes
    # The failed add did not linger in memory.
    assert set(registry.list_servers()) == {"existing"}
    assert registry.get_server("existing")["env"] == EXISTING["env"]
    # A fresh load agrees with both.
    reloaded = MCPRegistry(config_path=config_path, client=_StubClient())
    assert set(reloaded.list_servers()) == {"existing"}
    assert reloaded.get_server("existing")["installed_by"] == "plugin:demo"


async def test_failed_update_save_preserves_stored_config(config_path, monkeypatch):
    """The Settings 'unrelated edit': toggling `enabled`, save fails."""
    original_bytes = _seed(config_path, {"existing": EXISTING})
    registry = MCPRegistry(config_path=config_path, client=_StubClient())

    _partial_write_failure(monkeypatch)
    with pytest.raises(OSError, match="SYNTHETIC_DISK_FULL"):
        await registry.update_server("existing", enabled=False)

    assert config_path.read_bytes() == original_bytes
    stored = registry.get_server("existing")
    assert stored["enabled"] is True  # the toggle was rolled back
    assert stored["env"] == EXISTING["env"]
    assert stored["timeout"] == 45
    reloaded = MCPRegistry(config_path=config_path, client=_StubClient())
    assert reloaded.get_server("existing")["enabled"] is True


async def test_failed_remove_save_preserves_server(config_path, monkeypatch):
    original_bytes = _seed(config_path, {"existing": EXISTING})
    registry = MCPRegistry(config_path=config_path, client=_StubClient())

    _partial_write_failure(monkeypatch)
    with pytest.raises(OSError, match="SYNTHETIC_DISK_FULL"):
        await registry.remove_server("existing")

    assert config_path.read_bytes() == original_bytes
    assert set(registry.list_servers()) == {"existing"}
    reloaded = MCPRegistry(config_path=config_path, client=_StubClient())
    assert set(reloaded.list_servers()) == {"existing"}


def test_failed_remove_sync_save_preserves_server(config_path, monkeypatch):
    original_bytes = _seed(config_path, {"existing": EXISTING})
    registry = MCPRegistry(config_path=config_path, client=_StubClient())

    _partial_write_failure(monkeypatch)
    with pytest.raises(OSError, match="SYNTHETIC_DISK_FULL"):
        registry.remove_server_sync("existing")

    assert config_path.read_bytes() == original_bytes
    assert set(registry.list_servers()) == {"existing"}


def test_failed_save_leaves_no_temp_file_behind(config_path, monkeypatch):
    _seed(config_path, {"existing": EXISTING})
    registry = MCPRegistry(config_path=config_path, client=_StubClient())

    _partial_write_failure(monkeypatch)
    with pytest.raises(OSError):
        registry.add_server("new", NEW)

    leftovers = [p.name for p in config_path.parent.iterdir()
                 if p.name != config_path.name]
    assert leftovers == []


# -- happy path: preservation and exact-change guards ---------------------

async def test_unrelated_toggle_preserves_every_other_field(config_path):
    raw = {
        "existing": EXISTING,
        "bad": {"transport": "streamable_http", "url": "http://a/b",
                "command": "npx"},  # invalid entry: preserved verbatim
    }
    _seed(config_path, raw)
    registry = MCPRegistry(config_path=config_path, client=_StubClient())
    before = registry.get_server("existing")

    await registry.update_server("existing", enabled=False)

    on_disk = json.loads(config_path.read_text(encoding="utf-8"))
    expected = dict(before, enabled=False)
    assert on_disk["existing"] == expected
    assert registry.get_server("existing") == expected
    assert on_disk["bad"] == raw["bad"]


async def test_explicit_flag_change_applies_exactly(config_path):
    _seed(config_path, {"existing": EXISTING})
    registry = MCPRegistry(config_path=config_path, client=_StubClient())
    before = registry.get_server("existing")

    await registry.update_server("existing", auto_connect=False)

    after = registry.get_server("existing")
    assert after == dict(before, auto_connect=False)
    on_disk = json.loads(config_path.read_text(encoding="utf-8"))
    assert on_disk["existing"] == after


def test_add_writes_exactly_the_validated_config(config_path):
    _seed(config_path, {"existing": EXISTING})
    registry = MCPRegistry(config_path=config_path, client=_StubClient())

    registry.add_server("new", NEW)

    on_disk = json.loads(config_path.read_text(encoding="utf-8"))
    assert on_disk["existing"] == registry.get_server("existing")
    assert on_disk["new"] == registry.get_server("new")
    assert on_disk["new"]["url"] == NEW["url"]
    assert on_disk["new"]["transport"] == "streamable_http"


# -- websocket surface: a storage failure gets an answer, not a dead socket --

class _NoResponse(Exception):
    """Raised when the socket sends nothing within the bound."""


@contextlib.contextmanager
def _bounded(seconds=20):
    """Bound a blocking TestClient receive (SIGALRM, main thread).

    Pre-fix, a storage OSError escapes the ADD handler and the socket
    never answers — an unbounded receive would hang the suite instead
    of failing. Post-fix the response arrives in milliseconds.
    """
    def _handler(signum, frame):
        raise _NoResponse(f"no websocket response within {seconds}s")

    previous = signal.signal(signal.SIGALRM, _handler)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)




class TestMCPConfigSaveWebSocket:
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
        self.config_path = self.tmp_path / "mcp_servers.json"
        _seed(self.config_path, {"existing": EXISTING})
        self.registry = MCPRegistry(
            config_path=self.config_path, client=_StubClient())
        self._patcher = patch(
            "aether_engine.mcp.registry.GLOBAL_MCP_REGISTRY", self.registry
        )
        self._patcher.start()

    def teardown_method(self):
        self._patcher.stop()
        self._db_patcher.stop()
        self.tmp_dir.cleanup()

    def _connect(self, client):
        # The app lifespan re-runs engine_state.initialize() when the
        # TestClient starts, regenerating the token - read the current one.
        return client.websocket_connect(
            "/ws/tasks",
            headers={"Authorization": f"Bearer {engine_state.auth_token}"},
        )

    def test_add_storage_failure_returns_error_response(self, monkeypatch):
        _partial_write_failure(monkeypatch)
        with TestClient(app) as client, self._connect(client) as ws:
            with _bounded():
                ws.receive_text()  # HELLO
                ws.send_text(Event(
                    type=EventType.MCP_SERVER_ADD_REQUEST,
                    payload={"name": "new", "config": NEW},
                ).to_json())
                resp = Event.from_json(ws.receive_text())
            assert resp.type == EventType.MCP_SERVER_ADD_RESPONSE
            assert resp.payload["success"] is False
            assert "SYNTHETIC_DISK_FULL" in resp.payload["error"]
            # The session is still alive and the old allow-list intact.
            ws.send_text(Event(
                type=EventType.MCP_SERVER_LIST_REQUEST, payload={},
            ).to_json())
            with _bounded():
                listing = Event.from_json(ws.receive_text())
            assert listing.type == EventType.MCP_SERVER_LIST_RESPONSE
            assert set(listing.payload["servers"]) == {"existing"}
