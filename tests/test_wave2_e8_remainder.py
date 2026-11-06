"""Wave 2 (Builder J) — E8 remainder + E1-14.

Failing-first tests for:
- E8-07: ``resync_active_state`` only ever ADDS to the model
  registry — removed providers keep their discovered models for the
  whole session (state.py).
- E8-08: restore stages the ENTIRE archive in memory before writing
  anything (backup.py).
- E8-09: restore's malformed-manifest errors escape as
  KeyError/TypeError, not the documented ValueError (backup.py).
- E8-10: dead ``LOCAL_MODELS`` catalogue in config.py.
- E8-11: ``EngineState.initialize_auth`` test-only alias in the
  production package.
- E8-14: ``install_crash_logging`` is not idempotent and drops the
  previous threading hook (diagnostics.py).
- E8-15: ``detect_checks`` walks the whole target tree on every
  call (aether_common/checks.py).
- E1-14: the capability-check timeout path in routes/websocket.py
  depends on the scheduler's private ``_record_run``.
"""

from __future__ import annotations

import hashlib
import json
import sys
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

# ---------------------------------------------------------------------------
# E8-07 — resync must deregister providers removed from the config
# ---------------------------------------------------------------------------


def _isolated_state(monkeypatch):
    from aether_engine import state as state_module
    from aether_engine.config import UserConfig
    from aether_engine.routing.registry import ModelRegistry
    from aether_engine.skills import manager as skill_manager

    st = state_module.EngineState()
    st.model_registry = ModelRegistry(models=[])
    st.user_config = UserConfig()

    class _ValRes:
        def __init__(self):
            self.active_provider = "google_gemini"
            self.active_model = "some-model"
            self.configured_providers: list[str] = []
            self.was_fallback = False
            self.fallback_reason = ""

    monkeypatch.setattr(
        state_module, "validate_startup_configuration", lambda **kw: _ValRes())
    monkeypatch.setattr(state_module, "SecretStore", lambda: object())
    monkeypatch.setattr(
        skill_manager.GLOBAL_SKILL_MANAGER,
        "update_workspace_roots", lambda roots: None)
    return st


def test_resync_deregisters_provider_removed_from_config(monkeypatch):
    st = _isolated_state(monkeypatch)
    st.user_config.provider_models = {
        "prov_a": ["model-a1"], "prov_b": ["model-b1"]}
    st.resync_active_state()
    assert st.model_registry.get_models_for_provider("prov_a")
    assert st.model_registry.get_models_for_provider("prov_b")

    # prov_a disappears from the config (provider removal / reset):
    # resync must drop its discovered models from the registry too.
    st.user_config.provider_models = {"prov_b": ["model-b1"]}
    st.resync_active_state()
    assert st.model_registry.get_models_for_provider("prov_a") == []
    assert st.model_registry.get_models_for_provider("prov_b")


def test_resync_deregisters_provider_whose_models_were_emptied(monkeypatch):
    st = _isolated_state(monkeypatch)
    st.user_config.provider_models = {"prov_a": ["model-a1"]}
    st.resync_active_state()
    assert st.model_registry.get_models_for_provider("prov_a")

    st.user_config.provider_models = {"prov_a": []}
    st.resync_active_state()
    assert st.model_registry.get_models_for_provider("prov_a") == []


def test_resync_deregistration_restores_the_curated_baseline(monkeypatch):
    """The registry is seeded from CURATED_MODELS at boot, so when
    resync deregisters a provider it must return to that baseline —
    only the config-discovered entries are gone (this is what makes
    resync a true mirror of a reboot with the new config)."""
    from aether_engine.routing.registry import CURATED_MODELS, ModelRegistry

    st = _isolated_state(monkeypatch)
    st.model_registry = ModelRegistry()  # curated seed, as at boot
    st.user_config.provider_models = {"google_gemini": ["discovered-only-1"]}
    st.resync_active_state()
    ids = {m.id for m in st.model_registry.get_models_for_provider("google_gemini")}
    assert ids == {"discovered-only-1"}

    st.user_config.provider_models = {}
    st.resync_active_state()
    ids = {m.id for m in st.model_registry.get_models_for_provider("google_gemini")}
    assert ids == {m.id for m in CURATED_MODELS if m.provider == "google_gemini"}
    assert "discovered-only-1" not in ids


# ---------------------------------------------------------------------------
# E8-08 / E8-09 — restore: streamed staging + documented ValueError
# ---------------------------------------------------------------------------


def _seed_home(tmp_path: Path) -> Path:
    home = tmp_path / "src_home"
    home.mkdir(parents=True)
    (home / "config.json").write_text('{"memory_backend": "native"}')
    (home / "learned.md").write_text("lessons")
    logs = home / "logs"
    logs.mkdir()
    (logs / "engine.log").write_bytes(b"log-line\n" * 1000)
    return home


