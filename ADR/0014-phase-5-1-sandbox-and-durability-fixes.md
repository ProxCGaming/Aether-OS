# ADR 0014: Phase 5.1 — Sandbox Bypass, Fail-Open Approval, Checkpoint Durability, and Per-Node Fallback Fixes

## Status
Accepted (Bug-fix record — no new architectural decisions)

## Context
A line-by-line audit of the live repository after Phase 5 revealed four confirmed bugs where the code contradicted decisions already recorded in ADRs 0009, 0010, and 0011, plus two smaller hygiene issues. This ADR documents what each bug was, why it mattered, and exactly what was changed to fix it.

## Bug Fixes

### Fix 1: Checkpoint durability (executor.py)
**Bug:** `AsyncSqliteSaver.from_conn_string(":memory:")` at `aether_engine/langgraph/executor.py` destroyed all checkpointed state when the Engine process exited, silently defeating ADR 0011 §4's requirement for durable, disk-backed checkpointing that survives Engine restarts.
**Fix:** Replaced `:memory:` with `~/.aether/checkpoints.db` (file path), with `mkdir(parents=True, exist_ok=True)` for the parent directory — the same pattern used by secrets storage. Verified by confirming the file is created on disk and tasks can be resumed after Engine restart.

### Fix 2: Sandbox enforcement for LangGraph tool execution (execute_tool.py)
**Bug:** `aether_engine/langgraph/nodes/execute_tool.py` called `tool.execute_fn()` directly for all tools, including risky ones (`write_file`, `delete_file`, `execute_shell`). This bypassed both pre-flight validation (ADR 0009 §2) and process-isolated worker execution (ADR 0009 §1, ADR 0010). The pre-flight validation function `validate_tool_request()` was only imported from the dead `orchestration/simple.py` module, meaning it never ran in the live system.
**Fix:**
  - Extracted `_execute_tool_in_worker()` from `app.py` into `aether_engine/workers/sandbox.py` as a shared, parameterized module (accepts audit_logger and task_classes explicitly instead of referencing the engine_state singleton).
  - Modified `execute_tool_node` to: (1) call `validate_tool_request()` before dispatching any tool, rejecting immediately if validation fails; (2) route risky tools through `execute_tool_in_worker()` (aether_worker subprocess with Job Object limits); (3) continue using `tool.execute_fn()` directly for safe tools only.
  - Added `workspace_roots` and `task_class` to the LangGraph `configurable` dict in `executor.py`.

### Fix 3: Fail-closed approval (pause.py)
**Bug:** `aether_engine/langgraph/nodes/pause.py` auto-approved tool calls when `approval_handler` was missing (`return {}`), directly contradicting ADR 0009 §4: "we favor fail-closed validation and explicit user consent for mutation-oriented operations."
**Fix:** When `handler` is `None`, the tool call is now rejected using the same rejection message shape already used for explicit user rejection. Logs `APPROVAL_HANDLER_MISSING` distinctly in the audit log to surface the configuration problem.

### Fix 4: Per-node fallback (litellm_provider.py)
**Bug:** `app.py`'s `execute_task_with_fallback()` only triggered on `first_event` failures, meaning a model call failure inside the Coder node after Researcher and Planner had succeeded was not caught. When it did fire, it restarted the entire graph from scratch. ADR 0006 and Phase 5 both required fallback protection at every individual node's model call.
**Fix:** Added per-call fallback directly inside `LiteLLMProvider.call_stream()`. On retriable errors (rate limit, timeout, network), the method automatically retries with fallback models computed from `get_fallback_candidates()` in `routing/fallback.py`. Logs `NODE_FALLBACK_TRIGGERED` (distinct from task-level `FALLBACK_TRIGGERED`) with which model failed and which succeeded. The existing `execute_task_with_fallback()` in `app.py` is preserved as a coarse safety net.

### Fix 5: Audit logging for export_keys.py
**Bug:** `scripts/export_keys.py` exported plaintext API keys with zero audit trail, despite every other security-relevant action in the project being audit-logged.
**Fix:** Added `SECRETS_EXPORTED` audit event with provider names, count, and export path (not key values) after successful export.

### Fix 6: Dead code cleanup
- **Deleted** `aether_engine/providers/gemini.py` — zero imports codebase-wide, fully superseded by `litellm_provider.py`.
- **Deleted** `aether_engine/orchestration/simple.py` — dead code never called by `app.py`.
- **Rewrote** `tests/test_fallback_rules.py` to remove the dead `simple.py` import and test the live fallback paths (`classify_provider_error`, `get_fallback_candidates`, `LiteLLMProvider.fallback_models`).
- **Deleted** `tests/test_orchestration_simple.py` — exclusively tested the dead `simple.py` orchestration loop.
- **Cleaned up** `aether_engine/langgraph/checkpointer.py` — replaced broken stub with a clean path constant module.

## Consequences
- Paused tasks now survive Engine restarts (Fix 1).
- All risky tool calls from LangGraph go through the same validated, sandboxed path as the rest of the system (Fix 2).
- Missing approval handler = rejected, not silently approved (Fix 3).
- Model failures mid-task retry with fallback models per-call, without restarting completed nodes (Fix 4).
- Secret exports are now audit-visible (Fix 5).
- Dead code removed, tests now exercise live paths (Fix 6).
