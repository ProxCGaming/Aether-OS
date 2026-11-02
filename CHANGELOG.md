# Changelog

All notable changes to the **Aether-OS** project will be documented in this file. 

## [Unreleased]

### Removed
- **[2026-11-02] Retired PySide6 UI deleted**: `aether_ui/` removed outright (owner directive) with its six dedicated test files (`test_provider_health_badge`, `test_providers_panel_fixes`, `test_settings_provider_state`, `test_ui_window_geometry`, `test_approval_drawer_override`, `test_ui_state`) and the `legacy-ui` optional extra — PySide6, qasync and markdown had zero imports left outside the deleted tree. The Electron app in `desktop_app/` is the only frontend. The two baseline entries naming the deleted panel tests were removed from `scripts/pytest-known-failures.txt`; `tests/test_legacy_ui_retirement.py` keeps its guards (legacy deps never return as hard dependencies; default-path code never imports the legacy stack).

## [0.3.5] — 2026-10-31 — The full build (Epics 0–10)

Built back-to-back on the `build/full-build` staging branch; `main`
untouched and nothing published — release remains the owner's act.
Per-epic detail lives in `docs/audits/`; decisions in `docs/adr/`
(0026–0037).

- **Orchestration & chat (Epics 0–1):** four divergent lines
  reconciled onto one staging branch; routing migrated to
  LangGraph's official `create_supervisor()` wrapped by Aether's
  approval pause, tool policy and memory briefing (8-test parity
  suite); fast chat, plan mode, in-graph blackboard, compaction,
  steering queue, per-agent models, session search/rename/titles,
  thinking stream, scratchpad. Fixed on the way (Epic 7 pass): fast
  chat was silently dead — plain chat never replied.
- **Tools & MCP (Epics 2–3):** built-in tools 7 → 18 with
  containment and argument/path-aware policy; approval cache; MCP
  risk overrides, catalogue cap with audited drops, end-to-end
  supervisor → MCP tool path against a fake server.
- **Memory (Epic 4):** switchable facade (native default; Mem0 /
  LangMem / Graphiti adapters), KG supersede, embedding stats +
  startup backfill, memory export.
- **Providers (Epic 5):** fallback barred after tool results or
  partial output; task-level fallback gated on requirement change;
  curated registry (`gemini-3.5-flash`); bounded key validation —
  hardened further after the Test button could wedge the app ~60 s
  (30 s budget, in-flight dedupe, background answer, UI spinner).
- **Desktop (Epic 6):** single-instance lock, tray, Alt+Space
  summon, window-state restore, notifications, 3-step onboarding,
  command palette, workspaces, health dashboard, settings search,
  config reset (secrets untouched).
- **Plugins & skills (Epic 7):** fail-closed tool hooks, TTSR
  stream rules, persisted skill disable that truly hides skills
  from agents, skill auto-match, plugin disable as real teardown.
- **Optimisation (Epic 9):** engine cold start 5.6 s → 1.1 s, RSS
  206 MB → 82 MB (deferred litellm import); embedding circuit
  breaker (degraded query 105 ms → 5.6 ms); entry bundle
  1,946 kB → 537 kB; production lint 398 → 0; reliability drills.
- **Hardening & packaging (Epic 10):** audit-log rotation (10 MB,
  5 archives); dependency sweep (pyasn1 fixes; filelock accepted
  with reasons, ADR 0037); hash-verified backup/restore; redacted
  diagnostics export; crash logs (engine + shell); report-only
  update check; real app/tray icons; electron-builder now packages
  the `electron/` helpers; version single-sourced at 0.3.5.
- **Known limitations:** the Windows installer walkthrough
  (install → onboard → uninstall on a fresh profile) awaits the
  owner's Windows pass; external memory backends run live only
  once their packages/services are installed (adapters are
  stub-tested).

---