def test_restore_streams_members_instead_of_reading_them_wholesale(
        tmp_path, monkeypatch):
    from aether_engine.backup import create_backup, restore_backup

    src = _seed_home(tmp_path)
    dest_zip = tmp_path / "backup.zip"
    create_backup(dest_zip, home=src)

    # The pre-fix restore called zf.read(rel) for every member and
    # accumulated all bytes in a list. Forbid wholesale member reads:
    # only the manifest may be read that way.
    real_read = zipfile.ZipFile.read

    def guarded_read(self, name, *args, **kwargs):
        if name != "manifest.json":
            raise AssertionError(
                f"restore read member {name!r} wholesale into memory")
        return real_read(self, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "read", guarded_read)

    out_home = tmp_path / "out_home"
    result = restore_backup(dest_zip, home=out_home)
    assert result["files"] >= 3
    assert (out_home / "config.json").read_text() == '{"memory_backend": "native"}'
    assert (out_home / "logs" / "engine.log").read_bytes() == b"log-line\n" * 1000
    # No staging temps survive a successful restore.
    assert not list(out_home.rglob("*.aether-restore-tmp"))


def test_restore_failure_leaves_no_temps_and_no_partial_profile(
        tmp_path, monkeypatch):
    """A corrupt second member must refuse the whole restore: the
    first member may be staged (to a temp) but never committed."""
    from aether_engine.backup import create_backup, restore_backup

    src = _seed_home(tmp_path)
    dest_zip = tmp_path / "backup.zip"
    create_backup(dest_zip, home=src)

    # Corrupt the archive: rewrite learned.md's bytes so its hash
    # no longer matches the manifest.
    corrupted = tmp_path / "corrupted.zip"
    with zipfile.ZipFile(dest_zip) as zin, \
            zipfile.ZipFile(corrupted, "w") as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "learned.md":
                data = b"tampered"
            zout.writestr(item, data)

    out_home = tmp_path / "out_home"
    out_home.mkdir()
    (out_home / "config.json").write_text("ORIGINAL")
    with pytest.raises(ValueError):
        restore_backup(corrupted, home=out_home)
    assert (out_home / "config.json").read_text() == "ORIGINAL"
    assert not (out_home / "learned.md").exists()
    assert not list(out_home.rglob("*.aether-restore-tmp"))


def _zip_with_manifest(tmp_path: Path, manifest_obj, members=None) -> Path:
    zpath = tmp_path / "handmade.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest_obj))
        for name, data in (members or {}).items():
            zf.writestr(name, data)
    return zpath


