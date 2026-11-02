"""Mediated credential retrieval (E8-6.3) — vault → form field only.

This service is the ONLY path by which a stored password leaves the
vault, and it never hands the plaintext to a caller: :meth:`fill_login`
takes a browser session plus (optionally) an entry id or a username
hint, checks the settled rules in order, and types the credential
into the page itself:

1. the Settings → Browser Agent "vault autofill" toggle must be on;
2. the page must be a web page on the entry's own site — registrable
   domain equality (see :mod:`.store`), so lookalike domains are
   refused even when their pages imitate the real login form. The
   page is read LIVE from the driver, never from the session's
   cached snapshot: a page can redirect without any browser
   action, silently ageing the cache;
3. an ask-every-time entry needs the owner's approval first, via a
   ``VAULT_FILL_APPROVAL_REQUEST`` push the desktop answers;
4. immediately before decrypting, the live page is re-read and
   revalidated against the approved entry's site and login form
   (finding M07): consent does not survive navigation to another
   site, and an unreadable page refuses the fill;
5. only then is the password decrypted — inside this module — and
   typed straight into the password field by the session.

Results are plain sentences with metadata only. Refusals are data,
not exceptions: a refused fill is information the agent should act
on ("vault autofill is off", "no credential for this site"), and
every refusal and fill is audit-logged with metadata only.

The mirror flow, :meth:`save_login_from_page`, stores a credential
an agent just created during a signup: it reads the form's own field
values through the driver (the password never transits tool
arguments or the transcript either), marks the entry
``created_by="agent"``, and never overwrites an entry the user
saved themselves. Its site/form identity comes from the live page
at save time, never from the session caches (finding PF-4): a
credential is bound to the site whose form its values were actually
read from, re-verified immediately before it is stored.
"""
from __future__ import annotations

import asyncio
import uuid
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from aether_common.contracts import Event, EventType

from ..audit import AuditLogger
from ..browser.policy import current_browser_policy
from ..browser.session import BrowserError
from ..event_bus import event_bus
from .store import (
    CredentialVault,
    VaultEntryNotFound,
    get_credential_vault,
    registrable_domain,
)

if TYPE_CHECKING:
    from ..browser.session import BrowserSession

#: How long the owner has to answer an ask-every-time prompt before
#: the fill fails closed. A constant, not a policy knob, for v1.
DEFAULT_APPROVAL_TIMEOUT_SECONDS = 120.0


# ---------------------------------------------------------------------
# Login-form field finding (over the session's HTML snapshot)
# ---------------------------------------------------------------------

class _InputCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.inputs: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag.lower() == "input":
            self.inputs.append(
                {str(k).lower(): (v or "") for k, v in attrs})


def _css_escape_ident(value: str) -> str:
    """Escape a string for use as a CSS identifier (``CSS.escape()``).

    Attribute values are page-controlled, and the resulting selector
    is the credential fill target: an unescaped id containing e.g.
    ``,`` or ``[`` would turn ``#<id>`` into a selector list naming
    an attacker-chosen field, and the vault would type the decrypted
    password into it. Every character outside ``[a-zA-Z0-9_-]`` (plus
    non-ASCII, which CSS allows) is backslash-escaped; control
    characters, a leading digit, and a digit after a leading ``-``
    use a hex escape with a trailing space (so the following
    character is not consumed as more hex), and a lone ``-`` is
    escaped as ``\\-``.
    """
    out: list[str] = []
    length = len(value)
    for index, char in enumerate(value):
        code = ord(char)
        if code == 0:
            out.append("\ufffd")
        elif (
            0x01 <= code <= 0x1F
            or code == 0x7F
            or (index == 0 and char.isdigit())
            or (index == 1 and char.isdigit() and value[0] == "-")
        ):
            out.append(f"\\{code:x} ")
        elif index == 0 and char == "-" and length == 1:
            out.append("\\-")
        elif (
            code >= 0x80
            or char in "-_"
            or ("a" <= char <= "z")
            or ("A" <= char <= "Z")
            or char.isdigit()
        ):
            out.append(char)
        else:
            out.append("\\" + char)
    return "".join(out)


