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
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
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
    """
    import os

    # normpath collapses ".." lexically, so a path cannot sneak out
    # of the dedicated root while appearing to sit inside it.
    candidate = Path(os.path.normpath(Path(path).expanduser()))
    root = dedicated_profile_dir()
    if candidate == root or root in candidate.parents:
        return candidate
    lowered = str(candidate).lower()
    hint = ""
    if any(marker in lowered for marker in _USER_PROFILE_MARKERS):
        hint = " That path looks like a real browser profile; the agent browser must never use the user's own browser data."
    raise BrowserProfileError(
        f"Refusing browser profile '{candidate}': agent browser sessions may only use "
        f"the dedicated profile at '{root}'.{hint}"
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
    ):
        self.profile_dir = validate_profile_dir(profile_dir)
        self.session_id = session_id or uuid.uuid4().hex
        # Identity binding (E8-6.1/6.3, review finding M04): the
        # graph task that created this session and the chat session
        # it belongs to. Challenge handoffs and vault approvals are
        # addressed by task_id; desktop control resolves by
        # chat_session_id. Both are bound at creation (or late-bound
        # by the manager when the creator's identity arrives after
        # the session) and never re-bound to a different task.
        self.task_id = task_id
        self.chat_session_id = chat_session_id
        self._audit_logger = audit_logger or AuditLogger()
        self._driver_factory = driver_factory
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
        for the bound task (when one is bound).
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
        if self.task_id:
            from aether_common.contracts import Event, EventType
            from aether_engine.event_bus import event_bus

            event_bus.publish(self.task_id, Event(
                type=EventType.BROWSER_CHALLENGE_DETECTED,
                request_id=self.task_id,
                payload={
                    "session_id": self.session_id,
                    "task_id": self.task_id,
                    "chat_session_id": self.chat_session_id,
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

    async def navigate(self, url: str) -> dict[str, Any]:
        self._require_agent()
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
        # A click can land on a 2FA step (post-login), so re-detect.
        challenge = await self._detect_after_action()
        return {"url": self.current_url, "challenge": challenge}

    async def type_text(self, selector: str, text: str) -> dict[str, Any]:
        self._require_agent()
        driver = await self._ensure_driver()
        # M05: re-detect on the live page BEFORE entering data —
        # a dynamically-appeared 2FA form must never receive text.
        await self._guard_challenge()
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
    ):
        self.profile_dir = (
            validate_profile_dir(profile_dir)
            if profile_dir is not None
            else dedicated_profile_dir()
        )
        self._audit_logger = audit_logger or AuditLogger()
        self._driver_factory = driver_factory
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
        drive the one live session. Once the owner disables it, only
        the OWNING task may keep using the live session — a new task
        must not silently inherit a running browser (see
        :meth:`_require_session_admission`).
        """
        active = self.active_session
        if active is not None:
            self._require_session_admission(
                active, task_id=task_id, chat_session_id=chat_session_id)
            if task_id and not active.task_id:
                active.task_id = task_id
            if chat_session_id and not active.chat_session_id:
                active.chat_session_id = chat_session_id
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


_manager: BrowserSessionManager | None = None


def get_browser_manager() -> BrowserSessionManager:
    global _manager
    if _manager is None:
        _manager = BrowserSessionManager()
    return _manager


def reset_browser_manager() -> None:
    """Drop the process-wide manager (tests; engine shutdown)."""
    global _manager
    _manager = None
