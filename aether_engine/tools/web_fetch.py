"""Web page fetching (Epic 2, E2-05): fetch_url.

READ_ONLY. Fetches one http(s) page and returns its readable text with
a citation header carrying the FINAL url (after redirects) and the
page title, so an answer can cite where its content came from.

Safety shape, mirroring the filesystem tools' doctrine of honest
results over exceptions:

- Scheme guard: http/https only.
- SSRF guard: the host is resolved and EVERY resolved address must be
  public — loopback, private, link-local, reserved, multicast and
  unspecified addresses are refused. Redirects are followed manually
  (at most ``MAX_REDIRECTS``) and the guard re-runs on every hop, so a
  public page cannot bounce the fetch onto the local network. The one
  escape hatch is the user's explicit opt-in: the environment variable
  ``AETHER_ALLOW_LOCAL_FETCH=1``.
- Address pinning (S2-M3): validating resolved IPs is not enough if the
  transport then re-resolves the hostname at connect time — a DNS
  answer that changes between check and connect (rebinding) would
  bypass the guard entirely. So the transport dials the guard's
  validated IP literals directly (``_PinnedTransport``), preserving the
  original hostname in the Host header and as the TLS SNI /
  certificate-verification name, so certificates are still verified
  against the hostname the user asked for. Every redirect hop is
  re-validated AND re-pinned. Proxy boundary: the plain client honours
  proxy environment variables, but a proxy resolves the hostname
  itself, so validated addresses cannot be pinned through one — a
  guarded fetch that would go through a proxy is refused (fail closed)
  instead of silently fetching unpinned. The opt-in local mode
  (``AETHER_ALLOW_LOCAL_FETCH=1``) keeps the historical unpinned
  behaviour, proxies included: the user has explicitly asked to reach
  local addresses, and pinning is a guard property, not a local one.
- Bounds: a ``FETCH_TIMEOUT_S`` deadline across the whole chain, a
  ``MAX_BODY_BYTES`` read cap at the transport, and a loud character
  cap on the returned text (``max_chars``, hard-capped).

Failures return ``Error: …`` strings, never exceptions and never
fabricated content. Extraction is trafilatura; when it finds nothing,
the raw page text is returned with a note saying so.
"""

from __future__ import annotations

import asyncio
import html as html_lib
import ipaddress
import os
import re
import socket
import time
import urllib.request
from contextvars import ContextVar
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import httpx

from aether_engine.tools.registry import Tool, ToolRisk

#: Environment opt-in that lifts the SSRF guard for local addresses.
ALLOW_LOCAL_FETCH_ENV = "AETHER_ALLOW_LOCAL_FETCH"

#: Total deadline for one fetch_url call, across all redirect hops.
FETCH_TIMEOUT_S = 15.0
#: Redirects followed before giving up.
MAX_REDIRECTS = 5
#: Transport read cap — the body is never read past this many bytes.
MAX_BODY_BYTES = 2 * 1024 * 1024
#: Character budget for the extracted text: default and hard cap.
FETCH_DEFAULT_CHARS = 12000
FETCH_MAX_CHARS = 40000

USER_AGENT = "Aether-OS fetch_url (local agent; +https://example.invalid/aether)"

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class FetchTimeoutError(Exception):
    """The transport hit the fetch deadline (translated from httpx)."""


class FetchProxyError(Exception):
    """A guarded fetch would have to go through a proxy (user-facing).

    Pinning cannot apply through a proxy — the proxy resolves the
    hostname — so the fetch is refused. ``str(error)`` is the complete
    tool-result string; the fetch loop returns it verbatim.
    """


#: The guard-validated addresses for the hop currently being fetched.
#: ``_fetch_url_impl`` sets this around its ``_request_once`` call so
#: the transport can pin them without changing the transport seam's
#: ``(url, timeout_s)`` signature that tests fake. A ContextVar (not a
#: plain global) because fetches run concurrently on several threads.
_PINNED_ADDRESSES: ContextVar[tuple[str, ...] | None] = ContextVar(
    "aether_fetch_pinned_addresses", default=None
)


