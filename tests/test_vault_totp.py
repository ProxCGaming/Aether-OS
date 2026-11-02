"""PRD Open Point 14 (DECIDED YES by the owner, 1 Nov 2026): the
credential vault doubles as the owner's authenticator app.

The owner enrols TOTP secrets in Settings → Password Vault (label /
issuer + the Base32 setup key a site shows beside its QR code) and
sees the live 6-digit codes there for his own manual use. Codes are
computed ENGINE-side (RFC 6238) and served only over the owner
Settings channel; the SECRET never crosses to the renderer and never
appears in any payload — only the current code, its countdown, and
entry metadata.

Agents remain STRICTLY FORBIDDEN from TOTP by every path: no tool,
no event, no model-visible surface may carry a TOTP secret or a
generated code, and the settled rule stands — agents never fetch or
enter 2FA codes; on a 2FA prompt the agent pauses and hands the
owner the browser. The isolation tests below plant a distinctive
Base32 canary secret, compute its current code in-test, and grep
every agent-visible surface (tool definitions, fill results, list
outputs, audit lines, at-rest DB bytes) for BOTH strings.

DPAPI is exercised through an injected test protector, exactly as
tests/test_credential_vault.py does.

Note on style: secret-bearing arguments are passed via dict
unpacking ({"secret": value}) throughout this file, deliberately —
the canary values never appear in plain keyword-assignment form,
so PIE804 is waived for the whole file.
"""
# ruff: noqa: PIE804
import json
import sqlite3
import time

import pytest

from aether_common.contracts import Event, EventType
from aether_engine.audit import AuditLogger
from aether_engine.browser.policy import (
    BrowserPolicy,
    set_browser_policy_loader,
)
from aether_engine.browser.tools import create_browser_tools
from aether_engine.secrets.dpapi import BaseProtector, SecretDecryptionError
from aether_engine.vault.service import VaultFillService
from aether_engine.vault.store import (
    CredentialVault,
    VaultEntryNotFound,
    VaultValidationError,
)
from aether_engine.vault.totp import (
    TotpValidationError,
    normalize_base32_secret,
    seconds_remaining,
    totp_code,
)

# RFC 6238 Appendix B seed: ASCII "12345678901234567890" (SHA-1).
RFC_SEED_B32 = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
# Distinctive, valid-Base32 canary (alphabet A-Z + 2-7 only).
TOTP_CANARY = "CANARYTOTPSECRETZZ42"
LOGIN_PASSWORD = "Canary!Vault-Passphrase-7f3a9-DO-NOT-LEAK"

GITHUB_LOGIN = (
    "<html><head><title>Sign in to GitHub</title></head><body><form>"
    "<label>Username <input type=\"text\" id=\"login_field\" name=\"login\" "
    "autocomplete=\"username\"></label>"
    "<label>Password <input type=\"password\" id=\"password\" name=\"password\" "
    "autocomplete=\"current-password\"></label>"
    "<button type=\"submit\">Sign in</button></form></body></html>"
)


# ---------------------------------------------------------------------
# Test protectors (the test_credential_vault.py seam)
# ---------------------------------------------------------------------

class XorProtector(BaseProtector):
    """Reversible transform protector: the stored bytes never contain
    the plaintext as a substring, so the at-rest test is meaningful."""

    _PREFIX = b"X1:"

    def protect(self, data: bytes) -> bytes:
        return self._PREFIX + bytes(b ^ 0xA5 for b in data)

    def unprotect(self, data: bytes) -> bytes:
        if not data.startswith(self._PREFIX):
            raise SecretDecryptionError("Invalid header")
        return bytes(b ^ 0xA5 for b in data[len(self._PREFIX):])


def make_vault(tmp_path, protector=None):
    return CredentialVault(
        db_path=tmp_path / "vault.db",
        protector=protector or XorProtector(),
        audit_logger=AuditLogger(log_path=tmp_path / "audit.log"),
    )


def audit_text(tmp_path):
    path = tmp_path / "audit.log"
    return path.read_text(encoding="utf-8") if path.exists() else ""


@pytest.fixture(autouse=True)
def _policy_loader_cleanup():
    yield
    set_browser_policy_loader(None)


# ---------------------------------------------------------------------
# RFC 6238 correctness (published test vectors, SHA-1, 6-digit form)
# ---------------------------------------------------------------------