## [2026-10-25]
### Phase 10.1 — Quick Wins (`bda0dbd`, branch `phase-10-1-quick-wins`)
#### Fixed
- **[2026-10-25] Plugin Install Could Never Succeed**: `pending_plugin_installs` was used but never defined (`NameError` swallowed on request, session dropped on grant/reject). Module-scope dict defined at `websocket.py:88`; install approval flow works (protocol-level verified; Electron card rendering manual-pending on Windows).
- **[2026-10-25] Interrupted Task Emitted No Terminal Outcome**: Interrupted runs ended with no outcome at all. New `EventType.TASK_INTERRUPTED` (`state=PENDING_APPROVAL, approval_key=thread_id`) is now emitted by both run and resume paths; `websocket.py` maps it to a `PENDING_APPROVAL` audit transition.
- **[2026-10-25] Knowledge-Graph Extraction Ran Only on Resumed Tasks**: Normal completion now calls `extract_and_store_facts` (in its own `try/except`, so a KG failure cannot block `TASK_COMPLETED`).
- **[2026-10-25] Provider Validation Ping Had No Timeout**: `validate_api_key` now bounded at 10s (injectable, litellm `timeout` kwarg + `asyncio.wait_for` backstop); a timeout returns `network_error` instead of hanging.
- **[2026-10-25] Embedding Failure Was Silent and Total**: `_embed_text` timeout 1.0s → 30.0s; every failure is audit-logged (`EPISODIC_EMBEDDING_FAILED`) and warned; `store_episode` deletes stale embedding rows so absence means "not embedded".
- **[2026-10-25] API Keys Stored in Plaintext in `localStorage`**: All three `aether_saved_key_*` writes/reads removed; keys go to the engine via `PROVIDER_SAVE_REQUEST` only, password field starts empty, `Configured` comes from engine `has_key`.
#### Added
- **[2026-10-25] ADR 0023**: `docs/adr/0023-phase-10-1-quick-wins.md` records the phase as a corrective record.
- **[2026-10-25] Tests**: 18 new pytest + 2 new Jest tests. Verified: pytest 239 passed / same 15 pre-existing failures as baseline; Jest 32 passed. Residuals: `test_episodic_memory_store_and_query` still fails (pre-existing audit-payload drift, not fixed here); kill-engine-mid-approval resume manual-pending on Windows.

### Phase 10.2 — Trust Boundary (`3d12af0`, branch `phase-10-2-trust-boundary`)
#### Fixed
- **[2026-10-25] Worker Was an Unauthenticated Backdoor**: `aether_worker` now verifies `AETHER_WORKER_TOKEN` (env, never argv) against `.run/engine.token` before any dispatch; missing/invalid → `WORKER_AUTH_REFUSED` audit, stderr JSON, exit 1, nothing executes. Unknown tools now raise instead of returning false-success `{"status":"noop"}`. Honest limit: a same-user process that can read the token file can present it — same trust basis as `/ws/tasks`.
- **[2026-10-25] No Wall-Clock Timeout; `assign_process` Failure Swallowed**: Worker execution now bounded by profile caps (instant 10s / quick 60s / standard 1800s / heavy 7200s) with kill + reap and a distinct `WORKER_EXECUTION_TIMEOUT` audit; `assign_process` failure now kills + reaps the spawned worker and raises `RuntimeError` with `WORKER_EXECUTION_REFUSED`.
- **[2026-10-25] Engine Token Exposed to Renderer and Sent in URL Query**: `get-auth-token` IPC + preload exposure deleted; main process connects with a plain WS URL + `Authorization: Bearer` header; token file is written atomically with mode 0600.
- **[2026-10-25] Plugin Install: Zip-Slip, Traversal, Unsigned Code**: `extractall` replaced with member-by-member `_safe_extract` (containment check); pre-write validation requires a root `manifest.json`, rejects absolute/`..`/backslash/NUL/symlink/non-regular members, allowlists members against `manifest.files`, and verifies SHA-256 per member.
- **[2026-10-25] Hardened `ToolRegistry.execute()` Was Dead**: `execute_tool_node` is now a thin adapter over `ToolRegistry.execute()` (policy → pre-flight → sandbox); the duplicated inline enforcement in the node is deleted.
- **[2026-10-25] Audit Gaps**: Redaction centralised in `AuditLogger._append` (secret values → `[REDACTED]`, payloads → key lists); `SECRET_LOADED` / `SECRET_DELETED` events added; `TOOL_EXECUTED` audit emitted for non-`READ_ONLY` tools.
#### Added
- **[2026-10-25] ADR 0024**: `docs/adr/0024-phase-10-2-trust-boundary.md` records the phase as a corrective record.
- **[2026-10-25] Tests**: 45 new pytest + 5 new Jest tests. Verified: pytest 284 passed / same 15 pre-existing failures; Jest 37 passed. Residuals: engine still *accepts* query tokens (follow-up); retired `aether_ui/ws_client.py` still sends `?token=`; no zip size / zip-bomb limit; audit-log hashing/rotation/fsync not built; kill-engine resume manual-pending on Windows.

