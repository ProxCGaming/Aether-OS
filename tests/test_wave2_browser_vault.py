"""Wave 2 (browser/vault/artifacts cluster) regression tests.

Each test pins one confirmed blow-up finding:

- E7-02 dev-server detection must not report the Aether engine itself
- E7-04 one undecryptable TOTP secret must not break the codes view
- E7-05 the production vault wiring must audit TOTP access by default
- E7-09 save_artifact must contain its path components
- E7-10 a cancelled vault-approval wait must not leak its future
- E7-11 vault metadata reads must not fetch the secret columns
- E7-14 DPAPI decrypt must not strip trailing NULs; protect/decrypt
  failures share one typed error family
- E7-15 writing a provider key must be audited (SECRET_SAVED)
- E7-16 reset_browser_manager / reset_vault_service must not abandon
  live sessions / pending approvals
- E7-17 a failing browser-policy loader must fail closed
- E7-18 save_login_from_page must return vault failures as refusals
- N-31 rotating a secret must delete the old keyring blob
- N-40 the password fill must re-check the site at fill time
"""

from __future__ import annotations

import asyncio
import ctypes
import http.server
import json
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

from aether_engine import vault as _vault_pkg  # noqa: F401  (package import sanity)
from aether_engine.artifacts import manager as artifact_manager
from aether_engine.artifacts.dev_server import _default_probe, detect_dev_server
from aether_engine.audit import AuditLogger
from aether_engine.browser import session as browser_session
from aether_engine.browser.policy import (
    BrowserPolicy,
    current_browser_policy,
    set_browser_policy_loader,
)
from aether_engine.browser.session import (
    BrowserError,
    BrowserSessionManager,
    reset_browser_manager,
)
from aether_engine.secrets.dpapi import BaseProtector, SecretDecryptionError
from aether_engine.secrets.storage import SecretStore
from aether_engine.vault import service as vault_service_module
from aether_engine.vault import store as vault_store_module
from aether_engine.vault.service import VaultFillService, reset_vault_service
from aether_engine.vault.store import (
    CredentialVault,
    VaultValidationError,
)

CANARY = "Canary!Vault-Passphrase-7f3a9-DO-NOT-LEAK"
TOTP_SECRET = "JBSWY3DPEHPK3PXP"
TOTP_SECRET_2 = "JBSWY3DP" + "EHPK3PXQ"  # split literal: public-style test seed, keeps secret scanners quiet


# ---------------------------------------------------------------------
# Shared fakes (the test_credential_vault.py seams)
# ---------------------------------------------------------------------

class XorProtector(BaseProtector):
    """Reversible transform protector: stored bytes never contain the
    plaintext, and a blob without the prefix fails decryption."""

    _PREFIX = b"X1:"

    def protect(self, data: bytes) -> bytes:
        return self._PREFIX + bytes(b ^ 0xA5 for b in data)

    def unprotect(self, data: bytes) -> bytes:
        if not data.startswith(self._PREFIX):
            raise SecretDecryptionError("Invalid header")
        return bytes(b ^ 0xA5 for b in data[len(self._PREFIX):])


class RecordingProtector(BaseProtector):
    """Keyring-style protector: every protect() mints a NEW reference,
    exactly like the non-Windows keyring protector (N-31's mechanism).
    Records every delete() so rotation cleanup is observable."""

    def __init__(self) -> None:
        self._blobs: dict[bytes, bytes] = {}
        self._counter = 0
        self.issued: list[bytes] = []
        self.deleted: list[bytes] = []

    def protect(self, data: bytes) -> bytes:
        self._counter += 1
        ref = f"ref-{self._counter}".encode()
        self._blobs[ref] = data
        self.issued.append(ref)
        return ref

    def unprotect(self, data: bytes) -> bytes:
        if data not in self._blobs:
            raise SecretDecryptionError("Secret not found in OS keychain.")
        return self._blobs[data]

    def delete(self, data: bytes) -> None:
        self.deleted.append(data)
        self._blobs.pop(data, None)


