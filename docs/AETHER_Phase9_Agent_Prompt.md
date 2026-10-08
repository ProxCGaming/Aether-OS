# Task: Build Aether OS — Phase 9 (Trust-Boundary Hardening + UI Honesty)

## Your role
You are a senior Python/React/Electron developer continuing an existing project. This is a **corrective phase in the Phase 5.1 mold**: every item below was confirmed by a direct audit of the live repository at commit `e3625f6` — not hypothetical concerns, not style preferences. Each item cites the exact file and line where the problem was verified. The code currently contradicts binding architectural decisions (ADRs 0003, 0009, 0010, 0021) or the repo's own agent rules in ways that are individually small but collectively mean the security architecture is documented-but-not-true. Fix that. Do not build new features.

## Before you start
Read `docs/adr/0003`, `0009`, `0010`, `0021` in full. Read `aether_common/contracts.py` and `.agents/rules/01-project-standards.md`. For each finding below: first confirm you can reproduce it in the current code (file:line references are from the audit at `e3625f6` — if the code has moved, re-verify; do not assume the reference is still accurate), then fix it, then write a test that would have failed before your fix and passes after.

---

## Part A: Trust boundary — authentication and fail-closed execution

### A1. Seven HTTP management routes have zero authentication (CRITICAL — do this first)
**Confirmed:** `aether_engine/routes/websocket.py` enforces the ADR 0003 token handshake in exactly one place — the `/ws/tasks` handler (lines 105–110, `verify_token` → close 1008). The same router exposes seven unauthenticated HTTP routes, including mutating ones:
```python
@router.get("/plugins")                  # line 1334
@router.post("/plugins/install")         # line 1338
@router.post("/plugins/confirm")         # line 1347 — arbitrary code into the engine process
@router.delete("/plugins/{name}")        # line 1357
@router.patch("/plugins/{name}/toggle")  # line 1362
@router.get("/agents/nodes")             # line 1374
@router.patch("/agents/nodes/{name}/toggle")  # line 1387
```
No `verify_token`, no auth dependency, on any of them (`verify_token` appears once in the whole file). ADR 0003's threat model explicitly names "unauthorized access by other local processes or malicious scripts" — this is precisely that hole. Loopback-only binding stops remote attackers, not local ones.

**Required fix:** delete all seven routes. Rationale (verified): the only callers are the retired PySide6 UI (`aether_ui/settings/plugins_tab.py:160`, `agents_tab.py:79` — superseded by ADR-0019) and one test. The shipped Electron UI uses the authenticated WebSocket flow (`PLUGIN_INSTALL_REQUEST` etc.). If you find a live caller that genuinely needs one of these routes, gate it behind the same token-verifying dependency as `/ws/tasks` instead of deleting — but default to deletion, and document the decision in this phase's ADR.

### A2. `ToolRegistry.execute()` is a public, unenforced execution path
**Confirmed at `aether_engine/tools/registry.py:179-186`:**
```python
async def execute(self, name: str, args: Optional[Dict[str, Any]] = None) -> Any:
    tool = self.get_tool(name)
    ...
    return tool.execute_fn(**call_args)   # direct in-process call
```
The raw risky closures still exist (`Path.write_text` at `registry.py:72`, `subprocess.run(shell=True)` at `registry.py:80-82`), and `execute()` invokes them with zero policy check, zero pre-flight validation, zero sandbox routing. No live callers today (tests only) — but it is public API, a landmine for the next phase that reaches for it.

**Required fix:** route `execute()` through the same policy → validate → sandbox pipeline that `execute_tool_node` uses. If that is not cleanly possible, rename it to a private method and mark it deprecated with an explicit warning. It must not remain a silent bypass.

### A3. Resource limits silently skipped on non-Windows — fail-open degradation of ADR 0010
**Confirmed at `aether_engine/workers/sandbox.py:60-64`:** `WorkerJobObject(time_cap, mem_cap)` raises on any non-Windows host; the exception is caught, a warning is logged, and execution continues with `job_obj = None` — i.e. unlimited. Every "sandboxed" execution on Linux/macOS runs with no time/memory caps, silent except a log line. The shipped artifact is an Electron app that runs on all three OSes.

**Required fix:** fail closed. If limits cannot be enforced, refuse to execute risky tools (with a distinct log/audit entry), or implement POSIX `rlimit`/cgroup caps. Do not silently run unlimited.

