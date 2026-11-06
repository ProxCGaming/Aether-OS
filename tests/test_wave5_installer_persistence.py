"""Wave 5, Annex C row 1 remainder — installer persistence discipline.

- E6-NEW-14: ``_save_registry`` must flush + fsync its temp file
  before ``os.replace`` (the discipline every sibling store follows),
  and ``get_all()`` must hand out a snapshot, not the live registry
  dict a caller could mutate behind the save discipline's back.
- E6-NEW-13: ``_safe_extract`` re-hashes each member against the
  manifest's files map as it streams out, so bytes swapped between
  validation and extraction are refused instead of landing on disk.
- E6-NEW-02 (uninstall half): ``uninstall()`` runs the same
  connected-server pre-check ``toggle()`` runs (E7-F01) — with a live
  session and a running event loop it refuses BEFORE removing
  anything, instead of half-uninstalling when remove_server_sync's
  refusal surfaces mid-loop.
"""

from __future__ import annotations

import hashlib
import os
import zipfile
from pathlib import Path

import pytest

from aether_engine.mcp.registry import MCPConfigError
from aether_engine.plugins.installer import PluginInstaller

PLUGIN = "Wave5Plugin"
SERVER = "wave5-server"


@pytest.fixture()
def installer(tmp_path) -> PluginInstaller:
    return PluginInstaller(tmp_path / "plugins")


# ---------------------------------------------------------------------------
# E6-NEW-14 — fsync + snapshot semantics
# ---------------------------------------------------------------------------

def test_save_registry_fsyncs_then_round_trips(installer, monkeypatch):
    real_fsync = os.fsync
    fsynced: list[int] = []

    def recording_fsync(fd):
        fsynced.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    installer._registry = {"demo": {"enabled": True}}
    installer._save_registry()

    assert fsynced, "_save_registry replaced the registry without fsync"
    fresh = PluginInstaller(installer.plugins_dir)
    assert fresh.get_all() == {"demo": {"enabled": True}}
    assert not (installer.plugins_dir / "registry.json.tmp").exists()


def test_get_all_returns_an_isolated_snapshot(installer):
    installer._registry = {"demo": {"enabled": True, "nested": {"n": 1}}}
    snapshot = installer.get_all()
    snapshot["demo"]["nested"]["n"] = 999
    snapshot["intruder"] = {}
    assert installer.get_all() == {"demo": {"enabled": True, "nested": {"n": 1}}}


# ---------------------------------------------------------------------------
# E6-NEW-13 — extraction re-hashes against the manifest map
# ---------------------------------------------------------------------------

def _make_zip(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)


def test_safe_extract_verifies_member_hashes(tmp_path):
    body = b"skill body"
    src = tmp_path / "pkg.zip"
    _make_zip(src, {"manifest.json": b"{}", "skill.md": body})

    target = tmp_path / "out"
    PluginInstaller._safe_extract(
        src, target, {"skill.md": hashlib.sha256(body).hexdigest()}
    )
    assert (target / "skill.md").read_bytes() == body

    wrong = {"skill.md": hashlib.sha256(b"different bytes").hexdigest()}
    with pytest.raises(ValueError, match="SHA-256"):
        PluginInstaller._safe_extract(src, tmp_path / "out2", wrong)


def test_safe_extract_never_hashes_the_manifest_itself(tmp_path):
    # manifest.json is the trust anchor and is not in its own files
    # map; even a (nonsensical) map entry for it must be skipped,
    # exactly as validation skips it.
    body = b"skill body"
    src = tmp_path / "pkg.zip"
    _make_zip(src, {"manifest.json": b"{}", "skill.md": body})
    hashes = {
        "manifest.json": "0" * 64,
        "skill.md": hashlib.sha256(body).hexdigest(),
    }
    target = tmp_path / "out"
    PluginInstaller._safe_extract(src, target, hashes)
    assert (target / "skill.md").read_bytes() == body


def test_safe_extract_without_hashes_still_extracts(tmp_path):
    src = tmp_path / "pkg.zip"
    _make_zip(src, {"manifest.json": b"{}", "skill.md": b"body"})
    target = tmp_path / "out"
    PluginInstaller._safe_extract(src, target)
    assert (target / "skill.md").read_bytes() == b"body"


# ---------------------------------------------------------------------------
# E6-NEW-02 — uninstall's connected-server pre-check
# ---------------------------------------------------------------------------

class _StubMCPRegistry:
    def __init__(self, status: str):
        self._status = status
        self.removed: list[str] = []

    def get_server(self, name):
        return {"command": "stub"} if name == SERVER else None

    def get_server_status(self, name):
        return {"status": self._status}

    def remove_server_sync(self, name):
        self.removed.append(name)


@pytest.fixture()
def uninstall_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    inst = PluginInstaller(tmp_path / "plugins")
    target = inst.plugins_dir / PLUGIN
    target.mkdir(parents=True)
    (target / "pkg.txt").write_text("payload")
    # Legacy entry (no recorded claims): teardown of its own server
    # is allowed by the E7-F08 ownership rule.
    inst._registry = {
        PLUGIN: {
            "enabled": True,
            "installed_mcp": [SERVER],
            "installed_skills": [],
        }
    }
    inst._save_registry()
    return inst, target


@pytest.mark.asyncio
async def test_uninstall_refuses_over_a_live_session_before_mutating(
    uninstall_env, monkeypatch
):
    inst, target = uninstall_env
    stub = _StubMCPRegistry("connected")
    monkeypatch.setattr(
        "aether_engine.mcp.registry.GLOBAL_MCP_REGISTRY", stub
    )
    with pytest.raises(MCPConfigError, match="still connected"):
        inst.uninstall(PLUGIN)
    # Nothing mutated: no server removed, registry entry and files
    # intact — the failed uninstall is atomic.
    assert stub.removed == []
    assert PLUGIN in inst.get_all()
    assert target.exists()


@pytest.mark.asyncio
async def test_uninstall_proceeds_when_server_disconnected(
    uninstall_env, monkeypatch
):
    inst, target = uninstall_env
    stub = _StubMCPRegistry("disconnected")
    monkeypatch.setattr(
        "aether_engine.mcp.registry.GLOBAL_MCP_REGISTRY", stub
    )
    inst.uninstall(PLUGIN)
    assert stub.removed == [SERVER]
    assert PLUGIN not in inst.get_all()
    assert not target.exists()


def test_uninstall_without_a_running_loop_still_proceeds(
    uninstall_env, monkeypatch
):
    # The websocket handler's shape: disconnection already awaited,
    # uninstall called from a worker thread with no running loop.
    # remove_server_sync only refuses inside a running loop, so the
    # pre-check must not fire here either.
    inst, target = uninstall_env
    stub = _StubMCPRegistry("connected")
    monkeypatch.setattr(
        "aether_engine.mcp.registry.GLOBAL_MCP_REGISTRY", stub
    )
    inst.uninstall(PLUGIN)
    assert stub.removed == [SERVER]
    assert PLUGIN not in inst.get_all()
    assert not target.exists()
