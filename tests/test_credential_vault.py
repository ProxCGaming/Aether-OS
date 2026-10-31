"""Epic 8, E8-6.3: credential vault — encrypted store, site binding,
mediated fill, ask-every-time, agent-saved credentials.

The vault rides the existing encrypted secret store: entries live in
``~/.aether/vault.db`` with passwords encrypted by the same
``BaseProtector`` the provider keys use (DPAPI on Windows / OS
keychain elsewhere) — no third-party password-manager code, no
hand-rolled crypto, no plaintext anywhere.

Retrieval is MEDIATED (grill Q31): a credential is bound to its site
(the web-platform sense — scheme + registrable domain), travels
vault → form field directly, and NEVER enters model context: the
fill tool's arguments carry no secret, its result is a sentence
("Filled the saved credential for github.com"), and the audit log
records metadata only. The zero-leakage tests below use a canary
password and grep every surface the plaintext could escape through —
tool results, WS frames, event-bus pushes, audit lines, error
strings, and the at-rest database bytes.

Playwright is fully faked (the 6.1 FakeDriver pattern); DPAPI is
exercised through an injected test protector, exactly as
tests/test_secret_store.py does for the provider store.
"""
import asyncio
import json
import sqlite3
import time

import pytest

from aether_common.contracts import Event, EventType
from aether_engine.audit import AuditLogger
from aether_engine.browser.policy import (
    BROWSER_TOOL_NAMES,
    BrowserPolicy,
    classify_browser_action,
    evaluate_browser_action,
    set_browser_policy_loader,
)
from aether_engine.browser.session import (
    BrowserSessionManager,
)
from aether_engine.browser.tools import create_browser_tools
from aether_engine.event_bus import event_bus
from aether_engine.secrets.dpapi import BaseProtector, SecretDecryptionError
from aether_engine.tools.registry import ToolRegistry, ToolRisk
from aether_engine.vault.service import (
    VaultFillService,
    reset_vault_service,
)
from aether_engine.vault.store import (
    CredentialVault,
    VaultEntryNotFound,
    VaultValidationError,
    get_credential_vault,
    registrable_domain,
    reset_credential_vault,
    sites_match,
)

CANARY = "Canary!Vault-Passphrase-7f3a9-DO-NOT-LEAK"
CANARY2 = "Canary!Second-Passphrase-51bc2-DO-NOT-LEAK"
TOTP_CANARY = "T0TPCANARYSECRET7F3A9"


# ---------------------------------------------------------------------
# Test protectors (the test_secret_store.py seam)
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


class BrokenUnprotectProtector(XorProtector):
    """Encryption works; every decryption fails (vault unavailable /
    locked / keychain gone) — fills must fail closed."""

    def unprotect(self, data: bytes) -> bytes:
        raise SecretDecryptionError("keychain unavailable")


# ---------------------------------------------------------------------
# Fake browser driver (the 6.1 pattern, plus form-field values)
# ---------------------------------------------------------------------

GITHUB_LOGIN = (
    "<html><head><title>Sign in to GitHub</title></head><body><form>"
    "<label>Username <input type=\"text\" id=\"login_field\" name=\"login\" "
    "autocomplete=\"username\"></label>"
    "<label>Password <input type=\"password\" id=\"password\" name=\"password\" "
    "autocomplete=\"current-password\"></label>"
    "<button type=\"submit\">Sign in</button></form></body></html>"
)
EXAMPLE_LOGIN = (
    "<html><head><title>Sign in</title></head><body><form>"
    "<input type=\"email\" name=\"email\" autocomplete=\"username\">"
    "<input type=\"password\" name=\"pw\">"
    "<button type=\"submit\">Log in</button></form></body></html>"
)
NO_FORM_PAGE = "<html><head><title>Home</title></head><body><h1>Hi</h1></body></html>"


class FakeDriver:
    """In-memory driver: page map, recorded fills, preset field values."""

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


# ---------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------

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


def make_service(tmp_path, vault):
    return VaultFillService(
        vault,
        audit_logger=AuditLogger(log_path=tmp_path / "audit.log"),
        approval_timeout_seconds=5.0,
    )


