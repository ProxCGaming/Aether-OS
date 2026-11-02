"""S2-N2: proxy credentials must not leak to untrusted stdio children.

The MCP stdio env allowlist passes proxy variables through (npm/npx
need them), but a proxy URL is a normal place for embedded
credentials (http://user:pass@host:port) — and the old comment even
claimed "no credentials". The child env now has proxy userinfo
stripped where it is built; these tests pin the helper and the
end-to-end spawn behaviour (via the fake stdio server's echo_env).
"""
import sys
from pathlib import Path

import pytest

from aether_engine.mcp.client import _strip_url_userinfo

FAKE_STDIO = Path(__file__).parent / "fake_stdio_server.py"


def test_strips_user_and_password():
    assert _strip_url_userinfo("http://user:pass@proxy.local:8080") == (
        "http://proxy.local:8080"
    )
    assert _strip_url_userinfo("socks5://user@10.0.0.1:1080") == "socks5://10.0.0.1:1080"


def test_credential_free_urls_pass_through():
    assert _strip_url_userinfo("http://proxy.local:8080") == "http://proxy.local:8080"
    assert _strip_url_userinfo("http://[2001:db8::1]:3128") == "http://[2001:db8::1]:3128"


def test_non_url_values_pass_through():
    assert _strip_url_userinfo("proxy.local:8080") == "proxy.local:8080"
    assert _strip_url_userinfo("") == ""


def test_at_sign_outside_netloc_is_untouched():
    assert _strip_url_userinfo("http://proxy.local/path@x") == "http://proxy.local/path@x"


@pytest.fixture()
def manager():
    from aether_engine.mcp.client import MCPClientManager

    return MCPClientManager()


async def test_spawned_child_sees_stripped_proxy(manager, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://alice:s3cret@proxy.local:8080")
    config = {
        "transport": "stdio",
        "command": sys.executable,
        "args": [str(FAKE_STDIO)],
        "timeout": 10,
    }
    assert (await manager.connect("fake", config))["ok"] is True
    seen = await manager.call_tool("fake", "echo_env", {"name": "HTTPS_PROXY"})
    assert seen.strip() == "http://proxy.local:8080"
    await manager.disconnect("fake")
