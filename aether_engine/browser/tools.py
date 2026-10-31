"""Browser tools + CAPTCHA/2FA detection (Epic 8, E8-6.1).

The four browser tools are ordinary :class:`Tool` entries: they are
registered in the one ToolRegistry, carry risk levels, and pass the
same policy → pre-flight → hooks pipeline as every other tool
(grill Q34 — browser actions are policy-checked like any risky
tool). They execute in-process (see ``ToolRegistry.execute``)
because they drive a live Playwright session held by the engine;
the worker subprocess cannot adopt it.

Risk levels: ``browser_extract`` only reads the page (READ_ONLY,
like ``fetch_url``). ``browser_navigate`` / ``browser_click`` /
``browser_type`` change browser and page state — WRITE, so the
default policy asks for approval; the per-category confirmation
taxonomy that relaxes ordinary browsing arrives with E8-6.2.

Detection (:func:`detect_challenge`) is deliberately simple marker
matching over the URL, title and HTML: well-known CAPTCHA widgets
and verification phrasing, plus 2FA markers (authenticator prompts,
one-time-code inputs). Its only job is to PARK the session — the
agent never attempts to solve a CAPTCHA and never reads, requests
or enters a 2FA code.
"""
from __future__ import annotations

import logging
from typing import Any

from aether_engine.browser.session import (
    BrowserError,
    BrowserSessionManager,
    get_browser_manager,
)
from aether_engine.tools.registry import Tool, ToolExecutionError, ToolRisk

logger = logging.getLogger("aether_engine.browser.tools")

_TWO_FACTOR_MARKERS = (
    "one-time-code",
    "authenticator app",
    "two-factor",
    "two factor",
    "two_factor",
    "verification code",
)
_TWO_FACTOR_URL_MARKERS = ("2fa", "two-factor", "two_factor", "mfa")

_CAPTCHA_MARKERS = (
    "g-recaptcha",
    "h-captcha",
    "recaptcha",
    "hcaptcha",
    "captcha",
    "verify you are human",
    "verify that you are human",
    "are you a robot",
    "i'm not a robot",
    "im not a robot",
)


def detect_challenge(
    *, url: str = "", title: str = "", content: str = ""
) -> dict[str, Any] | None:
    """Detect a CAPTCHA or 2FA prompt on a page.

    Returns ``{"challenge": "captcha" | "two_factor", "url": url}``
    or None. 2FA markers win over CAPTCHA markers: a verification
    step is the more specific (and more sensitive) stop.
    """
    text = f"{title}\n{content}".lower()
    url_lower = (url or "").lower()
    if any(m in text for m in _TWO_FACTOR_MARKERS) or any(
        m in url_lower for m in _TWO_FACTOR_URL_MARKERS
    ):
        return {"challenge": "two_factor", "url": url}
    if any(m in text for m in _CAPTCHA_MARKERS):
        return {"challenge": "captcha", "url": url}
    return None


def _challenge_note(challenge: dict[str, Any] | None) -> str:
    if not challenge:
        return ""
    kind = challenge.get("challenge", "verification")
    return (
        f"\nA {kind} challenge is now showing in the browser window. The "
        "session is parked in a 'waiting for you' state: the user must "
        "complete it there. The agent never solves CAPTCHAs and never "
        "enters 2FA codes, so do not retry browser actions until the "
        "user resumes the browser task."
    )


def create_browser_tools(
    manager: BrowserSessionManager | None = None,
) -> list[Tool]:
    """The four browser tools, bound to *manager* (default: global)."""

    def _manager() -> BrowserSessionManager:
        return manager if manager is not None else get_browser_manager()

    async def _navigate(url: str) -> str:
        session = _manager().get_or_create_active()
        try:
            outcome = await session.navigate(url)
        except BrowserError as exc:
            raise ToolExecutionError(str(exc)) from exc
        title = outcome.get("title") or "(untitled)"
        return (
            f"Navigated to {outcome['url']}\nTitle: {title}"
            + _challenge_note(outcome.get("challenge"))
        )

    async def _click(selector: str) -> str:
        session = _manager().get_or_create_active()
        try:
            outcome = await session.click(selector)
        except BrowserError as exc:
            raise ToolExecutionError(str(exc)) from exc
        return f"Clicked {selector}." + _challenge_note(outcome.get("challenge"))

    async def _type(selector: str, text: str) -> str:
        session = _manager().get_or_create_active()
        try:
            await session.type_text(selector, text)
        except BrowserError as exc:
            raise ToolExecutionError(str(exc)) from exc
        # Never echo the typed text back: it may be a password.
        return f"Typed into {selector}."

    async def _extract(selector: str | None = None) -> str:
        session = _manager().get_or_create_active()
        try:
            return await session.extract(selector)
        except BrowserError as exc:
            raise ToolExecutionError(str(exc)) from exc

    return [
        Tool(
            name="browser_navigate",
            description=(
                "Open a URL in the agent browser (a dedicated profile in a "
                "visible window — never the user's own browser). If the page "
                "shows a CAPTCHA or 2FA prompt, browsing parks and the user "
                "must complete it in the window."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The URL to open"},
                },
                "required": ["url"],
            },
            execute_fn=_navigate,
            risk=ToolRisk.WRITE,
            source="browser",
        ),
        Tool(
            name="browser_click",
            description="Click an element (CSS selector) in the agent browser.",
            input_schema={
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS selector of the element to click"},
                },
                "required": ["selector"],
            },
            execute_fn=_click,
            risk=ToolRisk.WRITE,
            source="browser",
        ),
        Tool(
            name="browser_type",
            description=(
                "Type text into a field (CSS selector) in the agent browser. "
                "Never type passwords, 2FA codes, or other credentials — if a "
                "page asks for them, stop and let the user take control."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "CSS selector of the field"},
                    "text": {"type": "string", "description": "Text to type into the field"},
                },
                "required": ["selector", "text"],
            },
            execute_fn=_type,
            risk=ToolRisk.WRITE,
            source="browser",
        ),
        Tool(
            name="browser_extract",
            description=(
                "Read the visible text of the current page in the agent "
                "browser, or of one element when a CSS selector is given."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "selector": {"type": "string", "description": "Optional CSS selector to read just one element"},
                },
            },
            execute_fn=_extract,
            risk=ToolRisk.READ_ONLY,
            source="browser",
        ),
    ]