@pytest.fixture(autouse=True)
def _policy_loader_cleanup():
    yield
    set_browser_policy_loader(None)


def enable_autofill():
    set_browser_policy_loader(lambda: BrowserPolicy(vault_autofill=True))


def disable_autofill():
    set_browser_policy_loader(lambda: BrowserPolicy(vault_autofill=False))


async def session_on(manager, url):
    session = manager.get_or_create_active(task_id="task-vault-1")
    await session.navigate(url)
    return session


def audit_text(tmp_path):
    path = tmp_path / "audit.log"
    return path.read_text(encoding="utf-8") if path.exists() else ""


# ---------------------------------------------------------------------
# Site matching (the web-platform "site": registrable domain)
# ---------------------------------------------------------------------

class TestSiteMatching:
    @pytest.mark.parametrize("raw,expected", [
        ("github.com", "github.com"),
        ("https://github.com/login", "github.com"),
        ("https://www.github.com/login?next=/home", "github.com"),
        ("GITHUB.COM.", "github.com"),
        ("gist.github.com", "github.com"),
        ("https://api.stripe.com:8443/v1", "stripe.com"),
        ("example.co.uk", "example.co.uk"),
        ("https://login.example.co.uk/auth", "example.co.uk"),
        ("localhost", "localhost"),
        ("http://127.0.0.1:8000/login", "127.0.0.1"),
        ("", ""),
    ])
    def test_registrable_domain(self, raw, expected):
        assert registrable_domain(raw) == expected

    @pytest.mark.parametrize("entry,page", [
        ("github.com", "https://github.com/login"),
        ("github.com", "https://gist.github.com/login"),
        ("https://github.com", "https://www.github.com/session"),
        ("example.com", "http://example.com/login"),
        ("example.co.uk", "https://login.example.co.uk/"),
    ])
    def test_matching_pairs(self, entry, page):
        assert sites_match(entry, page) is True

    @pytest.mark.parametrize("entry,page", [
        # A different site entirely.
        ("github.com", "https://gitlab.com/login"),
        # Lookalikes: the entry's domain as a subdomain of evil.com.
        ("github.com", "https://github.com.evil.com/login"),
        ("example.com", "https://example.com.evil.com/"),
        # Lookalike: the entry's domain glued to an attacker prefix.
        ("github.com", "https://evilgithub.com/login"),
        ("example.com", "https://evil-example.com/"),
        # Not a web page at all.
        ("github.com", "about:blank"),
        ("github.com", ""),
    ])
    def test_refused_pairs(self, entry, page):
        assert sites_match(entry, page) is False


# ---------------------------------------------------------------------
# Store: encryption at rest, metadata-only listing, CRUD
# ---------------------------------------------------------------------

