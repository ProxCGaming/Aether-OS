# ADR 0022 — Phase 9: Trust-Boundary Hardening + UI Honesty

Date: 2026-10-08
Status: Accepted
Scope: Corrective phase in the Phase 5.1 mold. Every item below was confirmed
by direct audit of the live repo at commit `e3625f6` before being fixed.

## Context

The Phase 9 audit checked the live code against ADRs 0003, 0009, 0010, 0021
and the repo's agent rules. It found ten places where the code contradicted
a recorded decision or rule — individually small, collectively meaning the
security architecture was documented-but-not-true. This ADR records each fix
as a corrective record (like ADR 0014), not a new architectural direction.

## A1. Deleted seven unauthenticated HTTP management routes

**Was:** `aether_engine/routes/websocket.py` enforced the ADR 0003 token
handshake only on `/ws/tasks` (lines 105–110). Seven HTTP routes —
`GET /plugins`, `POST /plugins/install`, `POST /plugins/confirm`,
`DELETE /plugins/{name}`, `PATCH /plugins/{name}/toggle`,
`GET /agents/nodes`, `PATCH /agents/nodes/{name}/toggle` — had no
authentication. ADR 0003's threat model explicitly names unauthorized access
by other local processes; loopback binding does not stop those.

**Changed:** all seven routes deleted. Verified the only callers were the
retired PySide6 UI (`aether_ui/settings/plugins_tab.py`,
`aether_ui/settings/agents_tab.py`, superseded by ADR-0019) and one test for
that retired path. Plugin/agent management now goes exclusively over the
authenticated WebSocket. A comment at the deletion site forbids re-adding
unauthenticated management routes. The obsolete test
`tests/test_agents_tab_live_model.py` was replaced by
`tests/test_phase9_trust_boundary.py`, which asserts all seven routes 404.

## A2. `ToolRegistry.execute()` now enforces the trust pipeline

**Was:** `aether_engine/tools/registry.py`'s `execute()` called
`tool.execute_fn()` directly — no policy check, no pre-flight validation, no
sandbox routing. A public bypass around ADRs 0009/0010/0021 for any future
caller (no live callers existed; only tests).

**Changed:** `execute()` now runs the same pipeline as `execute_tool_node`:
policy resolution (deny → raise; ask without `approved=True` → raise,
fail-closed), `validate_tool_request()`, then sandbox-worker routing for
non-`READ_ONLY` tools and in-process execution for `READ_ONLY` tools.
Signature is backward compatible (`name`, `args` positional) with new
keyword options (`workspace_roots`, `task_class`, `policies`, `approved`,
`audit_logger`). `tests/test_tool_registry.py` was updated to the new
contract and extended with deny/ask/pre-flight/worker-routing tests.

## A3. Sandbox fails closed when resource limits are unenforceable

**Was:** `aether_engine/workers/sandbox.py` caught `WorkerJobObject`
construction failure, logged a warning, and continued with `job_obj=None` —
silently unlimited execution on any non-Windows host, contradicting ADR 0010.

**Changed:** construction failure now raises `RuntimeError` (refuses to
execute) and emits a distinct `WORKER_EXECUTION_REFUSED` audit event.
POSIX `rlimit` enforcement was considered and deferred as future work; the
fail-closed behavior matches the phase's acceptance test.

## A4. `NODE_FALLBACK_TRIGGERED` is now an audit event

**Was:** `litellm_provider.py` logged per-call fallbacks via stdlib
`logger.info` only, violating Phase 5.1 acceptance test #4 (must be
distinguishable from task-level `FALLBACK_TRIGGERED` in the audit log).

**Changed:** the fallback path now emits
`AuditLogger().log_event("NODE_FALLBACK_TRIGGERED", {...})` with node name,
failed model/provider, and fallback model/provider. `call_stream()` gained
an optional `node_name` parameter (also added to `BaseProvider`), and the
coder/planner/researcher/supervisor call sites pass their node names.

## A5. `export_keys.py`: audit-before-write; dead code removed

**Was:** `scripts/export_keys.py` wrote the plaintext export file *before*
attempting the `SECRETS_EXPORTED` audit event, and swallowed audit failures
with a printed warning — fail-open on auditability (ADR 0003 §4).

**Changed:** the audit event is emitted first; if it fails, the script aborts
with exit 1 and no file is written.