class TestRfc6238Vectors:
    """The RFC 6238 Appendix B seed ("12345678901234567890") at the
    RFC's own timestamps. Expected values are the HOTP value
    (RFC 4226 dynamic truncation) mod 10^6, zero-padded — the
    T=59 row reduces the RFC's printed 8-digit 94287082 — and the
    full set was cross-verified against pyotp, an independent
    RFC 6238 implementation, which agrees on all six."""

    @pytest.mark.parametrize("at_time,expected", [
        (59, "287082"),           # RFC 8-digit: 94287082
        (1111111109, "081804"),   # 8-digit: 07081804
        (1111111111, "050471"),   # 8-digit: 14050471
        (1234567890, "005924"),   # 8-digit: 89005924 (zero-padded)
        (2000000000, "279037"),   # 8-digit: 69279037
        (20000000000, "353130"),  # 8-digit: 65353130
    ])
    def test_published_vectors(self, at_time, expected):
        assert totp_code(RFC_SEED_B32, at_time) == expected

    def test_seconds_remaining(self):
        assert seconds_remaining(59) == 1
        assert seconds_remaining(60) == 30
        assert seconds_remaining(0) == 30
        assert seconds_remaining(1234567890) == 30 - (1234567890 % 30)


# ---------------------------------------------------------------------
# Base32 normalisation / validation
# ---------------------------------------------------------------------

class TestBase32Validation:
    def test_accepts_and_normalizes(self):
        assert normalize_base32_secret(RFC_SEED_B32) == RFC_SEED_B32
        # Lowercase, spaces, hyphens and padding are cosmetic.
        messy = "gezd gnbv-gy3t qojq gezdgnbvgy3tqojq"
        assert normalize_base32_secret(messy) == RFC_SEED_B32
        assert normalize_base32_secret(
            "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ====") == RFC_SEED_B32

    @pytest.mark.parametrize("bad", [
        "",                 # empty
        "   ",              # whitespace only
        "ABC!EFGH",         # character outside the Base32 alphabet
        "ABC0189Z",         # 0/1/8/9 are NOT Base32 digits
        "A",                # impossible unpadded length (mod 8 == 1)
        "ABC",              # impossible unpadded length (mod 8 == 3)
        "====",             # padding only
    ])
    def test_rejects_malformed(self, bad):
        with pytest.raises(TotpValidationError):
            normalize_base32_secret(bad)


# ---------------------------------------------------------------------
# Standalone authenticator entries: storage, validation, encryption
# ---------------------------------------------------------------------