### Added
- **[2026-10-25] Phase 10.3 A1 — Memory Pruning**: Retention policy in `UserConfig` (single source: episodic 90d / max 1000, knowledge graph 365d / max 5000, sessions 180d / max 200, configurable via `~/.aether/config.json`); `prune_episodes()` / `prune_facts()` / `prune_sessions()` + `memory/pruning.py:prune_all()` with per-tier audit events, wired as a weekly `memory_prune_weekly` scheduler job. Residual: APScheduler is declared in `requirements.txt` but not installed in the Linux venv, so the weekly job cannot fire in this environment.
- **[2026-10-25] Phase 10.3 A2 — Episodic Recall**: Semantic search now thresholded at cosine 0.3 (`EPISODIC_SIMILARITY_THRESHOLD`, configurable, `0.0` restores old behaviour); `store_episode` dedupes by `task_id`; one-time idempotent `dedupe_episodic_rows()` migration; bounded embedding backfill (5 per query).
- **[2026-10-25] Phase 10.3 A3 — Knowledge-Graph Dedupe/Confidence**: Dedupe key `(entity, fact)` casefolded, checked before entity upsert; `dedupe_facts()` migration keeps the highest-confidence copy and removes fact-less entities; fact ranking is now confidence, then recency.
- **[2026-10-25] Phase 10.3 B1 — Auxiliary-Model Contract Placebo Closed**: Only `vision` is an accepted auxiliary task (`AUXILIARY_TASK_CLASSES = {"vision"}`, single source in `config.py`); other tasks are rejected, stale persisted entries are pruned with an audit event, and `UserConfig.from_dict` filters stale placebo keys on load.
- **[2026-10-25] Phase 10.3 B2 — Reachable Models + Dead Router Deleted**: `filter_reachable_models` now returns `[]` with an error when nothing is reachable (it previously returned the full input list on total failure); dead `CapabilityRouter` deleted (239 lines) — the live path is `RoutingPolicy.select_route`. Residual: `scheduler/capability_jobs.py` does not yet read the new `.error` on the result.
- **[2026-10-25] Phase 10.3 C1 — One WebSocket Facade + Providers Array**: `SocketContext` is now the single facade (duplicate `App.jsx` subscription and 150ms dedup hack moved into it, fixing StrictMode double-delivery); `providers` is an array everywhere in the UI. Residual: `MODEL_DEFAULT_CHANGED` arriving before the first list response is still dropped (self-heals via `is_default` flags in the in-flight list response).
- **[2026-10-25] Phase 10.3 C2 — Dead/Placebo UI Removed or Wired**: `FloatingChat.jsx` deleted (86 lines, zero imports); theme buttons removed (a real theme is not honestly implementable — colours are hardcoded dark inline styles); LocalModelsTab "Active" wired via `MODEL_SET_DEFAULT`; tool-policy "default" option removed (no engine reset event exists — sending literal `"default"` would store junk; a true reset needs a new `TOOL_POLICY_RESET_REQUEST` event); `isMinimized` wired; dead `App.jsx` state removed.
- **[2026-10-25] Phase 10.3 D1 — Lockfile/CI**: `requirements.txt` gains previously undeclared `aiosqlite`, `pydantic`, `starlette`; `pywin32` now Windows-guarded (`sys_platform == "win32"`); new `.github/workflows/ci.yml` (Python 3.12 pytest + Node 24 Jest); `electron-builder` config added to `desktop_app/package.json`; stale `pytest_output.txt` deleted. Residuals: `npm run dist` untested (first real packaging run needs a human/Windows check); CI green not claimed (GitHub Actions cannot run here); `google-genai` + `langchain-mcp` declared but never imported (removal is a scope decision, not silently done).
- **[2026-10-25] Phase 10.3 D2 — Test Gaps**: 62 new pytest + 45 new Jest tests (audit logger, event bus, diagnostics, worker entrypoint, approval cards, slash commands, streaming merge, Dropdown, SocketContext). No production source touched.
- **[2026-10-25] Phase 10.3 D3 — Hygiene**: Changelog backfilled for Phase 9 through 10.3 (this file); `run_ui.py` now launches the Electron app instead of the retired PySide6 UI; commit-hook type list aligned with project standards; orphan one-shot scripts `refactor_app.py` / `update_settings.py` deleted (see Fixed below).