@dataclass
class FetchResponse:
    """One HTTP response as the fetch loop consumes it.

    The transport seam (``_request_once``) returns this so tests can
    fake the network without touching httpx.
    """

    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    location: str | None = None
    body_capped: bool = False


def _allow_local_fetch() -> bool:
    return os.environ.get(ALLOW_LOCAL_FETCH_ENV) == "1"


def _resolve_addresses(hostname: str) -> list[str]:
    """All IP addresses a hostname resolves to (the resolver seam)."""
    infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    return sorted({info[4][0] for info in infos})


def _blocked_reason(address: str) -> str | None:
    """Why an address is not public, or None when it is fetchable."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return f"'{address}' is not a valid IP address"
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_loopback:
        return "a loopback address"
    if ip.is_link_local:
        return "a link-local address"
    if ip.is_multicast:
        return "a multicast address"
    if ip.is_unspecified:
        return "the unspecified address"
    if ip.is_reserved:
        return "a reserved address"
    if ip.is_private:
        return "a private address"
    return None


def _guard_url_pinned(url: str) -> tuple[str | None, tuple[str, ...] | None]:
    """Scheme + SSRF check for one URL, plus the addresses to pin.

    Returns ``(error, pinned)``: ``error`` is the refusal string (and
    ``pinned`` is None) or None on success, in which case ``pinned``
    holds the validated IP literals the transport MUST dial — never a
    hostname, so nothing is re-resolved at connect time (S2-M3). In
    the opt-in local mode (``AETHER_ALLOW_LOCAL_FETCH=1``) the guard
    does not resolve at all and ``pinned`` is None, meaning "fetch
    unpinned, as before".
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return (
            (
                f"Error: fetch_url only supports http/https URLs, "
                f"got scheme '{parsed.scheme or '(none)'}' in '{url}'."
            ),
            None,
        )
    host = parsed.hostname
    if not host:
        return f"Error: fetch_url needs a URL with a host, got '{url}'.", None
    if _allow_local_fetch():
        return None, None
    try:
        addresses = _resolve_addresses(host)
    except socket.gaierror as e:
        return f"Error: could not resolve host '{host}' for '{url}': {e}", None
    if not addresses:
        return (
            f"Error: could not resolve host '{host}' for '{url}': no addresses returned.",
            None,
        )
    for address in addresses:
        reason = _blocked_reason(address)
        if reason is not None:
            return (
                (
                    f"Error: refusing to fetch '{url}': host '{host}' resolves to "
                    f"{reason} ({address}). fetch_url does not connect to "
                    f"non-public addresses; set {ALLOW_LOCAL_FETCH_ENV}=1 to allow it."
                ),
                None,
            )
    pinned: list[str] = []
    for address in addresses:
        ip = ipaddress.ip_address(address)
        # Dial the plain IPv4 form of a v4-mapped IPv6 address: it is
        # the same endpoint, in the form URL hosts and SNI expect.
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            address = str(ip.ipv4_mapped)
        pinned.append(address)
    return None, tuple(pinned)


def _guard_url(url: str) -> str | None:
    """Scheme + SSRF check for one URL. Returns an error string or None."""
    error, _pinned = _guard_url_pinned(url)
    return error


def _env_proxy_for(url: str) -> str | None:
    """The proxy the environment configures for this URL, if any.

    Mirrors what the plain httpx client (``trust_env``) would use:
    the ``*_proxy`` environment variables, honouring ``no_proxy``
    bypasses. Only consulted for guarded (pinned) fetches — see
    ``_request_once``.
    """
    try:
        proxies = urllib.request.getproxies()
    except Exception:  # noqa: BLE001 — proxy detection must never crash a fetch
        return None
    if not proxies:
        return None
    parsed = urlparse(url)
    host = parsed.hostname or ""
    bypassed = False
    try:
        bypassed = bool(host and urllib.request.proxy_bypass(host))
    except Exception:  # noqa: BLE001 — a broken no_proxy must not crash the fetch
        bypassed = False
    if bypassed:
        return None
    return proxies.get(parsed.scheme) or proxies.get("all")