class TestCredentialVaultStore:
    def test_save_and_reveal_round_trip(self, tmp_path):
        vault = make_vault(tmp_path)
        entry_id = vault.save_entry(
            site="github.com", username="octocat", password=CANARY)
        assert entry_id
        assert vault.reveal_password(entry_id) == CANARY

    def test_site_is_normalised_to_the_registrable_domain(self, tmp_path):
        vault = make_vault(tmp_path)
        entry_id = vault.save_entry(
            site="https://www.github.com/login", username="octocat",
            password=CANARY)
        entry = vault.get_entry(entry_id)
        assert entry["site"] == "github.com"

    def test_list_entries_is_metadata_only(self, tmp_path):
        vault = make_vault(tmp_path)
        vault.save_entry(site="github.com", username="octocat",
                         password=CANARY, ask_every_time=True,
                         totp_secret=TOTP_CANARY)
        entries = vault.list_entries()
        assert len(entries) == 1
        entry = entries[0]
        assert set(entry) == {
            "id", "site", "username", "ask_every_time", "created_at",
            "created_by", "last_used_at",
        }
        assert entry["site"] == "github.com"
        assert entry["username"] == "octocat"
        assert entry["ask_every_time"] is True
        assert entry["created_by"] == "user"
        assert entry["last_used_at"] is None
        blob = json.dumps(entries)
        assert CANARY not in blob
        assert TOTP_CANARY not in blob

    def test_password_is_encrypted_at_rest(self, tmp_path):
        vault = make_vault(tmp_path)
        vault.save_entry(site="github.com", username="octocat", password=CANARY)
        raw = (tmp_path / "vault.db").read_bytes()
        assert CANARY.encode() not in raw
        with sqlite3.connect(tmp_path / "vault.db") as conn:
            (blob,) = conn.execute(
                "SELECT encrypted_password FROM vault_entries").fetchone()
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert CANARY.encode() not in bytes(blob)
        assert version == 1

    def test_update_preserves_creator_and_can_keep_password(self, tmp_path):
        vault = make_vault(tmp_path)
        entry_id = vault.save_entry(
            site="github.com", username="octocat", password=CANARY,
            created_by="agent")
        vault.save_entry(entry_id=entry_id, site="github.com",
                         username="octocat", password=None,
                         ask_every_time=True)
        entry = vault.get_entry(entry_id)
        assert entry["created_by"] == "agent"
        assert entry["ask_every_time"] is True
        assert vault.reveal_password(entry_id) == CANARY
        vault.save_entry(entry_id=entry_id, site="github.com",
                         username="octocat", password=CANARY2)
        assert vault.reveal_password(entry_id) == CANARY2

    def test_delete(self, tmp_path):
        vault = make_vault(tmp_path)
        entry_id = vault.save_entry(
            site="github.com", username="octocat", password=CANARY)
        assert vault.delete_entry(entry_id) is True
        assert vault.delete_entry(entry_id) is False
        assert vault.list_entries() == []
        with pytest.raises(VaultEntryNotFound):
            vault.get_entry(entry_id)

    def test_validation(self, tmp_path):
        vault = make_vault(tmp_path)
        with pytest.raises(VaultValidationError):
            vault.save_entry(site="", username="octocat", password=CANARY)
        with pytest.raises(VaultValidationError):
            vault.save_entry(site="github.com", username="", password=CANARY)
        with pytest.raises(VaultValidationError):
            vault.save_entry(site="github.com", username="octocat", password="")
        with pytest.raises(VaultValidationError):
            vault.save_entry(site="github.com", username="octocat",
                             password=CANARY, created_by="mallory")
        with pytest.raises(VaultEntryNotFound):
            vault.save_entry(entry_id="nope", site="github.com",
                             username="octocat", password=CANARY)

    def test_entries_for_site_matches_registrable_domain(self, tmp_path):
        vault = make_vault(tmp_path)
        vault.save_entry(site="github.com", username="octocat", password=CANARY)
        vault.save_entry(site="gitlab.com", username="octocat", password=CANARY2)
        hits = vault.entries_for_site("https://gist.github.com/login")
        assert [h["username"] for h in hits] == ["octocat"]
        assert hits[0]["site"] == "github.com"
        assert vault.entries_for_site("https://github.com.evil.com/") == []

    def test_mark_used(self, tmp_path):
        vault = make_vault(tmp_path)
        entry_id = vault.save_entry(
            site="github.com", username="octocat", password=CANARY)
        before = int(time.time())
        vault.mark_used(entry_id)
        entry = vault.get_entry(entry_id)
        assert entry["last_used_at"] is not None
        assert entry["last_used_at"] >= before

    def test_totp_is_stored_encrypted_and_owner_only(self, tmp_path):
        vault = make_vault(tmp_path)
        entry_id = vault.save_entry(
            site="github.com", username="octocat", password=CANARY,
            totp_secret=TOTP_CANARY)
        raw = (tmp_path / "vault.db").read_bytes()
        assert TOTP_CANARY.encode() not in raw
        # The owner's own accessor returns it (Settings manual use);
        # nothing else in this suite — fill, list, tools — ever does.
        assert vault.get_totp_secret(entry_id) == TOTP_CANARY
        assert vault.get_totp_secret(
            vault.save_entry(site="x.com", username="u", password=CANARY)
        ) is None

    def test_singleton_round_trip(self, tmp_path):
        reset_credential_vault()
        try:
            vault = get_credential_vault()
            assert vault is get_credential_vault()
        finally:
            reset_credential_vault()


# ---------------------------------------------------------------------
# Mediated fill
# ---------------------------------------------------------------------

