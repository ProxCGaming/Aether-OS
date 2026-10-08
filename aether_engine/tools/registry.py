import inspect
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


class ToolRisk(Enum):
    READ_ONLY = "read_only"
    WRITE = "write"
    DESTRUCTIVE = "destructive"
    EXECUTE = "execute"


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
    risk: ToolRisk = field(default=ToolRisk.EXECUTE) # default to fail closed
    timeout_s: int = field(default=60)
    max_output_chars: int = field(default=20000)
    source: str = field(default="builtin")

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
        risk=ToolRisk.READ_ONLY,
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
            risk=ToolRisk.WRITE,
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
            risk=ToolRisk.DESTRUCTIVE,
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
            risk=ToolRisk.EXECUTE,
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
            risk=ToolRisk.READ_ONLY,
        )
    ]


class ToolRegistry:
    """Registry holding tool definitions and handling on-demand asynchronous tool executions."""

    def __init__(self):
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def remove_tool(self, name: str) -> None:
        if name in self._tools:
            del self._tools[name]

    def get_tool(self, name: str) -> Tool:
        if name not in self._tools:
            raise ToolNotFoundError(f"Tool '{name}' is not registered.")
        return self._tools[name]

    def get_definitions(self) -> List[Dict[str, Any]]:
        return [tool.to_definition() for tool in self._tools.values()]

    async def execute(
        self,
        name: str,
        args: Optional[Dict[str, Any]] = None,
        *,
        workspace_roots: Optional[list] = None,
        task_class: str = "standard",
        policies: Optional[Dict[str, str]] = None,
        approved: bool = False,
        audit_logger: Any = None,
    ) -> Any:
        """Execute a tool through the full trust pipeline (Phase 9, A2).

        Pipeline (same as ``execute_tool_node``):
          1. Policy check (ADR 0021) — ``deny`` always raises; ``ask`` raises
             unless ``approved=True`` (fail-closed, ADR 0009).
          2. Pre-flight validation (ADR 0009 §2) — failures raise.
          3. Sandbox routing (ADR 0010) — non-READ_ONLY tools run in the
             ``aether_worker`` subprocess via ``execute_tool_in_worker``;
             READ_ONLY tools run in-process.

        There is intentionally no silent direct-execution path anymore:
        ``ToolRegistry.execute`` used to call ``tool.execute_fn`` with zero
        policy, validation, or sandboxing, which made it a bypass around the
        whole trust architecture for any future caller.
        """
        from aether_engine.audit import AuditLogger
        from aether_engine.tools.policy import resolve_policy_by_name
        from aether_engine.validation.pre_flight import validate_tool_request

        tool = self.get_tool(name)  # raises ToolNotFoundError — preserved
        call_args = args or {}
        log = audit_logger or AuditLogger()

        # ── Step 1: policy (ADR 0021) ──────────────────────────────────
        policy = resolve_policy_by_name(name, policies or {}, self)
        if policy == "deny":
            log.log_event("TOOL_BLOCKED_BY_POLICY", {"tool_name": name, "reason": "User Deny", "via": "registry.execute"})
            raise ToolExecutionError(f"Tool '{name}' is blocked by policy (deny).")
        if policy == "ask" and not approved:
            log.log_event("TOOL_BLOCKED_BY_POLICY", {"tool_name": name, "reason": "Bypassed Approval", "via": "registry.execute"})
            raise ToolExecutionError(
                f"Tool '{name}' requires approval (policy=ask). "
                "Pass approved=True only after explicit user consent."
            )

        # ── Step 2: pre-flight validation (ADR 0009 §2, fail-closed) ────
        validation = validate_tool_request(name, call_args, workspace_roots=workspace_roots)
        if not validation.is_valid:
            raise ToolExecutionError(f"Pre-flight validation failed for '{name}': {validation.error}")

        # ── Step 3: sandbox routing (ADR 0010) ─────────────────────────
        try:
            if tool.risk != ToolRisk.READ_ONLY:
                from aether_engine.workers.sandbox import execute_tool_in_worker
                return await execute_tool_in_worker(
                    tool_name=name,
                    args=call_args,
                    task_class=task_class,
                    audit_logger=log,
                )
            if inspect.iscoroutinefunction(tool.execute_fn):
                return await tool.execute_fn(**call_args)
            return tool.execute_fn(**call_args)
        except ToolExecutionError:
            raise
        except Exception as e:
            raise ToolExecutionError(f"Error executing tool '{name}': {e}") from e
