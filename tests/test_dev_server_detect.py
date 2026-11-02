"""Tests for dev-server detection (Epic 8, E8-2.2 — Canvas B project preview).

The detector probes the PRD candidate ports (5173, 3000, 8000, 8080) for a
live HTTP server and identifies the project framework from package.json.
Results feed DEV_SERVER_DETECT_RESPONSE: {"running", "port", "url",
"framework"}.
"""

import http.server
import json
import socket
import threading

import pytest

from aether_common.contracts import EventType
from aether_engine.artifacts.dev_server import (
    DEFAULT_PORTS,
    detect_dev_server,
    detect_framework,
    parse_package_json,
)

# ==============================================================================
# Contracts + candidate ports
# ==============================================================================

def test_detect_contracts_exist():
    assert EventType.DEV_SERVER_DETECT_REQUEST == "DEV_SERVER_DETECT_REQUEST"
    assert EventType.DEV_SERVER_DETECT_RESPONSE == "DEV_SERVER_DETECT_RESPONSE"


def test_default_ports_match_prd():
    assert tuple(DEFAULT_PORTS) == (5173, 3000, 8000, 8080)


# ==============================================================================
# package.json parsing + framework detection
# ==============================================================================

def _write_package_json(directory, data):
    (directory / "package.json").write_text(json.dumps(data), encoding="utf-8")


def test_parse_package_json_reads_valid_file(tmp_path):
    _write_package_json(tmp_path, {"name": "demo", "devDependencies": {"vite": "^5.0.0"}})
    parsed = parse_package_json(tmp_path)
    assert parsed is not None
    assert parsed["name"] == "demo"


def test_parse_package_json_missing_or_invalid(tmp_path):
    assert parse_package_json(tmp_path) is None
    assert parse_package_json(tmp_path / "does-not-exist") is None
    assert parse_package_json(None) is None

    (tmp_path / "package.json").write_text("{not json", encoding="utf-8")
    assert parse_package_json(tmp_path) is None


def test_parse_package_json_accepts_file_path(tmp_path):
    _write_package_json(tmp_path, {"name": "demo"})
    page = tmp_path / "index.html"
    page.write_text("<html></html>", encoding="utf-8")
    parsed = parse_package_json(page)
    assert parsed is not None
    assert parsed["name"] == "demo"


@pytest.mark.parametrize(
    ("package", "expected"),
    [
        ({"devDependencies": {"vite": "^5.0.0"}}, "Vite"),
        ({"dependencies": {"next": "14.0.0", "react": "18.0.0"}}, "Next.js"),
        ({"dependencies": {"nuxt": "^3.0.0"}}, "Nuxt"),
        ({"devDependencies": {"@angular/cli": "^17.0.0"}}, "Angular"),
        ({"dependencies": {"react-scripts": "5.0.1"}}, "Create React App"),
        ({"devDependencies": {"@sveltejs/kit": "^2.0.0"}}, "SvelteKit"),
        ({"devDependencies": {"svelte": "^4.0.0"}}, "Svelte"),
        ({"dependencies": {"vue": "^3.0.0"}}, "Vue"),
        ({"scripts": {"dev": "vite dev"}}, "Vite"),
        ({"scripts": {"dev": "next dev"}}, "Next.js"),
        ({"dependencies": {"lodash": "^4.0.0"}}, None),
        ({"name": "plain"}, None),
        (None, None),
    ],
)
def test_detect_framework(package, expected):
    assert detect_framework(package) == expected


# ==============================================================================
# Detection with an injected probe
# ==============================================================================

def test_detect_nothing_running(tmp_path):
    _write_package_json(tmp_path, {"devDependencies": {"vite": "^5.0.0"}})
    result = detect_dev_server(project_path=str(tmp_path), probe=lambda port: False)
    assert result == {"running": False, "port": None, "url": None, "framework": "Vite"}


def test_detect_first_open_port_in_candidate_order_wins():
    probed = []

    def probe(port):
        probed.append(port)
        return port in (3000, 5173)

    result = detect_dev_server(probe=probe)
    assert result["running"] is True
    assert result["port"] == 5173
    assert result["url"] == "http://localhost:5173"
    assert result["framework"] is None
    # Short-circuits on the first hit, in PRD candidate order.
    assert probed == [5173]


def test_detect_later_port(tmp_path):
    probed = []

    def probe(port):
        probed.append(port)
        return port == 8000

    result = detect_dev_server(project_path=str(tmp_path), probe=probe)
    assert result["running"] is True
    assert result["port"] == 8000
    assert result["url"] == "http://localhost:8000"
    assert probed == [5173, 3000, 8000]


def test_detect_probe_exception_counts_as_closed():
    def probe(port):
        raise OSError("connection refused")

    result = detect_dev_server(probe=probe)
    assert result["running"] is False
    assert result["port"] is None


def test_detect_framework_falls_back_to_workspace_root(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    _write_package_json(tmp_path, {"dependencies": {"next": "14.0.0"}})

    result = detect_dev_server(
        workspace_root=str(tmp_path),
        project_path=str(project_dir),
        probe=lambda port: False,
    )
    assert result["framework"] == "Next.js"


def test_detect_custom_port_list():
    result = detect_dev_server(ports=[9999], probe=lambda port: port == 9999)
    assert result == {
        "running": True,
        "port": 9999,
        "url": "http://localhost:9999",
        "framework": None,
    }


# ==============================================================================
# Real-socket behaviour of the default probe
# ==============================================================================

@pytest.fixture
def http_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()


def test_detect_real_http_server(http_server):
    result = detect_dev_server(ports=[http_server])
    assert result["running"] is True
    assert result["port"] == http_server
    assert result["url"] == f"http://localhost:{http_server}"


def test_detect_closed_port_not_running():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    free_port = sock.getsockname()[1]
    sock.close()

    result = detect_dev_server(ports=[free_port])
    assert result["running"] is False
    assert result["port"] is None


def test_detect_non_http_listener_not_a_dev_server():
    # A raw TCP listener that accepts but never speaks HTTP must not be
    # reported as a dev server — the preview iframe could not load it.
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def accept_and_hold():
        try:
            conn, _ = listener.accept()
            conn.recv(1024)
            threading.Event().wait(5)
            conn.close()
        except OSError:
            pass

    thread = threading.Thread(target=accept_and_hold, daemon=True)
    thread.start()
    try:
        result = detect_dev_server(ports=[port])
        assert result["running"] is False
    finally:
        listener.close()