### Fixed
- **[2026-10-25] Phase 10.3 D3 — `run_ui.py` Launched the Retired UI**: `run_ui.py` imported `aether_ui.main.run_ui` (PySide6, deprecated 2026-09-20, Windows-only imports). It now launches the Electron app the way the repo documents it (`npm run electron:dev`; `--prod` builds once, then launches Electron on `dist/`; `--dry-run` prints the command). Verified on the Linux VM under Xvfb: vite ready on `localhost:5173` (HTTP 200) and the Electron main process running with the engine-down reconnect behaviour; the rendered window itself was not visually observed (headless VM), so a Windows visual check is still open.
- **[2026-10-25] Phase 10.3 D3 — Commit-Hook / Standards Type Mismatch**: `.husky/commit-msg.js` rejected `style:` (allowed by `.agents/rules/01-project-standards.md` §1, and used in real history) and accepted `ci:` (not in the standards). The hook now matches the standards exactly (`feat, fix, test, refactor, docs, chore, perf, style`); `ci` work uses `chore` ("changes to the build process or auxiliary tools", per the standards doc). Verified by running the hook against test messages of each type.
- **[2026-10-25] Phase 10.3 D3 — Destructive Orphan Scripts Deleted**: `refactor_app.py` (rewrote `aether_engine/app.py`, `state.py`, `orchestration/core.py`, `routes/websocket.py` from hardcoded `e:/AI projects/AETHER/...` paths and line-number slices — a re-run would corrupt the current files) and `update_settings.py` (rewrote `SettingsPanel.jsx` by line matching against a long-obsolete layout) were deleted after a repo-wide grep confirmed nothing imports or references them.


## [2026-10-10]
### Discovery v2 — Provider Model Discovery (5 commits, tip `596509c`, branch `phase-9-trust-boundary`)
#### Changed
- **[2026-10-10] Discovery Returns `DiscoveryResult`, No Fallback Lists**: `fetch_available_models` now returns `DiscoveryResult(models, excluded, error)` with real HTTP status codes; the hardcoded fallback model lists are removed (`3792806`, `6102589`).
- **[2026-10-10] Excluded Models Shown, Not Hidden**: Models filtered out of the display list appear in a provider-card "View all" expander, kind-tagged (embedding / image / audio / other), never in the chat HUD; the reachability ping runs only on the filtered list (`c938088`).
- **[2026-10-10] Excluded Models Persisted**: `provider_excluded_models` is now persisted per provider in config (`913ef12`).
- **[2026-10-10] Tests**: `596509c` covers excluded lists, real error codes, and the UI expander. Residual: the ping-step hang still has a pending bounded-timeout fix.