class _PinnedTransport(httpx.BaseTransport):
    """An httpx transport that dials ONLY the guard-validated IPs.

    For each request the URL's host is rewritten to a validated IP
    literal (httpcore then connects to that literal — ``socket``
    never re-resolves a name), while:

    - the ``Host`` header is reset to the original netloc, and
    - the ``sni_hostname`` request extension carries the original
      hostname, so httpcore performs the TLS handshake with that SNI
      and the SSL context verifies the certificate against the
      original hostname — verification stays REAL, just aimed at the
      name the user asked for instead of the dialled IP.

    Validated addresses are tried in order on connect failure, the
    same fallback ``getaddrinfo`` + ``create_connection`` would have
    given (e.g. an unreachable IPv6 address before a working IPv4).
    """

    def __init__(self, addresses: tuple[str, ...] | list[str]) -> None:
        self._addresses = list(addresses)
        # Default transport settings: certificate verification on,
        # trust_env on so SSL_CERT_FILE / SSL_CERT_DIR are honoured
        # for the CA bundle exactly as the plain client honours them.
        self._inner = httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        original_url = request.url
        original_host = original_url.host
        host_header = original_url.netloc.decode("ascii")  # original host[:port]
        last_error: httpx.HTTPError | None = None
        for address in self._addresses:
            request.url = original_url.copy_with(host=address)
            request.headers["Host"] = host_header
            request.extensions["sni_hostname"] = original_host
            try:
                return self._inner.handle_request(request)
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                # Connection never established, so nothing was sent:
                # safe to fall back to the next validated address.
                last_error = e
        assert last_error is not None  # _addresses is non-empty (checked by caller)
        raise last_error

    def close(self) -> None:
        self._inner.close()