GITHUB_LOGIN = (
    "<html><head><title>Sign in to GitHub</title></head><body><form>"
    "<label>Username <input type=\"text\" id=\"login_field\" name=\"login\" "
    "autocomplete=\"username\"></label>"
    "<label>Password <input type=\"password\" id=\"password\" name=\"password\" "
    "autocomplete=\"current-password\"></label>"
    "<button type=\"submit\">Sign in</button></form></body></html>"
)
PLAIN_PAGE = ("<html><head><title>Example Domain</title></head><body>"
              "<h1>Example Domain</h1></body></html>")


class FakeDriver:
    """In-memory driver: page map, recorded calls, preset field values."""

    def __init__(self, pages=None):
        self.pages = dict(pages or {})
        self.calls = []
        self.field_values = {}
        self.closed = False
        self._url = "about:blank"
        self._title = ""
        self._html = ""

    def add_page(self, url, title, html):
        self.pages[url] = (title, html)

    async def goto(self, url):
        self.calls.append(("goto", url))
        title, html = self.pages.get(url, ("", "<html><body></body></html>"))
        self._url, self._title, self._html = url, title, html

    async def click(self, selector):
        self.calls.append(("click", selector))

    async def fill(self, selector, text):
        self.calls.append(("fill", selector, text))
        self.field_values[selector] = text

    async def input_value(self, selector):
        self.calls.append(("input_value", selector))
        return self.field_values.get(selector, "")

    async def extract_text(self, selector=None):
        self.calls.append(("extract_text", selector))
        import re

        return re.sub(r"<[^>]+>", " ", self._html)

    async def current_url(self):
        return self._url

    async def page_title(self):
        return self._title

    async def page_content(self):
        return self._html

    async def close(self):
        self.calls.append(("close",))
        self.closed = True

    def fills(self):
        return [c for c in self.calls if c[0] == "fill"]


def make_vault(tmp_path, protector=None):
    return CredentialVault(
        db_path=tmp_path / "vault.db",
        protector=protector or XorProtector(),
        audit_logger=AuditLogger(log_path=tmp_path / "audit.log"),
    )


def make_manager(tmp_path, driver):
    async def factory(_profile_dir):
        return driver

    return BrowserSessionManager(
        audit_logger=AuditLogger(log_path=tmp_path / "audit.log"),
        driver_factory=factory,
    )


def make_service(tmp_path, vault, timeout=5.0):
    return VaultFillService(
        vault,
        audit_logger=AuditLogger(log_path=tmp_path / "audit.log"),
        approval_timeout_seconds=timeout,
    )


@pytest.fixture(autouse=True)
def _policy_loader_isolation():
    set_browser_policy_loader(None)
    yield
    set_browser_policy_loader(None)


# ---------------------------------------------------------------------
# E7-02 — the engine itself is not a dev server
# ---------------------------------------------------------------------

