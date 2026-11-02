"""F-15 (EGM supplementary Blocker): "MCP servers inherit full env, no
timeouts" — verification pins. The claim DID NOT REPRODUCE on the
current tip; these tests pin the behaviour that refutes it so a future
regression re-opens the finding instead of silently standing.

Dedupe / provenance (no new issue is created by this file):
- The defects the claim describes were MCP issue 011 S1 (session calls
  other than ``call_tool`` had no timeout) and S2 (stdio subprocess
  inherited the engine's full ``os.environ``), fixed 26 Oct 2026 in
  commit ``bf32165`` on the MCP branch line, an ancestor of this tip:
  S1 via ``ClientSession(read_timeout_seconds=<server config timeout>)``
  plus ``asyncio.wait_for`` on ``initialize()`` and a per-request
  override + ``wait_for`` shield on ``call_tool``; S2 via the explicit
  ``MCP_STDIO_ENV_ALLOWLIST`` in ``aether_engine/mcp/client.py``.
- Issue 011's own tests (``test_mcp_review_should_fix_011.py``) already
  pin S1 stalls on ``tools/list`` + ``resources/list`` and S2 for one
  secret name. This file pins the parts 011 left uncovered: the exact
  allow-list contents, a battery of provider-key names, stall timeouts
  for ``initialize`` / ``tools/call`` / ``prompts/list`` /
  ``prompts/get`` / ``resources/read``, the session read-timeout wiring,
  and that the HTTP transport never spawns a stdio child.
- NOT pinned here (separate, already-recorded items, not F-15):
  EGM S2 N2 — credentialed proxy URLs in the allow-listed proxy vars
  pass verbatim to stdio children; EGM S2 M5 — the *worker sandbox*
  (``aether_engine/workers/sandbox.py``), a different subprocess
  boundary from the MCP spawn path.
"""
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest

from aether_engine.mcp.client import MCP_STDIO_ENV_ALLOWLIST, MCPClientError
from tests.fake_mcp_http import start_fake_http_server

FAKE_STDIO = Path(__file__).parent / "fake_stdio_server.py"
STALL_STDIO = Path(__file__).parent / "fake_stall_stdio_server.py"

# The exact allow-list as landed by 011 S2 (client.py). Any change to
# the set must be a deliberate edit here too — that is the pin.
EXPECTED_ALLOWLIST = frozenset(
    {
        "PATH", "PATHEXT", "HOME", "USER", "USERNAME", "LOGNAME",
        "LANG", "LC_ALL", "LC_CTYPE", "LANGUAGE",
        "TEMP", "TMP", "TMPDIR",
        "SystemRoot", "SystemDrive", "WINDIR", "ComSpec",
        "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432",
        "APPDATA", "LOCALAPPDATA", "HOMEDRIVE", "HOMEPATH", "USERPROFILE",
        "VIRTUAL_ENV", "PYTHONPATH", "SHELL", "TERM", "OS",
        "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
        "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy",
        "NODE_EXTRA_CA_CERTS", "NODE_USE_ENV_PROXY",
        "SSL_CERT_FILE", "CURL_CA_BUNDLE", "REQUESTS_CA_BUNDLE",
        "GIT_SSL_CAINFO",
    }
)

SECRET_NAMES = [
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
    "GOOGLE_API_KEY", "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN",
    "AETHER_TEST_SECRET_XYZ",
]


@pytest.fixture()
def manager():
    from aether_engine.mcp.client import MCPClientManager

    return MCPClientManager()


def _stdio_config(**overrides):
    config = {
        "transport": "stdio",
        "command": sys.executable,
        "args": [str(FAKE_STDIO)],
        "timeout": 10,
    }
    config.update(overrides)
    return config


def _stall_config(method, timeout=2):
    return {
        "transport": "stdio",
        "command": sys.executable,
        "args": [str(STALL_STDIO), method],
        "timeout": timeout,
    }


# -- env pins -------------------------------------------------------------

def test_f15_allowlist_is_exactly_the_pinned_set():
    """The stdio child env allow-list is exactly the 011 S2 set: no
    name in it is a credential-shaped name, and the set cannot grow
    silently."""
    assert MCP_STDIO_ENV_ALLOWLIST == EXPECTED_ALLOWLIST
    for name in MCP_STDIO_ENV_ALLOWLIST:
        upper = name.upper()
        assert "KEY" not in upper and "TOKEN" not in upper
        assert "SECRET" not in upper and "PASSWORD" not in upper


