"""E10-F01 + E10-F02 — backup secrets consent.

F01: a backup made WITHOUT secrets consent must contain no MCP
credential material. The MCP registry classifies header/env *values*
as secrets (see ``redact_mcp_config_for_ui``); a no-secrets backup
therefore carries a sanitised ``mcp_servers.json`` — server names,
commands, URLs and header/env *names* survive, the values are
redacted — instead of the raw file. With explicit consent the file
is copied raw, exactly as before. A config that cannot be parsed
cannot be verified secret-free, so it is omitted from a no-secrets
backup rather than copied raw.

F02: at the untyped WS payload boundary, consent is consent only
when it is the boolean ``True``. ``bool("false")`` is ``True`` in
Python, so the old truthiness coercion turned the string ``"false"``
into consent to include ``secrets.db``.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from aether_engine.backup import create_backup, restore_backup
from aether_engine.mcp.registry import MCPRegistry

HTTP_CREDENTIAL = "Bearer SYNTHETIC_MCP_CREDENTIAL_NOT_REAL"
ENV_CREDENTIAL = "SYNTHETIC_ENV_SECRET_NOT_REAL"


def _home_with_mcp_credentials(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir(parents=True)
    (home / "config.json").write_text('{"default_model": "gemini-3.5-flash"}')
    (home / "secrets.db").write_bytes(b"ciphertext")
    registry = MCPRegistry(config_path=home / "mcp_servers.json")
    registry.add_server("synthetic-http", {
        "url": "https://example.invalid/mcp",
        "headers": {"Authorization": HTTP_CREDENTIAL},
        "auto_connect": False,
    })
    registry.add_server("synthetic-stdio", {
        "command": "python",
        "args": ["-m", "synthetic_server"],
        "env": {"SYNTHETIC_API_KEY": ENV_CREDENTIAL},
        "auto_connect": False,
    })
    return home


def _members(zpath: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(zpath) as zf:
        return {name: zf.read(name) for name in zf.namelist()}


def _whole_archive(zpath: Path) -> bytes:
    return b"\n".join(_members(zpath).values())


# ---------------------------------------------------------------------------
# F01 — the collector itself
# ---------------------------------------------------------------------------


def test_default_backup_contains_no_mcp_credentials(tmp_path):
    home = _home_with_mcp_credentials(tmp_path)
    zpath = tmp_path / "backup.zip"
    manifest = create_backup(zpath, home=home)

    assert manifest["include_secrets"] is False
    members = _members(zpath)
    assert "secrets.db" not in members
    # The credential material appears NOWHERE in the archive.
    blob = _whole_archive(zpath)
    assert HTTP_CREDENTIAL.encode() not in blob
    assert ENV_CREDENTIAL.encode() not in blob


def test_default_backup_mcp_config_is_sanitised_not_dropped(tmp_path):
    home = _home_with_mcp_credentials(tmp_path)
    zpath = tmp_path / "backup.zip"
    manifest = create_backup(zpath, home=home)

    members = _members(zpath)
    assert "mcp_servers.json" in members
    config = json.loads(members["mcp_servers.json"])
    # Structure survives: both servers, their transports, and the
    # header/env NAMES — only the values are gone.
    assert config["synthetic-http"]["url"] == "https://example.invalid/mcp"
    assert config["synthetic-http"]["headers"] == {"Authorization": "[REDACTED]"}
    assert config["synthetic-stdio"]["command"] == "python"
    assert config["synthetic-stdio"]["env"] == {"SYNTHETIC_API_KEY": "[REDACTED]"}
    # The manifest says what it did.
    entry = next(f for f in manifest["files"] if f["path"] == "mcp_servers.json")
    assert entry.get("sanitized") is True


def test_backup_with_consent_copies_mcp_config_raw(tmp_path):
    home = _home_with_mcp_credentials(tmp_path)
    zpath = tmp_path / "backup.zip"
    manifest = create_backup(zpath, include_secrets=True, home=home)

    assert manifest["include_secrets"] is True
    members = _members(zpath)
    assert "secrets.db" in members
    assert members["mcp_servers.json"] == (home / "mcp_servers.json").read_bytes()
    entry = next(f for f in manifest["files"] if f["path"] == "mcp_servers.json")
    assert not entry.get("sanitized")


def test_sanitised_backup_restores_to_loadable_mcp_config(tmp_path):
    home = _home_with_mcp_credentials(tmp_path)
    zpath = tmp_path / "backup.zip"
    create_backup(zpath, home=home)

    dst = tmp_path / "restored_home"
    restore_backup(zpath, home=dst)
    registry = MCPRegistry(config_path=dst / "mcp_servers.json")
    assert registry.config_load_error is None
    assert set(registry.servers) == {"synthetic-http", "synthetic-stdio"}
    restored = (dst / "mcp_servers.json").read_text()
    assert HTTP_CREDENTIAL not in restored
    assert ENV_CREDENTIAL not in restored


def test_default_backup_without_mcp_file_is_unaffected(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text("{}")
    (home / "secrets.db").write_bytes(b"ciphertext")
    manifest = create_backup(tmp_path / "backup.zip", home=home)
    paths = {f["path"] for f in manifest["files"]}
    assert "config.json" in paths
    assert "mcp_servers.json" not in paths
    assert "secrets.db" not in paths


def test_unparsable_mcp_config_is_never_copied_raw_without_consent(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text("{}")
    garbage = b"not json {{{ SYNTHETIC_UNPARSEABLE"
    (home / "mcp_servers.json").write_bytes(garbage)

    # No consent: a file we cannot inspect is omitted, never copied raw.
    manifest = create_backup(tmp_path / "default.zip", home=home)
    assert "mcp_servers.json" not in {f["path"] for f in manifest["files"]}
    assert garbage not in _whole_archive(tmp_path / "default.zip")

    # With consent: copied raw, exactly as before.
    create_backup(tmp_path / "consent.zip", include_secrets=True, home=home)
    assert _members(tmp_path / "consent.zip")["mcp_servers.json"] == garbage


# ---------------------------------------------------------------------------
# F02 — the WS handler consent boundary
# ---------------------------------------------------------------------------

from unittest.mock import AsyncMock
from unittest.mock import patch as _patch

from fastapi.testclient import TestClient as _TestClient

import aether_engine.backup as _backup_mod
import aether_engine.routes.websocket as _wsm
from aether_common.contracts import Event as _Event
from aether_common.contracts import EventType as _ET
from aether_engine.app import app as _app
from aether_engine.memory import session_store as _ss
from aether_engine.state import engine_state as _es


def _ws_client(tmp_path, monkeypatch):
    _es.token_path = tmp_path / "engine.token"
    _es.audit_logger.log_path = tmp_path / "audit.log"
    _es.initialize_auth()
    monkeypatch.setattr(_ss, "DB_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(_ss, "_initialized", False)
    _es.user_config.workspace_roots = [str(tmp_path)]


def test_ws_backup_create_only_boolean_true_is_consent(tmp_path, monkeypatch):
    _ws_client(tmp_path, monkeypatch)
    seen = []

    def fake_create(dest, include_secrets=False):
        seen.append(include_secrets)
        return {"files": [], "include_secrets": include_secrets}

    with (
        _patch.object(_backup_mod, "create_backup", fake_create),
        _patch.object(_wsm, "SecretStore") as mock_store,
        _patch.object(_wsm, "get_pending_approvals", AsyncMock(return_value=[])),
        _TestClient(_app) as client,
    ):
        mock_store.return_value.list_providers.return_value = []
        with client.websocket_connect(
            "/ws/tasks", headers={"Authorization": f"Bearer {_es.auth_token}"}
        ) as ws:
            ws.receive_text()
            cases = [
                ({"include_secrets": "false"}, False),
                ({"include_secrets": "true"}, False),
                ({"include_secrets": 1}, False),
                ({"include_secrets": 0}, False),
                ({}, False),
                ({"include_secrets": False}, False),
                ({"include_secrets": True}, True),
            ]
            for payload, _expected in cases:
                ws.send_text(_Event(type=_ET.BACKUP_CREATE_REQUEST, payload=payload).to_json())
                resp = _Event.from_json(ws.receive_text())
                assert resp.type == _ET.BACKUP_CREATE_RESPONSE
                assert resp.payload["success"] is True
            assert seen == [expected for _payload, expected in cases]


def test_ws_backup_create_string_false_excludes_secrets_db(tmp_path, monkeypatch):
    _ws_client(tmp_path, monkeypatch)
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text("{}")
    (home / "secrets.db").write_bytes(b"ciphertext")

    with (
        _patch.object(_backup_mod, "data_home", lambda: home),
        _patch.object(_wsm, "SecretStore") as mock_store,
        _patch.object(_wsm, "get_pending_approvals", AsyncMock(return_value=[])),
        _TestClient(_app) as client,
    ):
        mock_store.return_value.list_providers.return_value = []
        with client.websocket_connect(
            "/ws/tasks", headers={"Authorization": f"Bearer {_es.auth_token}"}
        ) as ws:
            ws.receive_text()
            ws.send_text(_Event(
                type=_ET.BACKUP_CREATE_REQUEST,
                payload={"include_secrets": "false"},
            ).to_json())
            resp = _Event.from_json(ws.receive_text())
            assert resp.payload["success"] is True
            assert resp.payload["include_secrets"] is False
            assert "secrets.db" not in _members(Path(resp.payload["path"]))

            ws.send_text(_Event(
                type=_ET.BACKUP_CREATE_REQUEST,
                payload={"include_secrets": True},
            ).to_json())
            resp = _Event.from_json(ws.receive_text())
            assert resp.payload["success"] is True
            assert resp.payload["include_secrets"] is True
            assert "secrets.db" in _members(Path(resp.payload["path"]))
