# Task: Build Aether OS — Phase 5.1 (Critical Fixes: Sandbox Bypass, Fail-Open Approval, Checkpoint Durability, Per-Node Fallback)

## Your role
You are a senior Python/Windows systems developer continuing an existing
project. This phase exists to fix four specific, confirmed bugs found
during a direct line-by-line audit of the live repository — not
hypothetical concerns, not style preferences. Every issue below was
verified by reading the actual current code before this prompt was
written. Do not take shortcuts, do not defer any of the four fixes, and
do not start Phase 5.5 (Extensions UI) until these are resolved and
verified.

## Before you start
Read `ADR/0009`, `0010`, `0011` in full. Every fix in this phase exists
because the live code currently contradicts a decision already recorded
in one of those three ADRs. This phase does not change those decisions
— it makes the code actually match them.

## The four confirmed issues, in order of severity

### 1. LangGraph checkpointer is in-memory, not persistent — defeats the entire point of Phase 5
**Confirmed at `aether_engine/langgraph/executor.py`, line 57:**
```python
async with AsyncSqliteSaver.from_conn_string(":memory:") as saver:
    # Note: Using an in-memory saver for simplicity. In production, this should be persistent.
```
ADR 0011's decision #4 explicitly requires durable, disk-backed
checkpointing so a paused task survives an Engine restart. Right now it
does not — an in-memory SQLite connection is destroyed the moment the
Python process exits, silently defeating the exact guarantee Phase 5 was
built to provide. This is not a hardening task, it's completing
something ADR 0011 already committed to.

**Required fix:** change `AsyncSqliteSaver.from_conn_string(":memory:")`
to a real file path — consistent with the project's existing
`~/.aether/` storage convention (e.g. `~/.aether/checkpoints.db` — verify
the current `AsyncSqliteSaver` API for the correct connection string
format at build time, this library evolves). Ensure the parent directory
is created if it doesn't exist, the same pattern already used elsewhere
in the project for `~/.aether/` files.

### 2. LangGraph's tool execution completely bypasses the sandbox — confirmed, not partial
**Confirmed:** `aether_engine/langgraph/nodes/execute_tool.py` calls
`registry.get_tool(tool_name).execute_fn(**args)`, which resolves to the
raw closures in `aether_engine/tools/registry.py`:
```python
def _write_file(path, content=""):
    Path(path).write_text(content, encoding="utf-8")   # direct, in-process, unrestricted

def _execute_shell(command):
    subprocess.run(command, shell=True, ...)             # direct, in-process, no limits
```
No `aether_worker` subprocess. No Job Object time/memory cap. No
workspace restriction. No pre-flight validation at all — confirmed
separately that `aether_engine/validation/pre_flight.py`'s
`validate_tool_request()` is currently called from exactly one place in
the entire codebase: `aether_engine/orchestration/simple.py`, which is
itself dead code never invoked by the live `app.py`. **Pre-flight
validation is not just skipped by the LangGraph path — it does not run
anywhere in the live system, period.**

Meanwhile, `aether_engine/app.py` already contains a correctly-built,
working, sandboxed execution function — **do not write a new one, call
this one**:
```python
async def _execute_tool_in_worker(tool_name: str, args: Dict[str, Any], task_class: str) -> Any:
    # app.py line 295 — spawns aether_worker subprocess, assigns
    # WorkerJobObject with task-class time/memory limits, awaits result,
    # logs WORKER_EXECUTION_TERMINATED on failure. This is correct and
    # complete. Reuse it.
```

**Required fix:**
- `execute_tool_node` (in `aether_engine/langgraph/nodes/execute_tool.py`)
  must call `validate_tool_request()` (from `aether_engine/validation/
  pre_flight.py` — exact signature: `validate_tool_request(tool_name,
  args, workspace_roots)`) **before** dispatching, and reject cleanly if
  validation fails, consistent with ADR 0009's fail-closed principle.
