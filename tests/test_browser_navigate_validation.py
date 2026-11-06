"""E7-01: browser navigation URL validation (scheme + host guard).

The agent browser used to pass any URL straight to Playwright:
``file://`` pages and internal services (the engine's own port,
a router admin page, the cloud link-local metadata address) were
reachable, and the READ_ONLY ``browser_extract`` tool then read the
result back into model context. The vault fill path already refused
non-http(s) pages; the general browsing path had no equivalent rule.

The guard lives in ``aether_engine.browser.session``:

- ``navigate()`` validates BEFORE a driver is started: scheme must
  be http/https, and the host must not be loopback / link-local /
  private-range — as an IP literal, as ``localhost``, or as a
  hostname resolving to such an address.
- The LANDED URL is re-validated after ``goto`` (a public page can
  redirect to an internal one), and ``extract()`` refuses to read
  back a page that sits on a non-public URL, so the read-back half
  of the exfil path is closed too.
- Rejection is :class:`BrowserNavigationRejectedError`, a
  :class:`BrowserError` — the tools layer converts it to a
  ToolExecutionError the model/user sees. Host/scheme only in the
  message, never the full URL (finding M08 hygiene).

Host resolution is injectable (``host_resolver``) exactly the way
the driver is (``driver_factory``), so these tests never touch the
network. A hostname the resolver cannot resolve at all is allowed
through to the navigation attempt: it cannot be proven internal,
and Playwright fails honestly if it truly does not resolve.
"""
import pytest

from aether_engine.audit import AuditLogger
from aether_engine.browser.session import (
    BrowserError,
    BrowserNavigationRejectedError,
    BrowserSession,
    BrowserSessionManager,
    dedicated_profile_dir,
    validate_navigation_url,
)
from aether_engine.browser.tools import create_browser_tools
from aether_engine.tools.registry import ToolExecutionError, ToolRegistry

PLAIN_PAGE = ("<html><head><title>Example Domain</title></head><body>"
              "<h1>Example Domain</h1><p>This domain is for examples.</p>"
              "</body></html>")


class FakeDriver:
    """In-memory driver: a tiny page map, recorded calls, and an
    optional redirect map (requested URL -> landed URL)."""

    def __init__(self, pages=None, redirects=None):
        self.pages = dict(pages or {})
        self.redirects = dict(redirects or {})
        self.calls = []
        self._url = "about:blank"
        self._title = ""
        self._html = ""

    def add_page(self, url, title, html):
        self.pages[url] = (title, html)

    def land_on(self, url, title, html):
        """Put the fake browser on a page directly (as if the user
        or a redirect had taken it there without navigate())."""
        self._url, self._title, self._html = url, title, html

    async def goto(self, url):
        self.calls.append(("goto", url))
        landed = self.redirects.get(url, url)
        title, html = self.pages.get(
            landed, self.pages.get(url, ("", "<html><body></body></html>")))
        self._url, self._title, self._html = landed, title, html

    async def click(self, selector):
        self.calls.append(("click", selector))

    async def fill(self, selector, text):
        self.calls.append(("fill", selector, text))

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


async def public_resolver(host):
    return ("93.184.216.34",)


async def empty_resolver(host):
    return ()


async def exploding_resolver(host):
    raise AssertionError(f"resolver must not be called for {host!r}")


def make_session(tmp_path, driver, resolver=public_resolver):
    """A BrowserSession over a fake driver; returns (session,
    factory_calls) so tests can prove the driver was never started."""
    factory_calls = []

    async def factory(_profile_dir):
        factory_calls.append(_profile_dir)
        return driver

    session = BrowserSession(
        dedicated_profile_dir(),
        audit_logger=AuditLogger(log_path=tmp_path / "audit.log"),
        driver_factory=factory,
        host_resolver=resolver,
    )
    return session, factory_calls


def make_manager(tmp_path, driver, resolver=public_resolver):
    async def factory(_profile_dir):
        return driver

    return BrowserSessionManager(
        audit_logger=AuditLogger(log_path=tmp_path / "audit.log"),
        driver_factory=factory,
        host_resolver=resolver,
    )


def registry_with(manager):
    registry = ToolRegistry()
    for tool in create_browser_tools(manager):
        registry.register(tool)
    return registry


# ---------------------------------------------------------------------
# Scheme allowlist — rejected before Playwright is ever invoked
# ---------------------------------------------------------------------