async def test_f15_provider_secrets_never_reach_stdio_child(manager, monkeypatch):
    """A battery of provider-key-shaped engine env vars — not just one
    name (011's pin) — must all be invisible to the stdio child, while
    the config env and allow-listed PATH still reach it."""
    for name in SECRET_NAMES:
        monkeypatch.setenv(name, f"fake-{name}-value")
    config = _stdio_config(env={"AETHER_STDIO_MARKER": "hello-stdio"})
    assert (await manager.connect("fake", config))["ok"] is True
    for name in SECRET_NAMES:
        got = (await manager.call_tool("fake", "echo_env", {"name": name})).strip()
        assert got == "", f"{name} leaked to the stdio child"
    assert (await manager.call_tool("fake", "echo_env", {"name": "AETHER_STDIO_MARKER"})).strip() == "hello-stdio"
    assert (await manager.call_tool("fake", "echo_env", {"name": "PATH"})).strip() != ""
    await manager.disconnect("fake")


async def test_f15_config_env_may_explicitly_name_a_secret(manager):
    """By design (011 S2 / F-15 programme wording): a secret reaches
    the child only when the server's own config names it in ``env``."""
    config = _stdio_config(env={"OPENAI_API_KEY": "named-by-config"})
    assert (await manager.connect("fake", config))["ok"] is True
    assert (await manager.call_tool("fake", "echo_env", {"name": "OPENAI_API_KEY"})).strip() == "named-by-config"
    await manager.disconnect("fake")


async def test_f15_http_transport_never_spawns_a_stdio_child(manager, monkeypatch):
    """HTTP/SSE-class transport has no child process and therefore no
    env to inherit: connecting over HTTP must not touch stdio_client."""
    import aether_engine.mcp.client as client_module

    def _boom(*args, **kwargs):
        raise AssertionError("stdio_client must not be used for HTTP transport")

    monkeypatch.setattr(client_module, "stdio_client", _boom)
    url, stop = start_fake_http_server()
    try:
        config = {"transport": "streamable_http", "url": url, "timeout": 10}
        assert (await manager.connect("fake", config))["ok"] is True
        assert (await manager.call_tool("fake", "add", {"a": 1, "b": 2})).strip() == "3"
        await manager.disconnect("fake")
    finally:
        stop()


# -- timeout pins -----------------------------------------------------------

async def test_f15_session_read_timeout_is_wired_from_config(manager):
    """Every non-call_tool session method is bounded by the SDK session
    read timeout, which _open_session sets from the server config."""
    assert (await manager.connect("fake", _stdio_config(timeout=7)))["ok"] is True
    session = manager._sessions["fake"]
    assert session._session_read_timeout_seconds == timedelta(seconds=7)
    await manager.disconnect("fake")


async def test_f15_stalled_initialize_fails_within_timeout(manager):
    """A stdio child that never speaks MCP at all: connect must fail
    within the config timeout (plus cleanup slack), not hang."""
    config = {
        "transport": "stdio",
        "command": sys.executable,
        "args": ["-c", "import time; time.sleep(30)"],
        "timeout": 2,
    }
    start = time.monotonic()
    result = await manager.connect("silent", config)
    elapsed = time.monotonic() - start
    assert result["ok"] is False, result
    assert elapsed < 10.0, f"connect took {elapsed:.1f}s — initialize is not bounded"
    assert manager.get_status("silent")["status"] == "failed"
    assert not manager.is_connected("silent")


async def test_f15_stalled_call_tool_times_out(manager):
    """tools/call stall: bounded by the per-call timeout (not the
    session read timeout alone), marks the server failed."""
    assert (await manager.connect("stall", _stall_config("tools/call")))["ok"] is True
    start = time.monotonic()
    with pytest.raises(MCPClientError, match="timed out after 2"):
        await manager.call_tool("stall", "anything", {}, timeout=2)
    assert time.monotonic() - start < 8.0
    assert manager.get_status("stall")["status"] == "failed"
    await manager.disconnect("stall")


@pytest.mark.parametrize(
    "method,call",
    [
        ("prompts/list", "list_prompts"),
        ("prompts/get", "get_prompt"),
        ("resources/read", "read_resource"),
    ],
)
async def test_f15_stalled_session_methods_time_out(manager, method, call):
    """The remaining session methods 011 S1 covered only via the shared
    read timeout: each must actually time out on a stalled server and
    mark the server failed."""
    assert (await manager.connect("stall", _stall_config(method)))["ok"] is True
    start = time.monotonic()
    with pytest.raises(MCPClientError, match="Timed out"):
        if call == "list_prompts":
            await manager.list_prompts("stall")
        elif call == "get_prompt":
            await manager.get_prompt("stall", "x", {})
        else:
            await manager.read_resource("stall", "fake://notes")
    assert time.monotonic() - start < 8.0
    assert manager.get_status("stall")["status"] == "failed"
    await manager.disconnect("stall")