- For any tool classified as risky (write_file, delete_file,
  execute_shell — same classification already used by the pause
  conditional edge), `execute_tool_node` must call the existing
  `_execute_tool_in_worker()` function, not `registry.execute_fn()`
  directly. You will need to make `_execute_tool_in_worker` importable/
  callable from the `langgraph/nodes/` module — refactor its location if
  needed (e.g. move it to a shared module both `app.py` and
  `execute_tool.py` import from), but do not duplicate its logic.
  Document exactly what you moved and why in this phase's ADR.
  `task_class` and `workspace_roots` need to be threaded through
  `RunnableConfig`'s `configurable` dict (confirmed currently: only
  `thread_id`, `tool_registry`, `approval_handler`, `request_id` are
  present — `task_class` and `workspace_roots` need to be added at the
  point the graph is invoked in `executor.py`).
- Safe tools (read_file, web_search, summarize) can continue using
  `registry.execute_fn()` directly — they were never the problem, only
  the risky/mutating tools need to route through the sandbox.

### 3. Approval fails open, not closed — contradicts ADR 0009 directly
**Confirmed at `aether_engine/langgraph/nodes/pause.py`:**
```python
if not handler:
    # If no handler is provided, we auto-approve or fail. Here we'll auto-approve.
    return {}
```
ADR 0009: "we favor fail-closed validation and explicit user consent for
mutation-oriented operations, even when that introduces extra latency."
This code does the opposite of that stated principle in exactly the
scenario it matters most — when something has gone wrong with the
approval wiring itself.

**Required fix:** if `handler` is missing, treat the tool call as
rejected, not approved. Return the same rejection-message shape already
used for an explicit user rejection later in the same function — do not
invent a second rejection format. Log this specific case distinctly in
the audit log (e.g. `APPROVAL_HANDLER_MISSING`) since it indicates a
configuration problem worth surfacing, not routine user behavior.

### 4. Fallback exists but only at task-level, first-event-only, and restarts the whole graph
**Confirmed:** `app.py`'s `execute_task_with_fallback()` is real, working
code — audit-logs `FALLBACK_TRIGGERED/COMPLETED/FAILED` correctly. But
it only triggers `if primary_failed and first_event` — meaning only a
failure on the very first event of an entire task is caught. A failure
inside Coder after Researcher and Planner have already succeeded is not
caught by this mechanism at all. When it does fire, it restarts the
**entire graph** with a different provider — re-running every node from
scratch — rather than retrying just the one call that failed. ADR 0006
and the original Phase 5 prompt both required fallback protection at
every individual node's model call, not only at task start.

**Required fix — this is additive, not a replacement:**
- Keep `execute_task_with_fallback()` in `app.py` as-is — it's a
  reasonable coarse safety net for "the very first model call of a task
  can't reach any provider at all" and doesn't need to be removed.
- Add **per-call fallback** directly inside
  `aether_engine/providers/litellm_provider.py`'s `call_stream()`
  method, using LiteLLM's own native `fallbacks=[...]` parameter passed
  into the `litellm.acompletion()` call (verify the current parameter
  name/shape for your installed LiteLLM version at build time — this
  has shifted across versions). The fallback candidate list should reuse
  the existing `get_fallback_candidates()` function from
  `aether_engine/routing/fallback.py` (signature: `get_fallback_candidates
  (failed_provider, configured_providers, health_manager, registry)`) —
  do not write a second candidate-selection implementation.