def _entry(path, data: bytes) -> dict:
    return {"path": path, "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest()}


@pytest.mark.parametrize("manifest", [
    # manifest is a JSON list, not an object → .get raises AttributeError
    [],
    # entry missing "size"/"sha256" → KeyError
    {"format": 1, "files": [{"path": "config.json"}]},
    # entry is a bare string → TypeError on entry["path"]
    {"format": 1, "files": ["config.json"]},
    # "files" is not a list → iterating a dict yields keys (strings)
    {"format": 1, "files": {"path": "config.json"}},
    # entry path is not a string → TypeError in path join
    {"format": 1, "files": [{"path": 5, "size": 2, "sha256": "x"}]},
    # entry size is not an int → comparison still works for str? No:
    # len(data) != "2" is True → ValueError already... use a size whose
    # type breaks the sha comparison path instead: sha256 not a string.
    {"format": 1, "files": [{"path": "config.json", "size": 2, "sha256": 42}]},
])
def test_restore_malformed_manifest_raises_valueerror(tmp_path, manifest):
    from aether_engine.backup import FORMAT_VERSION, restore_backup

    if isinstance(manifest, dict) and manifest.get("format") == 1:
        manifest["format"] = FORMAT_VERSION
    zpath = _zip_with_manifest(tmp_path, manifest, {"config.json": b"{}"})
    with pytest.raises(ValueError):
        restore_backup(zpath, home=tmp_path / "out_home")


def test_restore_wellformed_manifest_still_works(tmp_path):
    from aether_engine.backup import FORMAT_VERSION, restore_backup

    data = b'{"a": 1}'
    manifest = {"format": FORMAT_VERSION, "files": [_entry("config.json", data)]}
    zpath = _zip_with_manifest(tmp_path, manifest, {"config.json": data})
    result = restore_backup(zpath, home=tmp_path / "out_home")
    assert result["files"] == 1
    assert (tmp_path / "out_home" / "config.json").read_bytes() == data


# ---------------------------------------------------------------------------
# E8-10 / E8-11 — dead production surface
# ---------------------------------------------------------------------------


def test_local_models_catalogue_removed_from_config():
    from aether_engine import config

    assert not hasattr(config, "LOCAL_MODELS"), (
        "LOCAL_MODELS is a dead catalogue: no production consumer "
        "exists (the live surface is models/local_models.py)")


def test_initialize_auth_alias_removed_from_engine_state():
    from aether_engine.state import EngineState

    assert not hasattr(EngineState, "initialize_auth"), (
        "initialize_auth is a test-only alias living in the "
        "production package; tests must call initialize() itself")
    assert hasattr(EngineState, "initialize")


# ---------------------------------------------------------------------------
# E8-14 — crash logging: idempotent + chains the threading hook
# ---------------------------------------------------------------------------


def test_install_crash_logging_idempotent_and_chains_thread_hook(
        tmp_path, monkeypatch):
    from aether_engine import diagnostics

    monkeypatch.setattr(diagnostics, "logs_dir", lambda: tmp_path)
    records: list[str] = []
    monkeypatch.setattr(
        diagnostics, "record_crash",
        lambda kind, et, e, tb: records.append(kind) or (tmp_path / "crash.log"))

    prev_sys_calls: list = []
    prev_thread_calls: list = []
    orig_sys, orig_thread = sys.excepthook, threading.excepthook
    sys.excepthook = lambda *a: prev_sys_calls.append(a)
    threading.excepthook = lambda args: prev_thread_calls.append(args)
    try:
        diagnostics.install_crash_logging()
        diagnostics.install_crash_logging()  # second install: no re-wrap

        try:
            raise RuntimeError("boom")
        except RuntimeError:
            exc = sys.exc_info()
        sys.excepthook(*exc)
        assert records == ["UNHANDLED"], (
            f"one crash must be recorded once, got {records}")
        assert len(prev_sys_calls) == 1

        args = SimpleNamespace(
            exc_type=RuntimeError, exc_value=RuntimeError("thread boom"),
            exc_traceback=None, thread=threading.current_thread())
        threading.excepthook(args)
        assert len(records) == 2 and records[1].startswith("THREAD")
        assert len(prev_thread_calls) == 1, (
            "the previous threading.excepthook must be chained, "
            "not silently dropped")
    finally:
        sys.excepthook, threading.excepthook = orig_sys, orig_thread


# ---------------------------------------------------------------------------
# E8-15 — detect_checks must not walk vendored trees
# ---------------------------------------------------------------------------


def test_detect_checks_ignores_tests_inside_vendored_trees(tmp_path):
    from aether_common.checks import detect_checks

    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    for vendored in ("node_modules/pkg", ".venv/lib/python3.12/site-packages/pkg"):
        d = tmp_path / vendored
        d.mkdir(parents=True)
        (d / "test_widget.py").write_text("def test_x():\n    pass\n")
    kinds = {c["kind"] for c in detect_checks(tmp_path)}
    assert "pytest" not in kinds, (
        "pytest was offered only because a vendored tree contains "
        "test_*.py files — detection walked node_modules/.venv")


def test_detect_checks_still_finds_real_tests(tmp_path):
    from aether_common.checks import detect_checks

    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    pkg = tmp_path / "src" / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "test_real.py").write_text("def test_x():\n    pass\n")
    kinds = {c["kind"] for c in detect_checks(tmp_path)}
    assert "pytest" in kinds


def test_detect_checks_tests_dir_short_circuit_still_works(tmp_path):
    from aether_common.checks import detect_checks

    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "tests").mkdir()
    kinds = {c["kind"] for c in detect_checks(tmp_path)}
    assert "pytest" in kinds


# ---------------------------------------------------------------------------
# E1-14 — capability runs are recorded through a public scheduler API
# ---------------------------------------------------------------------------


def test_capability_manager_records_run_through_public_api(tmp_path):
    from aether_engine.scheduler.capability_jobs import (
        CapabilitySchedulerManager,
    )

    mgr = CapabilitySchedulerManager(db_path=tmp_path / "capability.db")
    record = {
        "id": "run-timeout-1",
        "method": "option_a",
        "started_at": 100.0,
        "finished_at": 160.0,
        "models_tested": 0,
        "status": "FAILED",
        "cost_estimate": 0.0,
        "error_log": "Capability check timed out after 60 seconds",
    }
    mgr.record_run(record)  # public API — pre-fix this is AttributeError
    history = mgr.get_history()
    assert history and history[0]["id"] == "run-timeout-1"
    assert history[0]["status"] == "FAILED"


def test_routes_do_not_call_the_schedulers_private_record_run():
    import inspect

    from aether_engine.routes import websocket as ws_module

    assert "._record_run(" not in inspect.getsource(ws_module), (
        "routes/websocket.py must record timed-out capability runs "
        "through the scheduler's public API, not its private method")