def _css_escape_attr_value(value: str) -> str:
    """Escape a string for use inside a double-quoted CSS attribute
    selector value.

    Same page-controlled concern as :func:`_css_escape_ident`: a
    ``"`` in a name/autocomplete value (e.g. ``x"], input[id="loot``)
    would terminate the quoted string early and turn the selector
    into a list matching an attacker-chosen field — the field the
    vault types the decrypted password into. Backslash and
    double-quote are backslash-escaped (backslash first, per
    character, so an original ``\\`` cannot escape-away a following
    quote escape), and control characters use a hex escape with a
    trailing space so they can never terminate or corrupt the
    string either.
    """
    out: list[str] = []
    for char in value:
        code = ord(char)
        if code == 0:
            out.append("\ufffd")
        elif char == "\\":
            out.append("\\\\")
        elif char == '"':
            out.append('\\"')
        elif code < 0x20 or code == 0x7F:
            out.append(f"\\{code:x} ")
        else:
            out.append(char)
    return "".join(out)


def _selector_for(attrs: dict[str, str], input_type: str) -> str:
    """A Playwright selector for one input: id, then name, then
    autocomplete, then the bare type — most precise first.

    Every interpolated value is page-controlled, so all of them are
    CSS-escaped (see the helpers above); for ordinary safe values
    the escaping is a no-op and the selector is unchanged."""
    if attrs.get("id"):
        return f"#{_css_escape_ident(attrs['id'])}"
    if attrs.get("name"):
        return f'input[name="{_css_escape_attr_value(attrs["name"])}"]'
    if attrs.get("autocomplete"):
        return (
            'input[autocomplete="'
            f'{_css_escape_attr_value(attrs["autocomplete"])}"]'
        )
    return f'input[type="{_css_escape_attr_value(input_type)}"]'


def _username_score(attrs: dict[str, str]) -> int:
    """How likely an input is the username field (-1 = not a candidate)."""
    input_type = attrs.get("type", "text").lower() or "text"
    if input_type in ("password", "hidden", "submit", "button",
                      "checkbox", "radio", "file", "image", "reset"):
        return -1
    autocomplete = attrs.get("autocomplete", "").lower()
    haystack = " ".join(
        attrs.get(k, "") for k in ("name", "id", "class")).lower()
    if autocomplete == "username":
        return 100
    if "user" in haystack:
        return 90
    if autocomplete == "email":
        return 85
    if "email" in haystack or "e-mail" in haystack:
        return 84
    if input_type == "email":
        return 80
    if "login" in haystack:
        return 70
    if "account" in haystack:
        return 60
    if input_type in ("text", "tel"):
        return 40
    return -1


def find_login_form(html: str) -> tuple[str | None, str | None]:
    """(username_selector, password_selector) for a page's login form.

    Either element is None when the page has no such field; the
    password selector is None exactly when the page carries no
    password input at all (no login form to fill).
    """
    collector = _InputCollector()
    try:
        collector.feed(html or "")
    except Exception:  # noqa: BLE001 — malformed HTML must not break fills
        return None, None
    password_selector: str | None = None
    password_rank = -1
    username_selector: str | None = None
    username_rank = -1
    for attrs in collector.inputs:
        input_type = attrs.get("type", "text").lower() or "text"
        if input_type == "password":
            rank = 2 if attrs.get(
                "autocomplete", "").lower() == "current-password" else 1
            if rank > password_rank:
                password_rank = rank
                password_selector = _selector_for(attrs, "password")
            continue
        rank = _username_score(attrs)
        if rank > username_rank:
            username_rank = rank
            username_selector = _selector_for(attrs, input_type)
    return username_selector, password_selector


# ---------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------