class TestSchemeAllowlist:
    @pytest.mark.parametrize("url", [
        "file:///etc/passwd",
        "file:///home/user/.aether/vault.db",
        "data:text/html;base64,SGVsbG8=",
        "javascript:alert(1)",
        "chrome://settings",
        "about:blank",
        "ftp://example.com/file",
        "example.com/no-scheme",
        "http://",  # scheme but no host
        "",
    ])
    async def test_non_web_urls_rejected_before_driver_starts(
            self, tmp_path, url):
        driver = FakeDriver()
        session, factory_calls = make_session(tmp_path, driver)
        with pytest.raises(BrowserNavigationRejectedError):
            await session.navigate(url)
        # Rejected before Playwright: the driver was never even
        # created, let alone asked to load the URL.
        assert factory_calls == []
        assert driver.calls == []

    async def test_rejection_is_a_browser_error(self, tmp_path):
        session, _ = make_session(tmp_path, FakeDriver())
        with pytest.raises(BrowserError):
            await session.navigate("file:///etc/passwd")

    async def test_error_names_scheme_not_full_url(self, tmp_path):
        session, _ = make_session(tmp_path, FakeDriver())
        with pytest.raises(BrowserNavigationRejectedError) as excinfo:
            await session.navigate("file:///etc/passwd?token=CANARY")
        message = str(excinfo.value)
        assert "file" in message
        assert "CANARY" not in message
        assert "/etc/passwd" not in message


# ---------------------------------------------------------------------
# Host guard — IP literals
# ---------------------------------------------------------------------

class TestHostGuardLiterals:
    @pytest.mark.parametrize("url", [
        "http://127.0.0.1:8000/admin",       # the engine itself
        "http://127.0.0.1/",
        "http://192.168.1.1/",               # router admin
        "http://10.0.0.8:9000/",
        "http://172.16.5.4/",
        "http://172.31.255.255/",
        "http://169.254.169.254/latest/meta-data",  # cloud metadata
        "http://[::1]:8000/",
        "http://[fe80::1]/",
        "http://[fd00::1]/",
        "http://0.0.0.0/",
    ])
    async def test_internal_ip_literals_rejected(self, tmp_path, url):
        driver = FakeDriver()
        # Literal hosts are judged without any DNS lookup.
        session, factory_calls = make_session(
            tmp_path, driver, resolver=exploding_resolver)
        with pytest.raises(BrowserNavigationRejectedError):
            await session.navigate(url)
        assert factory_calls == []
        assert driver.calls == []

    async def test_internal_ip_error_hides_url_details(self, tmp_path):
        session, _ = make_session(tmp_path, FakeDriver())
        with pytest.raises(BrowserNavigationRejectedError) as excinfo:
            await session.navigate(
                "http://user:pw@127.0.0.1:8000/x?token=CANARY")
        message = str(excinfo.value)
        assert "127.0.0.1" in message
        assert "CANARY" not in message
        assert "pw" not in message


# ---------------------------------------------------------------------
# Host guard — localhost names and DNS resolution
# ---------------------------------------------------------------------

class TestHostGuardNames:
    @pytest.mark.parametrize("url", [
        "http://localhost:8000/",
        "http://localhost./",
        "https://api.localhost/",
        "http://LOCALHOST/",
    ])
    async def test_localhost_names_rejected_without_dns(self, tmp_path, url):
        session, factory_calls = make_session(
            tmp_path, FakeDriver(), resolver=exploding_resolver)
        with pytest.raises(BrowserNavigationRejectedError):
            await session.navigate(url)
        assert factory_calls == []

    async def test_hostname_resolving_to_loopback_rejected(self, tmp_path):
        async def resolver(host):
            assert host == "evil.example.com"
            return ("127.0.0.1",)

        session, factory_calls = make_session(
            tmp_path, FakeDriver(), resolver=resolver)
        with pytest.raises(BrowserNavigationRejectedError):
            await session.navigate("https://evil.example.com/")
        assert factory_calls == []

    async def test_hostname_resolving_mixed_public_and_private_rejected(
            self, tmp_path):
        async def resolver(host):
            return ("93.184.216.34", "10.1.2.3")

        session, _ = make_session(tmp_path, FakeDriver(), resolver=resolver)
        with pytest.raises(BrowserNavigationRejectedError):
            await session.navigate("https://mixed.example.com/")

    async def test_shorthand_loopback_hostname_rejected(self, tmp_path):
        # "127.1" is not an ipaddress literal, but the system
        # resolver (which the default resolver uses) maps it to
        # 127.0.0.1 — the resolution check is what catches it.
        async def resolver(host):
            return ("127.0.0.1",) if host == "127.1" else ()

        session, _ = make_session(tmp_path, FakeDriver(), resolver=resolver)
        with pytest.raises(BrowserNavigationRejectedError):
            await session.navigate("http://127.1:8000/")


# ---------------------------------------------------------------------
# Allowed navigations still work
# ---------------------------------------------------------------------

