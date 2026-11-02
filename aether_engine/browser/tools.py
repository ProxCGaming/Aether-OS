"""Browser tools + CAPTCHA/2FA detection (Epic 8, E8-6.1).

The browser tools are ordinary :class:`Tool` entries: they are
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
taxonomy that relaxes ordinary browsing arrived with E8-6.2.

E8-6.3 adds the credential vault's two mediated tools.
``browser_fill_login`` takes NO secret argument and returns no
secret: the vault service checks the autofill toggle, binds the
fill to the page's registrable domain, asks the owner first for
ask-every-time entries, and types the password into the form
itself. ``browser_save_login`` stores the credentials sitting in
the current signup form as an agent-created entry for the owner's
review. Neither tool can read a credential back, and no tool
exists for TOTP secrets — agents never handle them.

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
from aether_engine.cost.ledger import get_usage_context
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
    vault_service: Any = None,
) -> list[Tool]:
    """The browser tools, bound to *manager* (default: global).

    ``vault_service`` binds the E8-6.3 vault tools to a specific
    :class:`~aether_engine.vault.service.VaultFillService` (tests);
    by default they resolve the process-wide service lazily, so
    importing or registering the tools never touches the vault.
    """

    def _manager() -> BrowserSessionManager:
        return manager if manager is not None else get_browser_manager()

    def _vault_service() -> Any:
        if vault_service is not None:
            return vault_service
        from aether_engine.vault.service import get_vault_service

        return get_vault_service()

    def _session():
        """The active browser session, bound to the executing task.

        Tool execution learns its identity from the usage-attribution
        ContextVar (:func:`aether_engine.cost.ledger.get_usage_context`):
        the WebSocket task pump sets the chat session id up front and
        refines the task id as TASK_CREATED flows past — the same
        mechanism that attributes model calls to the cost ledger.
        Sessions created here are therefore owned by the task that
        created them, which is what routes CAPTCHA handoffs and
        vault ask-every-time approvals back to that task's stream
        (review finding M04). Outside a task (tests, one-shot calls)
        the context is empty and the session stays unbound, exactly
        as before.
        """
        ctx = get_usage_context()
        return _manager().get_or_create_active(
            task_id=ctx.get("task_id"),
            chat_session_id=ctx.get("session_id"),
        )

    async def _fill_login(username: str | None = None) -> str:
        session = _session()
        try:
            result = await _vault_service().fill_login(
                session, username=username,
                task_id=get_usage_context().get("task_id"))
        except BrowserError as exc:
            raise ToolExecutionError(str(exc)) from exc
        # The result is a sentence with metadata only — the service
        # guarantees the password is never in it.
        return str(result["message"])

    async def _save_login() -> str:
        session = _session()
        try:
            result = await _vault_service().save_login_from_page(session)
        except BrowserError as exc:
            raise ToolExecutionError(str(exc)) from exc
        return str(result["message"])

    async def _navigate(url: str) -> str:
        session = _session()
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
        session = _session()
        try:
            outcome = await session.click(selector)
        except BrowserError as exc:
            raise ToolExecutionError(str(exc)) from exc
        return f"Clicked {selector}." + _challenge_note(outcome.get("challenge"))

    async def _type(selector: str, text: str) -> str:
        session = _session()
        try:
            await session.type_text(selector, text)
        except BrowserError as exc:
            raise ToolExecutionError(str(exc)) from exc
        # Never echo the typed text back: it may be a password.
        return f"Typed into {selector}."

    async def _extract(selector: str | None = None) -> str:
        session = _session()
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
        Tool(
            name="browser_fill_login",
            description=(
                "Fill the current page's login form with the credential "
                "the user saved for THIS site in their vault (Settings → "
                "Password Vault). The password travels vault → form field "
                "directly: you never see it, and you must never ask the "
                "user to reveal it or type it with browser_type. Fills "
                "only on the saved site itself (subdomains included; "
                "lookalike domains are refused), only while vault "
                "autofill is enabled, and entries marked ask-every-time "
                "prompt the user before filling. If several usernames "
                "are saved for the site, ask the user which one and "
                "pass it as 'username'."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "username": {
                        "type": "string",
                        "description": "Optional: which saved username to use when the site has several",
                    },
                },
            },
            execute_fn=_fill_login,
            risk=ToolRisk.WRITE,
            source="browser",
        ),
        Tool(
            name="browser_save_login",
            description=(
                "Save the username and password currently sitting in "
                "this page's form (e.g. the account you just created in "
                "a signup task) into the user's credential vault. The "
                "entry is marked as added by an agent and waits in "
                "Settings → Password Vault for the user's review. It "
                "never overwrites an entry the user saved themselves, "
                "and it cannot read credentials back out of the vault."
            ),
            input_schema={"type": "object", "properties": {}},
            execute_fn=_save_login,
            risk=ToolRisk.WRITE,
            source="browser",
        ),
    ]
