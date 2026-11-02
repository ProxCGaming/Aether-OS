"""Encrypted credential storage for the vault (E8-6.3).

Mirrors :class:`aether_engine.secrets.storage.SecretStore`: a small
SQLite database under ``~/.aether`` whose secret columns hold only
protector-encrypted bytes. The schema is versioned with
``PRAGMA user_version`` (v1) so future migrations have a hook.

What is stored per entry: the site it is bound to, normalised to its
registrable domain (the web-platform meaning of "site" — so
``gist.github.com`` is the same site as ``github.com`` while
``github.com.evil.com`` is ``evil.com`` and never matches), the
username, the encrypted password, an optional encrypted TOTP secret
(the owner's own manual use in Settings only — no agent path reads
it), the ask-every-time flag, who created the entry (``"user"`` or
``"agent"`` — agent-created signups land here for the owner's
review), and use timestamps.

Listing is metadata-only by construction: :meth:`list_entries`
selects no secret column, so a password cannot leak through the
Settings surface. The plaintext exists in exactly one method,
:meth:`reveal_password`, called only by the mediated fill service.
"""
from __future__ import annotations

import sqlite3
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from publicsuffix2 import get_tld

from ..secrets.dpapi import BaseProtector, WindowsDPAPIProtector
from .totp import (
    PERIOD_SECONDS,
    TotpValidationError,
    normalize_base32_secret,
    seconds_remaining,
    totp_code,
)

if TYPE_CHECKING:
    from ..audit import AuditLogger

VAULT_DB_VERSION = 1

#: Site identity comes from the Public Suffix List, resolved offline
#: against the snapshot bundled with publicsuffix2 — PRIVATE section
#: included (github.io, pages.dev, …). The hand-maintained suffix set
#: this replaces missed exactly those suffixes, which conflated every
#: tenant of a private suffix into one "site" (M06): an entry saved
#: for alice.github.io also matched mallory.github.io.


class VaultError(Exception):
    """Base class for vault storage failures."""


class VaultEntryNotFound(VaultError):
    """The requested entry does not exist."""


class VaultValidationError(VaultError):
    """A save was attempted with fields the vault refuses to store."""


def _extract_host(raw: str) -> str:
    """Hostname out of a URL or a bare host[:port][/path] string."""
    text = (raw or "").strip()
    if not text:
        return ""
    if "://" in text:
        return urlparse(text).hostname or ""
    # Bare host: strip any path/query and userinfo, keep host:port.
    host = text.split("/")[0].split("?")[0].rsplit("@", 1)[-1]
    if host.startswith("["):  # IPv6 literal: keep it whole.
        return host
    return host.split(":")[0]


def registrable_domain(raw: str) -> str:
    """The registrable domain (eTLD+1) of a URL or hostname.

    Normalises case, trailing dots, ports and IDNA so comparisons
    are apples-to-apples. IP literals and single-label hosts
    (``localhost``) are their own registrable domain. Returns ""
    when nothing parseable remains.
    """
    host = _extract_host(raw).strip().lower().rstrip(".")
    if not host:
        return ""
    if host.startswith("["):
        return host
    try:
        host = host.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        pass
    labels = [label for label in host.split(".") if label]
    if not labels:
        return ""
    if len(labels) == 1 or all(label.isdigit() for label in labels):
        return ".".join(labels)
    try:
        suffix = get_tld(host)
    except Exception:  # noqa: BLE001 — a PSL lookup failure must fail closed, never open
        suffix = ""
    if not suffix or suffix == host or not host.endswith("." + suffix):
        # Unresolvable, or the host IS a public suffix (github.io,
        # co.ke): the host is its own bucket. An entry bound to a
        # bare public suffix can never select a credential for any
        # tenant beneath it, and an unresolved host only ever matches
        # itself — exact-host matching, never a guessed parent.
        return host
    label = host[: -(len(suffix) + 1)].rsplit(".", 1)[-1]
    if not label:
        return host
    return f"{label}.{suffix}"