**Dead code:** the audit flagged three modules for deletion, but
verification (per the phase's required process) showed two claims were
wrong, so the outcome differs from the prompt:
- `aether_engine/langgraph/checkpointer.py` — NOT deleted. It was never dead
  weight: it defines the shared `DEFAULT_CHECKPOINT_DB` constant, which
  `executor.py` was duplicating inline in three places. Instead of deleting
  the module, `executor.py` now imports the constant (deduplication), and the
  now-unused `pathlib` import was removed. Two test files import the constant
  from this module and remain green.
- `aether_engine/mcp/fallback.py` — NOT deleted. The audit claimed zero
  importers, but `tests/test_mcp_artifacts.py` imports and tests its graceful
  degradation behavior. Deleting tested scaffolding for no functional gain
  was judged wrong; it stays, documented as the seam for the real MCP client.
- `aether_engine/__init__.py` — the unused `SimulatedTask` re-export was
  dropped (verified: nothing does `from aether_engine import SimulatedTask`;
  the module itself and its test remain).

**Also fixed (found during verification):** five import sites
(`coder.py`, `planner.py`, `researcher.py`, `supervisor.py`,
`tests/test_fallback_rules.py`) did
`from aether_engine.app import _create_provider_instance`, but the function
is defined in `aether_engine/orchestration/core.py` and `app.py` never
re-exported it — a latent `ImportError` on the agent-model-override path and
a failing test. All five now import from `orchestration.core`.

## B1. Auxiliary model overrides are real (contract + persistence + routing)

**Was:** the seven per-task-class dropdowns in Settings sent
`AUXILIARY_MODEL_SET_REQUEST`, which had no contract entry and no handler —
a placebo control.

**Changed:**
- `aether_common/contracts.py`: added `AUXILIARY_MODEL_SET_REQUEST` /
  `AUXILIARY_MODEL_SET_RESPONSE` (additive; no `SCHEMA_VERSION` bump, per the
  project's convention for new event types).
- `UserConfig`: new persisted `auxiliary_models` map (task →
  `"provider:::model"`, absent = auto) with `to_dict`/`from_dict` support.
- `routes/websocket.py`: handler validates the task class, validates the
  provider against configured providers, persists via `save_config`, audit
  logs `AUXILIARY_MODEL_SET`, and returns the full map.
- `routing/policy.py`: `select_route()` accepts `auxiliary_models`; when a
  task requires vision (`has_images`) and a valid override exists for a
  configured, healthy provider, the override wins with reason
  "User auxiliary override (vision)". Overrides pointing at unconfigured
  providers are ignored (fail-safe). The other six task classes are
  persisted and returned so the UI is truthful; they are the documented seam
  for future per-task routing.
- Settings UI: dropdowns now initialize from `CONFIG_GET_RESPONSE`,
  optimistically update on change, and sync on `AUXILIARY_MODEL_SET_RESPONSE`;
  the per-task subtitle shows the actual override instead of hardcoded text.

## B2. Reasoning effort is engine-visible

**Was:** the Reasoning Effort selector wrote only to `localStorage`; the
engine could never see it.

**Changed:** `REASONING_EFFORT_SET_REQUEST` / `RESPONSE` contract events,
persisted `reasoning_effort` in `UserConfig` (validated to
standard|medium|high), a WS handler, and forwarding: `_create_provider_instance`
passes the configured effort into `LiteLLMProvider`, whose
`_build_call_kwargs` includes `reasoning_effort` only when non-default (so
models without reasoning support are unaffected). The UI initializes from
the engine value and keeps `localStorage` as a cache.

## B3. Fake UI removed

- `Dashboard.jsx`: the "Start New Workflow" button had no `onClick`. It now
  navigates to the Chat tab via a new `onStartWorkflow` prop.
- `SettingsPanel.jsx`: deleted the dead llama.cpp "mock check for demo"
  download flow (mock `localStorage` flag, simulated 3s install). It was
  unreachable — never rendered, never called. Local model downloads are
  handled for real by `LocalModelsTab.jsx` (which already sends
  `destination_dir`).
- `App.jsx`: removed the 30 random "ambient" nodes that simulated a vast
  knowledge-graph network. The background graph is now documented in code as
  decorative branding, not live data.

## C. Test integrity

- `SettingsPanel.test.jsx`: the `expect(true).toBe(true)` placeholder is
  gone; the test now feeds a `TOOL_LIST_RESPONSE` through the mocked socket
  and asserts real risk badges, the disabled-Always-Allow notice, and the
  empty state.
- New Jest suites: `App.test.jsx` (nav, tab switching incl. the fixed
  workflow button, no fabricated graph nodes), `Projects.test.jsx`
  (list/add/remove workspace flows), `LocalModelsTab.test.jsx` (catalog,
  download with `destination_dir`, downloaded state),
  `DiagnosticsPanel.test.jsx` (MCP list/add flows). Full Jest suite:
  10 suites, 25 tests, green.
- `test_fallback_rules.py`: the string-`fallback_models` test now passes
  `FallbackModel` instances (the live loop unpacks dataclass fields; strings
  would crash it), and a new test mocks a retriable primary failure and
  asserts the fallback model is attempted *and* `NODE_FALLBACK_TRIGGERED` is
  audit-logged with the node name.
- `test_supervisor_integration.py`: previously collected zero tests. Now five
  real tests: delegation, forced-agent short-circuit, all-disabled → END,
  invalid-node clamping, missing-provider failure.
- New: `tests/test_phase9_trust_boundary.py` (A1, A3) and
  `tests/test_phase9_settings_wiring.py` (B1/B2 contract, config, routing,
  provider kwargs).

## Verification

- `pytest` on the affected files: green (see phase report for raw output).
- `npx jest`: 10 suites / 25 tests green.
- Pre-existing failure noted but out of scope: none introduced by this phase.
  (One pre-existing broken import in `test_fallback_rules.py` was fixed as
  part of C3 since the file was already being repaired.)
