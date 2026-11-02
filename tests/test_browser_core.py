"""Epic 8, E8-6.1: browser core — dedicated profile, visible,
policy-checked, with CAPTCHA/2FA parking.

The agent browser drives Playwright against a DEDICATED profile
(``~/.aether/browser-profile``); the user's real browser and its
profile are never touched. Browser tools are ordinary ToolRegistry
tools: they pass the same policy → pre-flight → hooks pipeline as
every other tool, and they execute in-process (like MCP tools)
because the worker subprocess cannot hold a live browser session.

Playwright is fully faked here — a duck-typed FakeDriver stands in
for the real PlaywrightDriver, so no browser launches and no network
happens in this environment. The missing-Playwright path must fail
closed with an honest error, never a traceback or a silent no-op.

When a page presents a CAPTCHA or a 2FA prompt, the session parks in
a "waiting for you" state: the agent never attempts to solve a
CAPTCHA and never handles 2FA codes; it resumes only when the user
says so (resume / return control).
"""
import asyncio
import sys
import traceback
from pathlib import Path

import pytest

from aether_common.contracts import EventType
from aether_engine.audit import AuditLogger
from aether_engine.browser.session import (
    BrowserError,
    BrowserParkedError,
    BrowserProfileError,
    BrowserSessionManager,
    BrowserUnavailableError,
    dedicated_profile_dir,
    get_browser_manager,
    reset_browser_manager,
    validate_profile_dir,
)
from aether_engine.browser.tools import create_browser_tools, detect_challenge
from aether_engine.cost.ledger import reset_usage_context, set_usage_context
from aether_engine.event_bus import EventBus, event_bus
from aether_engine.tools.registry import (
    ToolExecutionError,
    ToolRegistry,
    ToolRisk,
    build_tool_registry,
)

# ---------------------------------------------------------------------
# Fake Playwright driver (duck-typed stand-in for PlaywrightDriver)
# ---------------------------------------------------------------------

PLAIN_PAGE = ("<html><head><title>Example Domain</title></head><body>"
              "<h1>Example Domain</h1><p>This domain is for examples.</p>"
              "</body></html>")
CAPTCHA_PAGE = ("<html><head><title>Robot check</title></head><body>"
                "<form><div class=\"g-recaptcha\" data-sitekey=\"abc\"></div>"
                "<p>Please verify you are human.</p></form></body></html>")
TWO_FACTOR_PAGE = ("<html><head><title>Two-factor authentication</title></head>"
                   "<body><p>Enter the 6-digit code from your "
                   "authenticator app to continue.</p>"
                   "<input autocomplete=\"one-time-code\" name=\"otp\">"
                   "</body></html>")
LOGIN_PAGE = ("<html><head><title>Sign in</title></head><body>"
              "<form><input type=\"text\" name=\"user\">"
              "<input type=\"password\" name=\"pw\">"
              "<button type=\"submit\">Sign in</button></form></body></html>")


class FakeDriver:
    """In-memory browser driver: a tiny page map, recorded calls."""

    def __init__(self, pages=None):
        self.pages = dict(pages or {})
        self.calls = []
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

    async def extract_text(self, selector=None):
        self.calls.append(("extract_text", selector))
        # Crude tag strip is enough for the fake.
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


def make_manager(tmp_path, driver):
    async def factory(_profile_dir):
        return driver

    return BrowserSessionManager(
        audit_logger=AuditLogger(log_path=tmp_path / "audit.log"),
        driver_factory=factory,
    )


def registry_with(manager):
    registry = ToolRegistry()
    for tool in create_browser_tools(manager):
        registry.register(tool)
    return registry


# ---------------------------------------------------------------------
# Profile isolation
# ---------------------------------------------------------------------

class TestProfileIsolation:
    def test_dedicated_profile_dir_is_under_aether_home(self):
        profile = dedicated_profile_dir()
        assert profile == Path.home() / ".aether" / "browser-profile"

    @pytest.mark.parametrize("bad", [
        Path.home() / ".config" / "google-chrome",
        Path.home() / ".config" / "chromium",
        Path.home() / ".mozilla" / "firefox" / "abcd.default",
        Path.home() / "Library" / "Application Support" / "Google" / "Chrome" / "Default",
        Path.home() / "AppData" / "Local" / "Google" / "Chrome" / "User Data",
        Path.home() / "Downloads",
        Path.home(),
    ])
    def test_user_or_arbitrary_profile_dirs_are_refused(self, bad):
        with pytest.raises(BrowserProfileError):
            validate_profile_dir(bad)

    def test_manager_refuses_a_user_profile(self, tmp_path):
        with pytest.raises(BrowserProfileError):
            BrowserSessionManager(
                profile_dir=Path.home() / ".config" / "google-chrome",
                audit_logger=AuditLogger(log_path=tmp_path / "audit.log"),
            )

    def test_subdir_of_dedicated_root_is_allowed(self):
        ok = dedicated_profile_dir() / "work"
        assert validate_profile_dir(ok) == ok