class TestMediatedFill:
    async def _github_session(self, tmp_path, driver=None):
        driver = driver or FakeDriver(
            {"https://github.com/login": ("Sign in to GitHub", GITHUB_LOGIN)})
        manager = make_manager(tmp_path, driver)
        session = await session_on(manager, "https://github.com/login")
        return session, driver

    async def test_fill_happy_path_and_canary_sweep(self, tmp_path):
        enable_autofill()
        vault = make_vault(tmp_path)
        vault.save_entry(site="github.com", username="octocat", password=CANARY)
        session, driver = await self._github_session(tmp_path)
        service = make_service(tmp_path, vault)

        result = await service.fill_login(session)

        assert result["filled"] is True
        assert result["site"] == "github.com"
        # The password reached exactly one place: the page's password
        # field, via the driver — the definition of a mediated fill.
        assert ("fill", "#password", CANARY) in driver.calls
        assert ("fill", "#login_field", "octocat") in driver.calls
        # …and NOWHERE else: not the result the model sees…
        assert CANARY not in json.dumps(result)
        # …not the audit trail (metadata only)…
        text = audit_text(tmp_path)
        assert "VAULT_CREDENTIAL_FILLED" in text
        assert CANARY not in text
        # …and last_used moved.
        entry = vault.list_entries()[0]
        assert entry["last_used_at"] is not None

    async def test_fill_through_the_tool_and_registry(self, tmp_path):
        enable_autofill()
        vault = make_vault(tmp_path)
        vault.save_entry(site="github.com", username="octocat", password=CANARY)
        driver = FakeDriver(
            {"https://github.com/login": ("Sign in to GitHub", GITHUB_LOGIN)})
        manager = make_manager(tmp_path, driver)
        service = make_service(tmp_path, vault)
        registry = ToolRegistry()
        for tool in create_browser_tools(manager, service):
            registry.register(tool)
        await registry.execute(
            "browser_navigate", {"url": "https://github.com/login"},
            approved=True, audit_logger=AuditLogger(tmp_path / "audit.log"))
        # The model supplies NO secret — the tool takes no password arg.
        result = await registry.execute(
            "browser_fill_login", {}, approved=True,
            audit_logger=AuditLogger(tmp_path / "audit.log"))
        assert isinstance(result, str)
        assert "github.com" in result
        assert CANARY not in result
        assert ("fill", "#password", CANARY) in driver.calls
        assert CANARY not in audit_text(tmp_path)

    async def test_fill_by_entry_id_and_site_mismatch_refusal(self, tmp_path):
        enable_autofill()
        vault = make_vault(tmp_path)
        entry_id = vault.save_entry(
            site="github.com", username="octocat", password=CANARY)
        session, driver = await self._github_session(tmp_path)
        service = make_service(tmp_path, vault)
        result = await service.fill_login(session, entry_id=entry_id)
        assert result["filled"] is True
        # The same entry_id against a different site's page is refused.
        driver.add_page("https://gitlab.com/login",
                        "Sign in", GITHUB_LOGIN)
        await session.navigate("https://gitlab.com/login")
        driver.calls.clear()
        result = await service.fill_login(session, entry_id=entry_id)
        assert result["filled"] is False
        assert result["reason"] == "site_mismatch"
        assert driver.fills() == []
        assert CANARY not in json.dumps(result)

    @pytest.mark.parametrize("url", [
        "https://gitlab.com/login",
        "https://github.com.evil.com/login",
        "https://evilgithub.com/login",
    ])
    async def test_lookalike_and_foreign_sites_are_refused(self, tmp_path, url):
        enable_autofill()
        vault = make_vault(tmp_path)
        vault.save_entry(site="github.com", username="octocat", password=CANARY)
        driver = FakeDriver({url: ("Sign in", GITHUB_LOGIN)})
        manager = make_manager(tmp_path, driver)
        session = await session_on(manager, url)
        service = make_service(tmp_path, vault)
        result = await service.fill_login(session)
        assert result["filled"] is False
        assert driver.fills() == []
        assert CANARY not in json.dumps(result)
        assert "VAULT_FILL_REFUSED" in audit_text(tmp_path)

    async def test_subdomain_of_the_saved_site_fills(self, tmp_path):
        enable_autofill()
        vault = make_vault(tmp_path)
        vault.save_entry(site="github.com", username="octocat", password=CANARY)
        driver = FakeDriver(
            {"https://gist.github.com/login": ("Sign in", GITHUB_LOGIN)})
        manager = make_manager(tmp_path, driver)
        session = await session_on(manager, "https://gist.github.com/login")
        service = make_service(tmp_path, vault)
        result = await service.fill_login(session)
        assert result["filled"] is True

    async def test_autofill_toggle_off_refuses(self, tmp_path):
        disable_autofill()
        vault = make_vault(tmp_path)
        vault.save_entry(site="github.com", username="octocat", password=CANARY)
        session, driver = await self._github_session(tmp_path)
        service = make_service(tmp_path, vault)
        result = await service.fill_login(session)
        assert result["filled"] is False
        assert result["reason"] == "autofill_disabled"
        assert driver.fills() == []

    async def test_no_credential_for_site_refuses(self, tmp_path):
        enable_autofill()
        vault = make_vault(tmp_path)
        session, driver = await self._github_session(tmp_path)
        service = make_service(tmp_path, vault)
        result = await service.fill_login(session)
        assert result["filled"] is False
        assert result["reason"] == "no_credential"
        assert driver.fills() == []

    async def test_multiple_entries_need_a_username_hint(self, tmp_path):
        enable_autofill()
        vault = make_vault(tmp_path)
        vault.save_entry(site="github.com", username="octocat", password=CANARY)
        vault.save_entry(site="github.com", username="hubot", password=CANARY2)
        session, driver = await self._github_session(tmp_path)
        service = make_service(tmp_path, vault)
        result = await service.fill_login(session)
        assert result["filled"] is False
        assert result["reason"] == "multiple_credentials"
        assert driver.fills() == []
        result = await service.fill_login(session, username="hubot")
        assert result["filled"] is True
        assert ("fill", "#password", CANARY2) in driver.calls
        assert CANARY2 not in json.dumps(result)

    async def test_page_without_a_login_form_refuses(self, tmp_path):
        enable_autofill()
        vault = make_vault(tmp_path)
        vault.save_entry(site="example.com", username="u@example.com",
                         password=CANARY)
        driver = FakeDriver(
            {"https://example.com/": ("Home", NO_FORM_PAGE)})
        manager = make_manager(tmp_path, driver)
        session = await session_on(manager, "https://example.com/")
        service = make_service(tmp_path, vault)
        result = await service.fill_login(session)
        assert result["filled"] is False
        assert result["reason"] == "no_login_form"
        assert driver.fills() == []

    async def test_parked_session_refuses(self, tmp_path):
        enable_autofill()
        vault = make_vault(tmp_path)
        vault.save_entry(site="github.com", username="octocat", password=CANARY)
        session, driver = await self._github_session(tmp_path)
        session.pause()
        service = make_service(tmp_path, vault)
        result = await service.fill_login(session)
        assert result["filled"] is False
        assert driver.fills() == []
        assert CANARY not in json.dumps(result)

    async def test_vault_unavailable_fails_closed(self, tmp_path):
        enable_autofill()
        vault = make_vault(tmp_path, protector=BrokenUnprotectProtector())
        vault.save_entry(site="github.com", username="octocat", password=CANARY)
        session, driver = await self._github_session(tmp_path)
        service = make_service(tmp_path, vault)
        result = await service.fill_login(session)
        assert result["filled"] is False
        assert result["reason"] == "vault_unavailable"
        assert driver.fills() == []
        assert CANARY not in json.dumps(result)
        assert CANARY not in audit_text(tmp_path)


