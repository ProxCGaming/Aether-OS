"""Graceful failure for MCP tool calls (issue 001).

The old scaffold raised a *simulated* ConnectionError. Now a real failure
— dead server, transport error, server-side tool error — is caught and
returned as a plain error string the model can reason about, so the graph
never crashes. No retry (PRD Q14): the server is marked failed by the
client and the user reconnects manually.

There is deliberately no synchronous entry point (issue 010 N5): the old
``execute_mcp_tool_with_fallback`` returned "cannot call an MCP tool from
inside a running event loop" as if the server were unavailable — a
programming error masquerading as a server failure. Callers use the async
path; with no client it returns the unavailable message, it does not raise.
"""
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("aether_engine.mcp.fallback")


class MCPToolExecutionError(Exception):
    pass


def format_mcp_error(server_name: str, tool_name: str, error: Any) -> str:
    """The graceful-degradation message returned to the model."""
    return f"{server_name} MCP is unavailable, tool call '{tool_name}' failed: {error}"


async def execute_mcp_tool_async(
    server_name: str, tool_name: str, kwargs: Dict[str, Any], client: Any,
    timeout: Optional[float] = None,
) -> str:
    """Attempt the real call via *client*; on failure return the error string."""
    try:
        result = await client.call_tool(server_name, tool_name, kwargs or {}, timeout=timeout)
        return result if isinstance(result, str) else str(result)
    except Exception as e:
        error_msg = format_mcp_error(server_name, tool_name, e)
        logger.error(error_msg)
        return error_msg