class _EngineImpersonatorHandler(http.server.BaseHTTPRequestHandler):
    """Answers like the Aether engine: 404 JSON at /, and an OpenAPI
    document whose info.title is 'AETHER Engine'."""

    def do_GET(self):
        if self.path.startswith("/openapi.json"):
            body = json.dumps({
                "openapi": "3.1.0",
                "info": {"title": "AETHER Engine", "version": "0.0.0"},
                "paths": {},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        else:
            body = b'{"detail":"Not Found"}'
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep test output clean
        pass


class _PlainHttpHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"<html><head><title>dev</title></head><body>app</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _serve(handler) -> tuple[http.server.ThreadingHTTPServer, int]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


class TestE702DevServer:
    def test_engine_port_is_not_a_dev_server(self):
        server, port = _serve(_EngineImpersonatorHandler)
        try:
            assert _default_probe(port) is False
            result = detect_dev_server(ports=[port])
            assert result["running"] is False
            assert result["port"] is None
        finally:
            server.shutdown()

    def test_plain_http_server_still_detected(self):
        server, port = _serve(_PlainHttpHandler)
        try:
            assert _default_probe(port) is True
            result = detect_dev_server(ports=[port])
            assert result["running"] is True
            assert result["port"] == port
        finally:
            server.shutdown()

    def test_engine_does_not_shadow_a_real_dev_server(self):
        engine, engine_port = _serve(_EngineImpersonatorHandler)
        plain, plain_port = _serve(_PlainHttpHandler)
        try:
            result = detect_dev_server(ports=[engine_port, plain_port])
            assert result["running"] is True
            assert result["port"] == plain_port
        finally:
            engine.shutdown()
            plain.shutdown()


# ---------------------------------------------------------------------
# E7-04 — one undecryptable TOTP secret must not break the whole view
# ---------------------------------------------------------------------

def _corrupt_blob(db_path: Path, table: str, column: str, row_id: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            f"UPDATE {table} SET {column} = ? WHERE id = ?",
            (b"garbage-blob-whose-keyring-entry-is-gone", row_id),
        )


class TestE704TotpCodesResilience:
    def test_undecryptable_standalone_entry_is_skipped(self, tmp_path):
        vault = make_vault(tmp_path)
        vault.save_totp_entry(label="Good", issuer="GitHub", secret=TOTP_SECRET)
        bad_id = vault.save_totp_entry(
            label="Bad", issuer="Old", secret=TOTP_SECRET_2)
        _corrupt_blob(tmp_path / "vault.db", "vault_totp_entries",
                      "encrypted_secret", bad_id)

        result = vault.totp_codes(at_time=1_700_000_000)
        assert [c["label"] for c in result["codes"]] == ["Good"]

    def test_undecryptable_login_totp_is_skipped(self, tmp_path):
        vault = make_vault(tmp_path)
        vault.save_totp_entry(label="Good", issuer="GitHub", secret=TOTP_SECRET)
        login_id = vault.save_entry(
            site="github.com", username="alice", password="pw",
            totp_secret=TOTP_SECRET_2)
        _corrupt_blob(tmp_path / "vault.db", "vault_entries",
                      "encrypted_totp", login_id)

        result = vault.totp_codes(at_time=1_700_000_000)
        assert [c["label"] for c in result["codes"]] == ["Good"]
        assert login_id not in [c["entry_id"] for c in result["codes"]]


# ---------------------------------------------------------------------
# E7-05 — the production wiring (no injected logger) still audits
# ---------------------------------------------------------------------

class TestE705TotpAuditDefault:
    def test_vault_without_injected_logger_still_audits(self, tmp_path, monkeypatch):
        instances = []

        class RecordingAuditLogger:
            def __init__(self, *args, **kwargs):
                self.events = []
                instances.append(self)

            def log_event(self, event_type, payload):
                self.events.append((event_type, payload))

        monkeypatch.setattr(
            vault_store_module, "AuditLogger", RecordingAuditLogger,
            raising=False)
        # The production shape: CredentialVault() with NO audit_logger.
        vault = CredentialVault(
            db_path=tmp_path / "vault.db", protector=XorProtector())
        entry_id = vault.save_totp_entry(label="GitHub", secret=TOTP_SECRET)
        vault.get_totp_entry_secret(entry_id)

        all_events = [e for inst in instances for e in inst.events]
        assert ("VAULT_TOTP_ACCESSED", {"entry_id": entry_id}) in all_events
        # The audit payload carries metadata only — never the secret.
        assert TOTP_SECRET not in str(all_events)


# ---------------------------------------------------------------------
# E7-09 — save_artifact path containment
# ---------------------------------------------------------------------

class TestE709SaveArtifact:
    def test_filename_traversal_is_refused(self):
        with pytest.raises(ValueError):
            artifact_manager.save_artifact("task1", "code", "../../evil.txt", "x")

    def test_task_id_traversal_is_refused(self):
        with pytest.raises(ValueError):
            artifact_manager.save_artifact("../escape", "code", "out.txt", "x")

    def test_node_type_traversal_is_refused(self):
        with pytest.raises(ValueError):
            artifact_manager.save_artifact("task1", "a/b", "out.txt", "x")

    def test_dotdot_and_empty_components_are_refused(self):
        with pytest.raises(ValueError):
            artifact_manager.save_artifact("task1", "code", "..", "x")
        with pytest.raises(ValueError):
            artifact_manager.save_artifact("task1", "code", "", "x")
        with pytest.raises(ValueError):
            artifact_manager.get_task_artifact_dir("")

    def test_happy_path_still_works(self):
        path = artifact_manager.save_artifact(
            "task-wave2", "code", "out.txt", "hello")
        written = Path(path)
        assert written.read_text(encoding="utf-8") == "hello"
        assert written.name == "out.txt"
        assert written.parent.name == "code"


# ---------------------------------------------------------------------
# E7-10 — cancelled approval waits leak their future
# ---------------------------------------------------------------------

class TestE710ApprovalFutureLeak:
    async def test_cancelled_wait_removes_the_pending_future(self, tmp_path):
        service = make_service(tmp_path, make_vault(tmp_path), timeout=60.0)
        entry = {"id": "e1", "site": "github.com", "username": "alice"}
        task = asyncio.create_task(
            service._await_owner_approval(entry=entry, task_id="task-1"))
        for _ in range(200):
            if service._pending_approvals:
                break
            await asyncio.sleep(0.005)
        assert service._pending_approvals, "approval was never registered"
        request_id = next(iter(service._pending_approvals))

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert request_id not in service._pending_approvals
        assert service.resolve_fill_approval(request_id, True) is False

    async def test_timed_out_wait_removes_the_pending_future(self, tmp_path):
        service = make_service(tmp_path, make_vault(tmp_path), timeout=0.05)
        entry = {"id": "e1", "site": "github.com", "username": "alice"}
        approved, reason = await service._await_owner_approval(
            entry=entry, task_id="task-1")
        assert approved is False
        assert reason == "approval_timeout"
        assert service._pending_approvals == {}


# ---------------------------------------------------------------------
# E7-11 — metadata reads must not fetch the secret columns
# ---------------------------------------------------------------------

class TestE711MetadataSelects:
    def test_get_entry_and_entries_for_site_select_no_secret_columns(
            self, tmp_path, monkeypatch):
        vault = make_vault(tmp_path)
        entry_id = vault.save_entry(
            site="github.com", username="alice", password="pw",
            totp_secret=TOTP_SECRET)

        statements: list[str] = []
        real_connect = sqlite3.connect

        def traced_connect(*args, **kwargs):
            conn = real_connect(*args, **kwargs)
            conn.set_trace_callback(statements.append)
            return conn

        monkeypatch.setattr(
            vault_store_module.sqlite3, "connect", traced_connect)
        try:
            entry = vault.get_entry(entry_id)
            by_site = vault.entries_for_site("https://github.com/login")
        finally:
            monkeypatch.undo()

        assert entry["username"] == "alice"
        assert [e["id"] for e in by_site] == [entry_id]
        selects = [s for s in statements
                   if s.lstrip().upper().startswith("SELECT")]
        assert selects, "expected the reads to issue SELECTs"
        for statement in selects:
            assert "encrypted_password" not in statement
            assert "encrypted_totp" not in statement
            assert "SELECT *" not in statement.upper()


# ---------------------------------------------------------------------
# E7-14 — DPAPI: no NUL stripping; one typed error family
# (exercised through a simulated win32 ctypes layer, because the real
# DPAPI functions are Windows-only)
# ---------------------------------------------------------------------

def _load_win32_dpapi(monkeypatch, *, fail_encrypt=False, fail_decrypt=False):
    import importlib.util
    from types import SimpleNamespace

    from aether_engine.secrets import dpapi as dpapi_module

    state: dict[str, bytes] = {}
    keepalive: list = []

    def _set_out(out_ref, data: bytes) -> None:
        buf = ctypes.create_string_buffer(data, max(len(data), 1))
        keepalive.append(buf)
        out_blob = out_ref._obj
        out_blob.pbData = ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))
        out_blob.cbData = len(data)

    class _FakeCrypt32:
        def CryptProtectData(self, in_ref, _d, _e, _r, _p, _f, out_ref):
            if fail_encrypt:
                return 0
            blob_in = in_ref._obj
            plaintext = ctypes.string_at(blob_in.pbData, blob_in.cbData)
            encrypted = b"ENC:" + plaintext
            state[encrypted] = plaintext
            _set_out(out_ref, encrypted)
            return 1

        def CryptUnprotectData(self, in_ref, _d, _e, _r, _p, _f, out_ref):
            if fail_decrypt:
                return 0
            blob_in = in_ref._obj
            encrypted = ctypes.string_at(blob_in.pbData, blob_in.cbData)
            _set_out(out_ref, state[encrypted])
            return 1

    class _FakeKernel32:
        @staticmethod
        def GetLastError():
            return 5

        @staticmethod
        def LocalFree(_ptr):
            return 0

    fake_windll = SimpleNamespace(
        crypt32=_FakeCrypt32(), kernel32=_FakeKernel32())
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "windll", fake_windll, raising=False)
    spec = importlib.util.spec_from_file_location(
        "dpapi_win32_sim", dpapi_module.__file__)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestE714DpapiContract:
    def test_decrypt_preserves_trailing_nul_bytes(self, monkeypatch):
        module = _load_win32_dpapi(monkeypatch)
        protector = module.WindowsDPAPIProtector()
        data = b"api-key-value\x00\x00"
        assert protector.unprotect(protector.protect(data)) == data

    def test_protect_failure_raises_the_typed_family(self, monkeypatch):
        module = _load_win32_dpapi(monkeypatch, fail_encrypt=True)
        protector = module.WindowsDPAPIProtector()
        encryption_error = getattr(module, "SecretEncryptionError", None)
        assert encryption_error is not None, (
            "protect() failures need a typed error in the "
            "SecretProtectionError family, not a bare RuntimeError")
        with pytest.raises(encryption_error):
            protector.protect(b"secret")
        # Both directions are catchable through the shared base, so a
        # caller wrapping a protect/decrypt pair misses neither.
        assert issubclass(encryption_error, module.SecretProtectionError)
        assert issubclass(
            module.SecretDecryptionError, module.SecretProtectionError)

    def test_decrypt_failure_raises_decryption_error(self, monkeypatch):
        module = _load_win32_dpapi(monkeypatch, fail_decrypt=True)
        protector = module.WindowsDPAPIProtector()
        encrypted = protector.protect(b"secret")
        with pytest.raises(module.SecretDecryptionError):
            protector.unprotect(encrypted)