# ---------------------------------------------------------------------
# Ask-every-time: the VAULT_FILL_APPROVAL round trip
# ---------------------------------------------------------------------

class TestAskEveryTime:
    async def _setup(self, tmp_path):
        enable_autofill()
        vault = make_vault(tmp_path)
        vault.save_entry(site="github.com", username="octocat",
                         password=CANARY, ask_every_time=True)
        driver = FakeDriver(
            {"https://github.com/login": ("Sign in to GitHub", GITHUB_LOGIN)})
        manager = make_manager(tmp_path, driver)
        session = await session_on(manager, "https://github.com/login")
        service = make_service(tmp_path, vault)
        queue: asyncio.Queue = asyncio.Queue()
        event_bus.subscribe("task-vault-1", queue)
        return vault, session, driver, service, queue

    async def test_approval_granted_fills_and_payload_is_clean(self, tmp_path):
        _vault, session, driver, service, queue = await self._setup(tmp_path)
        fill_task = asyncio.create_task(service.fill_login(session))
        request = await asyncio.wait_for(queue.get(), timeout=5)
        assert request.type == EventType.VAULT_FILL_APPROVAL_REQUEST
        # The approval prompt names the entry — never the secret.
        assert request.payload["site"] == "github.com"
        assert request.payload["username"] == "octocat"
        assert request.payload["task_id"] == "task-vault-1"
        assert CANARY not in request.to_json()
        assert service.resolve_fill_approval(
            request.payload["request_id"], True) is True
        result = await asyncio.wait_for(fill_task, timeout=5)
        assert result["filled"] is True
        assert ("fill", "#password", CANARY) in driver.calls
        assert CANARY not in audit_text(tmp_path)

    async def test_approval_denied_means_no_fill(self, tmp_path):
        _vault, session, driver, service, queue = await self._setup(tmp_path)
        fill_task = asyncio.create_task(service.fill_login(session))
        request = await asyncio.wait_for(queue.get(), timeout=5)
        assert service.resolve_fill_approval(
            request.payload["request_id"], False) is True
        result = await asyncio.wait_for(fill_task, timeout=5)
        assert result["filled"] is False
        assert result["reason"] == "approval_denied"
        assert driver.fills() == []

    async def test_no_answer_within_the_timeout_means_no_fill(self, tmp_path):
        _vault, session, driver, service, queue = await self._setup(tmp_path)
        service._approval_timeout_seconds = 0.2
        result = await service.fill_login(session)
        assert result["filled"] is False
        assert result["reason"] == "approval_timeout"
        assert driver.fills() == []
        # A late answer resolves nothing.
        request = queue.get_nowait()
        assert service.resolve_fill_approval(
            request.payload["request_id"], True) is False

    async def test_non_ask_entry_never_prompts(self, tmp_path):
        enable_autofill()
        vault = make_vault(tmp_path)
        vault.save_entry(site="github.com", username="octocat",
                         password=CANARY, ask_every_time=False)
        driver = FakeDriver(
            {"https://github.com/login": ("Sign in to GitHub", GITHUB_LOGIN)})
        manager = make_manager(tmp_path, driver)
        session = await session_on(manager, "https://github.com/login")
        service = make_service(tmp_path, vault)
        queue: asyncio.Queue = asyncio.Queue()
        event_bus.subscribe("task-vault-1", queue)
        result = await service.fill_login(session)
        assert result["filled"] is True
        assert queue.empty()