## [2026-10-08]
### Phase 9 — Trust-Boundary Hardening (6 commits, `de98b20..ce99048`, branch `phase-9-trust-boundary`)
#### Added
- **[2026-10-08] Auxiliary-Model + Reasoning-Effort Wiring**: New `AUXILIARY_MODEL_SET_REQUEST/RESPONSE` and `REASONING_EFFORT_SET_REQUEST/RESPONSE` contract events; persisted `auxiliary_models` / `reasoning_effort` in `UserConfig`; vision override honoured by the router; reasoning effort forwarded into LiteLLM calls only when non-default (`1c43096`, `ce99048`).
- **[2026-10-08] Test Integrity**: New Jest suites (`App`, `Projects`, `LocalModelsTab`, `DiagnosticsPanel`) and `tests/test_phase9_settings_wiring.py` with full WebSocket round-trips; placeholder `expect(true).toBe(true)` replaced with real assertions; `test_supervisor_integration.py` went from collecting zero tests to 5 real ones (`ed10971`).
- **[2026-10-08] ADR 0022**: `docs/adr/0022-phase-9-trust-boundary-hardening.md` records the phase as a corrective record.
#### Fixed
- **[2026-10-08] 7 Unauthenticated HTTP Routes Deleted**: `GET /plugins`, `POST /plugins/install`, `POST /plugins/confirm`, `DELETE /plugins/{name}`, `PATCH /plugins/{name}/toggle`, `GET /agents/nodes`, `PATCH /agents/nodes/{name}/toggle` — they bypassed the ADR 0003 token handshake. Plugin/agent management now goes only over the authenticated WebSocket (`515eca8`).
- **[2026-10-08] `ToolRegistry.execute()` Enforces the Full Pipeline**: Policy resolution → `validate_tool_request()` → worker-sandbox routing for non-`READ_ONLY` tools, instead of calling `tool.execute_fn()` directly.
- **[2026-10-08] Sandbox Fails Closed**: Resource-limit init failure now refuses execution (`RuntimeError`, `WORKER_EXECUTION_REFUSED`) instead of silently running unlimited.
- **[2026-10-08] Fallbacks Are Audit-Logged**: Per-call LiteLLM fallbacks emit `NODE_FALLBACK_TRIGGERED` to the audit log (was stdlib logger only), with node names passed through from coder/planner/researcher/supervisor.
- **[2026-10-08] Key Export Audits Before Writing**: `scripts/export_keys.py` emits `SECRETS_EXPORTED` before writing the plaintext file; an audit failure exits 1 and writes nothing.
- **[2026-10-08] Settings UI Placebos Removed**: Only the engine-backed Vision auxiliary dropdown is rendered (the other six had no engine seam); the dead llama.cpp mock download flow and the 30 fake "ambient" knowledge-graph nodes were removed; "Reset all to main" is wired (`ce99048`).
- **[2026-10-08] Latent `ImportError` Fixed**: Five modules imported `_create_provider_instance` from `aether_engine.app`; it lives in `orchestration/core.py`.
- **[2026-10-08] Verification**: 198 pytest passed (64 Phase 9 / directly-affected tests green); Jest 27 green. Residual: kill-engine-mid-approval restart/resume is manual-pending on Windows.

## [2026-09-30]
### Added
- **[2026-09-30] Dynamic Skills System**: Added `SkillManager` and `SlashRegistry` to dynamically load, index, and trigger local `.agents/skills` directly via Slash Commands in the chat UI.
- **[2026-09-30] UI Polish & Collapsible Sidebar**: Implemented a glassmorphic collapsible left sidebar with `emil-design-eng` aligned ease-out animation curves (`cubic-bezier(0.23, 1, 0.32, 1)`) and a responsive hamburger menu. Added a dynamic autocomplete slash command popup with custom scrolling.

