"""Fake Streamable HTTP MCP server fixture, reusable by MCP tests.

Runs a tiny FastMCP server (one ``add`` tool) over Streamable HTTP on an
ephemeral localhost port, in a background thread. Used by the MCP client /
registry / tool tests. No real network, no API key.
"""
import socket
import threading
import time

from mcp.server.fastmcp import FastMCP


def _make_fake_server():
    """A fresh FastMCP instance per call — its session manager can only run once."""
    mcp = FastMCP("fake")

    @mcp.tool()
    def add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    @mcp.resource("fake://notes")
    def notes() -> str:
        """A short note, for issue 006 resource tests."""
        return "The launch code is 42."

    @mcp.resource("fake://big")
    def big() -> str:
        """A 25,000-char resource, for the issue 006 truncation rule."""
        return "x" * 25000

    @mcp.prompt()
    def greet(name: str) -> str:
        """Greet someone by name (issue 007)."""
        return f"Hello, {name}! Please introduce yourself."

    @mcp.prompt()
    def plan(topic: str) -> list:
        """Plan a session about a topic (issue 007, list form)."""
        return [{"role": "user", "content": f"Plan a session about {topic}."}]

    @mcp.prompt()
    def intro() -> str:
        """A no-argument prompt (issue 007, slash-command form)."""
        return "Introduce yourself briefly."

    return mcp


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_fake_http_server():
    """Start the fake server; returns (url, stop_fn)."""
    import uvicorn

    port = _free_port()
    app = _make_fake_server().streamable_http_app()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.05)
    else:
        raise RuntimeError("fake MCP HTTP server did not start")
    url = f"http://127.0.0.1:{port}/mcp"

    def stop():
        server.should_exit = True
        thread.join(timeout=5)

    return url, stop


if __name__ == "__main__":
    # Standalone demo server for the human QA of issue 001:
    #   python tests/fake_mcp_http.py
    # then add it in Settings -> MCP with URL http://127.0.0.1:8765/mcp
    # and ask Aether to "use the fake add tool with 3 and 5".
    import uvicorn

    demo_app = _make_fake_server().streamable_http_app()
    print("Fake MCP server listening on http://127.0.0.1:8765/mcp  (Ctrl+C to stop)")
    uvicorn.run(demo_app, host="127.0.0.1", port=8765, log_level="error")