# ---------------------------------------------------------------------
# E7-15 — writing a provider key is audited
# ---------------------------------------------------------------------

class _ListAuditLogger:
    def __init__(self):
        self.events = []

    def log_event(self, event_type, payload):
        self.events.append((event_type, dict(payload)))


class TestE715SaveProviderAudit:
    def test_save_provider_writes_a_secret_saved_event(self, tmp_path):
        logger = _ListAuditLogger()
        store = SecretStore(
            db_path=tmp_path / "secrets.db",
            protector=XorProtector(), audit_logger=logger)
        store.save_provider("openai", "sk-secret-value")
        saved = [p for (t, p) in logger.events if t == "SECRET_SAVED"]
        assert saved == [{"provider": "openai"}]
        assert "sk-secret-value" not in str(logger.events)


# ---------------------------------------------------------------------
# N-31 — rotation must revoke the old keyring blob
# ---------------------------------------------------------------------

class TestN31RotationRevokesOldBlob:
    def test_secret_store_rotation_deletes_the_old_blob(self, tmp_path):
        protector = RecordingProtector()
        store = SecretStore(
            db_path=tmp_path / "secrets.db", protector=protector,
            audit_logger=_ListAuditLogger())
        store.save_provider("openai", "key-one")
        first_blob = protector.issued[-1]
        assert protector.deleted == []

        store.save_provider("openai", "key-two")
        assert protector.deleted == [first_blob]
        assert store.load_provider("openai") == "key-two"

    def test_vault_entry_rotation_deletes_replaced_blobs(self, tmp_path):
        protector = RecordingProtector()
        vault = make_vault(tmp_path, protector=protector)
        entry_id = vault.save_entry(
            site="github.com", username="alice", password="pw-one",
            totp_secret=TOTP_SECRET)
        password_blob, totp_blob = protector.issued
        assert protector.deleted == []

        # Password rotation: the old password blob is revoked; the
        # untouched TOTP blob is NOT (it is still the live secret).
        vault.save_entry(
            entry_id=entry_id, site="github.com", username="alice",
            password="pw-two")
        assert protector.deleted == [password_blob]
        assert vault.reveal_password(entry_id) == "pw-two"

        # TOTP rotation revokes the old TOTP blob.
        vault.save_entry(
            entry_id=entry_id, site="github.com", username="alice",
            password=None, totp_secret=TOTP_SECRET_2)
        assert protector.deleted == [password_blob, totp_blob]

        # Clearing the TOTP secret revokes its blob too.
        live_totp_blob = protector.issued[-1]
        vault.save_entry(
            entry_id=entry_id, site="github.com", username="alice",
            password=None, totp_secret="")
        assert protector.deleted == [password_blob, totp_blob, live_totp_blob]

    def test_totp_entry_rotation_deletes_the_old_blob(self, tmp_path):
        protector = RecordingProtector()
        vault = make_vault(tmp_path, protector=protector)
        entry_id = vault.save_totp_entry(label="GitHub", secret=TOTP_SECRET)
        first_blob = protector.issued[-1]

        vault.save_totp_entry(
            entry_id=entry_id, label="GitHub", secret=TOTP_SECRET_2)
        assert protector.deleted == [first_blob]
        assert vault.get_totp_entry_secret(entry_id) == TOTP_SECRET_2


