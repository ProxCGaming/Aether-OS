import inspect
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional


class ToolError(Exception):
    """Base exception for tool registry operations."""
    pass


class ToolNotFoundError(ToolError):
    """Raised when an unregistered tool name is requested or executed."""
    pass


class ToolExecutionError(ToolError):
    """Raised when a tool execution fails."""
    pass


@dataclass
class Tool:
    name: str
    description: str
    input_schema: Dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})
    execute_fn: Callable[..., Any] = field(default=lambda **kw: None)

    def to_definition(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            }
        }


def _get_current_time_fn(**_kwargs) -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def create_current_time_tool() -> Tool:
    return Tool(
        name="get_current_time",
        description="Get the current local date and time on the user's system.",
        input_schema={
            "type": "object",
            "properties": {},
        },
        execute_fn=_get_current_time_fn,
    )


def create_file_tools() -> List[Tool]:
    def _write_file(path: str, content: str = "") -> str:
        from pathlib import Path
        Path(path).write_text(content, encoding="utf-8")
        return f"Wrote file {path}"

    def _delete_file(path: str) -> str:
        from pathlib import Path
        Path(path).unlink(missing_ok=True)
        return f"Deleted file {path}"

    def _execute_shell(command: str) -> str:
        import subprocess
        completed = subprocess.run(command, shell=True, capture_output=True, text=True)
        return f"exit={completed.returncode}\nstdout={completed.stdout}\nstderr={completed.stderr}"

    return [
        Tool(
            name="write_file",
            description="Create or overwrite a file on disk with supplied content.",
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path"],
            },
            execute_fn=_write_file,
        ),
        Tool(
            name="delete_file",
            description="Delete a file from disk if it exists.",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            execute_fn=_delete_file,
        ),
        Tool(
            name="execute_shell",
            description="Run a local shell command.",
            input_schema={
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
            execute_fn=_execute_shell,
        ),
    ]


def create_web_tools() -> List[Tool]:
    def _search_web(query: str, max_results: int = 5) -> str:
        from ddgs import DDGS
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=max_results))
                if not results:
                    return "No results found."
                
                output = []
                for r in results:
                    output.append(f"Title: {r.get('title')}\nURL: {r.get('href')}\nSnippet: {r.get('body')}\n")
                return "\n".join(output)
        except Exception as e:
            return f"Search failed: {e}"

    return [
        Tool(
            name="search_web",
            description="Search the web for information using DuckDuckGo. Returns search results.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query"},
                    "max_results": {"type": "integer", "description": "Maximum number of results to return (default 5)"}
                },
                "required": ["query"],
            },
            execute_fn=_search_web,
        )
    ]


class ToolRegistry:
    """Registry holding tool definitions and handling on-demand asynchronous tool executions."""

    def __init__(self):
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get_tool(self, name: str) -> Tool:
        if name not in self._tools:
            raise ToolNotFoundError(f"Tool '{name}' is not registered.")
        return self._tools[name]

    def get_definitions(self) -> List[Dict[str, Any]]:
        return [tool.to_definition() for tool in self._tools.values()]

    async def execute(self, name: str, args: Optional[Dict[str, Any]] = None) -> Any:
        tool = self.get_tool(name)
        call_args = args or {}
        try:
            if inspect.iscoroutinefunction(tool.execute_fn):
                return await tool.execute_fn(**call_args)
            return tool.execute_fn(**call_args)
        except Exception as e:
            raise ToolExecutionError(f"Error executing tool '{name}': {e}") from e
