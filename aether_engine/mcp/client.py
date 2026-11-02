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
  on POSIX the child's stderr reaches the engine's stderr through a
  bounded relay (E3-F05), so a chatty server cannot corrupt the
  protocol or grow engine logs without limit.
- stdio servers are spawned through ``stdio_launcher.py`` (E3-F05),
  which applies worker-equivalent CPU/memory caps before the server's
  own code runs — a user-added server is untrusted code and must not
  inherit the engine's unlimited resource limits.
"""
import asyncio
import logging
import os
import re
import sys
import threading
from contextlib import AsyncExitStack
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

logger = logging.getLogger("aether_engine.mcp.client")

# Issue 006: cap for a loaded resource, same scale as Tool.max_output_chars.
MCP_RESOURCE_MAX_CHARS = 20000

# Issue 007: cap for one prompt message, same scale — a prompt template
# is user-supplied text from an untrusted server, never unbounded.
MCP_PROMPT_MAX_CHARS = 20000

# E3-F05: stdio resource governance. Stdio servers are spawned through
# stdio_launcher.py, which applies worker-equivalent caps before the
# server's code runs (POSIX: rlimits via exec; Windows: a supervising
# Job Object). The defaults are proportioned to a session-long process
# rather than a single tool call — the memory ceiling matches the
# worker "standard" profile (4096 MB, workers/sandbox.py) and the CPU
# budget is 3600s of cumulative CPU over the server's lifetime. A
# server config may override either with ``time_cap_seconds`` /
# ``memory_cap_mb`` (the worker-profile vocabulary); an explicit 0
# disables that cap — the user's choice for their own allow-listed
# server, never the silent default.
MCP_STDIO_DEFAULT_TIME_CAP_SECONDS = 3600
MCP_STDIO_DEFAULT_MEMORY_CAP_MB = 4096

# E3-F05: byte cap for one stdio server's relayed stderr (POSIX). The
# first bytes are forwarded to the engine's stderr (server logs stay
# visible, as before); anything beyond is drained and discarded, so a
# flooding server can neither block on a full pipe nor grow the
# engine's logs without limit.
MCP_STDIO_STDERR_CAP_BYTES = 1024 * 1024

_STDIO_LAUNCHER_PATH = Path(__file__).with_name("stdio_launcher.py")

# Issue 011 (S2): minimal environment allow-list for stdio children.
# A stdio server is untrusted code: it gets the config ``env`` (expanded)
# plus only these variables — never the engine's full environment, which
# holds provider API keys. The SDK default allow-list is not used either:
# it silently drops a server's own secrets (issue 003). Node / npx (the
# filesystem server) needs PATH + HOME (+ TEMP on Windows); a Python
# server run by absolute path needs PATH for its own subprocesses.
MCP_STDIO_ENV_ALLOWLIST = frozenset(
    {
        "PATH", "PATHEXT", "HOME", "USER", "USERNAME", "LOGNAME",
        "LANG", "LC_ALL", "LC_CTYPE", "LANGUAGE",
        "TEMP", "TMP", "TMPDIR",
        "SystemRoot", "SystemDrive", "WINDIR", "ComSpec",
        "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432",
        "APPDATA", "LOCALAPPDATA", "HOMEDRIVE", "HOMEPATH", "USERPROFILE",
        "VIRTUAL_ENV", "PYTHONPATH", "SHELL", "TERM", "OS",
        # Proxy / CA-bundle config (no credentials, just endpoints and
        # file paths). npx/npm resolve packages through the registry
        # even when cached: without these, a stdio server started via
        # npx hangs on registry access instead of starting (verified:
        # filesystem server answers in ~1s with them, never without).
        "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
        "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy",
        "NODE_EXTRA_CA_CERTS", "NODE_USE_ENV_PROXY",
        "SSL_CERT_FILE", "CURL_CA_BUNDLE", "REQUESTS_CA_BUNDLE",
        "GIT_SSL_CAINFO",
    }
)

_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _host_matches_no_proxy(host: str) -> bool:
    """True if *host* is listed in NO_PROXY / no_proxy (never raises).

    Entries are comma/space separated; ``*`` matches everything, a
    leading-dot entry (``.example.com``) matches the domain and its
    subdomains, anything else matches exactly. Malformed entries (e.g.
    the ``[::1]`` that used to break httpx URL parsing) are ignored, not
    fatal — a bad NO_PROXY entry must never break a request. CIDR ranges
    are not supported and are ignored.
    """
    try:
        raw = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
        host = host.lower()
        for entry in re.split(r"[,\s]+", raw):
            entry = entry.strip().strip("[]").lower()
            if not entry:
                continue
            if entry == "*":
                return True
            if entry == host:
                return True
            if entry.startswith(".") and host.endswith(entry):
                return True
        return False
    except (AttributeError, TypeError, ValueError):
        return False


def _make_http_client(url: str, headers: dict[str, str] | None, timeout: float) -> httpx.AsyncClient:
    """Build the httpx client for one server URL.

    httpx reads the proxy map from the environment when ``trust_env`` is
    left on, and a malformed ``NO_PROXY`` entry (e.g. ``[::1]``) makes
    *every* request fail at URL parse time, even to localhost. So we build
    the client explicitly with ``trust_env=False``: localhost servers never
    go through a proxy (standard ``no_proxy`` behaviour); remote servers
    use the plain ``HTTP(S)_PROXY`` env value if one is set and the host
    is not listed in ``NO_PROXY``, and no proxy otherwise.
    """
    host = (urlparse(url).hostname or "").lower()
    local = host in ("localhost", "127.0.0.1", "::1")
    if local or _host_matches_no_proxy(host):
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


def expand_env_vars(value: str, environ: dict[str, str] | None = None) -> str:
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


def _stdio_resource_caps(config: dict) -> tuple[int, int]:
    """Resolve ``(time_cap_seconds, memory_cap_mb)`` for a stdio server.

    Absent keys get the MCP defaults; an explicit 0 disables that cap;
    anything that is not a non-negative number is a loud
    :class:`MCPClientError` at connect time — a mistyped cap must
    never silently become "no cap", nor explode inside the launcher
    where the user would not see the reason.
    """
    caps = []
    for key, default, unit in (
        ("time_cap_seconds", MCP_STDIO_DEFAULT_TIME_CAP_SECONDS, "seconds"),
        ("memory_cap_mb", MCP_STDIO_DEFAULT_MEMORY_CAP_MB, "MB"),
    ):
        raw = config.get(key, default)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw < 0:
            raise MCPClientError(
                f"stdio server config {key!r} must be a non-negative number "
                f"of {unit} (0 disables the cap); got {raw!r}"
            )
        caps.append(int(raw))
    return caps[0], caps[1]


class _BoundedStderrRelay:
    """Bounded drain for one stdio server's stderr (E3-F05, POSIX).

    The SDK points the child's stderr at :attr:`writer` (a pipe). A
    daemon thread drains the pipe: the first
    ``MCP_STDIO_STDERR_CAP_BYTES`` bytes are forwarded to the engine's
    stderr (the pre-existing behaviour — server logs stay visible),
    everything beyond is read and discarded, with the overflow counted
    and logged once. The drain is what makes the bound real: capping
    without draining would just move the failure, since a chatty
    server would block forever on a full pipe. The thread ends at EOF
    — when the server has exited and the parent's writer copy has been
    closed via :meth:`close_writer` (done right after spawn).
    """

    def __init__(self, target) -> None:
        self._target = target
        self.total_bytes = 0
        self.forwarded_bytes = 0
        self.truncated = False
        read_fd, write_fd = os.pipe()
        self._read = os.fdopen(read_fd, "rb", buffering=0)
        self.writer = os.fdopen(write_fd, "wb", buffering=0)
        self._thread = threading.Thread(
            target=self._drain, name="mcp-stderr-relay", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def close_writer(self) -> None:
        try:
            self.writer.close()
        except OSError:
            pass

    def wait(self, timeout: float | None = None) -> bool:
        """Join the drain thread; True if it finished (EOF reached)."""
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def _drain(self) -> None:
        try:
            while True:
                chunk = self._read.read(65536)
                if not chunk:
                    break
                self.total_bytes += len(chunk)
                room = MCP_STDIO_STDERR_CAP_BYTES - self.forwarded_bytes
                if room > 0:
                    piece = chunk[:room]
                    self._forward(piece)
                    self.forwarded_bytes += len(piece)
                if len(chunk) > max(room, 0) and not self.truncated:
                    self.truncated = True
                    logger.warning(
                        "MCP stdio server stderr exceeded %d bytes; "
                        "further output is being discarded",
                        MCP_STDIO_STDERR_CAP_BYTES,
                    )
        except (OSError, ValueError):
            pass  # read end torn down mid-drain — nothing left to do

    def _forward(self, data: bytes) -> None:
        target = self._target
        if target is None:
            return
        try:
            stream = getattr(target, "buffer", None) or target
            try:
                stream.write(data)
            except TypeError:
                stream.write(data.decode("utf-8", errors="replace"))
            stream.flush()
        except (OSError, ValueError):
            pass  # engine stderr went away (teardown) — keep draining


def _make_stderr_relay() -> _BoundedStderrRelay | None:
    """The bounded stderr relay for a stdio spawn, or None where the
    SDK's direct passthrough stays: Windows (the child inherits the
    engine's stderr handle through the launcher supervisor; there is
    no pipe seam to drain) and headless POSIX runs with no stderr."""
    if sys.platform == "win32" or sys.stderr is None:
        return None
    return _BoundedStderrRelay(sys.stderr)


class MCPClientManager:
    """Holds one ``ClientSession`` per connected MCP server."""

    def __init__(self) -> None:
        self._sessions: dict[str, ClientSession] = {}
        self._stacks: dict[str, AsyncExitStack] = {}
        self._configs: dict[str, dict] = {}
        self._status: dict[str, dict[str, Any]] = {}
        # Session ownership (issue 004, E3-F03): EVERY session
        # registered here is owned by exactly one dedicated owner
        # task. The task that opened the session must also close it —
        # anyio forbids exiting a cancel scope from a different task,
        # and closing a stdio stack from another task silently leaks
        # the subprocess. The owner task parks after opening the
        # session and closes the stack itself when its close event is
        # set; callers (websocket handlers, startup, shutdown) only
        # ever signal the owner, they never touch the stack. There is
        # no unowned registered session: a caller task (a socket
        # handler above all) can die at any time, and a session whose
        # stack lives in a dead caller's task can neither be closed
        # cleanly nor survive its owner — it is simply orphaned.
        self._owner_tasks: dict[str, asyncio.Task] = {}
        self._close_events: dict[str, asyncio.Event] = {}

    # -- status ---------------------------------------------------------
    def get_status(self, name: str) -> dict[str, Any]:
        return dict(
            self._status.get(name, {"status": "disconnected", "error": None, "tool_count": 0})
        )

    def is_connected(self, name: str) -> bool:
        return name in self._sessions

    def _set_status(
        self, name: str, status: str, error: str | None = None, tool_count: int | None = None
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
            # Issue 011 (S1): every session call is bounded by the server
            # config timeout. ClientSession's read timeout applies to all
            # send_request calls (list_tools, list_resources, prompts, ...)
            # via anyio.fail_after, raising McpError("Timed out while
            # waiting for response to ...") — a stalled server can no
            # longer hang the sequential websocket loop forever. Only
            # call_tool overrides it, with the Tool timeout (see below).
            session = ClientSession(
                read, write, read_timeout_seconds=timedelta(seconds=timeout)
            )
            await stack.enter_async_context(session)
            await asyncio.wait_for(session.initialize(), timeout=timeout)
        except BaseException as e:
            # Closing the stack must never mask the original failure.
            try:
                await stack.aclose()
            except BaseException as close_err:  # noqa: BLE001 — cleanup must never mask the original failure
                logger.debug(
                    "MCP session stack close failed during connect cleanup: %s",
                    close_err,
                )
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

        The child's environment is the config ``env`` (``${VAR}``-expanded,
        overriding on collision) plus an explicit minimal allow-list of
        the engine's environment (issue 011, S2) — never the full
        ``os.environ``, which holds provider API keys a user-added
        server must not see. The SDK's default would only inherit an
        allow-list of variables, which would silently drop e.g. a
        server's API key from the environment (issue 003); this is
        the same idea, made explicit and auditable here instead.
        ``cwd`` is passed through when set. The child's stderr goes to
        the engine's stderr (SDK default); stdout is protocol only.
        Closing the stack terminates the subprocess.

        E3-F05: the SDK is handed the resource-capped launcher
        (``stdio_launcher.py``) as the "server" command, with the real
        command after ``--``. The launcher applies the resolved
        CPU/memory caps before the server's code runs (POSIX: rlimits
        then exec, so the capped process IS the SDK's child; Windows:
        a Job Object supervisor). The spawn therefore assumes
        ``sys.executable`` is a real Python interpreter — the same
        assumption the worker sandbox already makes
        (``python -m aether_worker``). On POSIX the child's stderr is
        additionally routed through the bounded relay.
        """
        command = config.get("command")
        if not command:
            raise MCPClientError("stdio server config requires 'command'")
        env = {
            k: v
            for k, v in os.environ.items()
            if k in MCP_STDIO_ENV_ALLOWLIST
        }
        env.update(
            {
                k: expand_env_vars(v)
                for k, v in (config.get("env") or {}).items()
            }
        )
        cpu_cap, memory_cap = _stdio_resource_caps(config)
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                str(_STDIO_LAUNCHER_PATH),
                f"--cpu-seconds={cpu_cap}",
                f"--memory-mb={memory_cap}",
                "--",
                command,
                *list(config.get("args") or []),
            ],
            env=env,
            cwd=config.get("cwd"),
        )
        relay = _make_stderr_relay()
        if relay is None:
            return await stack.enter_async_context(stdio_client(params))
        relay.start()
        try:
            streams = await stack.enter_async_context(
                stdio_client(params, errlog=relay.writer)
            )
        except BaseException:
            relay.close_writer()
            raise
        # The spawn has happened: the parent's copy of the pipe's write
        # end closes now, so the drain thread sees EOF once the server
        # (the only remaining writer) exits and the stack closes it.
        relay.close_writer()
        return streams

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
    async def _open_and_register(self, name: str, config: dict) -> dict[str, Any]:
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
        except Exception as e:  # noqa: BLE001 — connect boundary: any SDK/transport failure becomes a failed status
            message = str(e) or e.__class__.__name__
            logger.warning("MCP connect failed for %s: %s", name, message)
            self._set_status(name, "failed", error=message)
            return {"ok": False, "error": message}
        self._stacks[name] = stack
        self._sessions[name] = session
        self._configs[name] = dict(config)
        try:
            # Issue 011 (S1): list_tools is bounded by the session read
            # timeout (server config timeout). A server that stalls here
            # is a failed connect, not a connected server with no tools:
            # clean up the session opened in this task (closing it here
            # is safe — this is the task that opened it, manual or
            # owner) and report the failure instead of returning ok.
            tools = await session.list_tools()
            tool_count = len(tools.tools)
        except Exception as e:  # noqa: BLE001 — a server that stalls/fails listing tools is a failed connect, whatever the SDK raises
            message = _failure_message(e)
            logger.warning("MCP connect failed for %s while listing tools: %s", name, message)
            try:
                await stack.aclose()
            except BaseException as close_e:
                if _is_genuine_cancellation(close_e):
                    raise
                logger.warning("MCP connect cleanup failed for %s: %s", name, close_e)
            self._stacks.pop(name, None)
            self._sessions.pop(name, None)
            self._configs.pop(name, None)
            self._set_status(name, "failed", error=message)
            return {"ok": False, "error": message}
        self._set_status(name, "connected", error=None, tool_count=tool_count)
        return {"ok": True, "error": None}

    async def connect(self, name: str, config: dict) -> dict[str, Any]:
        """Connect to *name*; returns {"ok": bool, "error": str|None}.

        The session is opened by a dedicated owner task, never in
        the caller's task (E3-F03): see :meth:`connect_owned`. This
        used to open the session in whichever task called it — for a
        manual Connect that is a websocket handler task — which left
        the session with no stable owner: a disconnect from another
        socket closed its AnyIO stack from the wrong task (anyio
        cancel-scope error, first handler cancelled), a socket close
        orphaned it entirely, and the shutdown hook, which only
        closes owner-task sessions, never closed it. There is now
        exactly one way a session comes to be registered, and it
        always has an owner.
        """
        return await self.connect_owned(name, config)

    async def connect_owned(self, name: str, config: dict) -> dict[str, Any]:
        """Connect to *name* in a dedicated owner task (issue 004).

        The one way sessions are opened — auto-connect and manual
        connects alike (E3-F03). The caller must not block on the
        connect, and the session must be closable later from a
        different task (another websocket handler, the shutdown
        hook). The owner task opens the session, signals readiness,
        then parks until :meth:`disconnect` sets its close event —
        and closes the stack itself, in the task that opened it.
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
        result: dict[str, Any] = {}

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

        The session is already dead — closing it is best-effort. The
        owner task is woken and awaited (bounded), so its stack is
        closed in the task that opened it. The fallback below covers
        a registration whose owner task is already gone (e.g. it
        crashed): the registrations are dropped and the stack close
        is attempted here, best-effort. A live session is never
        dropped: callers only reach this when status is "failed".
        """
        owner = self._owner_tasks.get(name)
        if owner is not None and not owner.done():
            event = self._close_events.get(name)
            if event is not None:
                event.set()
            try:
                await asyncio.wait_for(asyncio.shield(owner), timeout=10)
            except TimeoutError:
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

    async def disconnect(self, name: str) -> dict[str, Any]:
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
            except TimeoutError:
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
        # No live owner: the session's owner task already finished
        # (or the registration predates ownership). Registered
        # sessions are always owned (E3-F03), so this is a defensive
        # close for a leftover registration — drop it and attempt the
        # stack close here, best-effort, rather than leaking it.
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
        """Close every session (engine shutdown, issue 004).

        Every registered session is owner-task managed (E3-F03), so
        signalling every owner closes manual and auto-connected
        sessions alike — before this, the shutdown hook left manual
        sessions (and their stdio children) running with no owner.

        Bounded: an owner still mid-connect (e.g. a 30s blackhole) must
        not hold shutdown hostage — wait at most 10s, then return; the
        process is exiting anyway.
        """
        owners = [t for t in self._owner_tasks.values() if not t.done()]
        for event in list(self._close_events.values()):
            event.set()
        if owners:
            await asyncio.wait(owners, timeout=10)

    async def test_connection(self, name: str, config: dict) -> dict[str, Any]:
        """Connect, list tools, disconnect. Leaves no session behind."""
        try:
            stack, session = await self._open_session(config)
        except Exception as e:  # noqa: BLE001 — test boundary: any failure is reported in the result dict
            message = str(e) or e.__class__.__name__
            logger.warning("MCP test connection failed for %s: %s", name, message)
            return {"ok": False, "error": message, "tools": []}
        try:
            tools = await session.list_tools()
            return {"ok": True, "error": None, "tools": [t.name for t in tools.tools]}
        except Exception as e:  # noqa: BLE001 — test boundary: any failure is reported in the result dict
            message = str(e) or e.__class__.__name__
            logger.warning(
                "MCP test connection failed for %s while listing tools: %s",
                name, message,
            )
            return {"ok": False, "error": str(e), "tools": []}
        finally:
            # Closing must never mask the test result (same guard as
            # disconnect): only a genuine cancellation escapes.
            try:
                await stack.aclose()
            except BaseException as e:
                if _is_genuine_cancellation(e):
                    raise
                logger.warning("MCP test_connection close failed for %s: %s", name, e)

    # -- tools ------------------------------------------------------------
    async def list_tools(self, name: str):
        session = self._sessions.get(name)
        if session is None:
            raise MCPClientError(f"MCP server '{name}' is not connected")
        try:
            # Issue 011 (S1): bounded by the session read timeout (server
            # config timeout); a stalled server fails here, marks the
            # server failed, and never hangs the caller forever.
            result = await session.list_tools()
        except BaseException as e:
            if _is_genuine_cancellation(e):
                raise
            if isinstance(e, BaseExceptionGroup):
                message = _first_leaf_message(e)
            else:
                message = _failure_message(e)
            self._set_status(name, "failed", error=message)
            raise MCPClientError(
                f"MCP server '{name}' list_tools failed: {message}"
            ) from e
        return result.tools

    async def call_tool(
        self, server: str, tool: str, args: dict[str, Any] | None = None,
        timeout: float | None = None,
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
        # The per-request timeout overrides the session read timeout
        # (issue 011, S1): a tool call is bounded by the Tool timeout
        # (60s, issue 009 S2), never by the shorter server connect
        # timeout — a slow tool must not be cut off at 30s.
        inner = asyncio.ensure_future(
            session.call_tool(
                tool, args or {}, read_timeout_seconds=timedelta(seconds=timeout)
            )
        )
        try:
            result = await asyncio.wait_for(asyncio.shield(inner), timeout=timeout)
        except TimeoutError:
            inner.cancel()
            try:
                await inner
            except BaseException as await_err:  # noqa: BLE001 — the timed-out task's outcome (incl. CancelledError) is intentionally discarded
                logger.debug(
                    "MCP tool call task ended after timeout cancel: %s", await_err
                )
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

    # -- resources (issue 006) ------------------------------------------
    async def list_resources(self, name: str) -> list[dict[str, Any]]:
        """List *name*'s resources as plain dicts (uri / name / description / mimeType)."""
        session = self._sessions.get(name)
        if session is None:
            raise MCPClientError(f"MCP server '{name}' is not connected")
        try:
            result = await session.list_resources()
        except BaseException as e:
            if _is_genuine_cancellation(e):
                raise
            if isinstance(e, BaseExceptionGroup):
                message = _first_leaf_message(e)
            else:
                message = _failure_message(e)
            self._set_status(name, "failed", error=message)
            raise MCPClientError(
                f"MCP server '{name}' list_resources failed: {message}"
            ) from e
        return [
            {
                "uri": str(r.uri),
                "name": r.name,
                "description": r.description,
                "mimeType": r.mimeType,
            }
            for r in result.resources
        ]

    async def read_resource(self, name: str, uri: str) -> str:
        """Read resource *uri* from *name*; returns its text, capped at
        ``MCP_RESOURCE_MAX_CHARS`` with a truncation note (never silently
        cut). Read-only: nothing is executed. No retry — a dead server
        fails immediately, like a tool call (PRD Q14).
        """
        session = self._sessions.get(name)
        if session is None:
            raise MCPClientError(f"MCP server '{name}' is not connected")
        try:
            result = await session.read_resource(uri)
        except BaseException as e:
            if _is_genuine_cancellation(e):
                raise
            if isinstance(e, BaseExceptionGroup):
                message = _first_leaf_message(e)
            else:
                message = _failure_message(e)
            self._set_status(name, "failed", error=message)
            raise MCPClientError(
                f"MCP server '{name}' resource '{uri}' failed: {message}"
            ) from e
        text = _resource_content_to_text(result.contents, uri)
        if len(text) > MCP_RESOURCE_MAX_CHARS:
            text = (
                text[:MCP_RESOURCE_MAX_CHARS]
                + f"\n...[resource truncated to {MCP_RESOURCE_MAX_CHARS} chars]"
            )
        return text

    # -- prompts (issue 007) --------------------------------------------
    async def list_prompts(self, name: str) -> list[dict[str, Any]]:
        """List *name*'s prompt templates as plain dicts (name /
        description / arguments[{name, description, required}])."""
        session = self._sessions.get(name)
        if session is None:
            raise MCPClientError(f"MCP server '{name}' is not connected")
        try:
            result = await session.list_prompts()
        except BaseException as e:
            if _is_genuine_cancellation(e):
                raise
            if isinstance(e, BaseExceptionGroup):
                message = _first_leaf_message(e)
            else:
                message = _failure_message(e)
            self._set_status(name, "failed", error=message)
            raise MCPClientError(
                f"MCP server '{name}' list_prompts failed: {message}"
            ) from e
        return [
            {
                "name": p.name,
                "description": p.description,
                "arguments": [
                    {
                        "name": a.name,
                        "description": a.description,
                        "required": bool(a.required),
                    }
                    for a in (p.arguments or [])
                ],
            }
            for p in result.prompts
        ]

    async def get_prompt(
        self, server: str, prompt: str, arguments: dict[str, Any] | None = None
    ) -> list[dict[str, str]]:
        """Run prompt template *prompt* on *server* with *arguments*.

        Arguments are user-supplied and are validated, never executed:
        they must be a dict of string values (the MCP protocol only
        takes string arguments). Returns the prompt's messages as
        ``[{role, content}]`` — roles are "user" / "assistant" only,
        anything else is treated as "user". Each message is capped at
        ``MCP_PROMPT_MAX_CHARS`` with a truncation note, never silently
        cut. Issue 011 (S3): every message is prefixed with
        ``[MCP prompt from server '<server>']`` — the text is
        server-supplied, so the chat, the supervisor briefing and the
        UI must be able to tell it from the model's (or the user's)
        own words. Tagging, not re-roling: few-shot prompts
        legitimately use assistant turns. No retry — a dead server
        fails immediately (PRD Q14).
        """
        args = _validate_prompt_arguments(arguments)
        session = self._sessions.get(server)
        if session is None:
            raise MCPClientError(f"MCP server '{server}' is not connected")
        try:
            result = await session.get_prompt(prompt, args)
        except BaseException as e:
            if _is_genuine_cancellation(e):
                raise
            if isinstance(e, BaseExceptionGroup):
                message = _first_leaf_message(e)
            else:
                message = _failure_message(e)
            self._set_status(server, "failed", error=message)
            raise MCPClientError(
                f"MCP server '{server}' prompt '{prompt}' failed: {message}"
            ) from e
        messages = []
        tag = f"[MCP prompt from server '{server}']"
        for msg in result.messages:
            role = msg.role if msg.role in ("user", "assistant") else "user"
            content = getattr(msg.content, "text", None)
            if content is None:
                content = "[non-text prompt content — not inlined]"
            if len(content) > MCP_PROMPT_MAX_CHARS:
                content = (
                    content[:MCP_PROMPT_MAX_CHARS]
                    + f"\n...[prompt message truncated to {MCP_PROMPT_MAX_CHARS} chars]"
                )
            messages.append({"role": role, "content": f"{tag} {content}"})
        return messages


def _validate_prompt_arguments(arguments: Any | None) -> dict[str, str]:
    """MCP prompt arguments must be a dict of string values.

    Raises MCPClientError otherwise — a prompt's arguments come from
    the user (panel form / slash command), and a non-string value has
    no meaning in the MCP protocol; failing loudly beats coercing
    silently (e.g. turning ``42`` into ``"42"`` and surprising the
    server, or ``None`` into ``"None"``).
    """
    if arguments is None:
        return {}
    if not isinstance(arguments, dict):
        raise MCPClientError("MCP prompt arguments must be an object of string values.")
    for k, v in arguments.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise MCPClientError(
                "MCP prompt arguments must be an object of string values."
            )
    return dict(arguments)


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
    seen: BaseException | None = exc
    while seen is not None:
        if str(seen):
            return str(seen)
        seen = seen.__context__
    return exc.__class__.__name__


def _resource_content_to_text(contents: list[Any], uri: str) -> str:
    """Resource contents -> plain text. Binary (blob) contents are not
    inlined — they are noted, never decoded as text."""
    parts = []
    for block in contents or []:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(text)
        else:
            parts.append(f"[binary resource '{uri}' — content not inlined]")
    return "\n".join(parts)


def _content_to_text(content: list[Any]) -> str:
    parts = []
    for block in content or []:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(text)
        else:
            parts.append(str(block))
    return "\n".join(parts)