# ---------------------------------------------------------------------
# Challenge detection (pure fixtures)
# ---------------------------------------------------------------------

class TestDetectChallenge:
    def test_recaptcha_widget_is_a_captcha(self):
        hit = detect_challenge(url="https://example.com/signup",
                               title="Robot check", content=CAPTCHA_PAGE)
        assert hit is not None and hit["challenge"] == "captcha"

    def test_hcaptcha_widget_is_a_captcha(self):
        hit = detect_challenge(url="https://example.com/", title="",
                               content='<div class="h-captcha"></div>')
        assert hit is not None and hit["challenge"] == "captcha"

    def test_verify_human_text_is_a_captcha(self):
        hit = detect_challenge(url="https://example.com/", title="",
                               content="<p>Please verify you are human</p>")
        assert hit is not None and hit["challenge"] == "captcha"

    def test_authenticator_code_prompt_is_two_factor(self):
        hit = detect_challenge(url="https://accounts.example.com/login",
                               title="Two-factor authentication",
                               content=TWO_FACTOR_PAGE)
        assert hit is not None and hit["challenge"] == "two_factor"

    def test_one_time_code_input_is_two_factor(self):
        hit = detect_challenge(url="https://example.com/verify", title="",
                               content='<input autocomplete="one-time-code">')
        assert hit is not None and hit["challenge"] == "two_factor"

    def test_plain_login_page_is_not_a_challenge(self):
        assert detect_challenge(url="https://example.com/login",
                                title="Sign in", content=LOGIN_PAGE) is None

    def test_plain_page_is_not_a_challenge(self):
        assert detect_challenge(url="https://example.com/",
                                title="Example Domain", content=PLAIN_PAGE) is None


# ---------------------------------------------------------------------
# Tools through the real policy pipeline
# ---------------------------------------------------------------------

class TestBrowserTools:
    def test_registry_registers_browser_tools_with_risk_levels(self):
        registry = build_tool_registry()
        names = {t.name: t for t in registry.get_tools()}
        for name in ("browser_navigate", "browser_click", "browser_type", "browser_extract"):
            assert name in names, f"{name} not registered"
        assert names["browser_extract"].risk == ToolRisk.READ_ONLY
        assert names["browser_navigate"].risk == ToolRisk.WRITE
        assert names["browser_click"].risk == ToolRisk.WRITE
        assert names["browser_type"].risk == ToolRisk.WRITE

    async def test_navigate_requires_approval_by_default(self, tmp_path):
        driver = FakeDriver({"https://example.com/": ("Example Domain", PLAIN_PAGE)})
        registry = registry_with(make_manager(tmp_path, driver))
        with pytest.raises(ToolExecutionError, match="requires approval"):
            await registry.execute("browser_navigate", {"url": "https://example.com/"})
        assert driver.calls == []  # policy stopped it before any browsing

    async def test_navigate_denied_by_user_policy(self, tmp_path):
        driver = FakeDriver({"https://example.com/": ("Example Domain", PLAIN_PAGE)})
        registry = registry_with(make_manager(tmp_path, driver))
        with pytest.raises(ToolExecutionError, match="blocked by policy"):
            await registry.execute(
                "browser_navigate", {"url": "https://example.com/"},
                policies={"browser_navigate": "Deny"}, approved=True)
        assert driver.calls == []

    async def test_approved_navigate_and_extract(self, tmp_path):
        driver = FakeDriver({"https://example.com/": ("Example Domain", PLAIN_PAGE)})
        registry = registry_with(make_manager(tmp_path, driver))
        result = await registry.execute(
            "browser_navigate", {"url": "https://example.com/"}, approved=True)
        assert "https://example.com/" in result
        assert "Example Domain" in result
        assert ("goto", "https://example.com/") in driver.calls
        # extract is READ_ONLY: no approval needed.
        text = await registry.execute("browser_extract", {})
        assert "Example Domain" in text

    async def test_click_and_type_reach_the_driver(self, tmp_path):
        driver = FakeDriver({"https://example.com/": ("Example Domain", PLAIN_PAGE)})
        registry = registry_with(make_manager(tmp_path, driver))
        await registry.execute("browser_navigate", {"url": "https://example.com/"}, approved=True)
        await registry.execute("browser_click", {"selector": "#go"}, approved=True)
        await registry.execute(
            "browser_type", {"selector": "#q", "text": "hello"}, approved=True)
        assert ("click", "#go") in driver.calls
        assert ("fill", "#q", "hello") in driver.calls