def sites_match(entry_site: str, page: str) -> bool:
    """True when ``page`` (URL or host) is on ``entry_site``'s site.

    Both sides reduce to registrable domains; equality is the whole
    rule, which is what refuses the lookalikes: an entry for
    ``github.com`` matches ``gist.github.com`` but never
    ``github.com.evil.com`` (site: ``evil.com``), ``gitlab.com``, or
    ``evilgithub.com``.
    """
    entry_domain = registrable_domain(entry_site)
    if not entry_domain:
        return False
    return entry_domain == registrable_domain(page)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS vault_entries (
    id TEXT PRIMARY KEY,
    site TEXT NOT NULL,
    username TEXT NOT NULL,
    encrypted_password BLOB NOT NULL,
    encrypted_totp BLOB,
    ask_every_time INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    last_used_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_vault_site ON vault_entries(site);

-- Standalone authenticator entries (Open Point 14, decided YES by
-- the owner 1 Nov 2026): TOTP secrets enrolled for the codes view
-- alone, with no login attached. A separate table on purpose —
-- these rows carry no password and must never become fill
-- candidates for the mediated fill service, which reads
-- vault_entries only. Added with IF NOT EXISTS so an existing v1
-- database gains it in place; the schema version stays 1.
CREATE TABLE IF NOT EXISTS vault_totp_entries (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    issuer TEXT NOT NULL DEFAULT '',
    encrypted_secret BLOB NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
"""


class CredentialVault:
    """Encrypted credential entries in ``~/.aether/vault.db``."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        protector: BaseProtector | None = None,
        audit_logger: AuditLogger | None = None,
    ) -> None:
        if db_path is None:
            db_path = Path.home() / ".aether" / "vault.db"
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.protector = protector or WindowsDPAPIProtector()
        self._audit_logger = audit_logger
        self._init_schema()

    # -- plumbing -----------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            conn.execute(f"PRAGMA user_version = {VAULT_DB_VERSION}")

    def _audit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._audit_logger is not None:
            self._audit_logger.log_event(event_type, payload)

    # -- writes ---------------------------------------------------------

    def save_entry(
        self,
        *,
        site: str,
        username: str,
        password: str | None,
        ask_every_time: bool = False,
        created_by: str = "user",
        totp_secret: str | None = None,
        entry_id: str | None = None,
    ) -> str:
        """Create or update an entry; returns its id.

        The site is normalised to its registrable domain at save
        time, so binding never depends on how the site was typed.
        On update, ``password=None`` keeps the stored password (the
        Settings edit form leaves the field blank to mean exactly
        that), and ``totp_secret=None`` keeps the stored TOTP secret;
        the original creator and creation time are always preserved.
        """
        domain = registrable_domain(site)
        if not domain:
            raise VaultValidationError("A site is required.")
        if not (username or "").strip():
            raise VaultValidationError("A username is required.")
        if created_by not in ("user", "agent"):
            raise VaultValidationError("created_by must be 'user' or 'agent'.")
        now = int(time.time())
        with self._connect() as conn:
            existing = None
            if entry_id is not None:
                existing = conn.execute(
                    "SELECT * FROM vault_entries WHERE id = ?",
                    (entry_id,),
                ).fetchone()
                if existing is None:
                    raise VaultEntryNotFound(f"No vault entry {entry_id!r}.")
            if password is None:
                if existing is None:
                    raise VaultValidationError("A password is required.")
                encrypted_password = existing["encrypted_password"]
            elif password == "":
                raise VaultValidationError("A password is required.")
            else:
                encrypted_password = self.protector.protect(
                    password.encode("utf-8"))
            if totp_secret is None:
                encrypted_totp = (
                    existing["encrypted_totp"] if existing is not None else None
                )
            elif totp_secret == "":
                encrypted_totp = None
            else:
                encrypted_totp = self.protector.protect(
                    totp_secret.encode("utf-8"))
            if existing is None:
                new_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO vault_entries (id, site, username,"
                    " encrypted_password, encrypted_totp, ask_every_time,"
                    " created_by, created_at, updated_at, last_used_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                    (new_id, domain, username.strip(), encrypted_password,
                     encrypted_totp, 1 if ask_every_time else 0,
                     created_by, now, now),
                )
                return new_id
            conn.execute(
                "UPDATE vault_entries SET site = ?, username = ?,"
                " encrypted_password = ?, encrypted_totp = ?,"
                " ask_every_time = ?, updated_at = ? WHERE id = ?",
                (domain, username.strip(), encrypted_password,
                 encrypted_totp, 1 if ask_every_time else 0, now, entry_id),
            )
            return entry_id

    def delete_entry(self, entry_id: str) -> bool:
        """Delete an entry; True when a row was actually removed."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT encrypted_password, encrypted_totp FROM vault_entries"
                " WHERE id = ?", (entry_id,)).fetchone()
            cursor = conn.execute(
                "DELETE FROM vault_entries WHERE id = ?", (entry_id,))
            deleted = cursor.rowcount > 0
        # S2-N3: clean the external store entries (OS keyring on
        # non-Windows) for both secret columns the row carried.
        if deleted and row is not None:
            for blob in row:
                if blob is not None:
                    self.protector.delete(bytes(blob))
        return deleted

    def mark_used(self, entry_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE vault_entries SET last_used_at = ? WHERE id = ?",
                (int(time.time()), entry_id),
            )

    # -- reads (metadata only) ------------------------------------------

    @staticmethod
    def _public(row: sqlite3.Row) -> dict[str, Any]:
        """The contract shape for one entry — no secret columns."""
        return {
            "id": row["id"],
            "site": row["site"],
            "username": row["username"],
            "ask_every_time": bool(row["ask_every_time"]),
            "created_at": row["created_at"],
            "created_by": row["created_by"],
            "last_used_at": row["last_used_at"],
        }

    def get_entry(self, entry_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM vault_entries WHERE id = ?", (entry_id,),
            ).fetchone()
        if row is None:
            raise VaultEntryNotFound(f"No vault entry {entry_id!r}.")
        return self._public(row)

    def list_entries(self) -> list[dict[str, Any]]:
        """Every entry, metadata only — passwords are never listed.

        Selects exactly the columns :meth:`_public` projects and
        nothing else (S2-M9): the secret columns
        (``encrypted_password``, ``encrypted_totp``) are never
        fetched from the database on this path, so listing is
        metadata-only by construction, not by after-the-fact
        projection.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, site, username, ask_every_time, created_at,"
                " created_by, last_used_at FROM vault_entries"
                " ORDER BY site, username",
            ).fetchall()
        return [self._public(row) for row in rows]

    def entries_for_site(self, url_or_host: str) -> list[dict[str, Any]]:
        """Entries bound to the site of ``url_or_host`` (metadata)."""
        domain = registrable_domain(url_or_host)
        if not domain:
            return []
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM vault_entries WHERE site = ?"
                " ORDER BY username",
                (domain,),
            ).fetchall()
        return [self._public(row) for row in rows]

    # -- secrets (narrow, deliberate) -------------------------------------

    def reveal_password(self, entry_id: str) -> str:
        """Decrypt one entry's password.

        INTERNAL — the mediated fill service is the only caller, and
        the plaintext goes from here into the page's password field
        and nowhere else. Raises VaultEntryNotFound for a missing
        entry and SecretDecryptionError when the protector cannot
        decrypt (vault unavailable) — callers fail closed.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT encrypted_password FROM vault_entries WHERE id = ?",
                (entry_id,),
            ).fetchone()
        if row is None:
            raise VaultEntryNotFound(f"No vault entry {entry_id!r}.")
        return self.protector.unprotect(bytes(row[0])).decode("utf-8")

    def get_totp_secret(self, entry_id: str) -> str | None:
        """Decrypt one entry's TOTP secret — OWNER PATH ONLY.

        This exists for the owner's own manual use from Settings.
        No agent tool, fill path, or event ever calls it: agents
        never retrieve TOTP codes (grill Q35 / PRD §E8-6.3).
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT encrypted_totp FROM vault_entries WHERE id = ?",
                (entry_id,),
            ).fetchone()
        if row is None:
            raise VaultEntryNotFound(f"No vault entry {entry_id!r}.")
        if row[0] is None:
            return None
        secret = self.protector.unprotect(bytes(row[0])).decode("utf-8")
        self._audit("VAULT_TOTP_ACCESSED", {"entry_id": entry_id})
        return secret

    # -- authenticator entries (Open Point 14) --------------------------

    def save_totp_entry(
        self,
        *,
        label: str,
        issuer: str = "",
        secret: str | None = None,
        entry_id: str | None = None,
    ) -> str:
        """Create or update a standalone authenticator entry.

        The secret is validated as Base32 and stored in its
        normalised form, encrypted with the same protector as every
        other vault secret; a malformed secret raises
        VaultValidationError and stores nothing. On update,
        ``secret=None`` keeps the stored secret (the Settings edit
        form never receives it, so it cannot send it back); a
        provided secret is re-validated before it replaces
        anything. A label is required — it is how the owner tells
        codes apart in the authenticator view.
        """
        if not (label or "").strip():
            raise VaultValidationError("A label is required.")
        now = int(time.time())
        with self._connect() as conn:
            existing = None
            if entry_id is not None:
                existing = conn.execute(
                    "SELECT * FROM vault_totp_entries WHERE id = ?",
                    (entry_id,),
                ).fetchone()
                if existing is None:
                    raise VaultEntryNotFound(
                        f"No TOTP entry {entry_id!r}.")
            if secret is None:
                if existing is None:
                    raise VaultValidationError("A TOTP secret is required.")
                encrypted_secret = existing["encrypted_secret"]
            else:
                try:
                    normalized = normalize_base32_secret(secret)
                except TotpValidationError as exc:
                    raise VaultValidationError(str(exc)) from exc
                encrypted_secret = self.protector.protect(
                    normalized.encode("utf-8"))
            if existing is None:
                new_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO vault_totp_entries (id, label, issuer,"
                    " encrypted_secret, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (new_id, label.strip(), (issuer or "").strip(),
                     encrypted_secret, now, now),
                )
                return new_id
            conn.execute(
                "UPDATE vault_totp_entries SET label = ?, issuer = ?,"
                " encrypted_secret = ?, updated_at = ? WHERE id = ?",
                (label.strip(), (issuer or "").strip(),
                 encrypted_secret, now, entry_id),
            )
            return entry_id

    @staticmethod
    def _public_totp(row: sqlite3.Row) -> dict[str, Any]:
        """The contract shape for one authenticator entry — the
        secret column is never selected into a listing."""
        return {
            "id": row["id"],
            "label": row["label"],
            "issuer": row["issuer"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list_totp_entries(self) -> list[dict[str, Any]]:
        """Every authenticator entry, metadata only."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM vault_totp_entries"
                " ORDER BY issuer, label",
            ).fetchall()
        return [self._public_totp(row) for row in rows]

    def delete_totp_entry(self, entry_id: str) -> bool:
        """Delete an authenticator entry; True when a row went."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT encrypted_secret FROM vault_totp_entries"
                " WHERE id = ?", (entry_id,)).fetchone()
            cursor = conn.execute(
                "DELETE FROM vault_totp_entries WHERE id = ?",
                (entry_id,))
            deleted = cursor.rowcount > 0
        # S2-N3: keyring cleanup, as in delete_entry.
        if deleted and row is not None and row[0] is not None:
            self.protector.delete(bytes(row[0]))
        return deleted

    def get_totp_entry_secret(self, entry_id: str) -> str:
        """Decrypt one authenticator entry's secret — OWNER PATH ONLY.

        The standalone-entry mirror of :meth:`get_totp_secret`: the
        Settings authenticator surface is the only caller, the
        access is audited, and no agent path can reach it.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT encrypted_secret FROM vault_totp_entries"
                " WHERE id = ?",
                (entry_id,),
            ).fetchone()
        if row is None:
            raise VaultEntryNotFound(f"No TOTP entry {entry_id!r}.")
        secret = self.protector.unprotect(bytes(row[0])).decode("utf-8")
        self._audit("VAULT_TOTP_ACCESSED", {"entry_id": entry_id})
        return secret

    def totp_codes(
        self, at_time: float | None = None,
    ) -> dict[str, Any]:
        """Current codes for every TOTP the owner holds — OWNER ONLY.

        Covers standalone authenticator entries and login entries
        carrying a TOTP secret. Codes are computed here, engine
        side; only the code, its period and the countdown leave
        this method — never a secret. Each secret read goes through
        the owner accessors above, so every view is audited
        (VAULT_TOTP_ACCESSED, one line per entry). A stored login
        secret that is not valid Base32 is skipped: the login form
        historically stored the raw string unchecked, and this view
        must neither crash on it nor fabricate a code from it.
        """
        when = int(time.time()) if at_time is None else int(at_time)
        remaining = seconds_remaining(when)
        codes: list[dict[str, Any]] = []
        for row in self.list_totp_entries():
            secret = self.get_totp_entry_secret(row["id"])
            codes.append({
                "entry_id": row["id"],
                "kind": "authenticator",
                "label": row["label"],
                "issuer": row["issuer"],
                "code": totp_code(secret, when),
                "period": PERIOD_SECONDS,
                "seconds_remaining": remaining,
            })
        with self._connect() as conn:
            login_ids = [r[0] for r in conn.execute(
                "SELECT id FROM vault_entries"
                " WHERE encrypted_totp IS NOT NULL"
                " ORDER BY site, username",
            ).fetchall()]
        by_id = {e["id"]: e for e in self.list_entries()}
        for login_id in login_ids:
            secret = self.get_totp_secret(login_id)
            if secret is None:
                continue
            try:
                code = totp_code(secret, when)
            except TotpValidationError:
                continue
            meta = by_id.get(login_id, {})
            codes.append({
                "entry_id": login_id,
                "kind": "login",
                "label": meta.get("site", ""),
                "issuer": "",
                "username": meta.get("username", ""),
                "code": code,
                "period": PERIOD_SECONDS,
                "seconds_remaining": remaining,
            })
        return {"generated_at": when, "codes": codes}


_vault: CredentialVault | None = None


def get_credential_vault() -> CredentialVault:
    """The process-wide vault (created on first use)."""
    global _vault
    if _vault is None:
        _vault = CredentialVault()
    return _vault


def reset_credential_vault() -> None:
    """Drop the singleton (tests; the next get rebuilds it)."""
    global _vault
    _vault = None