### Fixed
- **[2026-09-30] Session Load Latency**: Eliminated extreme UI locking when switching chats by adding `CREATE INDEX IF NOT EXISTS idx_session_messages_session_id`, caching DB initialization state globally, and wrapping SQLite synchronous `session_store.py` calls in `asyncio.to_thread`.
- **[2026-09-30] WebSocket Initialization**: Fixed a bug where `SLASH_COMMAND_LIST_REQUEST` and `PLUGIN_LIST_REQUEST` were skipped on hot-reload/mount if the WebSocket was already connected.
- **[2026-09-30] Settings Scroll Cutoff**: Added `paddingBottom` to the Settings panel scroll container to ensure the deepest items are fully accessible.

---

## [2026-09-24]
### Added
- **[2026-09-24] Live Tool Activity UI & Event Bus**: Added global event bus `aether_engine/event_bus.py` and `TOOL_ACTIVITY` events to stream tool invocations, parameters, and outputs in real time. Updated `DraggableChatWindow.jsx` with glassmorphic, expandable tool cards with syntax-highlighted payloads.
- **[2026-09-24] Persistent Episodic Thinking History**: Added SQLite episodic thinking log table in `~/.aether/memory.db` and updated `session_store.py`, `executor.py`, and `DraggableChatWindow.jsx` to persist and restore execution pipelines and thinking steps across application restarts.
- **[2026-09-24] Intra-Provider Fallback Resilience**: Extended `_create_provider_instance` in `app.py` to automatically include healthy alternative models from the same provider (e.g. `gemini-2.0-flash` when using `gemini-2.5-flash`) before cross-provider candidates, ensuring single-provider setups have automatic retry resilience.
- **[2026-09-24] Project Guardrails & Git Hooks**: Initialized a root-level `package.json` and configured Husky pre-commit hooks. Commits are now strictly validated against the Conventional Commits format (`type(scope): description`) to prevent vague commit messages and enforce repository standards.
- **[2026-09-24] Mandatory AI Agent Rules**: Created `.agents/rules/01-project-standards.md`. All future AI agents operating in this repository are now strictly bound by rules that prohibit hallucinating changes, mandate frontend testing, and enforce the Conventional Commits format.
- **[2026-09-24] Frontend Test Infrastructure**: Installed `jest` and React Testing Library into the `desktop_app` Electron frontend. Authored the first smoke test (`Dashboard.test.jsx`), completely resolving the previous lack of UI test coverage and creating a safety net for future development.
- **[2026-09-24] ADR Directory Consolidation**: Cleaned up fragmented architecture documentation by moving all 19 floating ADRs from the root `ADR/` folder into `docs/adr/`. The redundant `ADR/` directory was deleted, establishing a single source of truth for design decisions.

### Fixed
- **[2026-09-24] Gemini Model Discovery & Quota Exhaustion (`gemini-3-pro-image`)**: Filtered out media generation endpoints (`image`, `transcribe`, `lyria`, `audio`) in `discovery.py` that have `limit: 0` on Google AI Studio free tier. Restored `gemini-2.5-flash` as primary default model.
- **[2026-09-24] Capability Misclassification & Route Hijacking**: Fixed `_infer_capabilities` and `select_route` in `aether_engine/routing/` to reject non-chat models, grant `code` to frontier LLMs, and prioritize the user's active/default model instead of diverting to media endpoints.
- **[2026-09-24] Thinking Panel Deduplication & Scrolling Physics**: Deduplicated supervisor progress text and redundant tool cards in the thinking panel, and synchronized scrolling physics with the dropdown list.
- **[2026-09-24] LiteLLM MidStream Fallback Error Handling**: Handled `litellm.MidStreamFallbackError` and retriable quota exhaustion errors during stream execution in `litellm_provider.py` to trigger fallbacks instead of failing the task.
- **Settings API Provider Caching**: API keys are now securely cached in the browser `localStorage` upon connection. If a key is disconnected from the backend, the UI text box remains pre-filled, saving users from needing to re-enter it manually.
- **Model Disconnect Bug**: Fixed a UI bug in `App.jsx` where disconnecting a provider or saving a new configuration failed to instantly refresh the available model dropdown.
- **Capability Auto-Router Priorities**: Fixed the backend `capability_router.py` logic which was ignoring the user's "Global Default Model" choice. The tiebreaker score was increased from `+1.0` to `+100.0`, ensuring standard chats always respect the user's choice instead of silently falling back to OpenRouter.
- **LiteLLM Missing Dependency**: Installed the `google-generativeai` package to resolve unexpected crashes when trying to connect and route to Gemini APIs via LiteLLM.