class TestTotpEntryStorage:
    def test_save_list_delete_round_trip(self, tmp_path):
        vault = make_vault(tmp_path)
        entry_id = vault.save_totp_entry(
            label="octocat", issuer="GitHub", **{"secret": TOTP_CANARY})
        assert isinstance(entry_id, str) and entry_id
        entries = vault.list_totp_entries()
        assert len(entries) == 1
        row = entries[0]
        assert row["id"] == entry_id
        assert row["label"] == "octocat"
        assert row["issuer"] == "GitHub"
        # Metadata only: the listing carries no secret-shaped field.
        assert set(row) == {
            "id", "label", "issuer", "created_at", "updated_at"}
        assert TOTP_CANARY not in json.dumps(entries)
        assert vault.delete_totp_entry(entry_id) is True
        assert vault.list_totp_entries() == []
        assert vault.delete_totp_entry(entry_id) is False

    def test_secret_is_encrypted_at_rest(self, tmp_path):
        vault = make_vault(tmp_path)
        vault.save_totp_entry(label="octocat", issuer="GitHub",
                              **{"secret": TOTP_CANARY})
        raw = (tmp_path / "vault.db").read_bytes()
        assert TOTP_CANARY.encode() not in raw
        with sqlite3.connect(tmp_path / "vault.db") as conn:
            (blob,) = conn.execute(
                "SELECT encrypted_secret FROM vault_totp_entries"
            ).fetchone()
        assert TOTP_CANARY.encode() not in bytes(blob)

    def test_malformed_secret_is_refused_and_stores_nothing(self, tmp_path):
        vault = make_vault(tmp_path)
        for bad in ("", "ABC!EFGH", "ABC0189Z", "A"):
            with pytest.raises(VaultValidationError):
                vault.save_totp_entry(label="octocat", issuer="GitHub",
                                      **{"secret": bad})
        assert vault.list_totp_entries() == []

    def test_label_is_required(self, tmp_path):
        vault = make_vault(tmp_path)
        with pytest.raises(VaultValidationError):
            vault.save_totp_entry(label="  ", issuer="GitHub",
                                  **{"secret": TOTP_CANARY})
        assert vault.list_totp_entries() == []

    def test_messy_secret_is_stored_normalized(self, tmp_path):
        vault = make_vault(tmp_path)
        entry_id = vault.save_totp_entry(
            label="octocat", issuer="GitHub",
            **{"secret": "canary totp-secret zz42"})
        # The owner accessor returns the normalized form.
        assert vault.get_totp_entry_secret(entry_id) == TOTP_CANARY

    def test_update_keeps_secret_when_omitted_and_replaces_when_given(
            self, tmp_path):
        vault = make_vault(tmp_path)
        entry_id = vault.save_totp_entry(
            label="octocat", issuer="GitHub", **{"secret": TOTP_CANARY})
        vault.save_totp_entry(label="octocat-renamed", issuer="GitHub",
                              entry_id=entry_id)
        assert vault.get_totp_entry_secret(entry_id) == TOTP_CANARY
        rows = vault.list_totp_entries()
        assert rows[0]["label"] == "octocat-renamed"
        vault.save_totp_entry(label="octocat-renamed", issuer="GitHub",
                              entry_id=entry_id,
                              **{"secret": RFC_SEED_B32})
        assert vault.get_totp_entry_secret(entry_id) == RFC_SEED_B32

    def test_update_of_missing_entry_raises(self, tmp_path):
        vault = make_vault(tmp_path)
        with pytest.raises(VaultEntryNotFound):
            vault.save_totp_entry(label="x", entry_id="no-such-id",
                                  **{"secret": TOTP_CANARY})

    def test_owner_access_is_audited(self, tmp_path):
        vault = make_vault(tmp_path)
        entry_id = vault.save_totp_entry(
            label="octocat", issuer="GitHub", **{"secret": TOTP_CANARY})
        vault.get_totp_entry_secret(entry_id)
        audit = audit_text(tmp_path)
        assert "VAULT_TOTP_ACCESSED" in audit
        assert TOTP_CANARY not in audit  # metadata only, never the secret


# ---------------------------------------------------------------------
# Codes: computed engine-side, owner accessor, audit, countdown
# ---------------------------------------------------------------------

class TestTotpCodes:
    def test_codes_for_standalone_and_login_entries(self, tmp_path):
        vault = make_vault(tmp_path)
        solo_id = vault.save_totp_entry(
            label="octocat", issuer="GitHub", **{"secret": RFC_SEED_B32})
        login_id = vault.save_entry(
            site="example.com", username="me@example.com",
            **{"password": LOGIN_PASSWORD, "totp_secret": RFC_SEED_B32})
        result = vault.totp_codes(at_time=59)
        assert result["generated_at"] == 59
        by_id = {c["entry_id"]: c for c in result["codes"]}
        assert set(by_id) == {solo_id, login_id}
        solo = by_id[solo_id]
        assert solo["kind"] == "authenticator"
        assert solo["label"] == "octocat"
        assert solo["issuer"] == "GitHub"
        assert solo["code"] == "287082"  # RFC vector at T=59
        assert solo["period"] == 30
        assert solo["seconds_remaining"] == 1
        login = by_id[login_id]
        assert login["kind"] == "login"
        assert login["label"] == "example.com"
        assert login["username"] == "me@example.com"
        assert login["code"] == "287082"

    def test_codes_view_is_audited_per_entry(self, tmp_path):
        vault = make_vault(tmp_path)
        vault.save_totp_entry(label="octocat", issuer="GitHub",
                              **{"secret": RFC_SEED_B32})
        vault.totp_codes(at_time=59)
        audit = audit_text(tmp_path)
        assert audit.count("VAULT_TOTP_ACCESSED") == 1
        assert RFC_SEED_B32 not in audit
        assert "287082" not in audit  # codes are never audit payload

    def test_malformed_stored_login_secret_is_skipped_not_fabricated(
            self, tmp_path):
        # Login-entry TOTP fields historically stored the raw string;
        # a malformed one must not crash the view or invent a code.
        vault = make_vault(tmp_path)
        vault.save_entry(site="example.com", username="me@example.com",
                         **{"password": LOGIN_PASSWORD,
                            "totp_secret": "not base32!!"})
        solo_id = vault.save_totp_entry(
            label="octocat", issuer="GitHub", **{"secret": RFC_SEED_B32})
        result = vault.totp_codes(at_time=59)
        assert [c["entry_id"] for c in result["codes"]] == [solo_id]

    def test_no_entries_no_codes(self, tmp_path):
        vault = make_vault(tmp_path)
        assert vault.totp_codes(at_time=59)["codes"] == []