# ---------------------------------------------------------------------
# Control states: pause / take-control / stop
# ---------------------------------------------------------------------

class TestControlStates:
    async def _started(self, tmp_path, pages=None):
        driver = FakeDriver(pages or {"https://example.com/": ("Example Domain", PLAIN_PAGE)})
        manager = make_manager(tmp_path, driver)
        session = manager.get_or_create_active()
        await session.navigate("https://example.com/")
        return manager, session, driver

    async def test_pause_blocks_actions_until_resume(self, tmp_path):
        manager, session, driver = await self._started(tmp_path)
        result = await manager.control(session.session_id, "pause")
        assert result["success"] is True
        assert result["control_state"] == "paused"
        with pytest.raises(ToolExecutionError, match="paused"):
            await registry_with(manager).execute("browser_click", {"selector": "#x"}, approved=True)
        await manager.control(session.session_id, "resume")
        await registry_with(manager).execute("browser_click", {"selector": "#x"}, approved=True)
        assert ("click", "#x") in driver.calls

    async def test_take_control_blocks_agent_until_returned(self, tmp_path):
        manager, session, _driver = await self._started(tmp_path)
        result = await manager.control(session.session_id, "take_control")
        assert result["control_state"] == "user"
        with pytest.raises(ToolExecutionError, match="control"):
            await registry_with(manager).execute(
                "browser_navigate", {"url": "https://example.com/"}, approved=True)
        await manager.control(session.session_id, "return_control")
        await registry_with(manager).execute(
            "browser_navigate", {"url": "https://example.com/"}, approved=True)

    async def test_stop_halts_within_one_step(self, tmp_path):
        manager, session, driver = await self._started(tmp_path)
        result = await manager.control(session.session_id, "stop")
        assert result["control_state"] == "stopped"
        assert driver.closed is True
        calls_at_stop = list(driver.calls)
        with pytest.raises(ToolExecutionError, match="stopped"):
            await registry_with(manager).execute("browser_click", {"selector": "#x"}, approved=True)
        assert driver.calls == calls_at_stop  # nothing reached the browser after stop

    async def test_user_resume_after_stop_resets_the_session(self, tmp_path):
        manager, session, _driver = await self._started(tmp_path)
        await manager.control(session.session_id, "stop")
        # Only the user's resume re-arms browsing (browser relaunches).
        result = await manager.control(session.session_id, "resume")
        assert result["control_state"] == "agent"
        await registry_with(manager).execute(
            "browser_navigate", {"url": "https://example.com/"}, approved=True)

    async def test_control_without_a_session_is_an_honest_error(self, tmp_path):
        manager = make_manager(tmp_path, FakeDriver())
        result = await manager.control(None, "pause")
        assert result["success"] is False
        assert result["control_state"] == "none"

    async def test_unknown_control_action_is_an_error(self, tmp_path):
        manager, session, _driver = await self._started(tmp_path)
        result = await manager.control(session.session_id, "explode")
        assert result["success"] is False
        assert "error" in result


# ---------------------------------------------------------------------
# CAPTCHA / 2FA parking
# ---------------------------------------------------------------------