### A4. `NODE_FALLBACK_TRIGGERED` never reaches the audit log
**Confirmed at `aether_engine/providers/litellm_provider.py:435-439`:** the per-call fallback logs via stdlib `logger.info` only. Phase 5.1's acceptance test #4 required it to be logged distinctly in the audit log so it is distinguishable from task-level `FALLBACK_TRIGGERED`.

**Required fix:** emit it via the audit logger — `AuditLogger().log_event("NODE_FALLBACK_TRIGGERED", {...})` — including the node name, the failed model, and the fallback model that succeeded.

### A5. `export_keys.py` writes the export before the audit event + dead code deletion
**Confirmed at `scripts/export_keys.py:58-70`:** the plaintext export file is written *before* the `SECRETS_EXPORTED` audit attempt, and an audit failure is swallowed with a printed warning while the export stands — fail-open on the auditability requirement (ADR 0003 §4: every security-relevant action auditable).

**Required fix:** emit the audit event first; abort the export if the audit event fails.

**Dead code — delete all three** (each verified zero live importers via repo-wide grep excluding tests/scripts):
- `aether_engine/langgraph/checkpointer.py` — its `DEFAULT_CHECKPOINT_DB` is duplicated inline in `executor.py`; nothing imports it.
- `aether_engine/mcp/fallback.py` — `execute_mcp_tool_with_fallback` scaffolding that just raises a simulated `ConnectionError`; nothing imports it.
- `from .simulated_task import SimulatedTask` in `aether_engine/__init__.py:2` — never used by any live path.

Before deleting: check whether any test imports them; if so, update or remove those tests explicitly. Do not leave dangling imports.

---

## Part B: UI honesty — no placebo controls
Principle (from the Phase 6.1 prompt): never leave a control that looks functional but isn't.

### B1. `AUXILIARY_MODEL_SET_REQUEST` goes nowhere
**Confirmed:** `desktop_app/src/SettingsPanel.jsx:589` — all seven per-task-class dropdowns (Vision, Web extract, Compression, Skills hub, Approval, MCP, Title gen) send `type: 'AUXILIARY_MODEL_SET_REQUEST'`. A repo-wide grep finds zero matches for `AUXILIARY_MODEL` in `aether_common/contracts.py`, `aether_engine/`, or anywhere else. The user picks a model, the UI fires the message, the engine drops it. This is the exact "looks wired, but isn't" failure the Phase 6.1 prompt was written to kill.

