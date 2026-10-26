"""Raw MCP SDK client — one long-lived ``ClientSession`` per server.

Issue 001: Streamable HTTP only. stdio is issue 003 and is refused here
with a clear error, not a crash. No retry anywhere (PRD Q14): a failed
connect / call fails immediately and the server is marked failed; the
user reconnects manually.

Design notes:
- Transports are internal branches of this one deep module, not one file
  per transport.
- Sessions live in-process in the engine (PRD Q4); the MCP server itself
  is already isolated (remote HTTP / stdio subprocess).
- Header / env values support ``${VAR}`` / ``${VAR:-default}`` expansion
  from the environment. A variable with no value and no default is left
  untouched so a missing secret is visible, not silently empty.
"""
import asyncio
import logging
import os
import re
from contextlib import AsyncExitStack
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

logger = logging.getLogger("aether_engine.mcp.client")

_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _make_http_client(url: str, headers: Optional[Dict[str, str]], timeout: float) -> httpx.AsyncClient:
    """Build the httpx client for one server URL.

    httpx reads the proxy map from the environment when ``trust_env`` is
    left on, and a malformed ``NO_PROXY`` entry (e.g. ``[::1]``) makes
    *every* request fail at URL parse time, even to localhost. So we build
    the client explicitly with ``trust_env=False``: localhost servers never
    go through a proxy (standard ``no_proxy`` behaviour); remote servers
    use the plain ``HTTP(S)_PROXY`` env value if one is set, and no proxy
    otherwise.
    """
    host = (urlparse(url).hostname or "").lower()
    local = host in ("localhost", "127.0.0.1", "::1")
    if local:
        return httpx.AsyncClient(headers=headers, timeout=timeout, proxy=None, trust_env=False)
    scheme = urlparse(url).scheme
    proxy = os.environ.get("HTTPS_PROXY" if scheme == "https" else "HTTP_PROXY") or os.environ.get(
        "https_proxy" if scheme == "https" else "http_proxy"
    )
    return httpx.AsyncClient(headers=headers, timeout=timeout, proxy=proxy, trust_env=False)


class MCPClientError(Exception):
    """Raised when an MCP server cannot be reached or a call fails."""


class MCPToolExecutionError(MCPClientError):
    """Raised when a connected server reports a tool error."""


def expand_env_vars(value: str, environ: Optional[Dict[str, str]] = None) -> str:
    """Expand ``${VAR}`` / ``${VAR:-default}`` in *value* from *environ*."""
    env = os.environ if environ is None else environ

    def _repl(match: re.Match) -> str:
        name, default = match.group(1), match.group(2)
        if name in env:
            return env[name]
        if default is not None:
            return default
        return match.group(0)

    return _VAR_RE.sub(_repl, value)