# ---------------------------------------------------------------------
# Agent-saved credentials (signup flow)
# ---------------------------------------------------------------------

class TestAgentSave:
    async def _signup_session(self, tmp_path, *, password=CANARY,
                              username="newdev"):
        driver = FakeDriver(
            {"https://example.com/signup": ("Create account", EXAMPLE_LOGIN)})
        driver.field_values["input[name=\"email\"]"] = username
        driver.field_values["input[name=\"pw\"]"] = password
        manager = make_manager(tmp_path, driver)
        session = await session_on(manager, "https://example.com/signup")
        return session, driver

    async def test_agent_save_lands_in_the_vault_for_review(self, tmp_path):
        vault = make_vault(tmp_path)
        session, _driver = await self._signup_session(tmp_path)
        service = make_service(tmp_path, vault)
        result = await service.save_login_from_page(session)
        assert result["saved"] is True
        entries = vault.list_entries()
        assert len(entries) == 1
        assert entries[0]["site"] == "example.com"
        assert entries[0]["username"] == "newdev"
        assert entries[0]["created_by"] == "agent"
        assert vault.reveal_password(entries[0]["id"]) == CANARY
        # The agent's own report carries no secret.
        assert CANARY not in json.dumps(result)
        assert CANARY not in audit_text(tmp_path)
        assert "VAULT_CREDENTIAL_SAVED" in audit_text(tmp_path)

    async def test_save_through_the_tool(self, tmp_path):
        vault = make_vault(tmp_path)
        driver = FakeDriver(
            {"https://example.com/signup": ("Create account", EXAMPLE_LOGIN)})
        driver.field_values["input[name=\"email\"]"] = "newdev"
        driver.field_values["input[name=\"pw\"]"] = CANARY
        manager = make_manager(tmp_path, driver)
        service = make_service(tmp_path, vault)
        registry = ToolRegistry()
        for tool in create_browser_tools(manager, service):
            registry.register(tool)
        await registry.execute(
            "browser_navigate", {"url": "https://example.com/signup"},
            approved=True, audit_logger=AuditLogger(tmp_path / "audit.log"))
        result = await registry.execute(
            "browser_save_login", {}, approved=True,
            audit_logger=AuditLogger(tmp_path / "audit.log"))
        assert "example.com" in result
        assert CANARY not in result
        assert vault.list_entries()[0]["created_by"] == "agent"

    async def test_agent_save_never_overwrites_a_user_entry(self, tmp_path):
        vault = make_vault(tmp_path)
        user_id = vault.save_entry(site="example.com", username="newdev",
                                   password=CANARY2, created_by="user")
        session, _driver = await self._signup_session(tmp_path)
        service = make_service(tmp_path, vault)
        result = await service.save_login_from_page(session)
        assert result["saved"] is False
        assert result["reason"] == "already_saved_by_user"
        assert vault.reveal_password(user_id) == CANARY2

    async def test_agent_save_updates_its_own_entry(self, tmp_path):
        vault = make_vault(tmp_path)
        session, driver = await self._signup_session(tmp_path)
        service = make_service(tmp_path, vault)
        assert (await service.save_login_from_page(session))["saved"] is True
        driver.field_values["input[name=\"pw\"]"] = CANARY2
        assert (await service.save_login_from_page(session))["saved"] is True
        entries = vault.list_entries()
        assert len(entries) == 1
        assert vault.reveal_password(entries[0]["id"]) == CANARY2

    async def test_save_with_an_empty_form_refuses(self, tmp_path):
        vault = make_vault(tmp_path)
        session, _driver = await self._signup_session(tmp_path, password="")
        service = make_service(tmp_path, vault)
        result = await service.save_login_from_page(session)
        assert result["saved"] is False
        assert vault.list_entries() == []


