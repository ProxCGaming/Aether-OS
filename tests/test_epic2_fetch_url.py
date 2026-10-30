"""Epic 2, E2-05: fetch_url — extraction, citations, SSRF guard, caps.

No real network: the fetch loop is seam-driven, so tests fake the two
module functions that touch the outside world — ``_resolve_addresses``
(DNS) and ``_request_once`` (one HTTP hop) — and exercise the real
guard, redirect and extraction code against them.
"""

from __future__ import annotations

import socket

import pytest

from aether_engine.tools import web_fetch
from aether_engine.tools.registry import ToolRisk, build_tool_registry
from aether_engine.tools.web_fetch import (
    FetchResponse,
    FetchTimeoutError,
    _fetch_url_impl,
)

PUBLIC_IP = "93.184.216.34"

ARTICLE_HTML = """<!doctype html>
<html>
<head><title>Deep Sea Mining Explained</title></head>
<body>
<nav class="site-nav">Home About Contact Subscribe Login</nav>
<aside class="promo">Buy things now! Limited offer, click here!</aside>
<article>
<h1>Deep Sea Mining Explained</h1>
<p>The deep ocean floor holds vast quantities of polymetallic nodules,
potato-sized rocks rich in nickel, cobalt, copper and manganese that took
millions of years to form around shark teeth and fragments of shell.</p>
<p>Supporters argue these metals are essential for the batteries that power
electric vehicles and store renewable energy, and that collecting nodules
from the seabed disturbs far less land than opening new mines on shore.</p>
<p>Critics counter that almost nothing is known about the ecosystems of the
abyssal plain, that sediment plumes could smother life across wide areas,
and that the damage, once done, would be effectively irreversible.</p>
<p>Regulators at the International Seabed Authority continue to debate the
rules under which commercial collection might one day be permitted.</p>
</article>
<footer>Copyright 2026 Example News. Privacy policy. Terms of service.</footer>
</body>
</html>"""


def _html_response(html: str, status: int = 200) -> FetchResponse:
    return FetchResponse(
        status=status,
        headers={"content-type": "text/html; charset=utf-8"},
        body=html.encode("utf-8"),
    )


def _fake_resolver(mapping: dict[str, list[str]]):
    def _resolve(host: str) -> list[str]:
        if host in mapping:
            return mapping[host]
        raise socket.gaierror(f"unknown host: {host}")

    return _resolve


def _fake_transport(routes: dict[str, FetchResponse]):
    def _request(url: str, timeout_s: float) -> FetchResponse:
        if url not in routes:
            raise AssertionError(f"unexpected fetch of {url}")
        return routes[url]

    return _request


@pytest.fixture()
def net(monkeypatch):
    """Patch both seams to a public example.com world; returns helpers."""

    def install(routes, dns=None):
        monkeypatch.setattr(
            web_fetch,
            "_resolve_addresses",
            _fake_resolver(dns if dns is not None else {"example.com": [PUBLIC_IP]}),
        )
        monkeypatch.setattr(web_fetch, "_request_once", _fake_transport(routes))

    return install


def test_extracts_article_with_citation_header(net):
    net({"https://example.com/article": _html_response(ARTICLE_HTML)})
    out = _fetch_url_impl("https://example.com/article")
    assert out.startswith(
        "[fetch_url: https://example.com/article | title: Deep Sea Mining Explained]"
    )
    assert "polymetallic nodules" in out
    assert "International Seabed Authority" in out
    # nav / promo / footer junk is reduced by the extractor
    assert "Buy things now" not in out
    assert "Privacy policy" not in out


def test_redirect_chain_cites_final_url(net):
    net(
        {
            "https://example.com/old": FetchResponse(status=301, location="/final"),
            "https://example.com/final": _html_response(ARTICLE_HTML),
        }
    )
    out = _fetch_url_impl("https://example.com/old")
    assert out.startswith("[fetch_url: https://example.com/final | title:")
    assert "polymetallic nodules" in out


def test_redirect_into_private_ip_is_refused(net):
    net(
        {
            "https://example.com/start": FetchResponse(
                status=302, location="http://169.254.169.254/latest/meta-data"
            ),
        },
        dns={"example.com": [PUBLIC_IP], "169.254.169.254": ["169.254.169.254"]},
    )
    out = _fetch_url_impl("https://example.com/start")
    assert "refusing to fetch" in out
    assert "link-local" in out


def test_loopback_refused_without_opt_in(net, monkeypatch):
    monkeypatch.delenv("AETHER_ALLOW_LOCAL_FETCH", raising=False)
    net(
        {"http://127.0.0.1:8000/page": _html_response(ARTICLE_HTML)},
        dns={"127.0.0.1": ["127.0.0.1"]},
    )
    out = _fetch_url_impl("http://127.0.0.1:8000/page")
    assert "refusing to fetch" in out
    assert "loopback" in out


def test_private_hostname_refused(net):
    net(
        {"http://nas.internal/notes": _html_response(ARTICLE_HTML)},
        dns={"nas.internal": ["192.168.1.10"]},
    )
    out = _fetch_url_impl("http://nas.internal/notes")
    assert "refusing to fetch" in out
    assert "private address" in out


