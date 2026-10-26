"""Fake stdio MCP server fixture (for issue 003).

Run as a script: ``python tests/fake_stdio_server.py``. Speaks MCP over
stdin/stdout via FastMCP. Exposes the same ``add`` tool as the fake HTTP
server so later issues can test stdio against it. NOT wired into the
client in issue 001 — stdio transport is issue 003.
"""
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fake-stdio")


@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


if __name__ == "__main__":
    mcp.run()