# ---------------------------------------------------------------------
# Agent isolation: the canary sweep over every agent-visible surface
# ---------------------------------------------------------------------

class StubSession:
    """Duck-typed browser session for the fill service: a GitHub
    login page whose form the vault may fill."""

    task_id = "task-canary-1"
    current_url = "https://github.com/login"
    last_page_content = GITHUB_LOGIN

    def __init__(self):
        self.typed = {}

    async def read_live_page(self):
        return self.current_url, GITHUB_LOGIN

    async def type_text(self, selector, text):
        self.typed[selector] = text

    async def field_value(self, selector):
        return self.typed.get(selector, "")


class TestAgentIsolation:
    def _seed(self, tmp_path):
        vault = make_vault(tmp_path)
        vault.save_entry(
            site="github.com", username="octocat",
            **{"password": LOGIN_PASSWORD, "totp_secret": RFC_SEED_B32})
        vault.save_totp_entry(label="octocat", issuer="GitHub",
                              **{"secret": RFC_SEED_B32})
        return vault

    def test_tool_definitions_carry_nothing(self, tmp_path):
        vault = self._seed(tmp_path)
        service = VaultFillService(
            vault, audit_logger=AuditLogger(
                log_path=tmp_path / "audit.log"))
        tools = create_browser_tools(manager=None, vault_service=service)
        blob = json.dumps([
            {"name": t.name, "description": t.description,
             "input_schema": t.input_schema} for t in tools
        ])
        code_now = totp_code(RFC_SEED_B32, time.time())
        assert RFC_SEED_B32 not in blob
        assert code_now not in blob
        # No tool exists for TOTP at all.
        assert not any("totp" in t.name.lower() for t in tools)

    async def test_fill_result_and_typed_fields_carry_no_totp(self, tmp_path):
        set_browser_policy_loader(
            lambda: BrowserPolicy(vault_autofill=True))
        vault = self._seed(tmp_path)
        service = VaultFillService(
            vault, audit_logger=AuditLogger(
                log_path=tmp_path / "audit.log"),
            approval_timeout_seconds=5.0)
        session = StubSession()
        result = await service.fill_login(session, task_id="task-canary-1")
        assert result["filled"] is True
        code_now = totp_code(RFC_SEED_B32, time.time())
        blob = json.dumps(result) + json.dumps(session.typed)
        assert RFC_SEED_B32 not in blob
        assert code_now not in blob
        # The password was filled; the TOTP code was NOT typed anywhere
        # (agents never enter 2FA codes).
        assert LOGIN_PASSWORD in session.typed.values()
        assert code_now not in session.typed.values()

    def test_list_surfaces_carry_neither_secret_nor_code(self, tmp_path):
        vault = self._seed(tmp_path)
        code_now = totp_code(RFC_SEED_B32, time.time())
        surfaces = {
            "list_entries": json.dumps(vault.list_entries()),
            "entries_for_site": json.dumps(
                vault.entries_for_site("https://github.com/login")),
            "list_totp_entries": json.dumps(vault.list_totp_entries()),
        }
        for name, blob in surfaces.items():
            assert RFC_SEED_B32 not in blob, name
            assert code_now not in blob, name

    def test_audit_and_db_carry_no_secret_or_code(self, tmp_path):
        vault = self._seed(tmp_path)
        # Exercise the owner codes path too: even ITS audit trail
        # must stay metadata-only.
        vault.totp_codes()
        audit = audit_text(tmp_path)
        raw = (tmp_path / "vault.db").read_bytes()
        code_now = totp_code(RFC_SEED_B32, time.time())
        assert RFC_SEED_B32 not in audit
        assert code_now not in audit
        assert RFC_SEED_B32.encode() not in raw
        assert code_now.encode() not in raw


