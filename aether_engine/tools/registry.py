import asyncio
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
    # E2-F03: tools whose traversal discovers paths policy never judged
    # (recursive search) opt in to the run's path_rules the same way,
    # so they can apply per-descendant Deny rules before opening files.
    wants_path_rules: bool = field(default=False)

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

    def _edit_file(path: str, expected_hash: str | None = None, edits: list | None = None) -> str:
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
        # E2-F01: a SUPPLIED hash is checked exactly — including an
        # empty string, which can never match and so refuses like any
        # stale hash. Only an absent/None hash means "no hash", and
        # only that skips the check (the normal no-hash flow).
        if expected_hash is not None and actual != expected_hash:
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
        # Epic 2, E2-10: dispatch through the provider seam (ddgs by
        # default, or the user's configured keyed provider).
        from aether_engine.tools.web_search import (
            current_search_config,
            search_web_dispatch,
        )

        provider, fallback = current_search_config()
        return search_web_dispatch(
            query, max_results=max_results, provider=provider, fallback_to_ddgs=fallback
        )

    from aether_engine.tools.web_fetch import create_fetch_url_tool

    return [
        Tool(
            name="search_web",
            description=(
                "Search the web for information. Returns search "
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
    from aether_engine.tools.codebase import codebase_tools
    from aether_engine.tools.filesystem import filesystem_read_tools
    from aether_engine.tools.scratchpad import create_scratchpad_tool

    registry.register(create_scratchpad_tool())
    for tool in filesystem_read_tools():
        registry.register(tool)
    # Epic 8, E8-3.2: deliberate prose retrieval over the workspace
    # index (rag/). READ_ONLY and self-limiting — with no index it
    # answers "nothing indexed" instead of failing.
    from aether_engine.tools.workspace_search import create_workspace_search_tool

    registry.register(create_workspace_search_tool())
    # Epic 8, E8-4.3: content-based recall over the media index
    # (media/). READ_ONLY and self-limiting — with no analysed
    # media it answers "no matches" instead of failing.
    from aether_engine.tools.media_recall import create_media_recall_tool

    registry.register(create_media_recall_tool())
    if include_file_tools:
        for tool in create_file_tools():
            registry.register(tool)
        for tool in codebase_tools():
            registry.register(tool)
    if include_web_tools:
        for tool in create_web_tools():
            registry.register(tool)
    # Epic 8, E8-6.1: agent-browser tools. Registered like every
    # other built-in — risk levels attached, the policy pipeline
    # applies unchanged; the tools themselves fail closed when
    # Playwright is unavailable (see browser/tools.py).
    from aether_engine.browser.tools import create_browser_tools

    for tool in create_browser_tools():
        registry.register(tool)
    for tool in mcp_tools or []:
        registry.register(tool)
    return registry


def _call_kwargs(
    tool: Tool,
    call_args: dict[str, Any],
    workspace_roots: list | None,
    path_rules: list | None = None,
) -> dict[str, Any]:
    """Model args plus engine-injected context for tools that opt in."""
    kwargs = dict(call_args)
    if tool.wants_workspace_roots:
        kwargs["workspace_roots"] = workspace_roots
    if tool.wants_path_rules:
        kwargs["path_rules"] = path_rules
    return kwargs


async def _run_sync_tool(tool: Tool, kwargs: dict[str, Any]) -> Any:
    """Run a synchronous ``execute_fn`` off the event loop (E2-F06).

    Native dispatch used to call sync functions inline, so a slow
    scan or a pathological regex stalled every connection and task
    the loop serves, and ``Tool.timeout_s`` was consumed nowhere.
    The codebase's established offload is ``asyncio.to_thread``
    (executor, WS routes, media analyzer); the await is bounded by
    the tool's own ``timeout_s``. A thread cannot be killed, so on
    timeout the caller is released at the deadline and the late
    result is discarded — the same semantics as every other
    ``to_thread`` user here. Synchronous tool functions in this
    codebase are stateless over their arguments (filesystem scans,
    web-search dispatch), so running them on a worker thread is
    safe; coroutine functions never come through here — they are
    awaited directly, as before.
    """
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(tool.execute_fn, **kwargs),
            timeout=tool.timeout_s,
        )
    except TimeoutError as exc:
        raise ToolExecutionError(
            f"Tool '{tool.name}' timed out after {tool.timeout_s}s."
        ) from exc


class ToolRegistry:
    """Registry holding tool definitions and handling on-demand asynchronous tool executions."""

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        existing = self._tools.get(tool.name)
        if existing is not None and existing.source != tool.source:
            # E3-F01: a name is an identity. Silently replacing one
            # owner's tool with another's let a call execute the
            # wrong server's tool under the first tool's risk
            # classification. Same-source re-registration (a
            # refresh of the same owner's tools) stays legal.
            raise ValueError(
                f"Tool name {tool.name!r} is already registered by "
                f"{existing.source!r}; refusing to replace it with "
                f"a tool from {tool.source!r}."
            )
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
        task_classes: dict[str, dict[str, int]] | None = None,
        policies: dict[str, str] | None = None,
        approved: bool = False,
        audit_logger: Any = None,
        path_rules: list | None = None,
        shell_extras: dict | None = None,
        approval_mode: str = "ask",
        approval_cache: Any = None,
        final_args_gate: Callable | None = None,
    ) -> Any:
        """Execute a tool through the full trust pipeline (Phase 9, A2).

        Pipeline (same as ``execute_tool_node``):
          1. Policy check (ADR 0021) — ``deny`` always raises; ``ask`` raises
             unless ``approved=True`` (fail-closed, ADR 0009).
          2. Pre-flight validation (ADR 0009 §2) — failures raise.
          2.5. tool_call hooks (E7-01) — may rewrite args or block.
          2.55. Post-hook re-check (S2-M1) — when the hooks changed the
             arguments, Steps 1–2 re-run on the rewritten args: what
             executes is what was vetted, not what was proposed.
          2.6. Caller-supplied final-args gate (M03) — vets the exact
             arguments that will execute, post-edit and post-hook.
          3. Sandbox routing (ADR 0010) — non-READ_ONLY tools run in the
             ``aether_worker`` subprocess via ``execute_tool_in_worker``
             under the caller's configured resource profiles
             (``task_classes``, E2-F05); READ_ONLY tools run in-process,
             with synchronous functions offloaded off the event loop
             under their ``timeout_s`` (E2-F06).

        There is intentionally no silent direct-execution path anymore:
        ``ToolRegistry.execute`` used to call ``tool.execute_fn`` with zero
        policy, validation, or sandboxing, which made it a bypass around the
        whole trust architecture for any future caller.
        """
        from aether_engine.audit import AuditLogger
        from aether_engine.tools.policy import resolve_policy_by_name, shell_denial_rule
        from aether_engine.validation.pre_flight import validate_tool_request

        tool = self.get_tool(name)  # raises ToolNotFoundError — preserved
        call_args = args or {}
        log = audit_logger or AuditLogger()

        # Steps 1–2 live in two closures so the post-hook re-check
        # (Step 2.55, S2-M1) re-runs EXACTLY the same enforcement —
        # same verdicts, same audit events, same error shapes — on
        # the rewritten arguments, never a divergent copy of it.

        # ── Step 1: policy (ADR 0021; arg/path-aware since E2-06) ─────
        # F-03: policy resolution is part of the validation path (it
        # evaluates the shell safety rules for execute_shell), so an
        # internal error there must fail closed exactly like a
        # pre-flight error in Step 2 — denied, audited, and surfaced
        # as ToolExecutionError — never a raw exception escaping
        # execute() with no audit record of the refusal.
        def _enforce_policy(check_args: dict[str, Any]) -> None:
            try:
                policy = resolve_policy_by_name(name, policies or {}, self, args=check_args, path_rules=path_rules, shell_extras=shell_extras, approval_mode=approval_mode)
            except Exception as e:
                try:
                    log.log_event("TOOL_VALIDATION_FAILED", {"tool_name": name, "error": f"policy check failed internally: {e}", "via": "registry.execute"})
                except Exception:  # best-effort audit write
                    logger.debug("Failed to write TOOL_VALIDATION_FAILED audit event", exc_info=True)
                raise ToolExecutionError(f"Pre-flight validation failed for '{name}': policy check failed internally: {e}") from e
            # E2-09: a stored approval grant turns this call's "ask" into
            # "allow" — never a "deny", never a floored path (the cache
            # itself refuses those), and the hit is audited with its grant.
            if policy == "ask" and not approved and approval_cache is not None:
                try:
                    grant = approval_cache.covering_grant(risk=tool.risk.value, args=check_args)
                except Exception:
                    # PF-5: a cache failure must degrade to the normal
                    # ask, never break execution — the same guard
                    # tool_router (langgraph/graph.py) wraps around
                    # its covering_grant check. The grant is treated
                    # as absent, so the ordinary policy flow below
                    # decides (ask → approval refusal, fail-closed).
                    logger.debug("Approval-cache check failed in registry.execute", exc_info=True)
                    grant = None
                if grant is not None:
                    policy = "allow"
                    try:
                        log.log_event("APPROVAL_CACHE_HIT", {"tool_name": name, "grant_id": grant["id"], "prefix": grant["prefix"]})
                    except Exception:  # best-effort audit, as with TOOL_EXECUTED below
                        logger.debug("Failed to write APPROVAL_CACHE_HIT audit event", exc_info=True)
            if policy == "deny":
                payload = {"tool_name": name, "reason": "User Deny", "via": "registry.execute"}
                # E2-F13: when a shell safety rule produced the denial,
                # name the matched rule in the audit trail (the PRD
                # requires logging the matched pattern). A plain config
                # Deny matches no rule and keeps the bare payload.
                rule = shell_denial_rule(tool, check_args, shell_extras)
                if rule is not None:
                    payload["rule"] = rule
                log.log_event("TOOL_BLOCKED_BY_POLICY", payload)
                raise ToolExecutionError(f"Tool '{name}' is blocked by policy (deny).")
            if policy == "ask" and not approved:
                log.log_event("TOOL_BLOCKED_BY_POLICY", {"tool_name": name, "reason": "Bypassed Approval", "via": "registry.execute"})
                raise ToolExecutionError(
                    f"Tool '{name}' requires approval (policy=ask). "
                    "Pass approved=True only after explicit user consent."
                )

        # ── Step 2: pre-flight validation (ADR 0009 §2, fail-closed) ────
        # F-03: an internal error inside validation itself is a denial,
        # not an unwrapped escape: audit it as TOOL_VALIDATION_FAILED
        # and surface the same ToolExecutionError a validation *verdict*
        # produces below, so the error is logged/shown and no caller
        # can mistake the outcome for anything but a refusal.
        def _enforce_preflight(check_args: dict[str, Any]) -> None:
            try:
                validation = validate_tool_request(name, check_args, workspace_roots=workspace_roots)
            except Exception as e:
                try:
                    log.log_event("TOOL_VALIDATION_FAILED", {"tool_name": name, "error": f"internal validation error: {e}", "via": "registry.execute"})
                except Exception:  # best-effort audit write
                    logger.debug("Failed to write TOOL_VALIDATION_FAILED audit event", exc_info=True)
                raise ToolExecutionError(f"Pre-flight validation failed for '{name}': internal validation error: {e}") from e
            if not validation.is_valid:
                # E2-12: containment escapes and other validation failures
                # belong in the audit trail, not just the error string.
                try:
                    log.log_event("TOOL_VALIDATION_FAILED", {"tool_name": name, "error": validation.error, "via": "registry.execute"})
                except Exception:  # best-effort audit write
                    logger.debug("Failed to write TOOL_VALIDATION_FAILED audit event", exc_info=True)
                raise ToolExecutionError(f"Pre-flight validation failed for '{name}': {validation.error}")

        _enforce_policy(call_args)
        _enforce_preflight(call_args)

        # ── Step 2.5: tool_call hooks (Epic 7, E7-01) ────────────────
        # Registered guardrails may rewrite args or block the call;
        # a throwing hook fails closed (see tools/hooks.py).
        import copy

        from aether_engine.tools.hooks import HookBlock, apply_tool_call_hooks

        # Snapshot for the Step 2.55 change test (deep copy where
        # possible: a hook that mutates a nested value in place and
        # returns the same dict must still count as a rewrite).
        try:
            pre_hook_args = copy.deepcopy(call_args)
        except Exception:  # noqa: BLE001 — uncopyable args: a shallow copy still catches the documented return-a-dict rewrite.
            pre_hook_args = dict(call_args)
        try:
            call_args = apply_tool_call_hooks(name, call_args, audit_logger=log)
        except HookBlock as block:
            raise ToolExecutionError(f"Tool '{name}' blocked by a hook: {block}") from block

        # ── Step 2.55: re-check the FINAL args (S2-M1) ───────────────
        # Steps 1–2 vetted the PROPOSED arguments; the hooks may have
        # replaced them with anything (a benign shell command swapped
        # for a denied one, a workspace path swapped for a floored or
        # out-of-roots target). Policy verdicts are arg-aware — shell
        # content drives the safety rules, paths drive the floor and
        # the per-path rules — so a changed argument set is a new
        # call and is vetted as one, for every tool. Unchanged args
        # skip the re-run: the checks above already judged exactly
        # these arguments, and re-running would only double-audit
        # (e.g. a second APPROVAL_CACHE_HIT for the same grant).
        if call_args != pre_hook_args:
            _enforce_policy(call_args)
            _enforce_preflight(call_args)

        # ── Step 2.6: final-args gate (review finding M03) ───────────
        # The caller may vet the FINAL arguments — the only point in
        # the pipeline where they are fully determined: approval-time
        # edits arrive as ``args`` and hook rewrites land at Step 2.5.
        # Authorisation checks that must bind to the exact executed
        # action (the browser confirmation gate) live here, so the
        # edit path and the hook path share one implementation. The
        # gate raises ToolExecutionError to block; a graph caller may
        # also raise GraphInterrupt to pause for a fresh confirmation
        # — it propagates untouched (this call is deliberately outside
        # every try/except in this method). Non-graph callers pass no
        # gate and see no change.
        if final_args_gate is not None:
            gate_outcome = final_args_gate(name, call_args)
            if inspect.isawaitable(gate_outcome):
                await gate_outcome

        # ── Step 3: sandbox routing (ADR 0010) ─────────────────────────
        try:
            if tool.source.startswith("mcp:") or tool.source == "browser":
                # MCP tools run in-process in the engine (PRD Q4): the
                # worker hardcodes 3 builtin tools and cannot hold MCP
                # sessions, so routing an MCP tool there would silently
                # no-op. The MCP server itself is already isolated.
                # Browser tools (Epic 8, E8-6.1) share the rationale:
                # they drive a live Playwright session the engine
                # holds in its dedicated profile — a worker subprocess
                # cannot adopt it. Policy, pre-flight and hooks above
                # apply to both, unchanged.
                if inspect.iscoroutinefunction(tool.execute_fn):
                    result = await tool.execute_fn(**call_args)
                else:
                    result = await _run_sync_tool(tool, call_args)
                if isinstance(result, str) and len(result) > tool.max_output_chars:
                    result = (
                        result[: tool.max_output_chars]
                        + f"\n...[output truncated to {tool.max_output_chars} chars]"
                    )
                # MCP and browser executions fall through to the shared
                # tail: TOOL_EXECUTED audit + tool_result hooks below.
                # E0-02: MCP used to return through the result hooks
                # here, skipping the audit entirely — a successful MCP
                # execution left no TOOL_EXECUTED record. The tail
                # applies the hooks exactly once, so falling through
                # adds only the missing audit, nothing else.
            elif tool.risk != ToolRisk.READ_ONLY:
                from aether_engine.workers.sandbox import execute_tool_in_worker
                result = await execute_tool_in_worker(
                    tool_name=name,
                    args=call_args,
                    task_class=task_class,
                    audit_logger=log,
                    task_classes=task_classes,
                )
            elif inspect.iscoroutinefunction(tool.execute_fn):
                result = await tool.execute_fn(**_call_kwargs(tool, call_args, workspace_roots, path_rules))
            else:
                result = await _run_sync_tool(
                    tool, _call_kwargs(tool, call_args, workspace_roots, path_rules))
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
        # Epic 7, E7-01: tool_result hooks get the final word on what
        # the agent sees (replace or withhold; see tools/hooks.py).
        from aether_engine.tools.hooks import apply_tool_result_hooks

        return apply_tool_result_hooks(name, call_args, result, audit_logger=log)
