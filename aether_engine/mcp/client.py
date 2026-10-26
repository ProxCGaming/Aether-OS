"""Raw MCP SDK client — one long-lived ``ClientSession`` per server.

Transports: Streamable HTTP (issue 001) and stdio (issue 003). No retry
anywhere (PRD Q14): a failed connect / call fails immediately and the
server is marked failed; the user reconnects manually.

Design notes:
- Transports are internal branches of this one deep module, not one file
  per transport.
- Sessions live in-process in the engine (PRD Q4); the MCP server itself
  is already isolated (remote HTTP / stdio subprocess).
- Header / env values support ``${VAR}`` / ``${VAR:-default}`` expansion
  from the environment. A variable with no value and no default is left
  untouched so a missing secret is visible, not silently empty.
- stdio servers log to stderr, never stdout (stdout is protocol only);
  the SDK's stdio client sends the child's stderr to the engine's
  stderr by default, so a chatty server cannot corrupt the protocol.
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
from mcp.client.stdio import StdioServerParameters, stdio_client
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
        # Sessions opened by a background (auto-connect) task: the task
        # that opened the session must also close it — anyio forbids
        # exiting a cancel scope from a different task, and closing a
        # stdio stack from another task silently leaks the subprocess
        # (issue 004). The owner task parks after opening the session and
        # closes the stack itself when its close event is set.
        self._owner_tasks: Dict[str, asyncio.Task] = {}
        self._close_events: Dict[str, asyncio.Event] = {}

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
        if status == "disconnected":
            # No session, no tools: a disconnected server never reports
            # a stale tool count to the UI.
            current["tool_count"] = 0
        elif tool_count is not None:
            current["tool_count"] = tool_count
        self._status[name] = current

    def set_failed(self, name: str, error: str) -> None:
        """Mark *name* failed (e.g. after a tool-list refresh failure)."""
        self._set_status(name, "failed", error=error)

    # -- session plumbing ------------------------------------------------
    async def _open_session(self, config: dict):
        """Open a session for *config*; caller owns the returned stack."""
        transport = config.get("transport")
        if transport not in ("stdio", "streamable_http"):
            raise MCPClientError(f"Unsupported MCP transport: {transport!r}")
        timeout = float(config.get("timeout") or 30)

        stack = AsyncExitStack()
        try:
            if transport == "stdio":
                read, write = await self._open_stdio_session(stack, config, timeout)
            else:
                read, write = await self._open_http_session(stack, config, timeout)
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

    async def _open_stdio_session(self, stack: AsyncExitStack, config: dict, timeout: float):
        """Spawn the stdio subprocess and open its streams on *stack*.

        The child's environment is the engine's environment plus the
        config ``env`` (``${VAR}``-expanded, overriding on collision) —
        the SDK's default would only inherit an allow-list of variables,
        which would silently drop e.g. a server's API key from the
        environment. ``cwd`` is passed through when set. The child's
        stderr goes to the engine's stderr (SDK default); stdout is
        protocol only. Closing the stack terminates the subprocess.
        """
        command = config.get("command")
        if not command:
            raise MCPClientError("stdio server config requires 'command'")
        env = {
            **os.environ,
            **{
                k: expand_env_vars(v)
                for k, v in (config.get("env") or {}).items()
            },
        }
        params = StdioServerParameters(
            command=command,
            args=list(config.get("args") or []),
            env=env,
            cwd=config.get("cwd"),
        )
        return await stack.enter_async_context(stdio_client(params))

    async def _open_http_session(self, stack: AsyncExitStack, config: dict, timeout: float):
        """Open the Streamable HTTP streams on *stack*."""
        url = config.get("url")
        if not url:
            raise MCPClientError("Streamable HTTP server config requires 'url'")
        headers = {
            k: expand_env_vars(v)
            for k, v in (config.get("headers") or {}).items()
        }
        http_client = _make_http_client(url, headers, timeout)
        await stack.enter_async_context(http_client)
        read, write, _get_session_id = await stack.enter_async_context(
            streamable_http_client(url, http_client=http_client)
        )
        return read, write

    # -- lifecycle -------------------------------------------------------
    async def _open_and_register(self, name: str, config: dict) -> Dict[str, Any]:
        """Open a session in *this* task and register it.

        Shared by :meth:`connect` and the owner task in
        :meth:`connect_owned`. Returns {"ok": bool, "error": str|None}.
        Only a genuine cancellation escapes (re-raised by _open_session).
        """
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

    async def connect(self, name: str, config: dict) -> Dict[str, Any]:
        """Connect to *name* in the caller's task; returns {"ok": bool, "error": str|None}."""
        if name in self._sessions:
            if self.get_status(name)["status"] == "connected":
                return {"ok": True, "error": None}
            await self._drop_dead_session(name)
        owner = self._owner_tasks.get(name)
        if owner is not None and not owner.done():
            return {"ok": True, "error": None}
        return await self._open_and_register(name, config)

    async def connect_owned(self, name: str, config: dict) -> Dict[str, Any]:
        """Connect to *name* in a dedicated owner task (issue 004).

        For auto-connect: the caller must not block on the connect, and
        the session must be closable later from a different task (the
        websocket handler). The owner task opens the session, signals
        readiness, then parks until :meth:`disconnect` sets its close
        event — and closes the stack itself, in the task that opened it.
        Returns {"ok": bool, "error": str|None} once the session is up
        (or the connect has failed); the owner task keeps running.
        """
        if name in self._sessions:
            if self.get_status(name)["status"] == "connected":
                return {"ok": True, "error": None}
            await self._drop_dead_session(name)
        owner = self._owner_tasks.get(name)
        if owner is not None and not owner.done():
            return {"ok": True, "error": None}
        ready = asyncio.Event()
        result: Dict[str, Any] = {}

        async def _owner() -> None:
            close = asyncio.Event()
            self._close_events[name] = close
            my_stack = my_session = my_config = None
            try:
                try:
                    res = await self._open_and_register(name, config)
                except BaseException:
                    ready.set()
                    raise
                result.update(res)
                ready.set()
                if not result.get("ok"):
                    return
                # Capture our own registrations: a later reconnect may
                # replace them, and this task must never remove a new
                # session's registrations when it finishes.
                my_stack = self._stacks.get(name)
                my_session = self._sessions.get(name)
                my_config = self._configs.get(name)
                await close.wait()
                if self._stacks.get(name) is my_stack:
                    self._stacks.pop(name, None)
                if self._sessions.get(name) is my_session:
                    self._sessions.pop(name, None)
                if self._configs.get(name) is my_config:
                    self._configs.pop(name, None)
                if my_stack is not None:
                    try:
                        await my_stack.aclose()
                    except BaseException as e:
                        if _is_genuine_cancellation(e):
                            raise
                        logger.warning("MCP disconnect failed for %s: %s", name, e)
                self._set_status(name, "disconnected", error=None)
            finally:
                if self._owner_tasks.get(name) is asyncio.current_task():
                    self._owner_tasks.pop(name, None)
                if self._close_events.get(name) is close:
                    self._close_events.pop(name, None)

        task = asyncio.create_task(_owner())
        self._owner_tasks[name] = task
        await ready.wait()
        if not result.get("ok"):
            self._owner_tasks.pop(name, None)
            self._close_events.pop(name, None)
            return {"ok": False, "error": result.get("error") or "MCP connection was cancelled"}
        return {"ok": True, "error": None}

    async def _drop_dead_session(self, name: str) -> None:
        """Drop a failed (dead) session registration so a fresh connect can open a new one.

        The session is already dead — closing it is best-effort. For an
        owned session the owner task is woken and awaited (bounded), so
        its stack is closed in the task that opened it; for a manual
        session the stack is closed here. A live session is never
        dropped: callers only reach this when status is "failed".
        """
        owner = self._owner_tasks.get(name)
        if owner is not None and not owner.done():
            event = self._close_events.get(name)
            if event is not None:
                event.set()
            try:
                await asyncio.wait_for(asyncio.shield(owner), timeout=10)
            except asyncio.TimeoutError:
                logger.warning("MCP dead-session teardown timed out for %s", name)
                self._owner_tasks.pop(name, None)
                self._close_events.pop(name, None)
            except asyncio.CancelledError as e:
                if _is_genuine_cancellation(e):
                    raise
                logger.warning("MCP dead-session teardown failed for %s", name)
            return
        stack = self._stacks.pop(name, None)
        self._sessions.pop(name, None)
        self._configs.pop(name, None)
        if stack is not None:
            try:
                await stack.aclose()
            except BaseException as e:
                if _is_genuine_cancellation(e):
                    raise
                logger.warning("MCP dead-session close failed for %s: %s", name, e)
        self._set_status(name, "disconnected", error=None)

    async def disconnect(self, name: str) -> Dict[str, Any]:
        owner = self._owner_tasks.get(name)
        if owner is not None and not owner.done():
            # Route the close through the owner task: closing the stack
            # from this task would hit the anyio cancel-scope rule and
            # leak the session (stdio subprocess). Never cancel the owner
            # — cancelling inside the SDK's cancel scope corrupts it.
            event = self._close_events.get(name)
            if event is not None:
                event.set()
            try:
                await asyncio.wait_for(asyncio.shield(owner), timeout=30)
            except asyncio.TimeoutError:
                logger.warning(
                    "MCP disconnect timed out for %s; owner task abandoned", name
                )
                self._owner_tasks.pop(name, None)
                self._close_events.pop(name, None)
                self._set_status(name, "disconnected", error=None)
            except asyncio.CancelledError as e:
                if _is_genuine_cancellation(e):
                    raise
                logger.warning("MCP disconnect failed for %s: owner task ended", name)
                self._set_status(name, "disconnected", error=None)
            return {"ok": True, "error": None}
        self._owner_tasks.pop(name, None)
        self._close_events.pop(name, None)
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

    async def shutdown_owned(self) -> None:
        """Close every owner-task session (engine shutdown, issue 004).

        Bounded: an owner still mid-connect (e.g. a 30s blackhole) must
        not hold shutdown hostage — wait at most 10s, then return; the
        process is exiting anyway.
        """
        owners = [t for t in self._owner_tasks.values() if not t.done()]
        for event in list(self._close_events.values()):
            event.set()
        if owners:
            await asyncio.wait(owners, timeout=10)

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
