"""MCP server allow-list + tool conversion (issue 001).

Manages the user-approved allow-list in ``~/.aether/mcp_servers.json``
(validated — see :func:`validate_mcp_server_config`), delegates live
connections to :class:`aether_engine.mcp.client.MCPClientManager`, and
converts each connected server's tools into Aether ``Tool`` objects named
``server__tool`` (risk EXECUTE, source ``mcp:<server>``).

``get_mcp_tools()`` returns the *cached* converted tools: a server's tools
appear only after it has been connected (``connect_server`` /
``test_connection`` never happens implicitly here — the websocket layer
drives connects). Before that it returns ``[]``, honestly.
"""
import asyncio
import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any

from aether_engine.mcp.client import MCPClientManager
from aether_engine.mcp.fallback import execute_mcp_tool_async
from aether_engine.tools.registry import Tool, ToolRisk

logger = logging.getLogger("aether_engine.mcp.registry")

_MCP_CONFIG_PATH = Path.home() / ".aether" / "mcp_servers.json"
_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,100}$")


class MCPConfigError(ValueError):
    """Raised when an MCP server name or config fails validation."""


# Tool-call timeout (PRD §4: Tool.timeout_s, default 60s). The server
# config timeout bounds *connects* only; a tool call is bounded by this.
MCP_TOOL_TIMEOUT_S = 60


def redact_mcp_config_for_ui(config: Any) -> dict[str, Any]:
    """Copy of *config* that is safe to send to the UI.

    Header / env *values* are secrets (API keys) and must never reach
    browser memory, websocket traffic, or logs — only the names are
    returned, e.g. ``headers: ["x-api-key"]``.
    """
    if not isinstance(config, dict):
        return {}
    out = dict(config)
    for field in ("headers", "env"):
        value = out.get(field)
        if isinstance(value, dict):
            out[field] = sorted(value.keys())
    return out


def _valid_input_schema(schema: Any) -> dict[str, Any]:
    """An MCP tool's inputSchema must be an object schema.

    Servers are untrusted: a non-dict schema, or a schema whose type is
    not ``"object"``, falls back to an empty object schema with a warning
    instead of breaking tool conversion or the model call.
    """
    if isinstance(schema, dict) and schema.get("type") == "object":
        return schema
    logger.warning(
        "MCP tool inputSchema is not an object schema (%r); using empty object schema.",
        schema,
    )
    return {"type": "object", "properties": {}}