class TestChallengeParking:
    async def test_captcha_parks_the_session_and_notifies(self, tmp_path):
        bus = EventBus()
        queue: asyncio.Queue = asyncio.Queue()
        driver = FakeDriver({"https://example.com/signup": ("Robot check", CAPTCHA_PAGE)})
        manager = make_manager(tmp_path, driver)
        session = manager.get_or_create_active(task_id="task-42")
        bus.subscribe("task-42", queue)
        try:
            result = await registry_with(manager).execute(
                "browser_navigate", {"url": "https://example.com/signup"}, approved=True)
        finally:
            bus.unsubscribe("task-42", queue)
        assert session.control_state.value == "waiting_user"
        assert "waiting for you" in result.lower()
        event = queue.get_nowait()
        assert event.type == EventType.BROWSER_CHALLENGE_DETECTED
        assert event.payload["challenge"] == "captcha"
        assert event.payload["session_id"] == session.session_id

        # Parked: the agent attempts nothing further on the page.
        calls_at_park = list(driver.calls)
        with pytest.raises(ToolExecutionError, match="waiting for you"):
            await registry_with(manager).execute("browser_click", {"selector": "#submit"}, approved=True)
        assert driver.calls == calls_at_park

        # Only the user's resume un-parks it — and since M05 the
        # resume first re-checks the live page, so the user must
        # actually complete the challenge in the window (the page
        # moves on) before the park lifts.
        show_page(driver, "Example Domain", PLAIN_PAGE)
        outcome = await manager.control(session.session_id, "resume")
        assert outcome["control_state"] == "agent"
        await registry_with(manager).execute("browser_click", {"selector": "#submit"}, approved=True)
        assert ("click", "#submit") in driver.calls

    async def test_two_factor_page_parks_as_two_factor(self, tmp_path):
        driver = FakeDriver({"https://accounts.example.com/login": ("Two-factor authentication", TWO_FACTOR_PAGE)})
        manager = make_manager(tmp_path, driver)
        session = manager.get_or_create_active()
        result = await session.navigate("https://accounts.example.com/login")
        assert result["challenge"]["challenge"] == "two_factor"
        assert session.control_state.value == "waiting_user"
        with pytest.raises(BrowserParkedError, match="waiting for you"):
            await session.type_text("#otp", "123456")  # never handled by the agent


# ---------------------------------------------------------------------
# Challenge guards on typing and resume (review finding M05)
# ---------------------------------------------------------------------

def show_page(driver, title, html):
    """Point the fake's live page at new content WITHOUT navigating.

    Models a dynamic in-page transition (a login step swapping in a
    2FA form): no goto/click happens, so no post-action detection
    ever runs and the cached control state stays "agent" — exactly
    the gap M05 reproduced.
    """
    driver._title, driver._html = title, html


class GuardDriver(FakeDriver):
    """FakeDriver plus field reads and an unreadable-page switch."""

    def __init__(self, pages=None):
        super().__init__(pages)
        self.fail_reads = False

    async def input_value(self, selector):
        self.calls.append(("input_value", selector))
        return ""

    async def page_content(self):
        if self.fail_reads:
            raise RuntimeError("renderer gone")
        return await super().page_content()