# ---------------------------------------------------------------------
# Gate integration: the vault tools meet the 6.2 confirmation gate
# ---------------------------------------------------------------------

class TestGateIntegration:
    def test_vault_tools_are_registered_browser_writes(self):
        reset_vault_service()
        try:
            tools = {t.name: t for t in create_browser_tools()}
        finally:
            reset_vault_service()
        for name in ("browser_fill_login", "browser_save_login"):
            assert name in tools
            assert tools[name].risk == ToolRisk.WRITE
            assert tools[name].source == "browser"
            assert name in BROWSER_TOOL_NAMES
        # No tool may ever expose a TOTP secret to the model.
        assert not any("totp" in name for name in tools)

    def test_fill_tool_takes_no_password_argument(self):
        reset_vault_service()
        try:
            tools = {t.name: t for t in create_browser_tools()}
        finally:
            reset_vault_service()
        props = tools["browser_fill_login"].input_schema["properties"]
        assert "password" not in props
        assert "text" not in props

    def test_gate_lets_vault_tools_reach_the_vault_service(self):
        # At the 6.2 gate the vault tools classify "free": their
        # authorisation lives in the vault service itself (autofill
        # toggle, site binding, ask-every-time) which fails closed.
        # The master switch still denies them when no session runs.
        for name in ("browser_fill_login", "browser_save_login"):
            action = classify_browser_action(
                name, {}, url="https://github.com/login",
                title="Sign in to GitHub", content=GITHUB_LOGIN)
            decision = evaluate_browser_action(
                BrowserPolicy(), action, task_id="t1", session_running=True)
            assert decision.verdict == "allow"
            decision = evaluate_browser_action(
                BrowserPolicy(enabled=False), action, task_id="t1",
                session_running=False)
            assert decision.verdict == "deny"