def test_loopback_allowed_with_env_opt_in(net, monkeypatch):
    monkeypatch.setenv("AETHER_ALLOW_LOCAL_FETCH", "1")
    net({"http://127.0.0.1:8000/page": _html_response(ARTICLE_HTML)})
    out = _fetch_url_impl("http://127.0.0.1:8000/page")
    assert out.startswith("[fetch_url: http://127.0.0.1:8000/page | title:")
    assert "polymetallic nodules" in out


def test_non_http_schemes_refused(net, monkeypatch):
    def _boom(url, timeout_s):
        raise AssertionError("transport must not be called for a refused scheme")

    monkeypatch.setattr(web_fetch, "_request_once", _boom)
    for url in ("file:///etc/passwd", "ftp://example.com/x", "data:text/html,hi"):
        out = _fetch_url_impl(url)
        assert "only supports http/https" in out, url


def test_http_500_is_an_honest_error(net):
    net({"https://example.com/broken": FetchResponse(status=500, body=b"boom")})
    out = _fetch_url_impl("https://example.com/broken")
    assert "HTTP 500" in out
    assert "https://example.com/broken" in out


def test_dns_failure_is_an_honest_error(net):
    net({})  # resolver knows no hosts
    out = _fetch_url_impl("https://missing.example/article")
    assert "could not resolve host 'missing.example'" in out


def test_timeout_is_an_honest_error(net, monkeypatch):
    monkeypatch.setattr(web_fetch, "_resolve_addresses", _fake_resolver({"example.com": [PUBLIC_IP]}))

    def _slow(url, timeout_s):
        raise FetchTimeoutError("read timed out")

    monkeypatch.setattr(web_fetch, "_request_once", _slow)
    out = _fetch_url_impl("https://example.com/slow")
    assert "timed out" in out


def test_too_many_redirects(net, monkeypatch):
    monkeypatch.setattr(web_fetch, "_resolve_addresses", _fake_resolver({"example.com": [PUBLIC_IP]}))
    monkeypatch.setattr(
        web_fetch,
        "_request_once",
        lambda url, timeout_s: FetchResponse(status=302, location="/loop"),
    )
    out = _fetch_url_impl("https://example.com/start")
    assert "too many redirects" in out


def test_char_cap_is_loud(net):
    body = ("lorem ipsum dolor sit amet " * 400).encode("utf-8")
    net(
        {
            "https://example.com/plain": FetchResponse(
                status=200, headers={"content-type": "text/plain"}, body=body
            )
        }
    )
    out = _fetch_url_impl("https://example.com/plain", max_chars=1000)
    assert "[truncated at 1000 characters]" in out
    assert "lorem ipsum" * 60 not in out


def test_max_chars_hard_cap(net):
    body = ("a" * 50000).encode("utf-8")
    net(
        {
            "https://example.com/big": FetchResponse(
                status=200, headers={"content-type": "text/plain"}, body=body
            )
        }
    )
    out = _fetch_url_impl("https://example.com/big", max_chars=10**9)
    assert "[truncated at 40000 characters]" in out
    assert "a" * 40001 not in out


def test_plain_text_gets_untitled_header(net):
    net(
        {
            "https://example.com/notes.txt": FetchResponse(
                status=200,
                headers={"content-type": "text/plain"},
                body=b"just some notes",
            )
        }
    )
    out = _fetch_url_impl("https://example.com/notes.txt")
    assert out.startswith("[fetch_url: https://example.com/notes.txt | title: untitled]")
    assert "just some notes" in out


def test_fallback_note_when_extraction_finds_nothing(net, monkeypatch):
    # Force the extractor to find nothing: the tool must fall back to
    # the raw page text and say so, not return an empty result.
    import trafilatura

    monkeypatch.setattr(trafilatura, "extract", lambda *a, **k: None)
    net({"https://example.com/tiny": _html_response("<html><body><p>tiny</p></body></html>")})
    out = _fetch_url_impl("https://example.com/tiny")
    assert "tiny" in out
    assert "showing raw page text" in out


def test_empty_page_is_an_honest_error(net):
    net({"https://example.com/blank": _html_response("<html><body>   </body></html>")})
    out = _fetch_url_impl("https://example.com/blank")
    assert "no readable content" in out


def test_unsupported_content_type_is_an_honest_error(net):
    net(
        {
            "https://example.com/doc.pdf": FetchResponse(
                status=200,
                headers={"content-type": "application/pdf"},
                body=b"%PDF-1.7 not a web page",
            )
        }
    )
    out = _fetch_url_impl("https://example.com/doc.pdf")
    assert "unsupported content type" in out


@pytest.mark.asyncio
async def test_tool_execute_fn_runs_through_seams(net):
    net({"https://example.com/article": _html_response(ARTICLE_HTML)})
    tool = build_tool_registry().get_tool("fetch_url")
    out = await tool.execute_fn(url="https://example.com/article")
    assert "polymetallic nodules" in out


def test_fetch_url_registered_read_only():
    registry = build_tool_registry()
    assert "fetch_url" in registry.read_only_names()
    assert registry.get_tool("fetch_url").risk is ToolRisk.READ_ONLY
