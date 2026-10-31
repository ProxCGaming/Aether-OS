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
        audit_logger: AuditLogger | None = None,
        driver_factory: DriverFactory | None = None,
    ):
        self.profile_dir = validate_profile_dir(profile_dir)
        self.session_id = session_id or uuid.uuid4().hex
        self.task_id = task_id
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

    async def _detect_and_park(self) -> dict[str, Any] | None:
        """Run CAPTCHA/2FA detection over the current page.

        On a hit the session parks in WAITING_USER, the hit is
        audited, and a BROWSER_CHALLENGE_DETECTED event is published
        for the bound task (when one is bound).
        """
        from aether_engine.browser.tools import detect_challenge

        driver = self._driver
        content = await driver.page_content()
        title = await driver.page_title()
        # E8-6.2: refresh the classifier's page snapshot (capped —
        # classification needs element text/attrs, not whole apps).
        self.last_title = title or ""
        self.last_page_content = (content or "")[:200_000]
        hit = detect_challenge(url=self.current_url, title=title, content=content)
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
                    "url": self.current_url,
                    "challenge": hit["challenge"],
                },
            ))
        return hit

    # -- browsing actions (agent state only) --------------------------

    async def navigate(self, url: str) -> dict[str, Any]:
        self._require_agent()
        driver = await self._ensure_driver()
        await driver.goto(url)
        self.current_url = await driver.current_url()
        title = await driver.page_title()
        self._audit("BROWSER_NAVIGATED", {
            "session_id": self.session_id,
            # Host only: a full URL can carry tokens in its query.
            "host": urlparse(self.current_url).netloc,
        })
        challenge = await self._detect_and_park()
        return {"url": self.current_url, "title": title, "challenge": challenge}

    async def click(self, selector: str) -> dict[str, Any]:
        self._require_agent()
        driver = await self._ensure_driver()
        await driver.click(selector)
        self.current_url = await driver.current_url()
        # A click can land on a 2FA step (post-login), so re-detect.
        challenge = await self._detect_and_park()
        return {"url": self.current_url, "challenge": challenge}

    async def type_text(self, selector: str, text: str) -> dict[str, Any]:
        self._require_agent()
        driver = await self._ensure_driver()
        await driver.fill(selector, text)
        return {"url": self.current_url}

    async def extract(self, selector: str | None = None) -> str:
        self._require_agent()
        driver = await self._ensure_driver()
        return await driver.extract_text(selector)

    # -- user controls --------------------------------------------------

    def pause(self) -> None:
        if self._state != BrowserControlState.AGENT:
            raise BrowserError(f"Cannot pause while the browser is {self._state.value}.")
        self._set_state(BrowserControlState.PAUSED, "pause")

    def resume(self) -> None:
        # From STOPPED this resets the session: the browser is
        # already closed, so the next action relaunches it. Only the
        # user can issue a resume — the agent has no path to it.
        if self._state not in (
            BrowserControlState.PAUSED,
            BrowserControlState.WAITING_USER,
            BrowserControlState.STOPPED,
        ):
            raise BrowserError(f"Cannot resume while the browser is {self._state.value}.")
        self.challenge = None
        self._set_state(BrowserControlState.AGENT, "resume")

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

    def get_or_create_active(self, task_id: str | None = None) -> BrowserSession:
        """The live session for browser tools.

        A stopped session is returned as-is (its tools refuse), never
        silently replaced — otherwise the agent could defeat a user
        stop by simply retrying. The user's resume resets it.
        """
        active = self.active_session
        if active is not None:
            if task_id and not active.task_id:
                active.task_id = task_id
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
            audit_logger=self._audit_logger,
            driver_factory=self._driver_factory,
        )
        self._sessions[session.session_id] = session
        self._active_id = session.session_id
        return session

    def get(self, session_id: str) -> BrowserSession | None:
        return self._sessions.get(session_id)

    async def control(self, session_id: str | None, action: str) -> dict[str, Any]:
        """Apply a browser control action (the WS control channel).

        Always answers with a result dict — ``success`` plus the
        resulting ``control_state`` — never raises for a bad action
        or a missing session.
        """
        session = self._sessions.get(session_id) if session_id else self.active_session
        if session is None:
            return {
                "success": False,
                "session_id": session_id,
                "control_state": "none",
                "error": "No browser session exists yet — a browsing task must start one first.",
            }
        try:
            if action == "pause":
                session.pause()
            elif action == "resume":
                session.resume()
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