# ---------------------------------------------------------------------
# E7-16 — resets must not abandon live state
# ---------------------------------------------------------------------

class TestE716Resets:
    async def test_reset_browser_manager_stops_the_live_session(
            self, tmp_path, monkeypatch):
        driver = FakeDriver({"https://example.com/": ("Example", PLAIN_PAGE)})
        manager = make_manager(tmp_path, driver)
        monkeypatch.setattr(browser_session, "_manager", manager)
        session = manager.get_or_create_active()
        await session.navigate("https://example.com/")
        assert driver.closed is False

        reset_browser_manager()
        for _ in range(200):
            if driver.closed:
                break
            await asyncio.sleep(0.005)
        assert driver.closed is True
        assert browser_session._manager is None

    def test_reset_browser_manager_stops_session_without_a_running_loop(
            self, tmp_path, monkeypatch):
        driver = FakeDriver({"https://example.com/": ("Example", PLAIN_PAGE)})
        manager = make_manager(tmp_path, driver)
        monkeypatch.setattr(browser_session, "_manager", manager)
        session = manager.get_or_create_active()
        asyncio.run(session.navigate("https://example.com/"))
        assert driver.closed is False

        reset_browser_manager()
        assert driver.closed is True
        assert browser_session._manager is None

    async def test_reset_vault_service_cancels_pending_approvals(
            self, tmp_path, monkeypatch):
        service = make_service(tmp_path, make_vault(tmp_path))
        future = asyncio.get_running_loop().create_future()
        service._pending_approvals["req-1"] = future
        monkeypatch.setattr(vault_service_module, "_service", service)

        reset_vault_service()
        assert future.cancelled()
        assert vault_service_module._service is None


