"""S2-M3: DNS-rebinding TOCTOU — the guard's validated IPs must be PINNED.

The SSRF guard (``_guard_url``) resolves a hostname and validates every
address, but the pre-fix transport handed the original hostname URL to
httpx, which re-resolved it at connect time — a DNS answer that changed
between the check and the connect (rebinding) sailed past the guard.

These tests fake the two DNS surfaces independently:

- ``_resolve_addresses`` (the guard's resolver seam) always answers with
  PUBLIC addresses — the guard passes.
- ``socket.create_connection`` (what httpcore's backend actually calls
  at connect time) is the "network layer": a request to connect to a
  validated public IP reaches the benign public server (bound to
  127.0.0.2), while a request to connect to a HOSTNAME — i.e. an
  unpinned, re-resolved connection — is routed to the attacker's
  loopback server (127.0.0.1), exactly what a rebinding DNS answer
  would achieve. It also records every address it was asked to dial,
  so tests can assert the transport dialled IP literals, never names.

No real external network: all servers are local, all "public" IPs are
only ever passed through the stubbed network layer to local binds.
"""

from __future__ import annotations

import datetime
import ipaddress
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from aether_engine.tools import web_fetch
from aether_engine.tools.web_fetch import _fetch_url_impl, _request_once

PUBLIC_A = "93.184.216.34"
PUBLIC_B = "93.184.216.35"

SECRET_BODY = "TOP SECRET INTERNAL METADATA — never a fetch result"
PUBLIC_PAGE = "ordinary public page body"
PUBLIC_SECRET_PATH = "ordinary public page served at the secret path"
PUBLIC_FINAL = "public final page body after redirect"

_PROXY_ENV_VARS = (
    "http_proxy",
    "https_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "all_proxy",
    "ALL_PROXY",
    "no_proxy",
    "NO_PROXY",
)


def _make_handler(routes, default_body=None):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.server.hits.append((self.headers.get("Host"), self.path))
            route = routes.get(self.path)
            if route is None:
                if default_body is None:
                    status, headers, body = 404, {}, "no such page"
                else:
                    status, headers, body = 200, {"Content-Type": "text/plain"}, default_body
            else:
                status, headers, body = route
            payload = body.encode("utf-8")
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):  # silence test server logging
            pass

    return Handler


class _Server:
    def __init__(self, bind_ip: str, port: int, routes, default_body=None):
        self.httpd = ThreadingHTTPServer((bind_ip, port), _make_handler(routes, default_body))
        self.httpd.hits = []
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def hits(self):
        return self.httpd.hits

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class _Net:
    """The stubbed world: secret server on 127.0.0.1, public on 127.0.0.2."""

    def __init__(self, monkeypatch):
        for var in _PROXY_ENV_VARS:
            monkeypatch.delenv(var, raising=False)

        self.secret = _Server("127.0.0.1", 0, {}, default_body=SECRET_BODY)
        self.port = self.secret.port
        text = {"Content-Type": "text/plain"}
        self.public = _Server(
            "127.0.0.2",
            self.port,
            {
                "/page": (200, text, PUBLIC_PAGE),
                "/secret": (200, text, PUBLIC_SECRET_PATH),
                "/start": (
                    302,
                    {"Location": f"http://b.test:{self.port}/final"},
                    "",
                ),
                "/final": (200, text, PUBLIC_FINAL),
            },
        )

        self.refuse_public_a = False
        self.connect_calls: list[tuple[str, int]] = []
        real_create_connection = socket.create_connection

        def fake_create_connection(address, timeout=None, source_address=None):
            host, port = address[0], address[1]
            self.connect_calls.append((host, port))
            if host == PUBLIC_A and self.refuse_public_a:
                raise ConnectionRefusedError(111, "Connection refused (stubbed)")
            if host in (PUBLIC_A, PUBLIC_B):
                # A pinned, validated public IP: the real public server.
                target = ("127.0.0.2", port)
            else:
                # A hostname (re-resolved at connect time — rebinding!) or
                # a loopback literal: the attacker's local server.
                target = ("127.0.0.1", port)
            return real_create_connection(target, timeout, source_address=source_address)

        monkeypatch.setattr(socket, "create_connection", fake_create_connection)

        dns = {
            "a.test": [PUBLIC_A],
            "b.test": [PUBLIC_B],
            "rebind.test": [PUBLIC_A],
            "multi.test": [PUBLIC_A, PUBLIC_B],
        }

        def fake_resolver(host: str) -> list[str]:
            if host in dns:
                return dns[host]
            raise socket.gaierror(f"unknown host: {host}")

        monkeypatch.setattr(web_fetch, "_resolve_addresses", fake_resolver)

    def stop(self):
        self.secret.stop()
        self.public.stop()


@pytest.fixture()
def pinned_net(monkeypatch):
    net = _Net(monkeypatch)
    yield net
    net.stop()


def test_rebinding_answer_at_connect_time_does_not_reach_private_server(pinned_net):
    # Guard-time DNS says public (passes); connect-time the hostname
    # would rebind to loopback. Post-fix the fetch must dial the pinned
    # public IP and return the public server's content — the private
    # server must see no connection at all.
    out = _fetch_url_impl(f"http://rebind.test:{pinned_net.port}/secret")
    assert SECRET_BODY not in out
    assert pinned_net.secret.hits == []
    assert PUBLIC_SECRET_PATH in out
    assert pinned_net.connect_calls == [(PUBLIC_A, pinned_net.port)]