def _str_dict(value: Any, field: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise MCPConfigError(f"MCP server '{field}' must be an object of string values.")
    for k, v in value.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise MCPConfigError(f"MCP server '{field}' keys and values must be strings.")
    return dict(value)


def validate_mcp_server_config(name: str, config: dict) -> dict:
    """Validate + normalise one server config. Raises MCPConfigError.

    Rules (PRD §4 Q6): exactly one transport — ``command`` (stdio) or
    ``url`` (Streamable HTTP), never both, never neither; ``transport``
    is inferred from the shape when omitted (standard ``mcpServers``
    format); ``headers``/``env`` are string dicts; timeout 1–300s,
    default 30; ``enabled``/``auto_connect`` default true.
    """
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise MCPConfigError(
            f"Invalid MCP server name {name!r}: use 1-100 letters, digits, '_', '.' or '-'."
        )
    if not isinstance(config, dict):
        raise MCPConfigError(f"MCP server config for {name!r} must be an object.")

    has_command = config.get("command") is not None
    has_url = config.get("url") is not None
    if has_command and has_url:
        raise MCPConfigError(
            f"MCP server {name!r} must use exactly one transport: 'command' (stdio) "
            "or 'url' (Streamable HTTP), not both."
        )

    transport = config.get("transport")
    if transport is None:
        transport = "stdio" if has_command else "streamable_http" if has_url else None
    if transport not in ("stdio", "streamable_http"):
        raise MCPConfigError(
            f"MCP server {name!r} needs a transport: 'command' for stdio or "
            "'url' for Streamable HTTP."
        )

    cfg = dict(config)
    cfg["transport"] = transport
    if transport == "stdio":
        if not isinstance(cfg.get("command"), str) or not cfg["command"]:
            raise MCPConfigError(f"MCP server {name!r} (stdio) requires a 'command'.")
        args = cfg.get("args") or []
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise MCPConfigError(f"MCP server {name!r} 'args' must be a list of strings.")
        cfg["args"] = list(args)
        cfg["env"] = _str_dict(cfg.get("env") or {}, "env")
        if cfg.get("cwd") is not None and not isinstance(cfg["cwd"], str):
            raise MCPConfigError(f"MCP server {name!r} 'cwd' must be a string.")
    else:
        if not isinstance(cfg.get("url"), str) or not cfg["url"]:
            raise MCPConfigError(
                f"MCP server {name!r} (Streamable HTTP) requires a 'url'."
            )
        cfg["headers"] = _str_dict(cfg.get("headers") or {}, "headers")

    timeout = cfg.get("timeout", 30)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 300:
        raise MCPConfigError(f"MCP server {name!r} 'timeout' must be 1-300 seconds.")
    cfg["timeout"] = timeout
    cfg["enabled"] = bool(cfg.get("enabled", True))
    cfg["auto_connect"] = bool(cfg.get("auto_connect", True))
    return cfg


class MCPRegistry:
    """
    Manages the user-approved allow-list of MCP servers and handles connections.
    """

    def __init__(self, config_path: Path | None = None, client: MCPClientManager | None = None):
        self._config_path = Path(config_path) if config_path else _MCP_CONFIG_PATH
        self._client = client or MCPClientManager()
        self.servers: dict[str, dict] = {}
        self._tools_cache: dict[str, list[Tool]] = {}
        # Entries that failed validation: kept in memory (never silently
        # dropped) and preserved on disk by _save_config.
        self._invalid_servers: dict[str, dict[str, Any]] = {}
        # Set when the whole config file could not be parsed at all.
        self._config_load_error: str | None = None
        # Auto-connect (issue 004): background owner tasks, one per
        # eligible server, plus a guard so engine startup (lifespan)
        # never starts them twice.
        self._auto_connect_started = False
        self._auto_connect_tasks: list[asyncio.Task] = []
        self._load_config()

    @property
    def config_load_error(self) -> str | None:
        """Error from the last config-file load, or None if it loaded."""
        return self._config_load_error

    def _load_config(self):
        self.servers = {}
        self._invalid_servers = {}
        self._config_load_error = None
        if not self._config_path.exists():
            return
        try:
            with open(self._config_path, "r") as f:
                raw = json.load(f)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
            # B1: a corrupt file must never be silently destroyed. Keep a
            # .bak, refuse to load that registry, surface the error.
            self._config_load_error = (
                f"MCP config file {self._config_path} is corrupt and was not loaded: {e}"
            )
            try:
                backup = self._config_path.with_name(self._config_path.name + ".bak")
                shutil.copyfile(self._config_path, backup)
                self._config_load_error += f" A backup was saved to {backup}."
            except OSError:
                pass
            logger.error(self._config_load_error)
            return
        if not isinstance(raw, dict):
            self._config_load_error = (
                f"MCP config file {self._config_path} is corrupt: top level must be an object."
            )
            try:
                backup = self._config_path.with_name(self._config_path.name + ".bak")
                shutil.copyfile(self._config_path, backup)
                self._config_load_error += f" A backup was saved to {backup}."
            except OSError:
                pass
            logger.error(self._config_load_error)
            return
        for name, config in (raw or {}).items():
            try:
                self.servers[name] = validate_mcp_server_config(name, config)
            except MCPConfigError as e:
                # Never let one bad entry break loading the rest — and
                # never delete it: it stays in memory marked invalid and
                # is written back out by _save_config.
                logger.warning("Keeping invalid MCP server %r in memory (not loaded): %s", name, e)
                self._invalid_servers[name] = {"config": config, "error": str(e)}

    def _save_config(self):
        if self._config_load_error is not None:
            # The file on disk is corrupt JSON we refused to load. Writing
            # over it now would destroy the user's data.
            raise MCPConfigError(
                "MCP config file is corrupt and was not loaded; fix or delete it "
                "(a .bak backup was saved) before adding servers."
            )
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {}
        for name, entry in self._invalid_servers.items():
            data[name] = entry["config"]
        data.update(self.servers)
        # E3-F04: atomic write — serialise to a temp file in the same
        # directory, then os.replace() it over the live file (the
        # pattern E0-01 added for the plugin registry). A failed save
        # (disk full, crash mid-write) leaves the previous valid file
        # fully intact instead of a truncated fragment that would
        # reload as zero servers and take the whole allow-list with it.
        tmp_path = self._config_path.with_name(self._config_path.name + ".tmp")
        try:
            with open(tmp_path, "w") as f:
                json.dump(data, f, indent=4)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._config_path)
        except BaseException:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def _snapshot_servers(self) -> tuple[dict, dict]:
        """Shallow copies of the mutable config state (E3-F04 rollback)."""
        return dict(self.servers), dict(self._invalid_servers)

    def _save_config_rolled_back(self, snapshot: tuple[dict, dict]) -> None:
        """Save, restoring the in-memory state if the save fails.

        Mutators apply their change in memory and then save; without
        this, a failed save left memory holding a change the disk
        never took, and the two disagreed until a reload silently
        dropped it (E3-F04).
        """
        try:
            self._save_config()
        except BaseException:
            self.servers, self._invalid_servers = snapshot
            raise

    def add_server(self, name: str, config: dict):
        """
        Adds a server to the allow-list. Raises MCPConfigError on invalid config;
        nothing is saved in that case. The failure is audit-logged.
        """
        try:
            normalised = validate_mcp_server_config(name, config)
        except MCPConfigError as e:
            try:
                from aether_engine.audit import AuditLogger

                AuditLogger().log_event(
                    "MCP_SERVER_ADD_REJECTED", {"server_name": name, "error": str(e)}
                )
            except Exception as audit_err:  # noqa: BLE001 — audit logging is best-effort; it must never mask the rejection
                logger.debug(
                    "MCP_SERVER_ADD_REJECTED audit logging failed: %s", audit_err
                )
            raise
        snapshot = self._snapshot_servers()
        self.servers[name] = normalised
        self._save_config_rolled_back(snapshot)

    async def update_server(
        self, name: str, *, auto_connect: bool | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        """Toggle a server's ``auto_connect`` / ``enabled`` flags (issue 004).

        Persists the change. Disabling a connected server disconnects
        it; re-enabling does not reconnect — the user clicks Connect
        (or restarts the engine for auto-connect). Toggling
        ``auto_connect`` takes effect at the next engine start; it does
        not connect or disconnect anything now. Returns the redacted
        config. Raises MCPConfigError for an unknown server or a
        non-boolean flag.
        """
        if name not in self.servers:
            raise MCPConfigError(f"Unknown MCP server '{name}'")
        updates: dict[str, Any] = {}
        if auto_connect is not None:
            if not isinstance(auto_connect, bool):
                raise MCPConfigError(f"MCP server {name!r} 'auto_connect' must be true/false.")
            updates["auto_connect"] = auto_connect
        if enabled is not None:
            if not isinstance(enabled, bool):
                raise MCPConfigError(f"MCP server {name!r} 'enabled' must be true/false.")
            updates["enabled"] = enabled
        if updates:
            snapshot = self._snapshot_servers()
            self.servers[name] = validate_mcp_server_config(
                name, {**self.servers[name], **updates}
            )
            self._save_config_rolled_back(snapshot)
        if enabled is False:
            await self.disconnect_server(name)
        return redact_mcp_config_for_ui(self.servers[name])

    def get_server_status(self, name: str) -> dict[str, Any]:
        """Live status for one server (connected / failed / disabled / ...)."""
        status = self._client.get_status(name)
        config = self.servers.get(name)
        if config is not None and not config.get("enabled", True):
            status = dict(status, status="disabled")
        return status

    # -- auto-connect (issue 004) ---------------------------------------
    def start_auto_connect(self) -> list[str]:
        """Connect every enabled, auto-connect server in the background.

        Non-blocking: returns immediately with the names it attempted.
        One dead server cannot block startup or the others — each runs
        in its own owner task with its own timeout, and failures are
        isolated (see _auto_connect_one). Idempotent: a second call
        while already started returns [] until stop_auto_connect().
        """
        if self._auto_connect_started:
            return []
        self._auto_connect_started = True
        attempted: list[str] = []
        for name, config in self.servers.items():
            if not config.get("enabled", True):
                continue
            if not config.get("auto_connect", True):
                continue
            attempted.append(name)
            task = asyncio.create_task(self._auto_connect_one(name))
            self._auto_connect_tasks.append(task)
        return attempted

    async def _auto_connect_one(self, name: str) -> None:
        try:
            await self.connect_server_owned(name)
        except Exception as e:  # noqa: BLE001 — failure isolation: one dead server must never break startup or the others
            # Failure isolation: a dead server must never break startup
            # or the other servers. Its status is already "failed".
            logger.warning("MCP auto-connect failed for %s: %s", name, e)

    async def stop_auto_connect(self) -> None:
        """Stop auto-connect: close every owner-task session (shutdown).

        Every session is owner-task managed (E3-F03), so this — the
        app lifespan's shutdown call — closes manually connected
        sessions as well as auto-connected ones.
        """
        self._auto_connect_started = False
        self._auto_connect_tasks = []
        try:
            await self._client.shutdown_owned()
        except Exception as e:  # noqa: BLE001 — shutdown is best-effort; a failing close must not escape stop_auto_connect
            logger.warning("MCP auto-connect stop failed: %s", e)

    async def remove_server(self, name: str):
        """
        Removes a server from the allow-list and closes any live session
        inside the removal (S5). Async because closing an MCP session must
        be awaited in the task that opened it: the SDK's anyio cancel
        scope cannot be exited from a different task (doing so cancels
        the owner task). Sync callers use ``remove_server_sync``.
        """
        await self.disconnect_server(name)
        snapshot = self._snapshot_servers()
        removed = False
        if name in self.servers:
            del self.servers[name]
            removed = True
        if name in self._invalid_servers:
            del self._invalid_servers[name]
            removed = True
        if removed:
            self._tools_cache.pop(name, None)
            self._save_config_rolled_back(snapshot)

    def remove_server_sync(self, name: str):
        """Synchronous removal for sync callers (``plugins/installer.py``).

        Removes the config immediately. A live session is closed here
        only when no event loop is running in this thread; when a loop
        IS running and the server is connected, removal is REFUSED
        (issue 011, S6 — completing the first review's S5, which 009
        only half fixed): the session cannot be closed from this task
        (anyio cancel-scope rule — see ``remove_server``), and deleting
        the config anyway would leave a live session and handed-out
        ``Tool`` objects executing. The caller must disconnect first
        (``await remove_server``; the websocket PLUGIN_UNINSTALL
        handler does). Raises MCPConfigError on refusal. Plugin
        servers are never connected at install/rollback time.
        """
        if self._client.is_connected(name):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                asyncio.run(self._client.disconnect(name))
            else:
                raise MCPConfigError(
                    f"MCP server '{name}' is connected; disconnect it "
                    "first (await remove_server) — refusing to remove a "
                    "live session from a sync caller."
                )
        if name in self.servers:
            snapshot = self._snapshot_servers()
            del self.servers[name]
            self._tools_cache.pop(name, None)
            self._save_config_rolled_back(snapshot)
        elif name in self._invalid_servers:
            snapshot = self._snapshot_servers()
            del self._invalid_servers[name]
            self._save_config_rolled_back(snapshot)

    def get_server(self, name: str) -> dict | None:
        return self.servers.get(name)

    def list_servers(self) -> dict[str, dict]:
        return dict(self.servers)

    def list_servers_with_status(self) -> dict[str, dict]:
        """Configs plus live status (connected / failed / disabled), tool count, error.

        Header / env *values* are redacted (S1) — only the names leave the
        engine. Invalid entries are included, marked ``status: "invalid"``.
        """
        out = {}
        for name, config in self.servers.items():
            status = self.get_server_status(name)
            out[name] = {
                **redact_mcp_config_for_ui(config),
                "status": status["status"],
                "tool_count": status.get("tool_count", 0),
                "error": status.get("error"),
            }
        for name, entry in self._invalid_servers.items():
            out[name] = {
                **redact_mcp_config_for_ui(entry["config"]),
                "status": "invalid",
                "tool_count": 0,
                "error": entry["error"],
            }
        return out

    # -- live connections (delegated to the client) ----------------------
    async def _finish_connect(self, name: str, result: dict[str, Any]) -> dict[str, Any]:
        """After a successful connect, refresh the cached tools.

        A session that cannot list its tools is not a connected server
        (S4): disconnect it, mark it failed with the error, and report
        the failure — never leave a session behind with no tools.
        """
        if result["ok"]:
            try:
                await self._refresh_tools(name)
            except Exception as e:  # noqa: BLE001 — any listing failure marks the server failed (S4), whatever the SDK raises
                message = str(e) or e.__class__.__name__
                logger.warning(
                    "MCP connect failed for %s while listing tools: %s", name, message
                )
                await self._client.disconnect(name)
                self._client.set_failed(name, message)
                return {"ok": False, "error": message}
        return result

    async def connect_server(self, name: str) -> dict[str, Any]:
        config = self.servers.get(name)
        if config is None:
            if name in self._invalid_servers:
                return {
                    "ok": False,
                    "error": f"MCP server '{name}' has an invalid config: "
                    f"{self._invalid_servers[name]['error']}",
                }
            return {"ok": False, "error": f"Unknown MCP server '{name}'"}
        return await self._finish_connect(name, await self._client.connect(name, config))

    async def connect_server_owned(self, name: str) -> dict[str, Any]:
        """Like :meth:`connect_server`; the session lives in a
        dedicated owner task (issue 004, auto-connect).

        The owner task opens the session and parks; later disconnects
        from any task are routed through it, so a stdio session is
        actually closed instead of leaking (anyio cancel-scope rule).
        Since E3-F03 :meth:`connect_server` lands in the same owner
        machinery (``MCPClientManager.connect`` delegates to
        ``connect_owned``) — every registered session has exactly one
        owner, however it was connected; this entry point remains for
        the auto-connect call sites.
        """
        config = self.servers.get(name)
        if config is None:
            if name in self._invalid_servers:
                return {
                    "ok": False,
                    "error": f"MCP server '{name}' has an invalid config: "
                    f"{self._invalid_servers[name]['error']}",
                }
            return {"ok": False, "error": f"Unknown MCP server '{name}'"}
        return await self._finish_connect(
            name, await self._client.connect_owned(name, config)
        )

    async def disconnect_server(self, name: str) -> dict[str, Any]:
        self._tools_cache.pop(name, None)
        return await self._client.disconnect(name)

    async def test_connection(self, name: str) -> dict[str, Any]:
        config = self.servers.get(name)
        if config is None:
            return {"ok": False, "error": f"Unknown MCP server '{name}'", "tools": []}
        return await self._client.test_connection(name, config)

    # -- resources (issue 006) ------------------------------------------
    async def list_resources(self, name: str) -> list[dict[str, Any]]:
        """List *name*'s resources. Raises MCPClientError for an unknown
        or disconnected server."""
        if name not in self.servers:
            from aether_engine.mcp.client import MCPClientError

            raise MCPClientError(f"Unknown MCP server '{name}'")
        return await self._client.list_resources(name)

    async def read_resource(self, name: str, uri: str) -> str:
        """Read resource *uri* from *name* (text, capped — see client)."""
        if name not in self.servers:
            from aether_engine.mcp.client import MCPClientError

            raise MCPClientError(f"Unknown MCP server '{name}'")
        return await self._client.read_resource(name, uri)

    # -- prompts (issue 007) --------------------------------------------
    async def list_prompts(self, name: str) -> list[dict[str, Any]]:
        """List *name*'s prompt templates. Raises MCPClientError for an
        unknown or disconnected server."""
        if name not in self.servers:
            from aether_engine.mcp.client import MCPClientError

            raise MCPClientError(f"Unknown MCP server '{name}'")
        return await self._client.list_prompts(name)

    async def get_prompt(
        self, name: str, prompt: str, arguments: dict[str, Any] | None = None
    ) -> list[dict[str, str]]:
        """Run prompt template *prompt* on *name* (messages, capped —
        see client). Arguments are validated, never executed."""
        if name not in self.servers:
            from aether_engine.mcp.client import MCPClientError

            raise MCPClientError(f"Unknown MCP server '{name}'")
        return await self._client.get_prompt(name, prompt, arguments)

    async def _refresh_tools(self, name: str) -> None:
        mcp_tools = await self._client.list_tools(name)
        self._tools_cache[name] = [
            self._convert_tool(name, t) for t in mcp_tools
        ]
        # E3-F01: surface a name collision the moment it appears
        # (connect time), not at first use. Enforcement lives in
        # get_mcp_tools(); this log is the early, loud signal.
        for colliding, sources in self._colliding_tool_names().items():
            logger.error(
                "MCP tool name collision: %s is produced by more than "
                "one server/tool (%s); it will be refused by "
                "get_mcp_tools() until the ambiguity is resolved.",
                colliding, ", ".join(sources),
            )

    def _colliding_tool_names(self) -> dict[str, list[str]]:
        """Prefixed names produced by more than one (server, tool).

        The ``server__tool`` composite is ambiguous whenever two
        pairs map to the same string — server names may contain
        ``__`` themselves (``trusted__docs`` + ``read`` vs
        ``trusted`` + ``docs__read``), and a single server can list
        the same tool twice. Any such name cannot be trusted to
        identify one tool, one risk classification, or one
        execution target (E3-F01).
        """
        sources_by_name: dict[str, list[str]] = {}
        for server_tools in self._tools_cache.values():
            for tool in server_tools:
                sources_by_name.setdefault(tool.name, []).append(tool.source)
        return {
            name: sources
            for name, sources in sources_by_name.items()
            if len(sources) > 1
        }

    def _convert_tool(self, server: str, mcp_tool: Any) -> Tool:
        """One MCP tool -> one Aether Tool: ``server__tool``, EXECUTE, in-process."""
        original_name = mcp_tool.name
        prefixed = f"{server}__{original_name}"
        # The Tool owns its output cap (max_output_chars, default 20000);
        # the closure reads it off the constructed Tool so a changed cap
        # is honoured instead of a hardcoded number.
        holder: dict[str, Any] = {}

        async def _execute(**kwargs):
            # Graceful failure lives here: a dead server returns an error
            # string, it never raises into the graph. The call timeout is
            # the Tool's timeout (S2: 60s), not the server connect timeout.
            result = await execute_mcp_tool_async(
                server, original_name, kwargs, self._client, timeout=MCP_TOOL_TIMEOUT_S
            )
            cap = holder["tool"].max_output_chars
            if len(result) > cap:
                return result[:cap] + f"\n...[output truncated to {cap} chars]"
            return result

        tool = Tool(
            name=prefixed,
            description=mcp_tool.description or f"MCP tool {original_name} from server {server}.",
            input_schema=_valid_input_schema(mcp_tool.inputSchema),
            execute_fn=_execute,
            risk=ToolRisk.EXECUTE,
            timeout_s=MCP_TOOL_TIMEOUT_S,
            source=f"mcp:{server}",
        )
        holder["tool"] = tool
        return tool

    def get_mcp_tools(self, filter_node: str | None = None, *, risk_overrides: dict | None = None) -> list[Tool]:
        """
        Returns converted MCP tools for connected servers (cached at
        connect time). Empty until a server is connected — honestly, not
        scaffolded. ``filter_node`` is accepted for the old call sites;
        all agents get all MCP tools in v1 (PRD Q10), so it is ignored.

        Epic 3, E3-02: ``risk_overrides`` (UserConfig
        ``mcp_tool_risk_overrides``) maps ``server__tool`` to a risk
        value; matching tools are returned as copies with the
        overridden risk so every downstream consumer — policy, plan
        mode, the tool list — sees the user's classification instead
        of the EXECUTE default. Unknown names and invalid risk values
        are ignored with a log line.
        """
        tools: list[Tool] = []
        for server_tools in self._tools_cache.values():
            tools.extend(server_tools)
        # E3-F01: a name produced by more than one (server, tool)
        # pair identifies nothing — offering it would let a risk
        # override classified for one server's tool transfer to
        # another's, and the shared registry would silently keep
        # one tool as the other's stand-in. Refuse every tool
        # carrying a colliding name (loudly); unambiguous tools
        # are unaffected.
        colliding = self._colliding_tool_names()
        if colliding:
            for name, sources in colliding.items():
                logger.error(
                    "Refusing MCP tool %s: the name is produced by "
                    "more than one server/tool (%s), so it cannot "
                    "identify a single tool. Rename a server or a "
                    "tool to resolve the ambiguity.",
                    name, ", ".join(sources),
                )
            tools = [t for t in tools if t.name not in colliding]
        if not risk_overrides:
            return tools
        import dataclasses

        overridden: list[Tool] = []
        for tool in tools:
            raw = risk_overrides.get(tool.name)
            if raw is None:
                overridden.append(tool)
                continue
            try:
                risk = ToolRisk(str(raw))
            except ValueError:
                logger.warning("Ignoring invalid MCP risk override for %s: %r", tool.name, raw)
                overridden.append(tool)
                continue
            overridden.append(dataclasses.replace(tool, risk=risk) if risk != tool.risk else tool)
        return overridden


GLOBAL_MCP_REGISTRY = MCPRegistry()
