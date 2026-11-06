"""Playwright manager for the dedicated agent browser profile.

Epic 8, E8-6.1 (grill Q31–Q35). One rule outranks everything else in
this module: **the agent browser only ever runs in its dedicated
profile** — ``~/.aether/browser-profile`` — never the user's real
browser profile, never their cookies, history or logins.
:func:`validate_profile_dir` refuses anything outside that root, so
no caller (present or future) can point a session at a real profile
by mistake.

Control states (the browser task card's buttons drive these):

- ``agent`` — the agent drives; browser tools act.
- ``paused`` — user pause; tools refuse until resume.
- ``user`` — take-control; the user drives the visible window and
  tools refuse until control is returned.
- ``waiting_user`` — a CAPTCHA or 2FA page parked the session; the
  agent attempts nothing further (it never solves CAPTCHAs and never
  handles 2FA codes) until the user resumes it.
- ``stopped`` — the task was stopped; the browser is closed and
  tools refuse. The session is NOT silently replaced: the user's
  resume resets it (next action relaunches the browser), so a stop
  cannot be undone by the agent simply retrying.

Playwright is imported lazily and only when a session actually
starts. When it (or its browser build) is unavailable, starting
fails closed with :class:`BrowserUnavailableError` — an honest
error, never a silent no-op.

Navigation targets are guarded as strictly as the profile
(E7-01): :func:`validate_navigation_url` allows only http/https
URLs whose host is a public address — never ``file://`` or
browser-internal pages, never loopback / link-local /
private-range hosts, whether named by literal, by a ``localhost``
name, or by DNS resolution. The credential vault's fill path
already refused non-http(s) pages; the general browsing path
had no equivalent rule, so an approved navigation could put a
local file or an internal service in the page and the read-only
extract tool would hand its text to the model.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import uuid
from collections.abc import Awaitable, Callable, Iterable
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from aether_engine.audit import AuditLogger

logger = logging.getLogger("aether_engine.browser.session")


class BrowserError(Exception):
    """Base exception for agent-browser operations."""


class BrowserProfileError(BrowserError):
    """Raised when a session is pointed anywhere but the dedicated profile."""


class BrowserUnavailableError(BrowserError):
    """Raised when Playwright / its browser build cannot start."""


class BrowserParkedError(BrowserError):
    """Raised when a browser action is attempted in a non-agent state."""


class BrowserNavigationRejectedError(BrowserError):
    """Raised when a navigation target fails URL validation (E7-01).

    ``reason`` is the machine-readable refusal class (``scheme``,
    ``no_host``, ``internal_host``) for audit events. The message
    is the human/model-facing explanation and names the scheme or
    host only — never the full URL, which can carry credentials
    or tokens (finding M08).
    """

    def __init__(self, message: str, *, reason: str = ""):
        super().__init__(message)
        self.reason = reason


class BrowserControlState(str, Enum):
    AGENT = "agent"
    PAUSED = "paused"
    USER = "user"
    WAITING_USER = "waiting_user"
    STOPPED = "stopped"


def dedicated_profile_dir() -> Path:
    """The one and only profile the agent browser may ever use."""
    return Path.home() / ".aether" / "browser-profile"


# Path fragments that mark a REAL browser profile (used only to make
# refusal messages clearer — the allowlist check below is the rule).
_USER_PROFILE_MARKERS = (
    "google-chrome", "chrome", "chromium", "mozilla", "firefox",
    "microsoft edge", "brave", "opera", "safari", "vivaldi",
)


def validate_profile_dir(path: str | Path) -> Path:
    """Return *path* if it is the dedicated profile root or inside it.

    Anything else — a real browser profile, the home directory, an
    arbitrary folder — raises :class:`BrowserProfileError`. This is
    an allowlist, not a blocklist: the dedicated root is the only
    place a session may live.

    Containment is judged on RESOLVED paths (S2-M11): normpath alone
    is lexical, so a symlink inside the dedicated root pointing at
    the user's real profile used to pass — the path sat inside the
    root while the directory the browser would actually open sat
    outside it. Both the candidate and the root are resolved and
    the resolved candidate must be the resolved root or inside it,
    so a symlink whose target escapes is refused, while one whose
    target stays inside the root still passes.

    Resolution uses ``strict=False``: a profile directory that does
    not exist yet (the normal first-run case) is judged by its
    resolved would-be location — existing ancestors are resolved,
    the missing tail is taken lexically — exactly as it was judged
    before this fix. The path returned on success is the caller's
    path (expanded, normalised), unchanged from before.
    """
    import os

    # normpath collapses ".." lexically, so a path cannot sneak out
    # of the dedicated root while appearing to sit inside it.
    candidate = Path(os.path.normpath(Path(path).expanduser()))
    root = dedicated_profile_dir()
    try:
        resolved_candidate = candidate.resolve(strict=False)
        resolved_root = root.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        # Unresolvable (e.g. a symlink loop): fail closed — a path
        # whose real location cannot be established is never inside.
        raise BrowserProfileError(
            f"Refusing browser profile '{candidate}': its real location "
            f"could not be established ({exc}). Agent browser sessions may "
            f"only use the dedicated profile at '{root}'."
        ) from exc
    if resolved_candidate == resolved_root or (
        resolved_root in resolved_candidate.parents
    ):
        return candidate
    lowered = str(candidate).lower()
    hint = ""
    if any(marker in lowered for marker in _USER_PROFILE_MARKERS):
        hint = " That path looks like a real browser profile; the agent browser must never use the user's own browser data."
    raise BrowserProfileError(
        f"Refusing browser profile '{candidate}': agent browser sessions may only use "
        f"the dedicated profile at '{root}'.{hint}"
    )


# ---------------------------------------------------------------------
# Navigation URL validation (E7-01)
# ---------------------------------------------------------------------

#: The only schemes the agent browser may ever open. The credential
#: vault's fill path checks the same pair (vault/service.py); this
#: is that rule, extended with a host guard, for general browsing.
_ALLOWED_SCHEMES = ("http", "https")

_INTERNAL_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        # IPv4: this-host block, RFC 1918, CGNAT shared space,
        # loopback, link-local (incl. the cloud metadata address),
        # multicast, and the reserved block.
        "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
        "169.254.0.0/16", "172.16.0.0/12", "192.168.0.0/16",
        "224.0.0.0/4", "240.0.0.0/4",
        # IPv6: unspecified, loopback, link-local, unique-local,
        # multicast.
        "::/128", "::1/128", "fe80::/10", "fc00::/7", "ff00::/8",
    )
)

#: Resolves a hostname to its address strings. Injectable so tests
#: (and embedders with their own resolver) never touch the network;
#: the default is the system resolver via the event loop.
HostResolver = Callable[[str], Awaitable[Iterable[str]]]


async def _default_host_resolver(host: str) -> tuple[str, ...]:
    """System-resolver addresses for *host*; empty on any failure.

    The system resolver is deliberate: it honours /etc/hosts and
    the shorthand forms Chromium accepts (``127.1``, ``0x7f.1``),
    so a name the browser would map onto this machine is judged by
    the addresses it would actually reach.
    """
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return ()
    return tuple(sorted({str(info[4][0]) for info in infos}))


def _is_internal_address(raw: str) -> bool:
    """True when *raw* is an IP address on this machine or a
    private/local network — never a public web address.

    Judged against explicit networks, not ``is_private`` /
    ``is_global``: those flags also cover documentation and
    benchmarking ranges that host no services, and their exact
    membership shifts between Python versions.
    """
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address):
        mapped = address.ipv4_mapped
        if mapped is not None:
            address = mapped
    if (
        address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_unspecified
    ):
        return True
    return any(
        address.version == network.version and address in network
        for network in _INTERNAL_NETWORKS
    )


async def validate_navigation_url(
    url: str, *, resolver: HostResolver | None = None
) -> None:
    """Refuse any URL the agent browser must not open (E7-01).

    Allowed: http/https URLs whose host is a public address. The
    host is judged three ways — as an IP literal, as a
    ``localhost`` name, and by what it resolves to (any internal
    answer refuses the name). A hostname that does not resolve at
    all is NOT refused here: it cannot be proven internal, and
    the navigation attempt itself fails honestly downstream.

    Raises :class:`BrowserNavigationRejectedError`; returns None
    when the URL may be opened.
    """
    parsed = urlparse(url or "")
    scheme = parsed.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        shown = f"{scheme}:" if scheme else "a scheme-less"
        raise BrowserNavigationRejectedError(
            f"Refusing to open {shown} URL in the agent browser: only "
            "http and https pages can be opened. Local files and "
            "browser-internal pages are never readable by the agent.",
            reason="scheme",
        )
    host = parsed.hostname or ""
    if not host:
        raise BrowserNavigationRejectedError(
            "Refusing to open the URL in the agent browser: it has no "
            "host, so there is no web page to go to.",
            reason="no_host",
        )
    if _is_internal_address(host):
        raise BrowserNavigationRejectedError(
            f"Refusing to open {host} in the agent browser: that is "
            "this machine or a private/local network address. The "
            "agent browser can only open public web pages.",
            reason="internal_host",
        )
    hostname = host.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost"):
        raise BrowserNavigationRejectedError(
            f"Refusing to open {host} in the agent browser: localhost "
            "is this machine. The agent browser can only open public "
            "web pages.",
            reason="internal_host",
        )
    resolve = resolver if resolver is not None else _default_host_resolver
    try:
        addresses = await resolve(host)
    except Exception:
        # An unverifiable name is treated like an unresolvable
        # one (see docstring): the attempt proceeds and fails
        # honestly in the browser if the host is truly bogus.
        logger.debug("Host resolution check failed for %s", host, exc_info=True)
        addresses = ()
    for raw in addresses or ():
        if _is_internal_address(str(raw)):
            raise BrowserNavigationRejectedError(
                f"Refusing to open {host} in the agent browser: the "
                "name resolves to this machine or a private/local "
                "network address. The agent browser can only open "
                "public web pages.",
                reason="internal_host",
            )


class PlaywrightDriver:
    """Thin async wrapper over a Playwright persistent context.

    The session layer talks to this small duck-typed surface (the
    tests substitute a fake with the same methods), so no test ever
    launches a real browser.
    """

    def __init__(self, playwright: Any, context: Any, page: Any):
        self._playwright = playwright
        self._context = context
        self._page = page

    @classmethod
    async def launch(cls, profile_dir: Path) -> PlaywrightDriver:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise BrowserUnavailableError(
                "Playwright is not installed, so the agent browser cannot start. "
                "Install the 'playwright' package and its Chromium build, then retry."
            ) from exc
        playwright = None
        try:
            playwright = await async_playwright().start()
            # Visible window (headless=False): the owner watches the
            # agent browse and can take control at any moment.
            context = await playwright.chromium.launch_persistent_context(
                str(profile_dir), headless=False
            )
        except Exception as exc:
            if playwright is not None:
                try:
                    await playwright.stop()
                except Exception:
                    logger.debug("Playwright stop after failed launch failed", exc_info=True)
            raise BrowserUnavailableError(
                f"The agent browser could not start: {exc}"
            ) from exc
        page = context.pages[0] if context.pages else await context.new_page()
        return cls(playwright, context, page)

    async def goto(self, url: str) -> None:
        await self._page.goto(url)

    async def click(self, selector: str) -> None:
        await self._page.click(selector)

    async def fill(self, selector: str, text: str) -> None:
        await self._page.fill(selector, text)

    async def input_value(self, selector: str) -> str:
        return str(await self._page.input_value(selector, timeout=5000))

    async def extract_text(self, selector: str | None = None) -> str:
        return await self._page.inner_text(selector or "body")

    async def current_url(self) -> str:
        return str(self._page.url)

    async def page_title(self) -> str:
        return await self._page.title()

    async def page_content(self) -> str:
        return await self._page.content()

    async def close(self) -> None:
        try:
            await self._context.close()
        finally:
            await self._playwright.stop()


DriverFactory = Callable[[Path], Awaitable[Any]]


class BrowserSession:
    """One agent browsing session in the dedicated profile."""

    def __init__(
        self,
        profile_dir: str | Path,
        *,
        session_id: str | None = None,
        task_id: str | None = None,
        chat_session_id: str | None = None,
        audit_logger: AuditLogger | None = None,
        driver_factory: DriverFactory | None = None,
        host_resolver: HostResolver | None = None,
    ):
        self.profile_dir = validate_profile_dir(profile_dir)
        self.session_id = session_id or uuid.uuid4().hex
        # Identity binding (E8-6.1/6.3, review finding M04): the
        # graph task that created this session and the chat session
        # it belongs to. Vault approvals are addressed by task_id;
        # desktop control resolves by chat_session_id. Both are
        # bound at creation (or late-bound by the manager when the
        # creator's identity arrives after the session) and never
        # re-bound to a different task.
        self.task_id = task_id
        self.chat_session_id = chat_session_id
        # Driving identity (M04 residual): while the browser agent
        # is enabled, any task may drive the one live session, so the
        # manager also tracks the task driving it RIGHT NOW. The
        # CAPTCHA/2FA handoff is routed to the driving task — the
        # one whose action hit the challenge and whose stream is
        # waiting on it — not to the (possibly idle) creator. The
        # creator binding above never moves; only this does.
        self.driving_task_id = task_id
        self.driving_chat_session_id = chat_session_id
        self._audit_logger = audit_logger or AuditLogger()
        self._driver_factory = driver_factory
        # E7-01: hostname resolver for the navigation guard
        # (None = the system resolver; see validate_navigation_url).
        self._host_resolver = host_resolver
        self._driver: Any = None
        self._state = BrowserControlState.AGENT
        self.current_url = ""
        self.challenge: dict[str, Any] | None = None
        # Epic 8, E8-6.2: page snapshot cache (title + HTML as of the
        # last navigate/click). The confirmation classifier reads
        # this synchronously at approval time to name the exact
        # action a pending click/type would perform on THIS page.
        self.last_title: str = ""
        self.last_page_content: str = ""

    @property
    def control_state(self) -> BrowserControlState:
        return self._state

    # -- internals ----------------------------------------------------

    def _audit(self, action: str, details: dict[str, Any]) -> None:
        try:
            self._audit_logger.log_event(action, details)
        except Exception:
            logger.debug("Failed to write %s audit event", action, exc_info=True)

    async def _ensure_driver(self) -> Any:
        if self._driver is None:
            factory = self._driver_factory or PlaywrightDriver.launch
            self._driver = await factory(self.profile_dir)
            self._audit("BROWSER_SESSION_STARTED", {
                "session_id": self.session_id,
                "task_id": self.task_id,
                "chat_session_id": self.chat_session_id,
                "profile": "dedicated",
            })
        return self._driver

    def _require_agent(self) -> None:
        if self._state == BrowserControlState.AGENT:
            return
        if self._state == BrowserControlState.PAUSED:
            raise BrowserParkedError(
                "The browser is paused by the user. Resume it from the browser "
                "task card before continuing."
            )
        if self._state == BrowserControlState.USER:
            raise BrowserParkedError(
                "The user has taken control of the browser. Browser actions are "
                "blocked until they return control."
            )
        if self._state == BrowserControlState.WAITING_USER:
            kind = (self.challenge or {}).get("challenge", "verification")
            raise BrowserParkedError(
                f"The browser is waiting for you: a {kind} challenge needs the "
                "user in the browser window. The agent never solves CAPTCHAs "
                "and never enters 2FA codes."
            )
        raise BrowserParkedError("The browser task was stopped by the user.")

    def _set_state(self, new_state: BrowserControlState, action: str) -> None:
        old = self._state
        self._state = new_state
        self._audit("BROWSER_CONTROL_CHANGED", {
            "session_id": self.session_id,
            "action": action,
            "from_state": old.value,
            "to_state": new_state.value,
        })

    async def _read_live_challenge(self) -> dict[str, Any] | None:
        """Detect a challenge on the page as it is NOW.

        Reads the live driver state (URL, title, HTML), refreshes
        the snapshot cache, and returns the detection hit (or
        None). No side effects beyond the caches — parking is
        :meth:`_detect_and_park`'s job, verdict-only re-checks
        (resume verification) use this directly.
        """
        from aether_engine.browser.tools import detect_challenge

        driver = self._driver
        url = await driver.current_url()
        if url:
            self.current_url = url
        content = await driver.page_content()
        title = await driver.page_title()
        # E8-6.2: refresh the classifier's page snapshot (capped —
        # classification needs element text/attrs, not whole apps).
        self.last_title = title or ""
        self.last_page_content = (content or "")[:200_000]
        return detect_challenge(url=self.current_url, title=title, content=content)

    async def _detect_and_park(self) -> dict[str, Any] | None:
        """Run CAPTCHA/2FA detection over the current page.

        On a hit the session parks in WAITING_USER, the hit is
        audited, and a BROWSER_CHALLENGE_DETECTED event is published
        for the task currently DRIVING the session (when one is
        known) — the task whose action hit the challenge. The
        creator binding is the fallback when no driver is tracked;
        it is never used to override a known driver (M04 residual).
        """
        hit = await self._read_live_challenge()
        if hit is None:
            return None
        self.challenge = hit
        self._set_state(BrowserControlState.WAITING_USER, "challenge_detected")
        host = urlparse(self.current_url).netloc
        self._audit("BROWSER_CHALLENGE_DETECTED", {
            "session_id": self.session_id,
            "challenge": hit["challenge"],
            "host": host,
        })
        handoff_task_id = self.driving_task_id or self.task_id
        if handoff_task_id:
            from aether_common.contracts import Event, EventType
            from aether_engine.event_bus import event_bus

            event_bus.publish(handoff_task_id, Event(
                type=EventType.BROWSER_CHALLENGE_DETECTED,
                request_id=handoff_task_id,
                payload={
                    "session_id": self.session_id,
                    "task_id": handoff_task_id,
                    "chat_session_id": (
                        self.driving_chat_session_id or self.chat_session_id
                    ),
                    "url": self.current_url,
                    "challenge": hit["challenge"],
                },
            ))
        return hit

    async def _guard_challenge(self) -> None:
        """Refuse an agent action if the LIVE page shows a challenge.

        Detection historically ran only AFTER navigate()/click(),
        so a challenge that appeared without one — a dynamic
        in-page 2FA transition — left the cached control state at
        AGENT and type_text() would happily fill a one-time code
        (review finding M05). Every agent action therefore
        re-detects against the page as it is now, before acting.
        An unreadable page fails closed: no action is ever taken
        on a page whose challenge state cannot be determined.
        (The owner's own use of the window while parked is not an
        agent action and is never gated here.)
        """
        if self._driver is None:
            return
        try:
            hit = await self._detect_and_park()
        except Exception as exc:
            raise BrowserParkedError(
                "The browser page could not be checked for a verification "
                "challenge, so the action was refused (fail closed)."
            ) from exc
        if hit is not None:
            # _detect_and_park parked the session; _require_agent
            # raises the parked error matching the new state.
            self._require_agent()

    async def refresh_page_snapshot(self) -> None:
        """Re-read the live page into the classifier's snapshot
        cache (current URL, title, HTML).

        The execution-time browser gate (finding M03) classifies
        the FINAL action arguments against the page as it is NOW —
        the cache from the last navigate/click may predate the
        approval pause, and the page may have changed since. Never
        launches a driver just to classify, and a driver that
        cannot be read leaves the cache as it stands (the gate
        then classifies from the cache, and any doubt resolves to
        unknown — fail-closed).
        """
        driver = self._driver
        if driver is None:
            return
        try:
            url = await driver.current_url()
            title = await driver.page_title()
            content = await driver.page_content()
        except Exception:
            logger.debug(
                "Page snapshot refresh failed; keeping the cached snapshot",
                exc_info=True)
            return
        if url:
            self.current_url = url
        self.last_title = title or ""
        self.last_page_content = (content or "")[:200_000]

    async def read_live_page(self) -> tuple[str, str] | None:
        """(url, html) read from the live driver RIGHT NOW, or None.

        The strict sibling of :meth:`refresh_page_snapshot`: that
        one serves classification and tolerates a stale cache, but
        a caller that must not act on an unverified page — the
        credential vault's fill-moment revalidation (review
        finding M07) — needs a verdict. None means the live page
        could not be read at all (no driver yet, or the driver
        failed), and the caller must fail closed. A successful
        read also refreshes the URL/HTML snapshot caches, keeping
        them coherent for the classifier.
        """
        driver = self._driver
        if driver is None:
            return None
        try:
            url = await driver.current_url()
            content = await driver.page_content()
        except Exception:
            logger.debug("Live page read failed", exc_info=True)
            return None
        if url:
            self.current_url = url
        self.last_page_content = (content or "")[:200_000]
        return (url or ""), (content or "")

    # -- browsing actions (agent state only) --------------------------
    #
    # Exception hygiene (review finding M08): driver error text is
    # built from the call — the selector, the URL, and for fill()
    # the very value being typed, which on the credential-vault
    # path IS the password. No raw driver exception therefore ever
    # crosses this boundary: the action methods below convert
    # driver failures to BrowserError with generic text (selector
    # or host at most, never a typed value, never the driver's own
    # words) and a suppressed chain (``from None``), so neither the
    # error message nor a rendered traceback can carry driver text
    # into tool results, error surfaces, or logs. field_value()
    # set the pattern; every action method follows it.

    async def _detect_after_action(self) -> dict[str, Any] | None:
        """Post-action challenge detection under the M08 boundary.

        :meth:`_detect_and_park` reads the live page; a driver
        failure there must surface as a generic BrowserError too,
        never the driver's own text.
        """
        try:
            return await self._detect_and_park()
        except BrowserError:
            raise
        except Exception:  # noqa: BLE001 — normalised below (M08)
            raise BrowserError(
                "The page could not be checked after the action."
            ) from None

    async def _validate_navigation(self, url: str) -> None:
        """Run the E7-01 navigation guard, auditing any refusal.

        The audit event carries the host and the refusal class
        only — never the full URL (finding M08).
        """
        try:
            await validate_navigation_url(url, resolver=self._host_resolver)
        except BrowserNavigationRejectedError as exc:
            self._audit("BROWSER_NAVIGATION_REJECTED", {
                "session_id": self.session_id,
                "host": urlparse(url or "").hostname or "",
                "reason": exc.reason,
            })
            raise

    async def _validate_open_page(self) -> None:
        """Hold the CURRENT page to the same rule as navigate().

        The browser can arrive on a page without an agent
        navigation — a redirect chain, a link, the owner's own
        driving while in control. Reading such a page back is
        refused when its URL is not a public web address, which
        is what closes the read-back half of E7-01. No page yet
        (empty URL / about:blank) has nothing to read and passes.
        """
        url = self.current_url
        if not url or url == "about:blank":
            return
        await self._validate_navigation(url)

    async def navigate(self, url: str) -> dict[str, Any]:
        self._require_agent()
        # E7-01: the target is validated BEFORE the driver starts,
        # so a refused URL never reaches Playwright at all.
        await self._validate_navigation(url)
        driver = await self._ensure_driver()
        await self._guard_challenge()
        try:
            await driver.goto(url)
            self.current_url = await driver.current_url()
            title = await driver.page_title()
        except BrowserError:
            raise
        except Exception:  # noqa: BLE001 — normalised below (M08)
            # Host only, as in the audit event below: a full URL
            # can carry tokens in its query.
            raise BrowserError(
                "Could not open the page at "
                f"{urlparse(url).netloc or 'that address'}."
            ) from None
        # E7-01: the LANDED URL is re-validated — a public page
        # can redirect (or be redirected) to an internal one, and
        # the requested URL's clean bill does not transfer.
        if self.current_url and self.current_url != url:
            await self._validate_navigation(self.current_url)
        self._audit("BROWSER_NAVIGATED", {
            "session_id": self.session_id,
            # Host only: a full URL can carry tokens in its query.
            "host": urlparse(self.current_url).netloc,
        })
        challenge = await self._detect_after_action()
        return {"url": self.current_url, "title": title, "challenge": challenge}

    async def click(self, selector: str) -> dict[str, Any]:
        self._require_agent()
        driver = await self._ensure_driver()
        await self._guard_challenge()
        try:
            await driver.click(selector)
            self.current_url = await driver.current_url()
        except BrowserError:
            raise
        except Exception:  # noqa: BLE001 — normalised below (M08)
            raise BrowserError(
                f"Could not click {selector!r}."
            ) from None
        # E7-01: a click can navigate (links, form submits), so
        # the page it landed on is held to the navigate rule.
        await self._validate_open_page()
        # A click can land on a 2FA step (post-login), so re-detect.
        challenge = await self._detect_after_action()
        return {"url": self.current_url, "challenge": challenge}

    async def type_text(
        self,
        selector: str,
        text: str,
        *,
        require_site: str | None = None,
    ) -> dict[str, Any]:
        self._require_agent()
        driver = await self._ensure_driver()
        # M05: re-detect on the live page BEFORE entering data —
        # a dynamically-appeared 2FA form must never receive text.
        await self._guard_challenge()
        if require_site is not None:
            # N-40: the credential vault's caller revalidates the
            # page's site before decrypting, but the page can still
            # navigate between that check and this fill (or between
            # the username fill and the password fill). When the
            # caller names the site the text is bound for, the LIVE
            # page is re-checked against it here — at fill time, the
            # last moment a refusal can still protect the value.
            from aether_engine.vault.store import registrable_domain

            live_url = await driver.current_url()
            if registrable_domain(live_url or "") != require_site:
                raise BrowserError(
                    "The browser page is no longer on "
                    f"{require_site}, so the text was not entered."
                )
        try:
            await driver.fill(selector, text)
        except BrowserError:
            raise
        except Exception:  # noqa: BLE001 — normalised below (M08)
            # M08: this is the sensitive conversion — the failed
            # call's arguments include the text itself, and the
            # message names the selector only, never the value.
            raise BrowserError(
                f"Could not fill the field {selector!r}."
            ) from None
        return {"url": self.current_url}

    async def field_value(self, selector: str) -> str:
        """Read a form field's current value (agent state only).

        Exists for exactly one caller: the credential vault's
        agent-save flow (E8-6.3), which stores the password an agent
        just created in a signup form. No tool exposes this — a
        value read here goes into the vault, never into a
        transcript. Driver failures are normalised to BrowserError.
        """
        self._require_agent()
        driver = await self._ensure_driver()
        # M05: a field on a challenge page (e.g. the OTP input
        # itself) is never read either — agents do not handle 2FA
        # codes in any direction.
        await self._guard_challenge()
        try:
            return str(await driver.input_value(selector))
        except BrowserError:
            raise
        except Exception:  # noqa: BLE001 — normalised below (M08)
            raise BrowserError(
                f"Could not read the field {selector!r}."
            ) from None

    async def extract(self, selector: str | None = None) -> str:
        self._require_agent()
        driver = await self._ensure_driver()
        await self._guard_challenge()
        # E7-01: extract is the read-back half of the browsing
        # path — the page as it stands NOW (refreshed into
        # current_url by the challenge guard above) must be a
        # public web page before any of its text reaches the model.
        await self._validate_open_page()
        try:
            return await driver.extract_text(selector)
        except BrowserError:
            raise
        except Exception:  # noqa: BLE001 — normalised below (M08)
            target = f" from {selector!r}" if selector else ""
            raise BrowserError(
                f"Could not extract text{target}."
            ) from None

    # -- user controls --------------------------------------------------

    def pause(self) -> None:
        if self._state != BrowserControlState.AGENT:
            raise BrowserError(f"Cannot pause while the browser is {self._state.value}.")
        self._set_state(BrowserControlState.PAUSED, "pause")

    async def resume(self) -> None:
        # From STOPPED this resets the session: the browser is
        # already closed, so the next action relaunches it. Only the
        # user can issue a resume — the agent has no path to it.
        if self._state not in (
            BrowserControlState.PAUSED,
            BrowserControlState.WAITING_USER,
            BrowserControlState.STOPPED,
        ):
            raise BrowserError(f"Cannot resume while the browser is {self._state.value}.")
        if self._state == BrowserControlState.WAITING_USER:
            await self._verify_challenge_cleared()
        self.challenge = None
        self._set_state(BrowserControlState.AGENT, "resume")

    async def _verify_challenge_cleared(self) -> None:
        """Re-inspect the live page before un-parking (finding M05).

        resume() used to clear the challenge unconditionally, so a
        resume issued while the CAPTCHA/2FA was still on screen
        returned the session to agent control — typing included.
        The park now lifts only when the page as it is NOW no
        longer shows the challenge; a challenge still present, or
        a page that cannot be read, keeps the session parked and
        the owner handoff standing (fail closed).
        """
        if self._driver is None:
            if self.challenge is not None:
                raise BrowserError(
                    "The browser page cannot be re-checked for the "
                    "verification challenge, so the session stays parked."
                )
            return
        try:
            hit = await self._read_live_challenge()
        except Exception as exc:
            raise BrowserError(
                "The browser page could not be re-checked for the "
                "verification challenge, so the session stays parked "
                "(fail closed)."
            ) from exc
        if hit is not None:
            self.challenge = hit
            kind = hit.get("challenge", "verification")
            raise BrowserError(
                f"The page is still showing a {kind} challenge. Complete "
                "it in the browser window, then resume."
            )

    def take_control(self) -> None:
        if self._state not in (BrowserControlState.AGENT, BrowserControlState.PAUSED):
            raise BrowserError(f"Cannot take control while the browser is {self._state.value}.")
        self._set_state(BrowserControlState.USER, "take_control")

    def return_control(self) -> None:
        if self._state != BrowserControlState.USER:
            raise BrowserError(f"Cannot return control while the browser is {self._state.value}.")
        self._set_state(BrowserControlState.AGENT, "return_control")

    async def stop(self) -> None:
        if self._state == BrowserControlState.STOPPED:
            return
        driver, self._driver = self._driver, None
        self.challenge = None
        self._set_state(BrowserControlState.STOPPED, "stop")
        if driver is not None:
            try:
                await driver.close()
            except Exception:
                logger.debug("Browser driver close failed during stop", exc_info=True)

    def control_snapshot(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "task_id": self.task_id,
            "chat_session_id": self.chat_session_id,
            "control_state": self._state.value,
            "current_url": self.current_url,
            "challenge": self.challenge,
        }


class BrowserSessionManager:
    """Owns the engine's browser sessions (one active at a time)."""

    def __init__(
        self,
        profile_dir: str | Path | None = None,
        *,
        audit_logger: AuditLogger | None = None,
        driver_factory: DriverFactory | None = None,
        host_resolver: HostResolver | None = None,
    ):
        self.profile_dir = (
            validate_profile_dir(profile_dir)
            if profile_dir is not None
            else dedicated_profile_dir()
        )
        self._audit_logger = audit_logger or AuditLogger()
        self._driver_factory = driver_factory
        self._host_resolver = host_resolver
        self._sessions: dict[str, BrowserSession] = {}
        self._active_id: str | None = None

    @property
    def active_session(self) -> BrowserSession | None:
        if self._active_id is None:
            return None
        return self._sessions.get(self._active_id)

    def get_or_create_active(
        self,
        task_id: str | None = None,
        chat_session_id: str | None = None,
    ) -> BrowserSession:
        """The live session for browser tools, bound to its task.

        A stopped session is returned as-is (its tools refuse), never
        silently replaced — otherwise the agent could defeat a user
        stop by simply retrying. The user's resume resets it.

        Identity (review finding M04): the creating task's id and its
        chat session id are bound at creation; a session whose
        creator's identity arrives late is late-bound, never
        re-bound. While the browser agent is enabled, any task may
        drive the one live session; the latest admitted driver is
        tracked separately (``driving_task_id``) so challenge
        handoffs route to the task that hit the challenge. Once the
        owner disables it, only the OWNING task may keep using the
        live session — a new task must not silently inherit a running
        browser (see :meth:`_require_session_admission`).
        """
        active = self.active_session
        if active is not None:
            self._require_session_admission(
                active, task_id=task_id, chat_session_id=chat_session_id)
            if task_id and not active.task_id:
                active.task_id = task_id
            if chat_session_id and not active.chat_session_id:
                active.chat_session_id = chat_session_id
            # M04 residual: whoever is driving NOW owns the challenge
            # handoff. The creator binding above stays put (admission
            # and control resolution depend on it); the driving
            # identity follows the latest admitted driver.
            if task_id:
                active.driving_task_id = task_id
            if chat_session_id:
                active.driving_chat_session_id = chat_session_id
            return active
        # Epic 8, E8-6.2: the master switch blocks NEW browser tasks
        # outright — a session may only be CREATED while the browser
        # agent is enabled. A live session (returned above) is never
        # blocked or replaced by the toggle: a running task is not
        # killed mid-action.
        from aether_engine.browser.policy import current_browser_policy

        if not current_browser_policy().enabled:
            raise BrowserError(
                "The browser agent is disabled in Settings (Browser Agent "
                "Policy). Enable it there to start a new browser task."
            )
        session = BrowserSession(
            self.profile_dir,
            task_id=task_id,
            chat_session_id=chat_session_id,
            audit_logger=self._audit_logger,
            driver_factory=self._driver_factory,
            host_resolver=self._host_resolver,
        )
        self._sessions[session.session_id] = session
        self._active_id = session.session_id
        return session

    @staticmethod
    def _require_session_admission(
        session: BrowserSession,
        *,
        task_id: str | None,
        chat_session_id: str | None,
    ) -> None:
        """Master-switch admission for a LIVE session (E8-6.2 + M04).

        Enabled: no-op — the one live session is shared, bound to
        its creator. Disabled: only the owning task is admitted,
        proven by a presented identity that matches the binding
        (task id is decisive; the chat session id identifies the
        owner when no task id is presented). A caller presenting no
        identity at all is admitted only when the session is itself
        unbound — the direct-manager path (tests, engine internals);
        a bound session with an unidentified or contradicting
        caller fails closed.
        """
        from aether_engine.browser.policy import current_browser_policy

        if current_browser_policy().enabled:
            return
        if task_id is not None:
            owner = session.task_id == task_id
        elif chat_session_id is not None:
            owner = session.chat_session_id == chat_session_id
        else:
            owner = session.task_id is None and session.chat_session_id is None
        if not owner:
            raise BrowserError(
                "The browser agent is disabled in Settings (Browser Agent "
                "Policy), and the running browser session belongs to "
                "another task. Enable it there to start a new browser "
                "task, or let that task finish first."
            )

    def get(self, session_id: str) -> BrowserSession | None:
        return self._sessions.get(session_id)

    def _find_by_chat_session(self, chat_session_id: str) -> BrowserSession | None:
        active = self.active_session
        if active is not None and active.chat_session_id == chat_session_id:
            return active
        for session in self._sessions.values():
            if session.chat_session_id == chat_session_id:
                return session
        return None

    def _find_by_task(self, task_id: str) -> BrowserSession | None:
        active = self.active_session
        if active is not None and active.task_id == task_id:
            return active
        for session in self._sessions.values():
            if session.task_id == task_id:
                return session
        return None

    def _resolve_control_target(
        self,
        session_id: str | None,
        chat_session_id: str | None,
    ) -> BrowserSession | None:
        """Resolve a control message's target across the ID spaces.

        In order: the browser session id itself; the session bound
        to ``chat_session_id``; a chat session id or task id carried
        in ``session_id`` (the shipped desktop addressed control
        with the CHAT session id there, which never matched a
        browser UUID — M04). With no identifier at all, the active
        session. An identifier that matches nothing resolves to
        None — an honest error, never a silent fallback to whatever
        session happens to be active.
        """
        if session_id:
            session = self._sessions.get(session_id)
            if session is not None:
                return session
        if chat_session_id:
            session = self._find_by_chat_session(chat_session_id)
            if session is not None:
                return session
        if session_id:
            return (
                self._find_by_chat_session(session_id)
                or self._find_by_task(session_id)
            )
        if not chat_session_id:
            return self.active_session
        return None

    async def control(
        self,
        session_id: str | None = None,
        action: str = "",
        *,
        chat_session_id: str | None = None,
    ) -> dict[str, Any]:
        """Apply a browser control action (the WS control channel).

        Always answers with a result dict — ``success`` plus the
        resulting ``control_state`` — never raises for a bad action
        or a missing session.
        """
        session = self._resolve_control_target(session_id, chat_session_id)
        if session is None:
            return {
                "success": False,
                "session_id": session_id,
                "chat_session_id": chat_session_id,
                "control_state": "none",
                "error": "No browser session exists yet — a browsing task must start one first.",
            }
        try:
            if action == "pause":
                session.pause()
            elif action == "resume":
                await session.resume()
            elif action == "take_control":
                session.take_control()
            elif action == "return_control":
                session.return_control()
            elif action == "stop":
                await session.stop()
            else:
                return {
                    "success": False,
                    **session.control_snapshot(),
                    "error": f"Unknown browser control action: {action!r}.",
                }
        except BrowserError as exc:
            return {"success": False, **session.control_snapshot(), "error": str(exc)}
        return {"success": True, **session.control_snapshot()}

    async def shutdown(self) -> None:
        """Stop every session this manager owns (engine shutdown).

        Each session's driver is closed through its own stop(), so
        no Chromium process outlives the manager that launched it.
        Sessions are forgotten as they are stopped.
        """
        sessions = list(self._sessions.values())
        self._sessions.clear()
        self._active_id = None
        for session in sessions:
            await session.stop()


_manager: BrowserSessionManager | None = None


def get_browser_manager() -> BrowserSessionManager:
    global _manager
    if _manager is None:
        _manager = BrowserSessionManager()
    return _manager


def reset_browser_manager() -> None:
    """Drop the process-wide manager (tests; engine shutdown).

    E7-16: the dropped manager's live sessions are stopped first —
    dropping the singleton used to abandon a running Chromium in the
    dedicated profile. This function is synchronous (test fixtures
    call it as one): with an event loop running, the shutdown is
    scheduled on it; without one, it runs to completion on a
    short-lived loop. Either way the singleton is dropped first, so
    no new session can be created against the dying manager.
    """
    global _manager
    manager, _manager = _manager, None
    if manager is None or not manager._sessions:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None:
        loop.create_task(manager.shutdown())
        return
    try:
        asyncio.run(manager.shutdown())
    except Exception:  # a failed stop must not break the reset
        logger.debug("Browser manager shutdown during reset failed",
                     exc_info=True)