class TestChallengeGuardsM05:
    """Detection must not depend on a prior navigate/click, and a
    resume must not un-park a challenge that is still on screen."""

    async def test_dynamic_challenge_blocks_type_text(self, tmp_path):
        driver = FakeDriver({"https://example.com/login": ("Sign in", LOGIN_PAGE)})
        manager = make_manager(tmp_path, driver)
        session = manager.get_or_create_active()
        await session.navigate("https://example.com/login")
        assert session.control_state.value == "agent"
        # The login step swaps in a 2FA form in-page: no navigate,
        # no click — detection has never seen this content.
        show_page(driver, "Two-factor authentication", TWO_FACTOR_PAGE)
        with pytest.raises(BrowserParkedError, match="waiting for you"):
            await session.type_text("#otp", "123456")
        assert session.control_state.value == "waiting_user"
        assert session.challenge["challenge"] == "two_factor"
        assert not any(c[0] == "fill" for c in driver.calls)

    async def test_dynamic_challenge_blocks_browser_type_tool(self, tmp_path):
        driver = FakeDriver({"https://example.com/login": ("Sign in", LOGIN_PAGE)})
        manager = make_manager(tmp_path, driver)
        registry = registry_with(manager)
        await registry.execute(
            "browser_navigate", {"url": "https://example.com/login"}, approved=True)
        show_page(driver, "Two-factor authentication", TWO_FACTOR_PAGE)
        with pytest.raises(ToolExecutionError, match="waiting for you"):
            await registry.execute(
                "browser_type", {"selector": "#otp", "text": "123456"}, approved=True)
        assert not any(c[0] == "fill" for c in driver.calls)

    async def test_dynamic_challenge_blocks_field_value(self, tmp_path):
        driver = GuardDriver({"https://example.com/login": ("Sign in", LOGIN_PAGE)})
        manager = make_manager(tmp_path, driver)
        session = manager.get_or_create_active()
        await session.navigate("https://example.com/login")
        show_page(driver, "Two-factor authentication", TWO_FACTOR_PAGE)
        with pytest.raises(BrowserParkedError, match="waiting for you"):
            await session.field_value("#otp")
        assert not any(c[0] == "input_value" for c in driver.calls)

    async def test_type_text_fails_closed_when_the_page_is_unreadable(self, tmp_path):
        driver = GuardDriver({"https://example.com/login": ("Sign in", LOGIN_PAGE)})
        manager = make_manager(tmp_path, driver)
        session = manager.get_or_create_active()
        await session.navigate("https://example.com/login")
        driver.fail_reads = True
        with pytest.raises(BrowserParkedError, match="fail closed"):
            await session.type_text("#user", "alice")
        assert not any(c[0] == "fill" for c in driver.calls)

    async def test_resume_refused_while_the_challenge_is_still_present(self, tmp_path):
        driver = FakeDriver({"https://example.com/signup": ("Robot check", CAPTCHA_PAGE)})
        manager = make_manager(tmp_path, driver)
        session = manager.get_or_create_active()
        await session.navigate("https://example.com/signup")
        assert session.control_state.value == "waiting_user"
        # The user hits Resume without completing the challenge:
        # the park must stand (before the fix this un-parked blindly).
        outcome = await manager.control(session.session_id, "resume")
        assert outcome["success"] is False
        assert session.control_state.value == "waiting_user"
        assert session.challenge is not None
        # The user completes it in the window; the page moves on.
        show_page(driver, "Example Domain", PLAIN_PAGE)
        outcome = await manager.control(session.session_id, "resume")
        assert outcome["success"] is True
        assert outcome["control_state"] == "agent"
        assert session.challenge is None

    async def test_resume_from_pause_does_not_need_a_challenge_check(self, tmp_path):
        driver = FakeDriver({"https://example.com/": ("Example Domain", PLAIN_PAGE)})
        manager = make_manager(tmp_path, driver)
        session = manager.get_or_create_active()
        await session.navigate("https://example.com/")
        await manager.control(session.session_id, "pause")
        outcome = await manager.control(session.session_id, "resume")
        assert outcome["success"] is True
        assert outcome["control_state"] == "agent"


# ---------------------------------------------------------------------
# Driver exception hygiene (review finding M08)
# ---------------------------------------------------------------------

TEXT_CANARY = "Canary!Typed-Text-9e1b7-DO-NOT-LEAK"
URL_TOKEN_CANARY = "Canary!Url-Token-4d2c8-DO-NOT-LEAK"


class LeakyDriver(FakeDriver):
    """A driver whose failures embed their call arguments.

    Real driver stacks build error text from the call — the
    selector, the URL, and for fill() the very value being typed.
    Finding M08: that text must never cross the session boundary,
    because on the credential-vault path the typed value IS the
    password. ``fail_on`` names the driver methods that explode.
    """

    def __init__(self, pages=None, fail_on=()):
        super().__init__(pages)
        self.fail_on = set(fail_on)

    async def goto(self, url):
        if "goto" in self.fail_on:
            raise RuntimeError(f"driver: navigation to {url} failed")
        await super().goto(url)

    async def click(self, selector):
        if "click" in self.fail_on:
            raise RuntimeError(
                f"driver: click on {selector} failed; page near {TEXT_CANARY}")
        await super().click(selector)

    async def fill(self, selector, text):
        if "fill" in self.fail_on:
            raise RuntimeError(
                f"driver: fill({selector}, {text!r}) timed out")
        await super().fill(selector, text)

    async def extract_text(self, selector=None):
        if "extract" in self.fail_on:
            raise RuntimeError(
                f"driver: extract({selector}) failed; page near {TEXT_CANARY}")
        return await super().extract_text(selector)


