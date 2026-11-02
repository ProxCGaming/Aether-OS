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


def _guard_url(url: str) -> str | None:
    """Scheme + SSRF check for one URL. Returns an error string or None."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return (
            f"Error: fetch_url only supports http/https URLs, "
            f"got scheme '{parsed.scheme or '(none)'}' in '{url}'."
        )
    host = parsed.hostname
    if not host:
        return f"Error: fetch_url needs a URL with a host, got '{url}'."
    if _allow_local_fetch():
        return None
    try:
        addresses = _resolve_addresses(host)
    except socket.gaierror as e:
        return f"Error: could not resolve host '{host}' for '{url}': {e}"
    if not addresses:
        return f"Error: could not resolve host '{host}' for '{url}': no addresses returned."
    for address in addresses:
        reason = _blocked_reason(address)
        if reason is not None:
            return (
                f"Error: refusing to fetch '{url}': host '{host}' resolves to "
                f"{reason} ({address}). fetch_url does not connect to "
                f"non-public addresses; set {ALLOW_LOCAL_FETCH_ENV}=1 to allow it."
            )
    return None


def _request_once(url: str, timeout_s: float) -> FetchResponse:
    """One GET with no redirect following (the transport seam).

    Reads at most ``MAX_BODY_BYTES``; beyond that the read stops and
    ``body_capped`` is set so the caller can say so loudly.
    """
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html, application/xhtml+xml, text/plain;q=0.9, */*;q=0.5",
    }
    try:
        with (
            httpx.Client(follow_redirects=False, timeout=httpx.Timeout(timeout_s)) as client,
            client.stream("GET", url, headers=headers) as resp,
        ):
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
        guard_error = _guard_url(current)
        if guard_error is not None:
            return guard_error
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return f"Error: fetching '{url}' timed out after {FETCH_TIMEOUT_S:.0f}s."
        try:
            resp = _request_once(current, remaining)
        except FetchTimeoutError:
            return f"Error: fetching '{current}' timed out after {FETCH_TIMEOUT_S:.0f}s."
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