def test_normal_fetch_dials_pinned_ip_and_preserves_host_header(pinned_net):
    out = _fetch_url_impl(f"http://a.test:{pinned_net.port}/page")
    assert PUBLIC_PAGE in out
    assert pinned_net.connect_calls == [(PUBLIC_A, pinned_net.port)]
    # The server saw the ORIGINAL host in the Host header, not the IP.
    assert pinned_net.public.hits == [(f"a.test:{pinned_net.port}", "/page")]
    assert pinned_net.secret.hits == []


def test_redirect_hop_is_revalidated_and_repinned(pinned_net):
    out = _fetch_url_impl(f"http://a.test:{pinned_net.port}/start")
    assert PUBLIC_FINAL in out
    assert f"[fetch_url: http://b.test:{pinned_net.port}/final" in out
    # Each hop dialled its own guard-validated IP — never a hostname.
    assert pinned_net.connect_calls == [
        (PUBLIC_A, pinned_net.port),
        (PUBLIC_B, pinned_net.port),
    ]
    assert pinned_net.public.hits == [
        (f"a.test:{pinned_net.port}", "/start"),
        (f"b.test:{pinned_net.port}", "/final"),
    ]
    assert pinned_net.secret.hits == []


def test_second_validated_address_is_tried_when_first_refuses(pinned_net):
    # getaddrinfo + create_connection normally walk every validated
    # address; pinning must keep that fallback, not pin only the first.
    pinned_net.refuse_public_a = True
    out = _fetch_url_impl(f"http://multi.test:{pinned_net.port}/page")
    assert PUBLIC_PAGE in out
    assert pinned_net.connect_calls == [
        (PUBLIC_A, pinned_net.port),
        (PUBLIC_B, pinned_net.port),
    ]


def test_proxied_fetch_fails_closed_because_pinning_cannot_apply(pinned_net, monkeypatch):
    # The pre-fix client honoured proxy env vars; a proxy re-resolves
    # the hostname itself, so the validated IPs cannot be pinned through
    # it. The fetch must be refused, not silently unpinned.
    monkeypatch.setenv("http_proxy", "http://proxy.invalid:8080")
    out = _fetch_url_impl(f"http://a.test:{pinned_net.port}/page")
    assert "proxy" in out.lower()
    assert "pin" in out.lower()
    assert PUBLIC_PAGE not in out
    assert pinned_net.connect_calls == []
    assert pinned_net.public.hits == []
    assert pinned_net.secret.hits == []


# --- TLS: SNI + certificate hostname stay the original hostname --------


def _write_pem(path, *parts: bytes):
    path.write_bytes(b"".join(parts))


def _make_ca_and_server_cert(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    def key():
        return rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def pem_key(k):
        return k.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )

    def pem_cert(c):
        return c.public_bytes(serialization.Encoding.PEM)

    now = datetime.datetime.now(datetime.UTC)
    ca_key = key()
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Aether Test CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )

    server_key = key()
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "pin.test")]))
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("pin.test")]), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )

    ca_pem = tmp_path / "ca.pem"
    _write_pem(ca_pem, pem_cert(ca_cert))
    server_pem = tmp_path / "server.pem"
    _write_pem(server_pem, pem_cert(server_cert), pem_key(server_key))
    return ca_pem, server_pem


def test_https_pinned_fetch_verifies_cert_against_original_hostname(tmp_path, monkeypatch):
    for var in _PROXY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    ca_pem, server_pem = _make_ca_and_server_cert(tmp_path)
    monkeypatch.setenv("SSL_CERT_FILE", str(ca_pem))

    sni_seen: list[str | None] = []
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=str(server_pem), keyfile=str(server_pem))
    context.sni_callback = lambda _sslobj, name, _ctx: sni_seen.append(name)

    server = _Server(
        "127.0.0.1",
        0,
        {"/tls": (200, {"Content-Type": "text/plain"}, "tls pinned body")},
    )
    try:
        server.httpd.socket = context.wrap_socket(server.httpd.socket, server_side=True)
        # The cert is for pin.test only (no IP SAN): if the transport let
        # TLS verify against the pinned IP instead of the hostname, the
        # handshake would fail — a 200 here proves hostname verification.
        resp = _request_once(
            f"https://pin.test:{server.port}/tls",
            5.0,
            pinned_addresses=["127.0.0.1"],
        )
        assert resp.status == 200
        assert resp.body == b"tls pinned body"
        assert sni_seen == ["pin.test"]
        assert server.hits == [(f"pin.test:{server.port}", "/tls")]
    finally:
        server.stop()


def test_pinned_addresses_are_ip_literals_only(tmp_path):
    # Defence in depth for the transport API itself: a caller passing a
    # hostname as a "pinned address" would reintroduce re-resolution.
    with pytest.raises(ValueError):
        _request_once(
            "http://a.test:1/page", 1.0, pinned_addresses=["a.test"]
        )
    # Sanity: the guard never produces such a value.
    assert ipaddress.ip_address(PUBLIC_A)