class TestDriverExceptionHygiene:
    """Session action methods raise only BrowserError, and the
    converted error carries no driver text — in its message OR in
    a rendered traceback (the exception chain is suppressed, so
    logging the error cannot resurrect the driver's version)."""

    async def test_type_text_driver_error_is_normalised_and_secret_free(
            self, tmp_path):
        driver = LeakyDriver(
            {"https://example.com/login": ("Sign in", LOGIN_PAGE)},
            fail_on={"fill"})
        manager = make_manager(tmp_path, driver)
        session = manager.get_or_create_active()
        await session.navigate("https://example.com/login")
        with pytest.raises(BrowserError) as excinfo:
            await session.type_text("#user", TEXT_CANARY)
        err = excinfo.value
        assert TEXT_CANARY not in str(err)
        assert TEXT_CANARY not in "".join(traceback.format_exception(err))
        assert err.__cause__ is None
        assert err.__suppress_context__ is True

    async def test_browser_type_tool_error_carries_no_typed_text(
            self, tmp_path):
        driver = LeakyDriver(
            {"https://example.com/login": ("Sign in", LOGIN_PAGE)},
            fail_on={"fill"})
        manager = make_manager(tmp_path, driver)
        registry = registry_with(manager)
        await registry.execute(
            "browser_navigate", {"url": "https://example.com/login"},
            approved=True)
        with pytest.raises(ToolExecutionError) as excinfo:
            await registry.execute(
                "browser_type",
                {"selector": "#user", "text": TEXT_CANARY},
                approved=True)
        assert TEXT_CANARY not in str(excinfo.value)

    async def test_navigate_driver_error_hides_url_details(self, tmp_path):
        url = f"https://example.com/login?session_token={URL_TOKEN_CANARY}"
        driver = LeakyDriver(fail_on={"goto"})
        manager = make_manager(tmp_path, driver)
        session = manager.get_or_create_active()
        with pytest.raises(BrowserError) as excinfo:
            await session.navigate(url)
        err = excinfo.value
        assert URL_TOKEN_CANARY not in str(err)
        assert URL_TOKEN_CANARY not in "".join(
            traceback.format_exception(err))

    async def test_click_and_extract_driver_errors_are_normalised(
            self, tmp_path):
        driver = LeakyDriver(
            {"https://example.com/login": ("Sign in", LOGIN_PAGE)},
            fail_on={"click", "extract"})
        manager = make_manager(tmp_path, driver)
        session = manager.get_or_create_active()
        await session.navigate("https://example.com/login")
        with pytest.raises(BrowserError) as click_err:
            await session.click("#submit")
        assert TEXT_CANARY not in str(click_err.value)
        with pytest.raises(BrowserError) as extract_err:
            await session.extract()
        assert TEXT_CANARY not in str(extract_err.value)


# ---------------------------------------------------------------------
# Task identity binding (review finding M04)
# ---------------------------------------------------------------------

