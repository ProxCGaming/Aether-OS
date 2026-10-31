"""Epic 10 — hardening guards (grows with the epic).

E10-02: the audit logger rotates at its size cap, keeps the
configured number of gzipped archives, drops the oldest, and marks
the rotation in the fresh log — and a rotation failure never loses
the audit write itself.
"""

from __future__ import annotations

import gzip
import json

from aether_engine.audit import AuditLogger


def _events(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_audit_log_rotates_and_keeps_archives(tmp_path):
    log = tmp_path / "audit.log"
    logger = AuditLogger(log, max_bytes=400, backup_count=2)
    for i in range(40):
        logger.log_event("ENTRY", {"i": i, "pad": "x" * 40})
    # Current log exists and is under (or near) the cap; archives exist.
    assert log.exists()
    assert (tmp_path / "audit.log.1.gz").exists()
    # Rotations are recorded in the fresh log.
    current = _events(log)
    actions = [e.get("action") for e in current]
    assert "AUDIT_LOG_ROTATED" in actions
    assert "ENTRY" in actions
    # Archive content is real audit JSON.
    with gzip.open(tmp_path / "audit.log.1.gz", "rt") as f:
        archived = [json.loads(line) for line in f if line.strip()]
    assert archived and all(e.get("type") == "ENGINE_EVENT" for e in archived)
    # No write was lost to rotation: the final entry (i=39) is in the
    # current log or the newest archive.
    seen = {e.get("details", {}).get("i") for e in current + archived}
    assert 39 in seen


def test_audit_log_drops_oldest_archive(tmp_path):
    log = tmp_path / "audit.log"
    logger = AuditLogger(log, max_bytes=300, backup_count=2)
    for i in range(120):
        logger.log_event("ENTRY", {"i": i, "pad": "y" * 40})
    assert (tmp_path / "audit.log.1.gz").exists()
    assert (tmp_path / "audit.log.2.gz").exists()
    assert not (tmp_path / "audit.log.3.gz").exists()


def test_audit_log_under_cap_never_rotates(tmp_path):
    log = tmp_path / "audit.log"
    logger = AuditLogger(log, max_bytes=10 * 1024 * 1024, backup_count=2)
    logger.log_event("ONLY", {})
    assert not (tmp_path / "audit.log.1.gz").exists()
    assert [e.get("action") for e in _events(log)] == ["ONLY"]


# ---------------------------------------------------------------------------
# E10-03 — backup / restore
# ---------------------------------------------------------------------------

import sqlite3 as _sqlite3

import pytest

from aether_engine.backup import create_backup, restore_backup


def _seed_home(home):
    home.mkdir(parents=True)
    (home / "config.json").write_text('{"default_model": "gemini-3.5-flash"}')
    (home / "mcp_servers.json").write_text('{"servers": []}')
    (home / "secrets.db").write_bytes(b"ciphertext")
    conn = _sqlite3.connect(home / "memory.db")
    conn.execute("CREATE TABLE t (v TEXT)")
    conn.execute("INSERT INTO t VALUES ('hello')")
    conn.commit()
    conn.close()
    (home / "skills" / "demo").mkdir(parents=True)
    (home / "skills" / "demo" / "SKILL.md").write_text("# demo")


def test_backup_restore_round_trip_on_clean_profile(tmp_path):
    src = tmp_path / "src_home"
    _seed_home(src)
    zpath = tmp_path / "backup.zip"
    manifest = create_backup(zpath, home=src)
    paths = {f["path"] for f in manifest["files"]}
    assert "config.json" in paths and "memory.db" in paths
    assert "skills/demo/SKILL.md" in paths
    # Secrets are excluded by default.
    assert "secrets.db" not in paths

    dst = tmp_path / "dst_home"
    result = restore_backup(zpath, home=dst)
    assert result["files"] == len(manifest["files"])
    assert (dst / "config.json").read_text() == '{"default_model": "gemini-3.5-flash"}'
    conn = _sqlite3.connect(dst / "memory.db")
    assert conn.execute("SELECT v FROM t").fetchone() == ("hello",)
    conn.close()
    assert (dst / "skills" / "demo" / "SKILL.md").read_text() == "# demo"
    assert not (dst / "secrets.db").exists()


def test_backup_can_include_secrets_store(tmp_path):
    src = tmp_path / "src_home"
    _seed_home(src)
    manifest = create_backup(tmp_path / "b.zip", include_secrets=True, home=src)
    assert "secrets.db" in {f["path"] for f in manifest["files"]}


def test_restore_refuses_tampered_backup(tmp_path):
    import zipfile as _zf

    src = tmp_path / "src_home"
    _seed_home(src)
    zpath = tmp_path / "backup.zip"
    create_backup(zpath, home=src)
    # Rewrite the zip with one member's bytes changed.
    bad = tmp_path / "bad.zip"
    with _zf.ZipFile(zpath) as zin, _zf.ZipFile(bad, "w") as zout:
        for item in zin.namelist():
            data = zin.read(item)
            if item == "config.json":
                data = b'{"default_model": "tampered"}'
            zout.writestr(item, data)
    with pytest.raises(ValueError, match="corrupt"):
        restore_backup(bad, home=tmp_path / "dst")
    # Nothing was written by the refused restore.
    assert not (tmp_path / "dst" / "config.json").exists()


# ---------------------------------------------------------------------------
# E10-03 — WS plumbing for backup contracts (logic unit-tested above)
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


def test_ws_backup_create_and_restore(tmp_path, monkeypatch):
    _es.token_path = tmp_path / "engine.token"
    _es.audit_logger.log_path = tmp_path / "audit.log"
    _es.initialize_auth()
    monkeypatch.setattr(_ss, "DB_PATH", tmp_path / "memory.db")
    monkeypatch.setattr(_ss, "_initialized", False)
    _es.user_config.workspace_roots = [str(tmp_path)]

    seen = {}

    def fake_create(dest, include_secrets=False):
        seen["dest"] = str(dest)
        seen["include_secrets"] = include_secrets
        return {"files": [{"path": "config.json"}], "include_secrets": include_secrets}

    def fake_restore(path):
        seen["restore_path"] = str(path)
        return {"restored": ["config.json"], "files": 1}

    with (
        _patch.object(_backup_mod, "create_backup", fake_create),
        _patch.object(_backup_mod, "restore_backup", fake_restore),
        _patch.object(_wsm, "SecretStore") as mock_store,
        _patch.object(_wsm, "get_pending_approvals", AsyncMock(return_value=[])),
        _TestClient(_app) as client,
    ):
        mock_store.return_value.list_providers.return_value = []
        with client.websocket_connect(f"/ws/tasks?token={_es.auth_token}") as ws:
            ws.receive_text()
            ws.send_text(_Event(type=_ET.BACKUP_CREATE_REQUEST, payload={"include_secrets": True}).to_json())
            resp = _Event.from_json(ws.receive_text())
            assert resp.type == _ET.BACKUP_CREATE_RESPONSE
            assert resp.payload["success"] is True
            assert resp.payload["files"] == 1
            assert seen["include_secrets"] is True
            assert "AetherBackups" in seen["dest"]

            ws.send_text(_Event(type=_ET.BACKUP_RESTORE_REQUEST, payload={"path": "/tmp/x.zip"}).to_json())
            resp = _Event.from_json(ws.receive_text())
            assert resp.type == _ET.BACKUP_RESTORE_RESPONSE
            assert resp.payload["success"] is True
            assert resp.payload["restart_required"] is True


# ---------------------------------------------------------------------------
# E10-04 — diagnostics bundle redaction + version lockstep (E10-06)
# ---------------------------------------------------------------------------

import json as _json
from pathlib import Path as _Path

from aether_engine.config import UserConfig as _UC
from aether_engine.diagnostics import build_diagnostics
from aether_engine.version import APP_VERSION


def test_diagnostics_bundle_is_redacted_and_counts_only(tmp_path):
    audit = tmp_path / "audit.log"
    audit.write_text('{"type": "ENGINE_EVENT", "action": "X"}\n' * 3)
    cfg = _UC()
    cfg.ttsr_rules = []  # untouched defaults otherwise
    bundle = build_diagnostics(
        user_config=cfg,
        health={"gemini": {"status": "ok"}},
        kg_count=7,
        episodic_count=42,
        audit_log_path=audit,
        providers=["gemini"],
    )
    assert bundle["app_version"] == APP_VERSION
    assert bundle["memory"] == {"kg_facts": 7, "episodes": 42}
    assert bundle["providers_configured"] == ["gemini"]
    assert len(bundle["audit_tail"]) == 3
    # The serialised bundle must never carry secret-shaped values.
    text = _json.dumps(bundle)
    assert "sk-" not in text


def test_app_version_matches_desktop_package_json():
    pkg = _json.loads((_Path(__file__).parent.parent / "desktop_app" / "package.json").read_text())
    assert pkg["version"] == APP_VERSION
