"""Wave 2 / E8 — config, contracts, backup and redaction floor.

Each test pins one E8 card's promised behaviour:

* E8-01 — concurrent ``save_config`` calls use unique temp files, so
  two savers can never splice one another's writes.
* E8-02 — the project config layer cannot set
  ``approval_binding_mode`` (the weaker "carry" binding stays
  global-only, exactly like ``approval_mode``).
* E8-05 — a backup manifest that lists the same target twice is
  refused during validation; the live profile is left byte-identical
  (the backup module's "fully replaced or left exactly as it was"
  contract).
* E8-06 — one corrupt numeric config value defaults that field only;
  it must not nuke (or quarantine) the whole config.
* E8-12 — the shared redactor covers the Google API key shape, URL
  userinfo credentials, and the sibling shapes in its own pattern
  families, so the diagnostics bundle is safe to hand to another
  person.
* E8-16 — ``SimulatedTask`` lives under the tests' support area, not
  in the production ``aether_engine`` package.
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import aether_engine.config as config_module
from aether_engine.audit import _redact
from aether_engine.backup import restore_backup
from aether_engine.config import UserConfig, load_config
from aether_engine.config_layers import effective_user_config
from aether_engine.diagnostics import build_diagnostics

# ---------------------------------------------------------------------------
# E8-01 — save_config temp names are unique per save
# ---------------------------------------------------------------------------

def test_save_config_uses_unique_temp_names_under_concurrent_saves(tmp_path, monkeypatch):
    target = tmp_path / "config.json"
    configs = [UserConfig(default_model="model-a"), UserConfig(default_model="model-b")]

    real_replace = os.replace
    replace_sources: list[Path] = []
    both_staged = threading.Barrier(2)

    def recording_replace(src, dst):
        # Hold each saver at the commit step until both have staged,
        # so the two writes genuinely overlap the way two processes
        # (or a future threaded caller) sharing ~/.aether would.
        replace_sources.append(Path(src))
        both_staged.wait(timeout=5)
        return real_replace(src, dst)

    monkeypatch.setattr(config_module.os, "replace", recording_replace)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda cfg: config_module.save_config(cfg, target), configs))

    assert len(replace_sources) == 2
    assert len(set(replace_sources)) == 2, (
        f"both saves staged through the same temp file: {replace_sources}")
    final = json.loads(target.read_text(encoding="utf-8"))
    assert final["default_model"] in {"model-a", "model-b"}


# ---------------------------------------------------------------------------
# E8-02 — approval_binding_mode is global-only
# ---------------------------------------------------------------------------

def test_project_layer_cannot_set_approval_binding_mode(tmp_path):
    cfg_dir = tmp_path / ".aether"
    cfg_dir.mkdir()
    (cfg_dir / "config.json").write_text(
        json.dumps({"approval_binding_mode": "carry"}), encoding="utf-8")

    effective = effective_user_config(
        UserConfig(approval_binding_mode="rebind"), workspace_root=str(tmp_path))

    assert effective.approval_binding_mode == "rebind"


# ---------------------------------------------------------------------------
# E8-05 — duplicate manifest paths are refused before anything is written
# ---------------------------------------------------------------------------

def _write_zip(path: Path, entries: list[dict], members: dict[str, bytes]) -> None:
    manifest = {
        "format": 1,
        "app_version": "test",
        "created_at": 0,
        "include_secrets": False,
        "files": entries,
    }
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        for name, data in members.items():
            zf.writestr(name, data)


def test_restore_with_duplicate_manifest_path_is_refused_and_writes_nothing(tmp_path):
    backup_data = b'{"default_model": "BACKUP"}'
    entry = {
        "path": "config.json",
        "size": len(backup_data),
        "sha256": hashlib.sha256(backup_data).hexdigest(),
    }
    zpath = tmp_path / "dup.zip"
    _write_zip(zpath, [entry, dict(entry)], {"config.json": backup_data})

    dst = tmp_path / "dst_home"
    dst.mkdir()
    original = b'{"default_model": "ORIGINAL"}'
    (dst / "config.json").write_bytes(original)

    with pytest.raises(ValueError, match="duplicate"):
        restore_backup(zpath, home=dst)

    # The promise: left exactly as it was — original bytes, no temps.
    assert (dst / "config.json").read_bytes() == original
    assert {p.name for p in dst.iterdir()} == {"config.json"}


# ---------------------------------------------------------------------------
# E8-06 — a corrupt numeric value defaults its own field only
# ---------------------------------------------------------------------------

def test_from_dict_corrupt_numeric_fields_default_individually():
    cfg = UserConfig.from_dict({
        "default_model": "survives",
        "window_width": None,
        "window_height": "wide",
        "daily_budget_usd": "not-a-number",
        "compaction_trigger_messages": 9,
    })
    assert cfg.default_model == "survives"
    assert cfg.window_width == 420
    assert cfg.window_height == 680
    assert cfg.daily_budget_usd is None
    assert cfg.compaction_trigger_messages == 9


def test_load_config_does_not_quarantine_one_corrupt_numeric_field(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({
        "default_model": "survives",
        "window_width": "wide",
    }), encoding="utf-8")

    cfg = load_config(path)

    assert cfg.default_model == "survives"
    assert cfg.window_width == 420
    assert path.exists(), "one bad field must not quarantine the whole config"


# ---------------------------------------------------------------------------
# E8-12 — the redactor covers Google keys, URL credentials, sibling shapes
# ---------------------------------------------------------------------------

GOOGLE_KEY = "AIza" + "A" * 35
AWS_TEMP_KEY = "ASIA" + "B" * 16
GITHUB_OAUTH_TOKEN = "gho_" + "C" * 36


def test_redactor_covers_google_key_url_credentials_and_sibling_shapes():
    redacted = _redact({
        "note": f"provider said no to key {GOOGLE_KEY} today",
        "url": "https://alice:secretpass@api.example/v1",
        "aws_temp": AWS_TEMP_KEY,
        "github_oauth": GITHUB_OAUTH_TOKEN,
        "pem": "-----BEGIN PRIVATE KEY-----\nSECRET_BODY_BYTES\n-----END PRIVATE KEY-----",
        "ordinary": "ordinary log text",
    })
    dumped = json.dumps(redacted)
    assert GOOGLE_KEY not in dumped
    assert "secretpass" not in dumped
    assert AWS_TEMP_KEY not in dumped
    assert GITHUB_OAUTH_TOKEN not in dumped
    assert "SECRET_BODY_BYTES" not in dumped
    # Only the credential is scrubbed — the URL stays readable.
    assert redacted["url"] == "https://alice:[REDACTED]@api.example/v1"
    assert redacted["ordinary"] == "ordinary log text"


def test_diagnostics_bundle_redacts_google_key_in_config_url(tmp_path):
    cfg = UserConfig(custom_base_urls={
        "custom_openai": f"https://user:{GOOGLE_KEY}@api.example/v1",
    })
    bundle = build_diagnostics(
        user_config=cfg,
        health={},
        kg_count=0,
        episodic_count=0,
        audit_log_path=tmp_path / "missing-audit.log",
        providers=[],
        crash_log_path=tmp_path / "missing-crash.log",
    )
    assert GOOGLE_KEY not in json.dumps(bundle)


# ---------------------------------------------------------------------------
# E8-16 — SimulatedTask is test support, not production package surface
# ---------------------------------------------------------------------------

def test_simulated_task_lives_under_tests_support_not_production_package():
    assert importlib.util.find_spec("aether_engine.simulated_task") is None
    support_spec = importlib.util.find_spec("tests.support.simulated_task")
    assert support_spec is not None
    module = importlib.import_module("tests.support.simulated_task")
    assert module.SimulatedTask.__name__ == "SimulatedTask"