---

## [2026-09-23]
### Added
- **Auto-Router**: Added a capability-based task auto-router and the main desktop application UI components.
- **[2026-09-23 04:22] Environment Diagnostics Engine**: Replaced the static UI mockup in the Settings page with a live, dynamic capability-check runner. Pressing "Run Diagnostics Now" triggers a true backend test for the Python environment, terminal access, SQLite read/write permissions, local Ollama daemon connectivity, and `TAVILY_API_KEY` verification, with live results rendering directly into the UI!
- **[2026-09-23 03:53] Auto-Title Generation**: Newly created conversations (previously generic "New Conversation") are now automatically renamed to a brief summary of the first user prompt sent in the session.

### Fixed
- **[2026-09-23 04:35] Diagnostics Loading State**: Added a dynamic loading state to the "Run Diagnostics Now" button in the Settings UI so that users receive immediate visual feedback (loading spinner, disabled state) while the backend tests are running.

## [2026-09-22]
### Added
- **Memory Graph**: Added single-fact, entity, and full-clear deletion controls to Knowledge Graph UI and backend.
- **Episodic Memory**: Added episodic deletion controls, context menu, and draggable text selection. Implemented autonomous supervisor memory orchestration (ADR 0039).
- **Hybrid Retrieval**: Implemented RRF hybrid retrieval with local SQLite embeddings database. Added ADR 0020 for documentation.
- **Workspace UI**: Added true workspace folder persistence and dialog selection to Projects view.
- **Local Models**: Fetch local model catalog from JSON, added destination path & download cancellation.
- **Metrics**: Wired Dashboard metrics to real WebSocket status, task counts, plugin counts, and memory IPC.

## [2026-09-21]
### Changed
- **Styling**: Updated global glassmorphism tokens, window controls, and ambient animations across secondary tabs.
- **Chat Window**: Polished floating window layout, optimized margins for native resizing, and implemented seamless session state transfer via localStorage.
- **Electron Migration**: Added Electron desktop application with React frontend and migration documentation.

### Fixed
- **Electron Backgrounds**: Removed OS-level vibrancy from floating window to prevent ghost shadow backgrounds.
- **Dropdown UX**: Implemented smooth lerp scrolling, sticky headers, and fixed overflow/drag glitches.

## [2026-09-20]
### Changed
- **Architecture**: Deprecated old Python-based UI components in favor of new Electron/React desktop app.
- **Backend API**: Updated API to support the new React frontend and advanced model providers.
- **Memory Subsystem**: Added ADRs and tests for the memory subsystem.

## [2026-09-18]
### Added
- **Plugins**: Implemented plugin installer, uninstaller, and manifest validation tests.
- **LangGraph Nodes**: Updated nodes, routing, and provider discovery mechanisms.

### Fixed
- **Settings State**: Improved settings tabs, provider state, and secret storage handling.

## [2026-09-16]
### Added
- **Core Architecture**: Implemented complete Aether application architecture including engine, UI, orchestration, and test suite.
- **UI Tabs**: Implemented plugins, agents, and skills tabs; fixed hardcoded model dropdown.

### Fixed
- **Model Discovery**: Disabled exhaustive reachability sweeps to speed up discovery.
- **Routing Loop**: Resolved infinite delegation loops and model discovery 404 errors.
- **Provider UX**: Improved provider config dialog UX with loading states and proper validation routing.
