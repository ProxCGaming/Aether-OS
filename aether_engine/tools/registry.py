import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

logger = logging.getLogger("aether_engine.tools.registry")


class ToolRisk(Enum):
    READ_ONLY = "read_only"
    WRITE = "write"
    DESTRUCTIVE = "destructive"
    EXECUTE = "execute"


class ToolError(Exception):
    """Base exception for tool registry operations."""


class ToolNotFoundError(ToolError):
    """Raised when an unregistered tool name is requested or executed."""


class ToolExecutionError(ToolError):
    """Raised when a tool execution fails."""


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})
    execute_fn: Callable[..., Any] = field(default=lambda **kw: None)
    risk: ToolRisk = field(default=ToolRisk.EXECUTE) # default to fail closed
    timeout_s: int = field(default=60)
    max_output_chars: int = field(default=20000)
    source: str = field(default="builtin")
    # Epic 2: in-process tools that enforce path containment themselves
    # opt in to receiving the run's workspace_roots as a keyword the
    # model never sees (it is not in the input schema).
    wants_workspace_roots: bool = field(default=False)

    def to_definition(self) -> dict[str, Any]:
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


def create_file_tools() -> list[Tool]:
    def _write_file(path: str, content: str = "") -> str:
        from pathlib import Path
        # Epic 2, E2-04 (decision D3): missing parents inside the
        # workspace are created implicitly — one approval covers the
        # write; containment is pre-flight's job, as for every tool.
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"Wrote file {path}"

    def _create_directory(path: str) -> str:
        from pathlib import Path
        Path(path).mkdir(parents=True, exist_ok=True)
        return f"Created directory {path}"

    def _edit_file(path: str, expected_hash: str = "", edits: list | None = None) -> str:
        # Epic 2, E2-03: the graph path executes this in the worker; this
        # in-process implementation serves direct registry callers and
        # shares the exact aether_common contract with the worker, so
        # behaviour cannot drift between the two.
        from pathlib import Path

        from aether_common.text_edit import (
            TextEditError,
            apply_text_edits,
            atomic_write_bytes,
            content_hash,
        )

        target = Path(path)
        if not target.is_file():
            raise ToolExecutionError(f"edit_file failed: '{path}' does not exist or is not a file.")
        data = target.read_bytes()
        actual = content_hash(data)
        if expected_hash and actual != expected_hash:
            raise ToolExecutionError(
                f"edit_file failed: '{path}' changed since it was read "
                f"(expected sha256 {expected_hash[:12]}…, found {actual[:12]}…). Re-read the file."
            )
        try:
            new_text = apply_text_edits(data.decode("utf-8"), edits or [])
        except (TextEditError, UnicodeDecodeError) as exc:
            raise ToolExecutionError(f"edit_file failed for '{path}': {exc}") from exc
        atomic_write_bytes(path, new_text.encode("utf-8"))
        return f"Edited file {path} ({len(edits or [])} edit(s) applied)"

    def _delete_file(path: str) -> str:
        from pathlib import Path
        Path(path).unlink(missing_ok=True)
        return f"Deleted file {path}"

    def _execute_shell(command: str) -> str:
        import subprocess
        completed = subprocess.run(command, shell=True, capture_output=True, text=True, check=False)
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
            name="edit_file",
            description=(
                "Edit an existing file with anchored replacements. Each edit's old_text must "
                "occur exactly once in the file (include surrounding context to make it unique); "
                "the edit fails loudly if an anchor is missing or ambiguous, and nothing is "
                "written unless every edit applies. Pass expected_hash from read_file's header: "
                "if the file changed since you read it, the edit is refused. Prefer this over "
                "write_file for changes to existing files."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "expected_hash": {
                        "type": "string",
                        "description": "The sha256 from read_file's header for this file.",
                    },
                    "edits": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "old_text": {"type": "string"},
                                "new_text": {"type": "string"},
                            },
                            "required": ["old_text", "new_text"],
                        },
                    },
                },
                "required": ["path", "edits"],
            },
            execute_fn=_edit_file,
            risk=ToolRisk.WRITE,
        ),
        Tool(
            name="create_directory",
            description="Create a directory (and any missing parents) inside the workspace.",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            execute_fn=_create_directory,
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


def create_web_tools() -> list[Tool]:
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
        except Exception as e:  # noqa: BLE001 — ddgs raises arbitrary network/HTTP errors; the failure is surfaced to the caller as the tool result
            return f"Search failed: {e}"

    from aether_engine.tools.web_fetch import create_fetch_url_tool

    return [
        Tool(
            name="search_web",
            description=(
                "Search the web for information using DuckDuckGo. Returns search "
                "results (titles, URLs, snippets) — use fetch_url to read a "
                "result page in full."
            ),
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
        ),
        create_fetch_url_tool(),
    ]


def build_tool_registry(
    mcp_tools: list[Tool] | None = None,
    *,
    include_file_tools: bool = True,
    include_web_tools: bool = True,
) -> "ToolRegistry":
    """One place that builds the full registry (PRD Q3).

    Built-ins + MCP tools, in one place, so the three registry
    construction sites in ``routes/websocket.py`` (task start, approval
    resume, tool list) can no longer drift apart. There is deliberately
    no ``user_config`` parameter: in v1 all agents get all MCP tools
    (PRD Q10), so per-agent filtering would be a dead parameter.
    ``include_file_tools`` exists because task start only offers file
    tools for coding tasks.
    """
    registry = ToolRegistry()
    registry.register(create_current_time_tool())
    from aether_engine.tools.filesystem import filesystem_read_tools
    from aether_engine.tools.scratchpad import create_scratchpad_tool

    registry.register(create_scratchpad_tool())
    for tool in filesystem_read_tools():
        registry.register(tool)
    if include_file_tools:
        for tool in create_file_tools():
            registry.register(tool)
    if include_web_tools:
        for tool in create_web_tools():
            registry.register(tool)
    for tool in mcp_tools or []:
        registry.register(tool)
    return registry


def _call_kwargs(tool: Tool, call_args: dict[str, Any], workspace_roots: list | None) -> dict[str, Any]:
    """Model args plus engine-injected roots for tools that opt in."""
    if tool.wants_workspace_roots:
        return {**call_args, "workspace_roots": workspace_roots}
    return call_args


class ToolRegistry:
    """Registry holding tool definitions and handling on-demand asynchronous tool executions."""

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def remove_tool(self, name: str) -> None:
        if name in self._tools:
            del self._tools[name]

    def get_tool(self, name: str) -> Tool:
        if name not in self._tools:
            raise ToolNotFoundError(f"Tool '{name}' is not registered.")
        return self._tools[name]

    def get_tools(self) -> list[Tool]:
        return list(self._tools.values())

    def get_definitions(self) -> list[dict[str, Any]]:
        return [tool.to_definition() for tool in self._tools.values()]

    def read_only_names(self) -> set[str]:
        """Names of tools whose risk class is READ_ONLY (Epic 1, E1-09).

        Plan mode filters specialist tool lists to exactly this set:
        anything that can write, execute or destroy — including MCP
        tools, which default to EXECUTE — is absent, not just refused.
        """
        return {tool.name for tool in self._tools.values() if tool.risk == ToolRisk.READ_ONLY}

    async def execute(
        self,
        name: str,
        args: dict[str, Any] | None = None,
        *,
        workspace_roots: list | None = None,
        task_class: str = "standard",
        policies: dict[str, str] | None = None,
        approved: bool = False,
        audit_logger: Any = None,
        path_rules: list | None = None,
        shell_extras: dict | None = None,
        approval_mode: str = "ask",
        approval_cache: Any = None,
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

        # ── Step 1: policy (ADR 0021; arg/path-aware since E2-06) ─────
        policy = resolve_policy_by_name(name, policies or {}, self, args=call_args, path_rules=path_rules, shell_extras=shell_extras, approval_mode=approval_mode)
        # E2-09: a stored approval grant turns this call's "ask" into
        # "allow" — never a "deny", never a floored path (the cache
        # itself refuses those), and the hit is audited with its grant.
        if policy == "ask" and not approved and approval_cache is not None:
            grant = approval_cache.covering_grant(risk=tool.risk.value, args=call_args)
            if grant is not None:
                policy = "allow"
                try:
                    log.log_event("APPROVAL_CACHE_HIT", {"tool_name": name, "grant_id": grant["id"], "prefix": grant["prefix"]})
                except Exception:  # best-effort audit, as with TOOL_EXECUTED below
                    logger.debug("Failed to write APPROVAL_CACHE_HIT audit event", exc_info=True)
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
            if tool.source.startswith("mcp:"):
                # MCP tools run in-process in the engine (PRD Q4): the
                # worker hardcodes 3 builtin tools and cannot hold MCP
                # sessions, so routing an MCP tool there would silently
                # no-op. The MCP server itself is already isolated.
                if inspect.iscoroutinefunction(tool.execute_fn):
                    result = await tool.execute_fn(**call_args)
                else:
                    result = tool.execute_fn(**call_args)
                if isinstance(result, str) and len(result) > tool.max_output_chars:
                    result = (
                        result[: tool.max_output_chars]
                        + f"\n...[output truncated to {tool.max_output_chars} chars]"
                    )
                return result
            if tool.risk != ToolRisk.READ_ONLY:
                from aether_engine.workers.sandbox import execute_tool_in_worker
                result = await execute_tool_in_worker(
                    tool_name=name,
                    args=call_args,
                    task_class=task_class,
                    audit_logger=log,
                )
            elif inspect.iscoroutinefunction(tool.execute_fn):
                result = await tool.execute_fn(**_call_kwargs(tool, call_args, workspace_roots))
            else:
                result = tool.execute_fn(**_call_kwargs(tool, call_args, workspace_roots))
        except ToolExecutionError:
            raise
        except Exception as e:
            raise ToolExecutionError(f"Error executing tool '{name}': {e}") from e

        # Phase 10.2, C2: the registry previously audited only denials
        # (TOOL_BLOCKED_BY_POLICY above). A successful destructive /
        # execute / write tool therefore left no registry-level record
        # that it had run — the only trace was the worker layer's
        # WORKER_EXECUTION_COMPLETED, which records the process, not the
        # registry's allow-decision, and carries no argument info. Log
        # TOOL_EXECUTED for every successful non-READ_ONLY execution, with
        # argument *names* only — never values, never the full payload
        # (audit.py redacts sensitive/payload keys as a second line of
        # defence). Best-effort: a broken audit log must not turn a
        # successful execution into an error. READ_ONLY tools are not
        # logged: they change nothing, and logging them would flood the
        # trail (cf. SECRET_LOADED volume for routine key loads).
        if tool.risk != ToolRisk.READ_ONLY:
            try:
                log.log_event("TOOL_EXECUTED", {
                    "tool_name": name,
                    "risk": tool.risk.value,
                    "arg_keys": sorted(str(k) for k in call_args),
                })
            except Exception:  # best-effort audit write: a logging failure must never fail a successful execution
                logger.debug("Failed to write TOOL_EXECUTED audit event", exc_info=True)
        return result
