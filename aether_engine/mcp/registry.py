import json
from pathlib import Path
from typing import List, Dict, Optional

_MCP_CONFIG_PATH = Path.home() / ".aether" / "mcp_servers.json"

class MCPRegistry:
    """
    Manages the user-approved allow-list of MCP servers and handles connections.
    """
    def __init__(self):
        self.servers = {}
        self._load_config()

    def _load_config(self):
        if _MCP_CONFIG_PATH.exists():
            with open(_MCP_CONFIG_PATH, "r") as f:
                self.servers = json.load(f)
        else:
            self.servers = {}

    def _save_config(self):
        _MCP_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_MCP_CONFIG_PATH, "w") as f:
            json.dump(self.servers, f, indent=4)

    def add_server(self, name: str, config: dict):
        """
        Adds a server to the allow-list. config should contain URL/path/command.
        """
        self.servers[name] = config
        self._save_config()

    def remove_server(self, name: str):
        """
        Removes a server from the allow-list.
        """
        if name in self.servers:
            del self.servers[name]
            self._save_config()

    def get_server(self, name: str) -> Optional[dict]:
        return self.servers.get(name)

    def list_servers(self) -> Dict[str, dict]:
        return self.servers

    def get_mcp_tools(self, filter_node: str = None) -> List[dict]:
        """
        Returns loaded MCP tools. 
        In a real implementation, this would use langchain-mcp to query the active servers
        and convert their exposed functions into OpenAI-compatible tool schemas.
        """
        # Scaffolded for phase 5
        tools = []
        return tools

GLOBAL_MCP_REGISTRY = MCPRegistry()