# ---------------------------------------------------------------------
# WebSocket surface: LIST / SAVE / DELETE + fill-approval answers
# ---------------------------------------------------------------------

class TestVaultWebSocket:
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
        reset_vault_service()

    def teardown_method(self):
        from aether_engine.app import engine_state
        from aether_engine.vault import store as vault_store

        engine_state.user_config = self._saved_config
        vault_store._vault = None
        reset_vault_service()
        self._db_patcher.stop()
        self.tmp_dir.cleanup()

    def _connect(self, client):
        from aether_engine.app import engine_state

        token = engine_state.token_path.read_text(encoding="utf-8").strip()
        return client.websocket_connect(f"/ws/tasks?token={token}")

    def _send(self, ws, event_type, payload):
        ws.send_text(Event(type=event_type, payload=payload).to_json())
        return Event.from_json(ws.receive_text())

    def test_save_list_delete_round_trip_without_leaks(self):
        from starlette.testclient import TestClient

        from aether_engine.app import app

        with TestClient(app) as client, self._connect(client) as ws:
            ws.receive_text()  # HELLO
            resp = self._send(ws, EventType.VAULT_ENTRY_SAVE_REQUEST, {
                "id": None, "site": "github.com", "username": "octocat",
                "password": CANARY, "ask_every_time": True,
                "totp_secret": TOTP_CANARY,
            })
            assert resp.type == EventType.VAULT_ENTRY_SAVE_RESPONSE
            assert resp.payload["success"] is True
            entry_id = resp.payload["id"]
            assert CANARY not in resp.to_json()
            assert TOTP_CANARY not in resp.to_json()

            resp = self._send(ws, EventType.VAULT_ENTRY_LIST_REQUEST, {})
            assert resp.type == EventType.VAULT_ENTRY_LIST_RESPONSE
            entries = resp.payload["entries"]
            assert len(entries) == 1
            assert entries[0]["id"] == entry_id
            assert entries[0]["created_by"] == "user"
            assert entries[0]["ask_every_time"] is True
            assert CANARY not in resp.to_json()
            assert TOTP_CANARY not in resp.to_json()
            assert self._vault.reveal_password(entry_id) == CANARY

            resp = self._send(ws, EventType.VAULT_ENTRY_DELETE_REQUEST,
                              {"id": entry_id})
            assert resp.payload == {"success": True, "id": entry_id}
            resp = self._send(ws, EventType.VAULT_ENTRY_LIST_REQUEST, {})
            assert resp.payload["entries"] == []
            audit = (self.tmp_path / "audit.log").read_text(encoding="utf-8")
            assert CANARY not in audit
            assert TOTP_CANARY not in audit

    def test_save_validation_failure_is_a_clean_refusal(self):
        from starlette.testclient import TestClient

        from aether_engine.app import app

        with TestClient(app) as client, self._connect(client) as ws:
            ws.receive_text()  # HELLO
            resp = self._send(ws, EventType.VAULT_ENTRY_SAVE_REQUEST, {
                "id": None, "site": "github.com", "username": "octocat",
                "password": "", "ask_every_time": False,
            })
            assert resp.payload["success"] is False
            assert resp.payload["id"] == ""
            assert self._vault.list_entries() == []

    def test_fill_approval_response_for_unknown_request_is_tolerated(self):
        from starlette.testclient import TestClient

        from aether_engine.app import app

        with TestClient(app) as client, self._connect(client) as ws:
            ws.receive_text()  # HELLO
            ws.send_text(Event(
                type=EventType.VAULT_FILL_APPROVAL_RESPONSE,
                payload={"request_id": "no-such-request", "approved": True},
            ).to_json())
            # The connection stays healthy and keeps answering.
            resp = self._send(ws, EventType.VAULT_ENTRY_LIST_REQUEST, {})
            assert resp.type == EventType.VAULT_ENTRY_LIST_RESPONSE