def _request_once(
    url: str, timeout_s: float, pinned_addresses: tuple[str, ...] | list[str] | None = None
) -> FetchResponse:
    """One GET with no redirect following (the transport seam).

    Reads at most ``MAX_BODY_BYTES``; beyond that the read stops and
    ``body_capped`` is set so the caller can say so loudly.

    When ``pinned_addresses`` are given (directly, or via the
    ``_PINNED_ADDRESSES`` context the fetch loop sets) the connection
    is pinned to those validated IP literals through
    ``_PinnedTransport``; a configured proxy makes the fetch fail
    closed with ``FetchProxyError`` because a proxy would re-resolve
    the hostname and defeat the pin. With no pinned addresses (the
    opt-in local mode) the historical plain client is used unchanged.
    """
    pins = pinned_addresses if pinned_addresses is not None else _PINNED_ADDRESSES.get()
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html, application/xhtml+xml, text/plain;q=0.9, */*;q=0.5",
    }
    try:
        if pins:
            for address in pins:
                try:
                    ipaddress.ip_address(address)
                except ValueError:
                    raise ValueError(
                        f"pinned address {address!r} is not an IP literal; "
                        "refusing to let the transport resolve it"
                    ) from None
            proxy = _env_proxy_for(url)
            if proxy is not None:
                raise FetchProxyError(
                    f"Error: refusing to fetch '{url}': the environment configures "
                    f"a proxy for this host ({proxy}), and a proxy resolves the "
                    f"hostname itself, so the SSRF guard's validated addresses "
                    f"cannot be pinned through it. fetch_url will not fetch "
                    f"unpinned through a proxy; unset the proxy for this host "
                    f"(or add it to no_proxy) to fetch it directly."
                )
            client = httpx.Client(
                transport=_PinnedTransport(pins),
                follow_redirects=False,
                timeout=httpx.Timeout(timeout_s),
                trust_env=False,
            )
        else:
            client = httpx.Client(follow_redirects=False, timeout=httpx.Timeout(timeout_s))
        with client, client.stream("GET", url, headers=headers) as resp:
            lowered = {k.lower(): v for k, v in resp.headers.items()}
            if resp.status_code in _REDIRECT_STATUSES:
                return FetchResponse(
                    status=resp.status_code,
                    headers=lowered,
                    location=lowered.get("location"),
                )
            buf = bytearray()
            capped = False
            for chunk in resp.iter_bytes():
                buf.extend(chunk)
                if len(buf) > MAX_BODY_BYTES:
                    del buf[MAX_BODY_BYTES:]
                    capped = True
                    break
            return FetchResponse(
                status=resp.status_code,
                headers=lowered,
                body=bytes(buf),
                body_capped=capped,
            )
    except httpx.TimeoutException as e:
        raise FetchTimeoutError(str(e) or "timed out") from e


_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def _page_title(html_text: str) -> str | None:
    """The page's <title>, falling back to trafilatura's metadata."""
    match = _TITLE_RE.search(html_text)
    if match:
        title = html_lib.unescape(re.sub(r"\s+", " ", match.group(1))).strip()
        if title:
            return title
    try:
        import trafilatura

        metadata = trafilatura.extract_metadata(html_text)
        if metadata is not None and metadata.title:
            return str(metadata.title).strip()
    except Exception:  # noqa: BLE001 — metadata is a nice-to-have; any failure just means 'untitled'
        return None
    return None


def _raw_text_fallback(html_text: str) -> str:
    """Readable-ish text straight from the markup, when trafilatura
    finds nothing: drop script/style wholesale, turn block boundaries
    into line breaks, strip the remaining tags."""
    text = re.sub(r"(?is)<(script|style|noscript|template)[^>]*>.*?</\1>", " ", html_text)
    text = re.sub(r"(?i)</(p|div|li|tr|h[1-6]|article|section)>", "\n", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _extract_text(html_text: str, final_url: str) -> tuple[str, bool]:
    """(readable text, used_fallback) — trafilatura first, raw second."""
    try:
        import trafilatura

        extracted = trafilatura.extract(html_text, url=final_url)
    except Exception:  # noqa: BLE001 — extraction must never crash the tool; the fallback below covers it
        extracted = None
    if extracted and extracted.strip():
        return extracted.strip(), False
    return _raw_text_fallback(html_text), True


def _decode_body(resp: FetchResponse) -> str:
    charset = "utf-8"
    content_type = resp.headers.get("content-type", "")
    for part in content_type.split(";"):
        part = part.strip()
        if part.startswith("charset="):
            charset = part[len("charset=") :].strip() or "utf-8"
    try:
        return resp.body.decode(charset, errors="replace")
    except LookupError:  # unknown charset name in the header
        return resp.body.decode("utf-8", errors="replace")


def _fetch_url_impl(url: str, max_chars: int | None = None) -> str:
    """The whole fetch: guards, redirect chain, extraction, caps.

    Synchronous and seam-driven (``_guard_url`` resolves through
    ``_resolve_addresses``; hops go through ``_request_once``) so tests
    fake the network at those two module functions. Never raises.
    """
    if not url or not str(url).strip():
        return "Error: fetch_url needs a non-empty url."
    url = str(url).strip()
    if max_chars and max_chars > 0:
        limit = min(int(max_chars), FETCH_MAX_CHARS)
    else:
        limit = FETCH_DEFAULT_CHARS

    deadline = time.monotonic() + FETCH_TIMEOUT_S
    current = url
    resp: FetchResponse | None = None
    for hop in range(MAX_REDIRECTS + 1):
        guard_error, pinned = _guard_url_pinned(current)
        if guard_error is not None:
            return guard_error
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return f"Error: fetching '{url}' timed out after {FETCH_TIMEOUT_S:.0f}s."
        try:
            # Hand this hop's validated addresses to the transport via
            # the context var, keeping the seam signature unchanged.
            token = _PINNED_ADDRESSES.set(pinned)
            try:
                resp = _request_once(current, remaining)
            finally:
                _PINNED_ADDRESSES.reset(token)
        except FetchTimeoutError:
            return f"Error: fetching '{current}' timed out after {FETCH_TIMEOUT_S:.0f}s."
        except FetchProxyError as e:
            return str(e)
        except Exception as e:  # noqa: BLE001 — transport raises arbitrary network errors; surface honestly as the tool result
            return f"Error: fetching '{current}' failed: {e}"
        if resp.status in _REDIRECT_STATUSES and resp.location:
            if hop >= MAX_REDIRECTS:
                return f"Error: too many redirects (more than {MAX_REDIRECTS}) starting at '{url}'."
            current = urljoin(current, resp.location)
            continue
        break

    assert resp is not None  # the loop always assigns before break/return
    final_url = current
    if resp.status >= 400:
        return f"Error: fetching '{final_url}' returned HTTP {resp.status}."

    body = resp.body
    body_capped = resp.body_capped
    if len(body) > MAX_BODY_BYTES:  # defence in depth if a transport over-delivers
        body = body[:MAX_BODY_BYTES]
        body_capped = True
    if not body:
        return f"Error: fetching '{final_url}' returned an empty response body."

    content_type = resp.headers.get("content-type", "").lower()
    raw_text = _decode_body(resp)
    sniffed = raw_text.lstrip()[:64].lower()
    looks_html = "html" in content_type or sniffed.startswith(
        ("<!doctype", "<html", "<head", "<body")
    )
    notes: list[str] = []
    if looks_html:
        title = _page_title(raw_text) or "untitled"
        text, used_fallback = _extract_text(raw_text, final_url)
        if used_fallback:
            notes.append("readability extraction found nothing; showing raw page text")
    elif content_type.startswith("text/") or "json" in content_type or not content_type:
        title = "untitled"
        text = raw_text.strip()
    else:
        return (
            f"Error: '{final_url}' returned unsupported content type "
            f"'{content_type or '(none)'}'; fetch_url reads web pages and text."
        )

    if not text:
        return f"Error: no readable content could be extracted from '{final_url}'."
    if body_capped:
        notes.append(f"response body stopped at the {MAX_BODY_BYTES} byte read cap")
    if len(text) > limit:
        text = text[:limit] + f"\n[truncated at {limit} characters]"

    header = f"[fetch_url: {final_url} | title: {title}]"
    note_lines = "".join(f"\n[note: {note}]" for note in notes)
    return f"{header}{note_lines}\n{text}"


async def _fetch_url_fn(url: str, max_chars: int | None = None) -> str:
    # The implementation is blocking (DNS + HTTP); run it off the event
    # loop so a slow page never stalls the engine.
    return await asyncio.to_thread(_fetch_url_impl, url, max_chars)


def create_fetch_url_tool() -> Tool:
    return Tool(
        name="fetch_url",
        description=(
            "Fetch one web page by URL and return its readable text, headed by a "
            "citation line with the final URL and page title — quote that URL when "
            "you use the content. http/https only; local/private addresses are "
            "refused. Use this to read a specific page (e.g. a search_web result); "
            "use search_web to discover pages."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The http(s) URL to fetch."},
                "max_chars": {
                    "type": "integer",
                    "description": f"Maximum extracted characters to return (default {FETCH_DEFAULT_CHARS}, max {FETCH_MAX_CHARS}).",
                },
            },
            "required": ["url"],
        },
        execute_fn=_fetch_url_fn,
        risk=ToolRisk.READ_ONLY,
        timeout_s=30,
        max_output_chars=FETCH_MAX_CHARS + 4096,
    )
