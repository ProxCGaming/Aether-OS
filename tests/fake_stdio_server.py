"""Fake stdio MCP server fixture (issue 003).

Run as a script: ``python tests/fake_stdio_server.py``. Speaks MCP over
stdin/stdout via FastMCP. Exposes the same ``add`` tool as the fake HTTP
server, plus ``echo_env`` / ``pwd`` (to prove env + cwd reach the
subprocess) and ``crash`` (test only: terminate the process, to prove a
server that dies mid-session is marked failed, not retried).

Server logs go to stderr, never stdout — stdout is protocol only.
"""
import os

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fake-stdio")


@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


@mcp.resource("fake://notes")
def notes() -> str:
    """A short note, for issue 006 resource tests."""
    return "The launch code is 42."


@mcp.tool()
def echo_env(name: str) -> str:
    """Return the value of environment variable *name* ('' if missing)."""
    return os.environ.get(name, "")


@mcp.tool()
def pwd() -> str:
    """Return the server process's working directory."""
    return os.getcwd()


@mcp.tool()
def crash() -> str:
    """Test only: terminate the server process immediately."""
    os._exit(1)


if __name__ == "__main__":
    mcp.run()