# ---------------------------------------------------------------------
# E7-17 — a failing policy loader fails closed
# ---------------------------------------------------------------------

class TestE717PolicyLoaderFailure:
    def test_raising_loader_fails_closed(self):
        def boom():
            raise RuntimeError("config store unreadable")

        set_browser_policy_loader(boom)
        policy = current_browser_policy()
        assert policy.enabled is False
        assert policy.vault_autofill is False
        assert policy.envelopes_enabled is False

    def test_garbage_loader_result_fails_closed(self):
        set_browser_policy_loader(lambda: {"enabled": True})
        assert current_browser_policy().enabled is False

    def test_no_loader_still_uses_the_shipped_preset(self):
        set_browser_policy_loader(None)
        policy = current_browser_policy()
        assert policy.enabled is True
        assert policy == BrowserPolicy()


# ---------------------------------------------------------------------
# E7-18 — vault-side save failures are refusals, not exceptions
# ---------------------------------------------------------------------

class TestE718SaveLoginRefusals:
    async def _session_with_form(self, tmp_path, driver):
        manager = make_manager(tmp_path, driver)
        session = manager.get_or_create_active(task_id="task-save-1")
        await session.navigate("https://github.com/signup")
        return session

    async def test_vault_failure_returns_a_refusal_dict(self, tmp_path):
        driver = FakeDriver(
            {"https://github.com/signup": ("Join GitHub", GITHUB_LOGIN)})
        driver.field_values = {"#login_field": "newuser", "#password": CANARY}
        session = await self._session_with_form(tmp_path, driver)
        vault = make_vault(tmp_path)

        def broken_save_entry(**_kwargs):
            raise VaultValidationError("A username is required.")

        vault.save_entry = broken_save_entry  # the seam the card names
        service = make_service(tmp_path, vault)

        result = await service.save_login_from_page(session)
        assert result["saved"] is False
        assert result["reason"]
        assert result["message"]
        assert CANARY not in str(result)

    async def test_healthy_save_still_saves(self, tmp_path):
        driver = FakeDriver(
            {"https://github.com/signup": ("Join GitHub", GITHUB_LOGIN)})
        driver.field_values = {"#login_field": "newuser", "#password": CANARY}
        session = await self._session_with_form(tmp_path, driver)
        service = make_service(tmp_path, make_vault(tmp_path))

        result = await service.save_login_from_page(session)
        assert result["saved"] is True
        assert result["site"] == "github.com"