class VaultFillService:
    """Site-bound, owner-gated, audit-logged credential fills."""

    def __init__(
        self,
        vault: CredentialVault,
        audit_logger: AuditLogger | None = None,
        approval_timeout_seconds: float = DEFAULT_APPROVAL_TIMEOUT_SECONDS,
    ) -> None:
        self._vault = vault
        self._audit = audit_logger or AuditLogger()
        self._approval_timeout_seconds = approval_timeout_seconds
        self._pending_approvals: dict[str, asyncio.Future] = {}

    # -- results --------------------------------------------------------

    def _refuse(self, reason: str, message: str, **audit: Any) -> dict:
        self._audit.log_event("VAULT_FILL_REFUSED",
                              {"reason": reason, **audit})
        return {"filled": False, "reason": reason, "message": message}

    # -- ask-every-time ---------------------------------------------------

    async def _await_owner_approval(
        self, *, entry: dict[str, Any], task_id: str | None,
    ) -> tuple[bool, str]:
        """Push the approval request and wait. (approved, refusal_reason)."""
        if not task_id:
            return False, "approval_unavailable"
        request_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self._pending_approvals[request_id] = future
        event_bus.publish(task_id, Event(
            type=EventType.VAULT_FILL_APPROVAL_REQUEST,
            request_id=request_id,
            payload={
                "request_id": request_id,
                "entry_id": entry["id"],
                "site": entry["site"],
                "username": entry["username"],
                "task_id": task_id,
            },
        ))
        try:
            approved = await asyncio.wait_for(
                future, timeout=self._approval_timeout_seconds)
        except TimeoutError:
            self._pending_approvals.pop(request_id, None)
            return False, "approval_timeout"
        return bool(approved), "" if approved else "approval_denied"

    def resolve_fill_approval(self, request_id: str, approved: bool) -> bool:
        """Answer a pending ask-every-time prompt (the WS handler's
        half of the round trip). False for unknown/stale requests."""
        future = self._pending_approvals.pop(request_id, None)
        if future is None or future.done():
            return False
        future.set_result(bool(approved))
        return True

    # -- the mediated fill --------------------------------------------------

    async def fill_login(
        self,
        session: BrowserSession,
        *,
        entry_id: str | None = None,
        username: str | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any]:
        """Fill the current page's login form from the vault.

        Returns ``{"filled": True, "site", "username", "message"}``
        or a refusal ``{"filled": False, "reason", "message"}``.
        Neither shape ever contains the password.
        """
        task_id = task_id or getattr(session, "task_id", None)
        audit_ctx: dict[str, Any] = {"task_id": task_id}

        # M07: bind everything below to the LIVE page, read from
        # the driver now. session.current_url / last_page_content
        # are caches from the last browser action, and a page can
        # redirect without one (meta refresh, script, a slow
        # redirect chain) — the cached origin is not evidence.
        live = await session.read_live_page()
        if live is None:
            return self._refuse(
                "page_unverifiable",
                "The browser page could not be re-checked, so no "
                "credential was filled.", **audit_ctx)
        url, page_html = live
        domain = registrable_domain(url)
        audit_ctx["site"] = domain

        if urlparse(url).scheme not in ("http", "https") or not domain:
            return self._refuse(
                "not_a_web_page",
                "The browser is not on a web page, so there is no "
                "login form to fill.", **audit_ctx)
        if not current_browser_policy().vault_autofill:
            return self._refuse(
                "autofill_disabled",
                "Vault autofill is off, so no credential was filled. "
                "The user can turn it on in Settings → Browser Agent "
                "(“Vault autofill”); ask-every-time entries still "
                "prompt before each fill.", **audit_ctx)

        # Resolve exactly one entry, bound to this page's site.
        if entry_id is not None:
            try:
                entry = self._vault.get_entry(entry_id)
            except VaultEntryNotFound:
                return self._refuse(
                    "not_found", "That vault entry no longer exists.",
                    entry_id=entry_id, **audit_ctx)
            audit_ctx["entry_id"] = entry_id
            if entry["site"] != domain:
                return self._refuse(
                    "site_mismatch",
                    "That credential is bound to a different site and "
                    "cannot be used on this page.", **audit_ctx)
        else:
            candidates = self._vault.entries_for_site(url)
            if username:
                candidates = [
                    c for c in candidates if c["username"] == username]
            if not candidates:
                return self._refuse(
                    "no_credential",
                    f"No credential is saved for {domain}. The user "
                    "can add one in Settings → Password Vault.",
                    **audit_ctx)
            if len(candidates) > 1:
                names = ", ".join(c["username"] for c in candidates)
                return self._refuse(
                    "multiple_credentials",
                    f"More than one credential is saved for {domain} "
                    f"({names}). Ask the user which one to use, then "
                    "call again with that username.", **audit_ctx)
            entry = candidates[0]
            audit_ctx["entry_id"] = entry["id"]

        # The page must actually hold a login form before we prompt
        # anyone or decrypt anything.
        username_selector, password_selector = find_login_form(page_html)
        if password_selector is None:
            return self._refuse(
                "no_login_form",
                "This page has no login form to fill.", **audit_ctx)

        if entry["ask_every_time"]:
            approved, reason = await self._await_owner_approval(
                entry=entry, task_id=task_id)
            if not approved:
                messages = {
                    "approval_denied":
                        "The user declined this fill, so no "
                        "credential was filled.",
                    "approval_timeout":
                        "The user did not answer the vault prompt "
                        "in time, so no credential was filled.",
                    "approval_unavailable":
                        "This credential asks every time, but no "
                        "approval channel is available for this "
                        "task, so nothing was filled.",
                }
                return self._refuse(
                    reason,
                    messages.get(reason, "The fill was not approved."),
                    **audit_ctx)

        # M07: the approval (or, for entries that do not ask, the
        # checks above) was bound to the page as it WAS. Re-read
        # the live page and revalidate before decrypting anything:
        # consent does not survive navigation to another site, the
        # form selectors are re-derived from the page as it IS,
        # and a page that can no longer be read refuses the fill.
        live = await session.read_live_page()
        if live is None:
            return self._refuse(
                "page_unverifiable",
                "The browser page could not be re-checked before "
                "filling, so no credential was filled.", **audit_ctx)
        live_url, live_html = live
        live_domain = registrable_domain(live_url)
        if (urlparse(live_url).scheme not in ("http", "https")
                or live_domain != entry["site"]):
            return self._refuse(
                "page_changed",
                "The browser page moved to a different site "
                f"({live_domain or 'a non-web page'}) while this "
                f"fill was waiting, so the credential for "
                f"{entry['site']} was not filled. Consent does not "
                "carry across sites — if the fill is still wanted, "
                "go back to the site and try again.",
                live_site=live_domain, **audit_ctx)
        username_selector, password_selector = find_login_form(live_html)
        if password_selector is None:
            return self._refuse(
                "no_login_form",
                "The page changed while this fill was waiting and "
                "no longer shows a login form, so nothing was "
                "filled.", **audit_ctx)

        # Decrypt here and only here; the plaintext goes to the page.
        try:
            password = self._vault.reveal_password(entry["id"])
        except Exception:  # noqa: BLE001 — any vault failure fails closed
            return self._refuse(
                "vault_unavailable",
                "The credential vault could not be unlocked, so "
                "nothing was filled.", **audit_ctx)
        try:
            if username_selector:
                await session.type_text(username_selector, entry["username"])
            await session.type_text(password_selector, password)
        except BrowserError:
            return self._refuse(
                "fill_failed",
                "The browser session could not fill the form (it "
                "may be paused or user-controlled).", **audit_ctx)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — M08 defence in depth
            # session.type_text normalises driver failures to
            # BrowserError, but a credential fill must never let an
            # unexpected exception carry its text out of this
            # service either: driver error text can embed the value
            # being filled. The exception is dropped — deliberately
            # NOT logged — and the fill refuses with the same
            # generic message as any other fill failure.
            return self._refuse(
                "fill_failed",
                "The browser session could not fill the form (it "
                "may be paused or user-controlled).", **audit_ctx)
        finally:
            password = ""  # drop the plaintext reference promptly

        self._vault.mark_used(entry["id"])
        self._audit.log_event("VAULT_CREDENTIAL_FILLED", {
            "entry_id": entry["id"], "site": entry["site"],
            "username": entry["username"], "task_id": task_id,
        })
        return {
            "filled": True,
            "site": entry["site"],
            "username": entry["username"],
            "message": (
                f"Filled the saved credential for {entry['site']} "
                f"({entry['username']}) directly into the page's "
                "login form. The password was never shown to you — "
                "do not ask the user for it or try to read it back."
            ),
        }

    # -- agent-saved credentials -------------------------------------------

    async def save_login_from_page(
        self, session: BrowserSession,
    ) -> dict[str, Any]:
        """Store the credentials sitting in the current page's form.

        For the signup case: the agent just chose a password and
        typed it into the form; this reads the field values back
        through the driver and stores them as an agent-created entry
        for the owner to review in Settings. An entry the user saved
        themselves is never overwritten.

        The site the entry is bound to (and the form its selectors
        come from) is derived from the LIVE page — read through
        :meth:`BrowserSession.read_live_page` at save time and
        re-verified immediately before the entry is stored — never
        from the session's cached URL/HTML snapshot (PF-4). A page
        that cannot be re-read, or that moved to another site while
        the save was being read, refuses the save rather than
        storing a mis-bound entry.
        """
        task_id = getattr(session, "task_id", None)
        domain = ""

        def refuse(reason: str, message: str) -> dict[str, Any]:
            self._audit.log_event("VAULT_SAVE_REFUSED", {
                "reason": reason, "site": domain, "task_id": task_id})
            return {"saved": False, "reason": reason, "message": message}

        # PF-4: bind the entry to the LIVE page, read from the
        # driver now — the same discipline fill_login follows
        # (M07). This flow used to take the site from
        # session.current_url and the form from
        # session.last_page_content, caches from the last browser
        # action, while the field VALUES were read live: a page
        # that had moved since (redirect, script navigation) got
        # its credential stored under the site the cache
        # remembered, where the site-bound fill would later offer
        # it on the wrong page — or never on its own. The caches
        # are not consulted here at all; read_live_page refreshes
        # them as a side effect for their other consumers.
        live = await session.read_live_page()
        if live is None:
            return refuse(
                "page_unverifiable",
                "The browser page could not be re-checked, so "
                "nothing was saved.")
        url, page_html = live
        domain = registrable_domain(url)
        if urlparse(url).scheme not in ("http", "https") or not domain:
            return refuse("not_a_web_page",
                          "The browser is not on a web page.")
        username_selector, password_selector = find_login_form(page_html)
        if password_selector is None:
            return refuse("no_login_form",
                          "This page has no password field to save.")
        try:
            password_value = await session.field_value(password_selector)
            username_value = (
                await session.field_value(username_selector)
                if username_selector else ""
            )
        except BrowserError:
            return refuse(
                "form_unreadable",
                "The form could not be read (the session may be "
                "paused or user-controlled).")
        if not password_value:
            return refuse(
                "empty_password",
                "The password field on this page is empty — nothing "
                "to save.")
        if not username_value:
            return refuse(
                "missing_username",
                "The form has no username value to save with the "
                "password.")

        # Re-verify immediately before storing, as the fill does
        # before decrypting: the entry's site binding must describe
        # the page as it IS when the credential is stored, not as
        # it was when the field reads began. A page that moved to
        # another site, or lost its password form, in between
        # refuses the save instead of mislabelling the entry.
        live = await session.read_live_page()
        if live is None:
            return refuse(
                "page_unverifiable",
                "The browser page could not be re-checked before "
                "saving, so nothing was saved.")
        live_url, live_html = live
        live_domain = registrable_domain(live_url)
        if (urlparse(live_url).scheme not in ("http", "https")
                or live_domain != domain):
            return refuse(
                "page_changed",
                "The browser page moved to a different site "
                f"({live_domain or 'a non-web page'}) while this "
                f"save was being read, so the credential was not "
                f"stored under {domain}. If the save is still "
                "wanted, run it again on the page as it stands.")
        _live_username, live_password_selector = find_login_form(live_html)
        if live_password_selector is None:
            return refuse(
                "no_login_form",
                "The page changed while this save was being read "
                "and no longer shows a password form, so nothing "
                "was saved.")

        for existing in self._vault.entries_for_site(url):
            if existing["username"] != username_value:
                continue
            if existing["created_by"] == "user":
                return refuse(
                    "already_saved_by_user",
                    f"The user already has a credential saved for "
                    f"{domain} ({username_value}); it was left "
                    "untouched.")
            saved_id = self._vault.save_entry(
                entry_id=existing["id"], site=domain,
                username=username_value, password=password_value,
                created_by="agent")
            break
        else:
            saved_id = self._vault.save_entry(
                site=domain, username=username_value,
                password=password_value, created_by="agent")

        self._audit.log_event("VAULT_CREDENTIAL_SAVED", {
            "entry_id": saved_id, "site": domain,
            "username": username_value, "created_by": "agent",
            "task_id": task_id,
        })
        return {
            "saved": True,
            "site": domain,
            "username": username_value,
            "message": (
                f"Saved the new login for {domain} ({username_value}) "
                "to the credential vault. It is marked as added by "
                "an agent and waits in Settings → Password Vault "
                "for the user's review."
            ),
        }


_service: VaultFillService | None = None


def get_vault_service() -> VaultFillService:
    """The process-wide fill service over the process-wide vault."""
    global _service
    if _service is None:
        _service = VaultFillService(get_credential_vault())
    return _service


def reset_vault_service() -> None:
    """Drop the singleton (tests; the next get rebuilds it)."""
    global _service
    _service = None