class TestTaskIdentityBinding:
    """Tool-created sessions belong to the task that created them.

    Tool execution learns its identity from the usage-attribution
    ContextVar (aether_engine.cost.ledger): the WebSocket task pump
    sets the chat session id up front and refines the task id as
    TASK_CREATED flows past — the same mechanism that attributes
    model calls to the ledger. Before the fix the tools never read
    it, so sessions were created with task_id=None: CAPTCHA handoff
    events silently went nowhere and ask-every-time vault fills
    always refused with approval_unavailable.
    """

    async def test_tool_created_session_binds_the_usage_context_identity(self, tmp_path):
        driver = FakeDriver({"https://example.com/": ("Example Domain", PLAIN_PAGE)})
        manager = make_manager(tmp_path, driver)
        token = set_usage_context(session_id="chat-7", task_id="task-7")
        try:
            await registry_with(manager).execute(
                "browser_navigate", {"url": "https://example.com/"}, approved=True)
        finally:
            reset_usage_context(token)
        session = manager.active_session
        assert session is not None
        assert session.task_id == "task-7"
        assert session.chat_session_id == "chat-7"

    async def test_tool_created_session_without_context_stays_unbound(self, tmp_path):
        driver = FakeDriver({"https://example.com/": ("Example Domain", PLAIN_PAGE)})
        manager = make_manager(tmp_path, driver)
        await registry_with(manager).execute(
            "browser_navigate", {"url": "https://example.com/"}, approved=True)
        session = manager.active_session
        assert session.task_id is None
        assert session.chat_session_id is None

    async def test_challenge_event_publishes_for_a_tool_created_session(self, tmp_path):
        queue: asyncio.Queue = asyncio.Queue()
        event_bus.subscribe("task-chal", queue)
        driver = FakeDriver({"https://example.com/signup": ("Robot check", CAPTCHA_PAGE)})
        manager = make_manager(tmp_path, driver)
        token = set_usage_context(session_id="chat-chal", task_id="task-chal")
        try:
            await registry_with(manager).execute(
                "browser_navigate", {"url": "https://example.com/signup"}, approved=True)
        finally:
            reset_usage_context(token)
            event_bus.unsubscribe("task-chal", queue)
        session = manager.active_session
        assert session.control_state.value == "waiting_user"
        event = queue.get_nowait()
        assert event.type == EventType.BROWSER_CHALLENGE_DETECTED
        assert event.payload["session_id"] == session.session_id
        assert event.payload["task_id"] == "task-chal"
        assert event.payload["chat_session_id"] == "chat-chal"

    async def test_control_resolves_the_owning_chat_session_id(self, tmp_path):
        # The shipped desktop addressed control with the CHAT session
        # id in the session_id field, which never matches a browser
        # session UUID — every control silently failed. The manager
        # now resolves the chat binding.
        driver = FakeDriver({"https://example.com/": ("Example Domain", PLAIN_PAGE)})
        manager = make_manager(tmp_path, driver)
        session = manager.get_or_create_active(task_id="task-1", chat_session_id="chat-1")
        await session.navigate("https://example.com/")
        outcome = await manager.control("chat-1", "pause")
        assert outcome["success"] is True
        assert outcome["control_state"] == "paused"
        assert outcome["chat_session_id"] == "chat-1"
        assert outcome["task_id"] == "task-1"

    async def test_control_resolves_the_chat_session_id_parameter(self, tmp_path):
        driver = FakeDriver({"https://example.com/": ("Example Domain", PLAIN_PAGE)})
        manager = make_manager(tmp_path, driver)
        session = manager.get_or_create_active(task_id="task-1", chat_session_id="chat-1")
        await session.navigate("https://example.com/")
        outcome = await manager.control(None, "pause", chat_session_id="chat-1")
        assert outcome["success"] is True
        assert outcome["control_state"] == "paused"

    async def test_control_with_an_unknown_identifier_never_falls_back(self, tmp_path):
        # An identifier that matches nothing is an honest error —
        # never a silent fallback to whatever session is active.
        driver = FakeDriver({"https://example.com/": ("Example Domain", PLAIN_PAGE)})
        manager = make_manager(tmp_path, driver)
        session = manager.get_or_create_active(task_id="task-1", chat_session_id="chat-1")
        await session.navigate("https://example.com/")
        outcome = await manager.control("no-such-session", "pause")
        assert outcome["success"] is False
        assert session.control_state.value == "agent"
        outcome = await manager.control(None, "pause", chat_session_id="no-such-chat")
        assert outcome["success"] is False
        assert session.control_state.value == "agent"


# ---------------------------------------------------------------------
# Missing Playwright: fail closed, honestly
# ---------------------------------------------------------------------

class TestPlaywrightMissing:
    async def test_session_start_fails_closed_without_playwright(self, tmp_path, monkeypatch):
        monkeypatch.setitem(sys.modules, "playwright", None)
        monkeypatch.setitem(sys.modules, "playwright.async_api", None)
        manager = BrowserSessionManager(
            audit_logger=AuditLogger(log_path=tmp_path / "audit.log"))
        session = manager.get_or_create_active()
        with pytest.raises(BrowserUnavailableError, match="Playwright is not installed"):
            await session.navigate("https://example.com/")

    async def test_tool_surfaces_unavailable_as_tool_error(self, tmp_path, monkeypatch):
        monkeypatch.setitem(sys.modules, "playwright", None)
        monkeypatch.setitem(sys.modules, "playwright.async_api", None)
        manager = BrowserSessionManager(
            audit_logger=AuditLogger(log_path=tmp_path / "audit.log"))
        with pytest.raises(ToolExecutionError, match="Playwright is not installed"):
            await registry_with(manager).execute(
                "browser_navigate", {"url": "https://example.com/"}, approved=True)


# ---------------------------------------------------------------------
# Audit trail + singleton
# ---------------------------------------------------------------------

class TestAuditAndSingleton:
    async def test_browser_actions_are_audited(self, tmp_path):
        log_path = tmp_path / "audit.log"
        driver = FakeDriver({"https://example.com/": ("Example Domain", PLAIN_PAGE)})
        manager = make_manager(tmp_path, driver)
        session = manager.get_or_create_active()
        await session.navigate("https://example.com/")
        await manager.control(session.session_id, "pause")
        text = log_path.read_text(encoding="utf-8")
        assert "BROWSER_SESSION_STARTED" in text
        assert "BROWSER_NAVIGATED" in text
        assert "BROWSER_CONTROL_CHANGED" in text
        # Full URLs can carry tokens in query strings; the audit trail
        # records the host, never the full URL.
        assert "example.com" in text

    def test_global_manager_is_a_singleton(self):
        reset_browser_manager()
        try:
            assert get_browser_manager() is get_browser_manager()
        finally:
            reset_browser_manager()