class TestAllowedNavigation:
    async def test_public_https_reaches_the_page(self, tmp_path):
        driver = FakeDriver(
            {"https://example.com/": ("Example Domain", PLAIN_PAGE)})
        session, _ = make_session(tmp_path, driver)
        outcome = await session.navigate("https://example.com/")
        assert ("goto", "https://example.com/") in driver.calls
        assert outcome["url"] == "https://example.com/"
        assert outcome["title"] == "Example Domain"

    async def test_uppercase_scheme_and_host_allowed(self, tmp_path):
        driver = FakeDriver(
            {"HTTPS://EXAMPLE.COM/": ("Example Domain", PLAIN_PAGE)})
        session, _ = make_session(tmp_path, driver)
        outcome = await session.navigate("HTTPS://EXAMPLE.COM/")
        assert ("goto", "HTTPS://EXAMPLE.COM/") in driver.calls
        assert outcome["title"] == "Example Domain"

    async def test_public_ip_literal_needs_no_dns(self, tmp_path):
        driver = FakeDriver(
            {"https://93.184.216.34/": ("Example Domain", PLAIN_PAGE)})
        session, _ = make_session(
            tmp_path, driver, resolver=exploding_resolver)
        outcome = await session.navigate("https://93.184.216.34/")
        assert ("goto", "https://93.184.216.34/") in driver.calls
        assert outcome["title"] == "Example Domain"

    async def test_unresolvable_hostname_still_attempted(self, tmp_path):
        # The resolver cannot prove the host internal (it cannot
        # resolve it at all): the navigation attempt proceeds and
        # the browser fails honestly if the host truly is bogus.
        driver = FakeDriver(
            {"https://example.com/": ("Example Domain", PLAIN_PAGE)})
        session, _ = make_session(tmp_path, driver, resolver=empty_resolver)
        outcome = await session.navigate("https://example.com/")
        assert ("goto", "https://example.com/") in driver.calls
        assert outcome["title"] == "Example Domain"


# ---------------------------------------------------------------------
# Landed-URL revalidation (redirects) + the extract read-back guard
# ---------------------------------------------------------------------

class TestLandedUrlAndExtract:
    async def test_redirect_to_internal_service_rejected(self, tmp_path):
        driver = FakeDriver(
            {"https://example.com/": ("Example Domain", PLAIN_PAGE)},
            redirects={"https://example.com/": "http://127.0.0.1:8000/admin"})
        session, _ = make_session(tmp_path, driver)
        with pytest.raises(BrowserNavigationRejectedError):
            await session.navigate("https://example.com/")

    async def test_extract_refuses_file_page(self, tmp_path):
        driver = FakeDriver()
        driver.land_on("file:///etc/passwd", "", "root:x:0:0:root:/root:/bin/sh")
        session, _ = make_session(tmp_path, driver)
        with pytest.raises(BrowserNavigationRejectedError):
            await session.extract()
        assert ("extract_text", None) not in driver.calls

    async def test_extract_refuses_internal_service_page(self, tmp_path):
        driver = FakeDriver()
        driver.land_on("http://127.0.0.1:8000/status", "", "engine secrets")
        session, _ = make_session(tmp_path, driver)
        with pytest.raises(BrowserNavigationRejectedError):
            await session.extract()
        assert ("extract_text", None) not in driver.calls

    async def test_extract_on_public_page_still_works(self, tmp_path):
        driver = FakeDriver(
            {"https://example.com/": ("Example Domain", PLAIN_PAGE)})
        session, _ = make_session(tmp_path, driver)
        await session.navigate("https://example.com/")
        text = await session.extract()
        assert "Example Domain" in text


# ---------------------------------------------------------------------
# Tool level: the model sees an explicit error, never a silent no-op
# ---------------------------------------------------------------------

class TestToolLevel:
    async def test_navigate_tool_rejects_file_url(self, tmp_path):
        driver = FakeDriver()
        manager = make_manager(tmp_path, driver)
        registry = registry_with(manager)
        with pytest.raises(ToolExecutionError) as excinfo:
            await registry.execute(
                "browser_navigate", {"url": "file:///etc/passwd"},
                approved=True)
        assert "http" in str(excinfo.value)
        assert driver.calls == []

    async def test_navigate_tool_rejects_internal_url(self, tmp_path):
        driver = FakeDriver()
        manager = make_manager(tmp_path, driver)
        registry = registry_with(manager)
        with pytest.raises(ToolExecutionError):
            await registry.execute(
                "browser_navigate", {"url": "http://192.168.1.1/admin"},
                approved=True)
        assert driver.calls == []

    async def test_extract_tool_rejects_internal_page(self, tmp_path):
        driver = FakeDriver()
        driver.land_on("file:///etc/passwd", "", "root:x:0:0:")
        manager = make_manager(tmp_path, driver)
        registry = registry_with(manager)
        with pytest.raises(ToolExecutionError):
            await registry.execute("browser_extract", {})
        assert ("extract_text", None) not in driver.calls


# ---------------------------------------------------------------------
# The validator itself
# ---------------------------------------------------------------------

class TestValidateNavigationUrl:
    async def test_public_url_passes(self):
        await validate_navigation_url(
            "https://example.com/path?q=1", resolver=public_resolver)

    async def test_ipv6_public_literal_passes(self):
        await validate_navigation_url(
            "https://[2606:2800:220:1:248:1893:25c8:1946]/",
            resolver=exploding_resolver)

    async def test_missing_host_rejected(self):
        with pytest.raises(BrowserNavigationRejectedError):
            await validate_navigation_url("http://", resolver=public_resolver)
