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
import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from aether_engine.mcp.client import MCPClientManager
from aether_engine.mcp.fallback import execute_mcp_tool_async
from aether_engine.tools.registry import Tool, ToolRisk

logger = logging.getLogger("aether_engine.mcp.registry")

_MCP_CONFIG_PATH = Path.home() / ".aether" / "mcp_servers.json"
_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,100}$")


class MCPConfigError(ValueError):
    """Raised when an MCP server name or config fails validation."""


def _str_dict(value: Any, field: str) -> Dict[str, str]:
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
    format); ``headers``/``env`` are string dicts; timeout 0–300s,
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
        raise MCPConfigError(f"MCP server {name!r} 'timeout' must be 0-300 seconds.")
    cfg["timeout"] = timeout
    cfg["enabled"] = bool(cfg.get("enabled", True))
    cfg["auto_connect"] = bool(cfg.get("auto_connect", True))
    return cfg


class MCPRegistry:
    """
    Manages the user-approved allow-list of MCP servers and handles connections.
    """

    def __init__(self, config_path: Optional[Path] = None, client: Optional[MCPClientManager] = None):
        self._config_path = Path(config_path) if config_path else _MCP_CONFIG_PATH
        self._client = client or MCPClientManager()
        self.servers: Dict[str, dict] = {}
        self._tools_cache: Dict[str, List[Tool]] = {}
        self._load_config()

    def _load_config(self):
        if not self._config_path.exists():
            self.servers = {}
            return
        with open(self._config_path, "r") as f:
            raw = json.load(f)
        self.servers = {}
        for name, config in (raw or {}).items():
            try:
                self.servers[name] = validate_mcp_server_config(name, config)
            except MCPConfigError as e:
                # Never let one bad entry break loading the rest.
                logger.warning("Skipping invalid MCP server %r: %s", name, e)

    def _save_config(self):
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._config_path, "w") as f:
            json.dump(self.servers, f, indent=4)

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
            except Exception:
                pass
            raise
        self.servers[name] = normalised
        self._save_config()

    def remove_server(self, name: str):
        """
        Removes a server from the allow-list. Callers should disconnect it
        first (``disconnect_server``) — removal alone does not close a live
        session.
        """
        if name in self.servers:
            del self.servers[name]
            self._tools_cache.pop(name, None)
            self._save_config()

    def get_server(self, name: str) -> Optional[dict]:
        return self.servers.get(name)

    def list_servers(self) -> Dict[str, dict]:
        return dict(self.servers)

    def list_servers_with_status(self) -> Dict[str, dict]:
        """Configs plus live status (connected / failed / disabled), tool count, error."""
        out = {}
        for name, config in self.servers.items():
            status = self._client.get_status(name)
            if not config.get("enabled", True):
                status = dict(status, status="disabled")
            out[name] = {
                **config,
                "status": status["status"],
                "tool_count": status.get("tool_count", 0),
                "error": status.get("error"),
            }
        return out

    # -- live connections (delegated to the client) ----------------------
    async def connect_server(self, name: str) -> Dict[str, Any]:
        config = self.servers.get(name)
        if config is None:
            return {"ok": False, "error": f"Unknown MCP server '{name}'"}
        result = await self._client.connect(name, config)
        if result["ok"]:
            await self._refresh_tools(name)
        return result

    async def disconnect_server(self, name: str) -> Dict[str, Any]:
        self._tools_cache.pop(name, None)
        return await self._client.disconnect(name)

    async def test_connection(self, name: str) -> Dict[str, Any]:
        config = self.servers.get(name)
        if config is None:
            return {"ok": False, "error": f"Unknown MCP server '{name}'", "tools": []}
        return await self._client.test_connection(name, config)

    async def _refresh_tools(self, name: str) -> None:
        mcp_tools = await self._client.list_tools(name)
        self._tools_cache[name] = [
            self._convert_tool(name, t) for t in mcp_tools
        ]

    def _convert_tool(self, server: str, mcp_tool: Any) -> Tool:
        """One MCP tool -> one Aether Tool: ``server__tool``, EXECUTE, in-process."""
        original_name = mcp_tool.name
        prefixed = f"{server}__{original_name}"

        async def _execute(**kwargs):
            # Graceful failure lives here: a dead server returns an error
            # string, it never raises into the graph.
            result = await execute_mcp_tool_async(server, original_name, kwargs, self._client)
            if len(result) > 20000:
                return result[:20000] + "\n...[output truncated to 20000 chars]"
            return result

        return Tool(
            name=prefixed,
            description=mcp_tool.description or f"MCP tool {original_name} from server {server}.",
            input_schema=mcp_tool.inputSchema or {"type": "object", "properties": {}},
            execute_fn=_execute,
            risk=ToolRisk.EXECUTE,
            timeout_s=60,
            source=f"mcp:{server}",
        )

    def get_mcp_tools(self, filter_node: str = None) -> List[Tool]:
        """
        Returns converted MCP tools for connected servers (cached at
        connect time). Empty until a server is connected — honestly, not
        scaffolded. ``filter_node`` is accepted for the old call sites;
        all agents get all MCP tools in v1 (PRD Q10), so it is ignored.
        """
        tools: List[Tool] = []
        for server_tools in self._tools_cache.values():
            tools.extend(server_tools)
        return tools


GLOBAL_MCP_REGISTRY = MCPRegistry()