# ---------------------------------------------------------------------
# WebSocket control channel (BROWSER_TASK_PAUSE_REQUEST)
# ---------------------------------------------------------------------

class TestBrowserControlWebSocket:
    """Drives the real /ws/tasks endpoint, as in
    test_mcp_websocket_handlers.py, with the browser manager's
    singleton swapped for a fake-driver manager."""

    def setup_method(self):
        import tempfile
        from pathlib import Path as _P
        from unittest.mock import patch

        from aether_engine.app import engine_state

        self._patch = patch
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
        self.driver = FakeDriver({"https://example.com/": ("Example Domain", PLAIN_PAGE)})
        self.manager = make_manager(self.tmp_path, self.driver)
        self._mgr_patcher = patch(
            "aether_engine.browser.session._manager", self.manager)
        self._mgr_patcher.start()

    def teardown_method(self):
        self._mgr_patcher.stop()
        self._db_patcher.stop()
        self.tmp_dir.cleanup()

    def _connect(self, client):
        from aether_engine.app import engine_state

        token = engine_state.token_path.read_text(encoding="utf-8").strip()
        return client.websocket_connect(f"/ws/tasks?token={token}")

    def _control(self, ws, action, session_id=None):
        from aether_common.contracts import Event

        payload = {"action": action}
        if session_id:
            payload["session_id"] = session_id
        ws.send_text(Event(
            type=EventType.BROWSER_TASK_PAUSE_REQUEST, payload=payload).to_json())
        return Event.from_json(ws.receive_text())

    async def _unused(self):  # pragma: no cover - keeps linters calm
        pass

    def test_pause_take_control_stop_round_trip(self):
        from starlette.testclient import TestClient

        from aether_engine.app import app

        session = self.manager.get_or_create_active()
        asyncio.run(session.navigate("https://example.com/"))
        with TestClient(app) as client, self._connect(client) as ws:
            ws.receive_text()  # HELLO
            resp = self._control(ws, "pause", session.session_id)
            assert resp.type == EventType.BROWSER_TASK_PAUSE_RESPONSE
            assert resp.payload["success"] is True
            assert resp.payload["control_state"] == "paused"
            resp = self._control(ws, "take_control", session.session_id)
            assert resp.payload["control_state"] == "user"
            resp = self._control(ws, "stop", session.session_id)
            assert resp.payload["control_state"] == "stopped"
            assert self.driver.closed is True

    def test_control_without_session_reports_none(self):
        from starlette.testclient import TestClient

        from aether_engine.app import app

        with TestClient(app) as client, self._connect(client) as ws:
            ws.receive_text()  # HELLO
            resp = self._control(ws, "pause")
            assert resp.type == EventType.BROWSER_TASK_PAUSE_RESPONSE
            assert resp.payload["success"] is False
            assert resp.payload["control_state"] == "none"

    def test_control_by_chat_session_id_round_trip(self):
        # M04: the desktop addresses browser control by the chat
        # session that owns the browser — in the dedicated
        # chat_session_id field, and (the shipped build's habit) in
        # the session_id field. Both must reach the bound session,
        # and the response must carry the full identity back.
        from starlette.testclient import TestClient

        from aether_common.contracts import Event
        from aether_engine.app import app

        session = self.manager.get_or_create_active(
            task_id="task-ws-1", chat_session_id="chat-ws-1")
        asyncio.run(session.navigate("https://example.com/"))
        with TestClient(app) as client, self._connect(client) as ws:
            ws.receive_text()  # HELLO
            ws.send_text(Event(
                type=EventType.BROWSER_TASK_PAUSE_REQUEST,
                payload={"action": "pause", "chat_session_id": "chat-ws-1"},
            ).to_json())
            resp = Event.from_json(ws.receive_text())
            assert resp.type == EventType.BROWSER_TASK_PAUSE_RESPONSE
            assert resp.payload["success"] is True
            assert resp.payload["control_state"] == "paused"
            assert resp.payload["session_id"] == session.session_id
            assert resp.payload["chat_session_id"] == "chat-ws-1"
            assert resp.payload["task_id"] == "task-ws-1"
            resp = self._control(ws, "resume", session_id="chat-ws-1")
            assert resp.payload["success"] is True
            assert resp.payload["control_state"] == "agent"