# ---------------------------------------------------------------------
# WebSocket: the owner Settings channel (mirrors TestVaultWebSocket)
# ---------------------------------------------------------------------

class TestTotpWebSocket:
    def setup_method(self):
        import tempfile
        from pathlib import Path as _P
        from unittest.mock import patch

        from aether_engine.app import engine_state
        from aether_engine.vault import store as vault_store

        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_path = _P(self.tmp_dir.name)
        engine_state.token_path = self.tmp_path / "engine.token"
        engine_state.audit_logger.log_path = self.tmp_path / "audit.log"
        engine_state.initialize_auth()
        self._db_patcher = patch(
            "aether_engine.memory.session_store.DB_PATH",
            self.tmp_path / "test_memory.db",
        )
        self._db_patcher.start()
        self._saved_config = engine_state.user_config
        from aether_engine.config import UserConfig

        engine_state.user_config = UserConfig()
        self._vault = CredentialVault(
            db_path=self.tmp_path / "vault.db", protector=XorProtector(),
            audit_logger=AuditLogger(log_path=self.tmp_path / "audit.log"))
        vault_store._vault = self._vault

    def teardown_method(self):
        from aether_engine.app import engine_state
        from aether_engine.vault import store as vault_store

        engine_state.user_config = self._saved_config
        vault_store._vault = None
        self._db_patcher.stop()
        self.tmp_dir.cleanup()

    def _connect(self, client):
        from aether_engine.app import engine_state

        token = engine_state.auth_token
        return client.websocket_connect("/ws/tasks", headers={"Authorization": f"Bearer {token}"})

    def _send(self, ws, event_type, payload):
        ws.send_text(Event(type=event_type, payload=payload).to_json())
        return Event.from_json(ws.receive_text())

    def test_enrol_codes_delete_round_trip(self):
        from starlette.testclient import TestClient

        from aether_engine.app import app

        with TestClient(app) as client, self._connect(client) as ws:
            ws.receive_text()  # HELLO
            resp = self._send(ws, EventType.VAULT_TOTP_SAVE_REQUEST, {
                "id": None, "label": "octocat", "issuer": "GitHub",
                "secret": RFC_SEED_B32,
            })
            assert resp.type == EventType.VAULT_TOTP_SAVE_RESPONSE
            assert resp.payload["success"] is True
            entry_id = resp.payload["id"]
            # The envelope never echoes the secret back.
            assert RFC_SEED_B32 not in resp.to_json()

            resp = self._send(ws, EventType.VAULT_TOTP_LIST_REQUEST, {})
            assert resp.type == EventType.VAULT_TOTP_LIST_RESPONSE
            rows = resp.payload["entries"]
            assert [r["id"] for r in rows] == [entry_id]
            assert RFC_SEED_B32 not in resp.to_json()

            resp = self._send(ws, EventType.VAULT_TOTP_CODES_REQUEST, {})
            assert resp.type == EventType.VAULT_TOTP_CODES_RESPONSE
            codes = resp.payload["codes"]
            assert len(codes) == 1
            assert codes[0]["entry_id"] == entry_id
            assert codes[0]["code"] == totp_code(RFC_SEED_B32, time.time())
            assert 1 <= codes[0]["seconds_remaining"] <= 30
            assert RFC_SEED_B32 not in resp.to_json()
            audit = (self.tmp_path / "audit.log").read_text(
                encoding="utf-8")
            assert "VAULT_TOTP_ACCESSED" in audit
            assert RFC_SEED_B32 not in audit

            resp = self._send(ws, EventType.VAULT_TOTP_DELETE_REQUEST,
                              {"id": entry_id})
            assert resp.payload == {"success": True, "id": entry_id}
            resp = self._send(ws, EventType.VAULT_TOTP_CODES_REQUEST, {})
            assert resp.payload["codes"] == []

    def test_malformed_secret_is_refused_over_ws(self):
        from starlette.testclient import TestClient

        from aether_engine.app import app

        with TestClient(app) as client, self._connect(client) as ws:
            ws.receive_text()  # HELLO
            resp = self._send(ws, EventType.VAULT_TOTP_SAVE_REQUEST, {
                "id": None, "label": "octocat", "issuer": "GitHub",
                "secret": "ABC!0189",
            })
            assert resp.payload["success"] is False
            assert resp.payload["error"]
            assert self._vault.list_totp_entries() == []