class MCPClientManager:
    """Holds one ``ClientSession`` per connected MCP server."""

    def __init__(self) -> None:
        self._sessions: Dict[str, ClientSession] = {}
        self._stacks: Dict[str, AsyncExitStack] = {}
        self._configs: Dict[str, dict] = {}
        self._status: Dict[str, Dict[str, Any]] = {}

    # -- status ---------------------------------------------------------
    def get_status(self, name: str) -> Dict[str, Any]:
        return dict(
            self._status.get(name, {"status": "disconnected", "error": None, "tool_count": 0})
        )

    def is_connected(self, name: str) -> bool:
        return name in self._sessions

    def _set_status(
        self, name: str, status: str, error: Optional[str] = None, tool_count: Optional[int] = None
    ) -> None:
        current = self._status.get(name, {"status": "disconnected", "error": None, "tool_count": 0})
        current["status"] = status
        current["error"] = error
        if tool_count is not None:
            current["tool_count"] = tool_count
        self._status[name] = current

    def set_failed(self, name: str, error: str) -> None:
        """Mark *name* failed (e.g. after a tool-list refresh failure)."""
        self._set_status(name, "failed", error=error)

    # -- session plumbing ------------------------------------------------
    async def _open_session(self, config: dict):
        """Open a session for *config*; caller owns the returned stack."""
        transport = config.get("transport")
        if transport == "stdio":
            raise MCPClientError("stdio transport is not implemented yet (issue 003)")
        if transport != "streamable_http":
            raise MCPClientError(f"Unsupported MCP transport: {transport!r}")

        url = config.get("url")
        if not url:
            raise MCPClientError("Streamable HTTP server config requires 'url'")
        timeout = float(config.get("timeout") or 30)
        headers = {
            k: expand_env_vars(v)
            for k, v in (config.get("headers") or {}).items()
        }

        stack = AsyncExitStack()
        try:
            http_client = _make_http_client(url, headers, timeout)
            await stack.enter_async_context(http_client)
            read, write, _get_session_id = await stack.enter_async_context(
                streamable_http_client(url, http_client=http_client)
            )
            session = ClientSession(read, write)
            await stack.enter_async_context(session)
            await asyncio.wait_for(session.initialize(), timeout=timeout)
        except BaseException as e:
            # Closing the stack must never mask the original failure.
            try:
                await stack.aclose()
            except BaseException:
                pass
            if _is_genuine_cancellation(e):
                raise
            if isinstance(e, MCPClientError):
                raise
            if isinstance(e, BaseExceptionGroup):
                # The SDK runs request handling in an internal anyio task
                # group; a dead server surfaces as an exception group, not
                # a plain Exception. Unwrap it into one readable error.
                raise MCPClientError(_first_leaf_message(e)) from e
            raise MCPClientError(_failure_message(e)) from e
        return stack, session

    # -- lifecycle -------------------------------------------------------
    async def connect(self, name: str, config: dict) -> Dict[str, Any]:
        """Connect to *name*; returns {"ok": bool, "error": str|None}."""
        if name in self._sessions:
            return {"ok": True, "error": None}
        timeout = float(config.get("timeout") or 30)
        try:
            # No asyncio.wait_for around _open_session: cancelling inside the
            # SDK's anyio cancel scope corrupts its task group. The httpx
            # client timeout (set in _make_http_client) bounds the wait.
            stack, session = await self._open_session(config)
        except Exception as e:
            message = str(e) or e.__class__.__name__
            logger.warning("MCP connect failed for %s: %s", name, message)
            self._set_status(name, "failed", error=message)
            return {"ok": False, "error": message}
        self._stacks[name] = stack
        self._sessions[name] = session
        self._configs[name] = dict(config)
        try:
            tools = await session.list_tools()
            tool_count = len(tools.tools)
        except Exception:
            tool_count = 0
        self._set_status(name, "connected", error=None, tool_count=tool_count)
        return {"ok": True, "error": None}

    async def disconnect(self, name: str) -> Dict[str, Any]:
        stack = self._stacks.pop(name, None)
        self._sessions.pop(name, None)
        self._configs.pop(name, None)
        if stack is not None:
            try:
                await stack.aclose()
            except BaseException as e:  # closing must never raise
                if _is_genuine_cancellation(e):
                    raise
                logger.warning("MCP disconnect failed for %s: %s", name, e)
        self._set_status(name, "disconnected", error=None)
        return {"ok": True, "error": None}

    async def test_connection(self, name: str, config: dict) -> Dict[str, Any]:
        """Connect, list tools, disconnect. Leaves no session behind."""
        try:
            stack, session = await self._open_session(config)
        except Exception as e:
            message = str(e) or e.__class__.__name__
            return {"ok": False, "error": message, "tools": []}
        try:
            tools = await session.list_tools()
            return {"ok": True, "error": None, "tools": [t.name for t in tools.tools]}
        except Exception as e:
            return {"ok": False, "error": str(e), "tools": []}
        finally:
            await stack.aclose()

    # -- tools ------------------------------------------------------------
    async def list_tools(self, name: str):
        session = self._sessions.get(name)
        if session is None:
            raise MCPClientError(f"MCP server '{name}' is not connected")
        result = await session.list_tools()
        return result.tools

    async def call_tool(
        self, server: str, tool: str, args: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> str:
        """Call *tool* on *server*; returns the result text. No retry.

        *timeout* is the tool-call timeout (the Tool's timeout_s, 60s);
        when omitted it falls back to the server's connect timeout. The
        connect timeout never bounds a tool call.
        """
        session = self._sessions.get(server)
        if session is None:
            raise MCPClientError(f"MCP server '{server}' is not connected")
        if timeout is None:
            timeout = float((self._configs.get(server) or {}).get("timeout") or 30)
        # The session call runs in a child task so a timeout cancels only
        # the call, not our work — but the SDK's anyio machinery can still
        # cancel OUR task directly (the session's cancel scope was entered
        # here at connect time). That synthesised CancelledError is
        # distinguished from a genuine cancellation by its message (see
        # _is_sdk_cancel): the synthesised one is a connection failure,
        # a genuine one re-raises.
        inner = asyncio.ensure_future(session.call_tool(tool, args or {}))
        try:
            result = await asyncio.wait_for(asyncio.shield(inner), timeout=timeout)
        except asyncio.TimeoutError:
            inner.cancel()
            try:
                await inner
            except BaseException:
                pass
            message = f"tool call '{tool}' timed out after {timeout}s"
            self._set_status(server, "failed", error=message)
            raise MCPClientError(
                f"MCP server '{server}' tool call '{tool}' failed: {message}"
            )
        except asyncio.CancelledError as e:
            if _is_genuine_cancellation(e):
                inner.cancel()
                raise
            message = "connection lost (server closed the connection)"
            self._set_status(server, "failed", error=message)
            raise MCPClientError(
                f"MCP server '{server}' tool call '{tool}' failed: {message}"
            )
        except BaseException as e:
            if _is_genuine_cancellation(e):
                raise
            if isinstance(e, BaseExceptionGroup):
                message = _first_leaf_message(e)
            else:
                message = _failure_message(e)
            self._set_status(server, "failed", error=message)
            raise MCPClientError(
                f"MCP server '{server}' tool call '{tool}' failed: {message}"
            ) from e
        if getattr(result, "isError", False):
            text = _content_to_text(result.content)
            raise MCPToolExecutionError(
                f"MCP server '{server}' tool '{tool}' returned an error: {text}"
            )
        return _content_to_text(result.content)


def _first_leaf_message(group: BaseExceptionGroup) -> str:
    for exc in group.exceptions:
        if isinstance(exc, BaseExceptionGroup):
            return _first_leaf_message(exc)
        message = str(exc) or exc.__class__.__name__
        return message
    return group.__class__.__name__


def _is_sdk_cancel(exc: BaseException) -> bool:
    """True if *exc* is a CancelledError synthesised by the SDK's anyio
    machinery (dead server), not a genuine cancellation of our task.

    Verified in this environment (mcp 1.30 / anyio): anyio raises
    ``CancelledError('Cancelled via cancel scope <hex>')`` when its task
    group fails, while a bare ``task.cancel()`` — every cancellation the
    engine itself issues — raises it with empty args. Fail-safe: an
    unrecognised message is treated as a genuine cancellation.
    """
    return (
        isinstance(exc, asyncio.CancelledError)
        and bool(exc.args)
        and str(exc.args[0]).startswith("Cancelled via cancel scope")
    )


def _is_genuine_cancellation(exc: BaseException) -> bool:
    """True only if our task was actually asked to cancel.

    ``_task_is_being_cancelled()`` alone is not enough: the SDK's
    synthesised cancellation also sets our task's cancelling count, so
    the SDK message must be excluded (see ``_is_sdk_cancel``).
    """
    return (
        isinstance(exc, asyncio.CancelledError)
        and _task_is_being_cancelled()
        and not _is_sdk_cancel(exc)
    )


def _task_is_being_cancelled() -> bool:
    """True only if *our* task was actually asked to cancel.

    The SDK's internal anyio machinery synthesises CancelledError from
    its own failures (e.g. a dead server makes its receive stream raise
    WouldBlock, which anyio turns into a cancel-scope CancelledError).
    That is a connection failure, not a cancellation of our work — and it
    must be reported as one instead of escaping as CancelledError.
    Use ``_is_genuine_cancellation`` (which also excludes the SDK
    message) rather than this helper on its own.
    """
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


def _failure_message(exc: BaseException) -> str:
    """First non-empty message walking the __context__ chain."""
    seen: Optional[BaseException] = exc
    while seen is not None:
        if str(seen):
            return str(seen)
        seen = seen.__context__
    return exc.__class__.__name__


def _content_to_text(content: List[Any]) -> str:
    parts = []
    for block in content or []:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(text)
        else:
            parts.append(str(block))
    return "\n".join(parts)