**Required fix:** implement it for real — add `AUXILIARY_MODEL_SET_REQUEST` / `AUXILIARY_MODEL_SET_RESPONSE` to `aether_common/contracts.py` (check the project's `SCHEMA_VERSION` convention first and bump only if required), add a `routes/websocket.py` handler that persists per-task-class model overrides, and wire the engine to actually consult them when selecting auxiliary models. If you discover a genuine reason this cannot be implemented cleanly, remove the seven dropdowns instead. Do not leave them as placebo controls.

### B2. Reasoning Effort selector persists to `localStorage` only
**Confirmed at `desktop_app/src/SettingsPanel.jsx:34, 543-545`:** the selector writes to `localStorage` and component state; nothing in the engine reads or consumes it, so it cannot affect model behavior.

**Required fix:** implement a `REASONING_EFFORT_SET` contract event + handler + engine consumption, or remove the control. Same rule as B1.

### B3. Remaining fake/dead UI — fix all three
- `desktop_app/src/Dashboard.jsx:52-54` — the "Start New Workflow" button is styled and clickable with **no `onClick`**; it does nothing. Wire it to a real action or remove it.
- `desktop_app/src/SettingsPanel.jsx:367,392` — `// Check if we have llama.cpp installed (mock check for demo)` and `// Simulate downloading native dependencies`, followed by a 3-second `setTimeout` that sets a `localStorage` flag. The user watches fake installer progress. Put the control in a clearly-labeled not-yet-implemented disabled state in the UI (honest in the UI, not just in a code comment). Also: the secondary `startModelDownload` (`:375-383`) still omits `destination_dir`, which the 6.1 A3.1 fix added only to `LocalModelsTab` — add it there too.
- `desktop_app/src/App.jsx:11,631` — `// Dummy graph data for "first-run" state` plus `// Add some random ambient nodes to simulate a vast but empty network` (30 `Math.random()` nodes) presented as the knowledge graph with no disclosure. Remove the ambient nodes, or add an explicit "demo visualization" label.

---

## Part C: Test integrity

### C1. Missing Jest tests — the repo's own rule (`.agents/rules/01-project-standards.md` §2) mandates a test per component
Tests exist only for `Dashboard` and `SettingsPanel`. Write real tests for at least: `App.jsx`, `Projects.jsx`, `LocalModelsTab.jsx`, `DiagnosticsPanel.jsx`, `MemoryManagement.jsx`. Each test must assert actual rendered behavior. No placeholders.

### C2. Replace the tautology test
**Confirmed at `desktop_app/src/__tests__/SettingsPanel.test.jsx:17`:** a test named "renders correctly with risk badges and disabled Always Allow for risky tools" whose entire assertion is `expect(true).toBe(true); // placeholder test`. Make it assert what its name claims: risk badges render, and "Always Allow" is disabled for risky tools. (Project-standards §3: test names must not describe behavior never checked.)

### C3. Fix the fallback test shape; add a real fallback test
**Confirmed at `tests/test_fallback_rules.py:106-116`:** `test_litellm_provider_stores_fallback_models` passes plain strings as `fallback_models`, but the live per-call fallback loop (`litellm_provider.py:358-360`) unpacks `FallbackModel` dataclass instances (`fm.model`, `fm.api_key`, …) — a provider built the way the test builds it would die with `AttributeError` on the live path. The test passes trivially while describing a configuration that cannot work.
**Required fix:** pass `FallbackModel` instances in the test. Additionally: add a test that mocks a retriable primary-model failure and asserts the fallback model is actually attempted — currently no test anywhere does this.

### C4. `tests/test_supervisor_integration.py` collects zero tests
25 lines, zero `def test_*` — pytest collects nothing, so it can never fail. Either write real integration tests or delete the file. A test file that cannot fail is worse than no file.

---

## Explicitly out of scope this phase
- No new features, no new nodes, no new UI tabs.
- Do not touch the checkpoint/resume mechanism itself (verified disk-backed and working) — only the fallback audit-logging around it (A4).
- Do not refactor the retired `aether_ui` PySide6 client beyond what A1 requires.
- Do not bump `SCHEMA_VERSION` in `aether_common/contracts.py` unless the project's contract convention requires it for new event types — check first.

## Required process
For each finding, before writing code: confirm you can reproduce it as described (read the exact current code at the referenced location first — do not assume this prompt's description is still accurate if the file has changed since the audit at `e3625f6`), then fix it, then write a test that would have failed before your fix and passes after. For A1, the "test" is an unauthenticated HTTP request asserting rejection (401) or absence (404 after deletion).

**Write one new ADR: `docs/adr/0022-phase-9-trust-boundary-hardening.md`.** For each fix: what the issue was (cite the exact file/function), why it mattered, and exactly what you changed. Frame it as a corrective record like 0014, not a new architectural direction.

## Acceptance tests for this phase
1. Unauthenticated `POST /plugins/confirm` → 401, or 404 if the route was deleted. The authenticated WebSocket plugin flow still works end-to-end from the Electron UI.
2. Calling `ToolRegistry.execute("execute_shell", …)` from a fresh caller routes through policy/pre-flight/sandbox — prove it via the audit log (`WORKER_EXECUTION_STARTED`) or a policy denial, not a bare return value.
3. With `WorkerJobObject` creation forced to fail (or on a non-Windows host), risky tool execution is refused — not silently unlimited.
4. Trigger a per-call model fallback; confirm `NODE_FALLBACK_TRIGGERED` appears in `logs/audit.log` with the node name, failed model, and fallback model.
5. Run `scripts/export_keys.py` with a broken audit logger → no export file is written.
6. The kill-engine-mid-approval resume test from Phase 5.1 acceptance #1, run for real this time: start a task, get it paused for approval, kill the Engine process, restart it, confirm the task resumes from the checkpoint. Do not report this phase done without this specific manual test passing.
7. Every auxiliary-model dropdown and the Reasoning Effort selector demonstrably changes engine behavior (show the engine-side effect), or the controls are gone.
8. Jest suite green on all touched and new component tests; zero placeholder assertions (`expect(true).toBe(true)` and friends) anywhere in `desktop_app/src/__tests__/`.

## When you're done
Report, for each fix: the exact issue, the exact fix, and the specific test that proves it. Paste full raw test output. Do not summarize test results as "all passing" without the actual output. Stop here — do not start the next phase without being explicitly told to, even if everything looks clean.