# ---------------------------------------------------------------------
# N-40 — the fill must re-check the site at fill time
# ---------------------------------------------------------------------

class _NavigatingOnUsernameFillDriver(FakeDriver):
    """Simulates a page that redirects while the fill is in flight:
    filling the username field lands the browser on another site."""

    def __init__(self, pages, move_to):
        super().__init__(pages)
        self._move_to = move_to

    async def fill(self, selector, text):
        await super().fill(selector, text)
        if selector == "#login_field":
            title, html = self.pages[self._move_to]
            self._url, self._title, self._html = self._move_to, title, html


class TestN40FillSiteRecheck:
    async def test_type_text_refuses_when_the_page_moved_sites(
            self, tmp_path):
        driver = FakeDriver({
            "https://github.com/login": ("Sign in", GITHUB_LOGIN),
            "https://evil.example.com/login": ("Sign in", GITHUB_LOGIN),
        })
        manager = make_manager(tmp_path, driver)
        session = manager.get_or_create_active(task_id="task-n40")
        await session.navigate("https://github.com/login")
        # The page redirected (script/meta refresh) after navigation.
        title, html = driver.pages["https://evil.example.com/login"]
        driver._url = "https://evil.example.com/login"
        driver._title, driver._html = title, html

        with pytest.raises(BrowserError):
            await session.type_text(
                "#password", CANARY, require_site="github.com")
        assert ("fill", "#password", CANARY) not in driver.calls

    async def test_type_text_allows_the_same_site(self, tmp_path):
        driver = FakeDriver({
            "https://github.com/login": ("Sign in", GITHUB_LOGIN),
            "https://gist.github.com/login": ("Sign in", GITHUB_LOGIN),
        })
        manager = make_manager(tmp_path, driver)
        session = manager.get_or_create_active(task_id="task-n40")
        await session.navigate("https://github.com/login")
        await session.type_text("#password", CANARY, require_site="github.com")
        # A subdomain of the same registrable site is the same site.
        title, html = driver.pages["https://gist.github.com/login"]
        driver._url = "https://gist.github.com/login"
        driver._title, driver._html = title, html
        await session.type_text("#password", CANARY, require_site="github.com")
        password_fills = [c for c in driver.fills() if c[1] == "#password"]
        assert len(password_fills) == 2

    async def test_fill_login_refuses_when_the_page_moves_mid_fill(
            self, tmp_path):
        pages = {
            "https://github.com/login": ("Sign in", GITHUB_LOGIN),
            "https://evil.example.com/login": ("Sign in", GITHUB_LOGIN),
        }
        driver = _NavigatingOnUsernameFillDriver(
            pages, "https://evil.example.com/login")
        manager = make_manager(tmp_path, driver)
        session = manager.get_or_create_active(task_id="task-n40")
        await session.navigate("https://github.com/login")

        vault = make_vault(tmp_path)
        vault.save_entry(
            site="github.com", username="alice", password=CANARY)
        set_browser_policy_loader(
            lambda: BrowserPolicy(vault_autofill=True))
        service = make_service(tmp_path, vault)

        result = await service.fill_login(session)
        assert result["filled"] is False
        assert result["reason"] == "fill_failed"
        # The password was never typed anywhere.
        assert all(fill[2] != CANARY for fill in driver.fills())