- When a per-call fallback fires (LiteLLM successfully used a fallback
  model within a single node's call), log it distinctly from the
  existing task-level `FALLBACK_TRIGGERED` — e.g.
  `NODE_FALLBACK_TRIGGERED` with which node, which model failed, which
  model succeeded — so the two mechanisms are distinguishable in the
  audit log, not conflated.

## Two smaller required fixes

### 5. `scripts/export_keys.py` has zero audit logging
Confirmed: no `audit` or `log_event` call anywhere in this script. It
exports every stored API key to plaintext JSON at
`~/.aether/exported_keys.json`. Add an audit log entry (e.g.
`SECRETS_EXPORTED`, with provider names and timestamp, **not** the key
values themselves) so this action is visible in the audit trail like
every other security-relevant action in the project.

### 6. Dead code cleanup
- Delete `aether_engine/providers/gemini.py` — confirmed zero imports
  anywhere in the codebase, fully superseded by
  `aether_engine/providers/litellm_provider.py` (confirmed live,
  correctly wired).
- `aether_engine/orchestration/simple.py` is confirmed dead (never
  called by `app.py`), **but** `tests/test_fallback_rules.py` currently
  tests fallback classification logic exclusively through this dead
  path — meaning that test currently provides no real coverage of live
  behavior. Before deleting `simple.py`, either: (a) rewrite
  `test_fallback_rules.py` to exercise the real live path (the new
  per-call fallback in `litellm_provider.py`, and/or
  `execute_task_with_fallback` in `app.py`), or (b) if the underlying
  functions it tests (`classify_provider_error`, `is_retriable_error`)
  are still used by the live fallback path regardless of which module
  imports them, confirm that and document it clearly rather than assume
  it. Do not delete `simple.py` while leaving a test that silently tests
  nothing real.

## Explicitly out of scope this phase
- No Phase 5.5 Extensions UI work (Tools/Skills/Plugins/Agents tabs).
- No new features, no new nodes, no new MCP functionality.
- Do not touch the Settings UI consolidation work (MCP tab transplant,
  local models transplant, Capabilities Check transplant) — that's
  already in progress separately.
- Do not change the `AetherState` schema — it was checked during this
  audit and is correctly built (overwrite-vs-accumulate fields are
  already correct via `operator.add` annotations on `messages` and
  `delegation_log` only). Leave it alone.

## Required process
For each of the four numbered fixes, before writing code: confirm you
can reproduce the problem as described (read the exact current code at
the referenced location first — do not assume this prompt's description
is still accurate if the file has changed since this audit), then fix
it, then write a test that would have failed before your fix and passes
after.

**Write one new ADR: `ADR/0013-phase-5-1-sandbox-and-durability-fixes.md`.**
For each of the four main fixes, state clearly: what the bug actually
was (cite the exact file/function), why it mattered, and exactly what
you changed. This ADR is a bug-fix record, not a new architectural
decision — frame it that way. Explicitly confirm, per fix, that you
verified the fix by reproducing the original failure first.

## Acceptance tests for this phase
1. **Checkpoint durability, for real:** start a task, get it paused for
   approval, kill the Engine process, restart it, confirm the task
   resumes from the checkpoint. This is the same test the original Phase
   5 prompt required — it must actually pass now. Do not report this
   phase done without this specific manual test passing.
2. **Sandbox enforcement, for real:** approve a `write_file` call to a
   path outside the declared workspace and confirm it's rejected by
   pre-flight validation before any approval prompt appears. Approve a
   `write_file` call inside the workspace and confirm it actually goes
   through `aether_worker` (check the audit log for
   `WORKER_EXECUTION_STARTED`/`COMPLETED` — these events should now
   appear for LangGraph-originated tool calls, which they currently do
   not).
3. **Fail-closed approval:** simulate a missing `approval_handler`
   (however you construct this in a test — mock config without the key)
   and confirm the tool call is rejected, not silently approved.
4. **Per-node fallback:** simulate a Coder node's model call failing
   partway through a multi-node task (Researcher/Planner already
   succeeded) and confirm the task still completes via fallback, without
   restarting Researcher or Planner's already-completed work.
5. `tests/test_fallback_rules.py` passes and demonstrably tests the live
   fallback path, not the dead `orchestration.simple` path — show the
   diff or explain exactly what changed.

## When you're done
Report, for each of the six fixes: the exact bug, the exact fix, and the
specific test that proves it. Paste full raw test output. Do not
summarize test results as "all passing" without the actual output. Stop
here — do not proceed to Phase 5.5 without being explicitly told to,
even if everything in this phase looks clean.
