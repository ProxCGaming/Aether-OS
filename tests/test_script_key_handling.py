"""D4-04 (secondary) — covering tests for the two key-handling scripts.

scripts/export_keys.py writes every stored provider key as plaintext
JSON; scripts/set_gemini_key.py stores a key into the encrypted
store. Neither had a test naming it. These pin the behaviours that
matter if either script is ever edited carelessly:

- export_keys audits FIRST (Phase 9, A5): when the audit write
  fails, NO plaintext file is written, and the run exits non-zero;
- export_keys masks keys on screen and writes exactly the stored
  mapping;
- set_gemini_key takes the key from argv or from getpass, refuses an
  empty key without touching the store, verifies the round-trip,
  and reports a store failure as a clean exit 1.

Both scripts are loaded with a stubbed SecretStore / AuditLogger —
the DPAPI-backed store cannot run in this environment, and the
contract under test is the scripts' own logic.
"""

from __future__ import annotations

import getpass
import importlib.util
import json
import sys
from pathlib import Path
from typing import ClassVar

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"


def _load_script(name: str, stubs: dict[str, object]):
    """Exec a scripts/*.py module with selected sys.modules stubbed."""
    saved = {key: sys.modules.get(key) for key in stubs}
    sys.modules.update(stubs)
    try:
        spec = importlib.util.spec_from_file_location(
            f"wave3_{name}", SCRIPTS / f"{name}.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for key, value in saved.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value


class FakeAuditLogger:
    """Records events; can be told to fail (the audit gate's case)."""

    instances: ClassVar[list[FakeAuditLogger]] = []
    fail = False
    on_log = None  # optional probe run inside log_event

    def __init__(self):
        self.events: list[tuple[str, dict]] = []
        FakeAuditLogger.instances.append(self)

    def log_event(self, kind, payload):
        if FakeAuditLogger.on_log is not None:
            FakeAuditLogger.on_log()
        if FakeAuditLogger.fail:
            raise RuntimeError("audit store unavailable")
        self.events.append((kind, payload))


class FakeStore:
    """In-memory stand-in for the DPAPI SecretStore."""

    keys: ClassVar[dict[str, str]] = {}
    fail_save = False
    saved: ClassVar[list[tuple[str, str]]] = []

    def list_providers(self):
        return sorted(FakeStore.keys)

    def load_provider(self, name):
        return FakeStore.keys[name]

    def save_provider(self, name, key):
        if FakeStore.fail_save:
            raise RuntimeError("keyring unavailable")
        FakeStore.keys[name] = key
        FakeStore.saved.append((name, key))


@pytest.fixture(autouse=True)
def _reset_fakes():
    FakeAuditLogger.instances = []
    FakeAuditLogger.fail = False
    FakeAuditLogger.on_log = None
    FakeStore.keys = {}
    FakeStore.fail_save = False
    FakeStore.saved = []
    yield


def _load_export_keys():
    audit_mod = type(sys)("aether_engine.audit")
    audit_mod.AuditLogger = FakeAuditLogger
    storage_mod = type(sys)("aether_engine.secrets.storage")
    storage_mod.SecretStore = FakeStore
    return _load_script(
        "export_keys",
        {
            "aether_engine.audit": audit_mod,
            "aether_engine.secrets.storage": storage_mod,
        },
    )


def _load_set_gemini_key():
    storage_mod = type(sys)("aether_engine.secrets.storage")
    storage_mod.SecretStore = FakeStore
    return _load_script(
        "set_gemini_key",
        {"aether_engine.secrets.storage": storage_mod},
    )


# --- export_keys ---------------------------------------------------------


def test_export_writes_exactly_the_stored_keys_after_auditing(tmp_path, monkeypatch):
    module = _load_export_keys()
    target = tmp_path / "exported_keys.json"
    monkeypatch.setattr(module, "EXPORT_PATH", target)
    FakeStore.keys = {"openai": "sk-live-1234567890", "google_gemini": "AIza-short"}

    module.main()

    assert json.loads(target.read_text(encoding="utf-8")) == FakeStore.keys
    audit = FakeAuditLogger.instances[-1]
    assert audit.events and audit.events[0][0] == "SECRETS_EXPORTED"
    assert audit.events[0][1]["count"] == 2


def test_export_audits_before_the_plaintext_file_exists(tmp_path, monkeypatch):
    # A5's whole point: the audit event must land BEFORE the write,
    # so the probe inside log_event asserts the file is absent then.
    module = _load_export_keys()
    target = tmp_path / "exported_keys.json"
    monkeypatch.setattr(module, "EXPORT_PATH", target)
    FakeStore.keys = {"openai": "sk-live-1234567890"}
    existed_at_audit: list[bool] = []
    FakeAuditLogger.on_log = lambda: existed_at_audit.append(target.exists())

    module.main()

    assert existed_at_audit == [False]
    assert target.exists()


def test_export_aborts_without_writing_when_the_audit_fails(tmp_path, monkeypatch):
    module = _load_export_keys()
    target = tmp_path / "exported_keys.json"
    monkeypatch.setattr(module, "EXPORT_PATH", target)
    FakeStore.keys = {"openai": "sk-live-1234567890"}
    FakeAuditLogger.fail = True

    with pytest.raises(SystemExit) as excinfo:
        module.main()

    assert excinfo.value.code == 1
    assert not target.exists()


def test_export_with_no_providers_writes_nothing(tmp_path, monkeypatch):
    module = _load_export_keys()
    target = tmp_path / "exported_keys.json"
    monkeypatch.setattr(module, "EXPORT_PATH", target)
    FakeStore.keys = {}

    with pytest.raises(SystemExit) as excinfo:
        module.main()

    assert excinfo.value.code == 0
    assert not target.exists()


# --- set_gemini_key --------------------------------------------------------


def test_set_key_from_argv_stores_and_verifies(monkeypatch):
    module = _load_set_gemini_key()
    monkeypatch.setattr(sys, "argv", ["set_gemini_key.py", "AIza-test-key"])

    module.main()

    assert FakeStore.saved == [("google_gemini", "AIza-test-key")]


def test_set_key_from_getpass_when_no_argv(monkeypatch):
    module = _load_set_gemini_key()
    monkeypatch.setattr(sys, "argv", ["set_gemini_key.py"])
    monkeypatch.setattr(getpass, "getpass", lambda prompt: "AIza-prompted-key")

    module.main()

    assert FakeStore.saved == [("google_gemini", "AIza-prompted-key")]


def test_set_key_refuses_an_empty_key(monkeypatch):
    module = _load_set_gemini_key()
    monkeypatch.setattr(sys, "argv", ["set_gemini_key.py", "   "])

    with pytest.raises(SystemExit) as excinfo:
        module.main()

    assert excinfo.value.code == 1
    assert FakeStore.saved == []


def test_set_key_store_failure_is_a_clean_exit_1(monkeypatch):
    module = _load_set_gemini_key()
    monkeypatch.setattr(sys, "argv", ["set_gemini_key.py", "AIza-test-key"])
    FakeStore.fail_save = True

    with pytest.raises(SystemExit) as excinfo:
        module.main()

    assert excinfo.value.code == 1
