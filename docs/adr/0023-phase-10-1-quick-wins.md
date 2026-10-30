# ADR 0023 — Phase 10.1 Quick Wins

- Status: Accepted
- Date: 2026-10-25
- Branch: `phase-10-1-quick-wins`, from `main` @ `7811318`
- Context: Critical audit (video-docs, 25 Oct 2026) found ~171 findings, 39 High. Phase 10.1 fixes the six outright breaks / hangs / leaks that were smallest and safest. Prompts were written against `phase-9-trust-boundary` @ `596509c`; all work was rebased to `main` @ `7811318` and every file:line was re-verified on main before fixing.
- Baseline (main @ 7811318, Linux VM): pytest 221 passed / 15 failed / 3 skipped / 2 errors (ignoring 4 PySide6 collection-error files); Jest 30/30.

## Fixes

### A1. Plugin install can never succeed — `pending_plugin_installs` undefined
- Issue: `aether_engine/routes/websocket.py:1153,1170,1188` used `pending_plugin_installs` with zero definitions repo-wide. Install request swallowed the `NameError` inside a `try` (returned `success=False`, never reached approval); grant/reject `pop` outside a `try` propagated to the outer WS `except` and dropped the session.
- Fix: module-scope `pending_plugin_installs: Dict[str, Dict[str, Any]] = {}` at `websocket.py:88`, keyed by request id, holding the `prepare_install()` payload. No use site renamed.
- Test: `tests/test_phase10_1_plugin_install.py` (5 tests: defined, approval request, grant installs, reject cancels + session survives, unknown grant id no session drop). Pre-fix 5 failed; post-fix relevant set 10 passed.
- Note: verified at WebSocket protocol level only; Electron approval-card rendering is manual-pending on Windows.

### A2. Interrupted task emitted no terminal outcome
- Issue: audit claimed an `UnboundLocalError` on `elapsed_ms` (`:364` vs `:388`). That did **not** reproduce on main — `executor.py:137-138` already initialises `start_time`/`elapsed_ms=0.0` before the loop, and `TASK_COMPLETED` sits inside `if not interrupted:`. The live defect was different: an interrupted run emitted `['TASK_CREATED', 'TOOL_APPROVAL_REQUEST']` and then ended with no terminal outcome at all.
- Fix: new `EventType.TASK_INTERRUPTED` in `aether_common/contracts.py` (+1; no `SCHEMA_VERSION` bump — additive enum member, envelope shape unchanged), paired with existing `TaskState.PENDING_APPROVAL`. `aether_engine/langgraph/executor.py` `run_langgraph_task` and `resume_langgraph_task` emit `TASK_INTERRUPTED` (`state=PENDING_APPROVAL, approval_key=thread_id, latency_ms`) on the interrupted branch, never `TASK_FAILED`/`TASK_COMPLETED`; resume path also inits `elapsed_ms=0.0`. `aether_engine/routes/websocket.py:180-184` maps `TASK_INTERRUPTED` → `PENDING_APPROVAL` in the audit transition (supervisor addition — the worker's fix forwarded the event but did not audit it).
- Test: `tests/test_phase10_1_interrupted_task.py` (3 tests: run interrupted, resume interrupted, normal completion still `TASK_COMPLETED`). Pre-fix 3 failed; post-fix relevant set 16 passed.
- Note: kill-engine-mid-approval resume is manual-pending on Windows (checkpoint/thread untouched).

### A3. Knowledge-graph extraction ran only on resumed tasks
- Issue: `executor.py` `resume_langgraph_task` (~:551) called `knowledge_graph.extract_and_store_facts(...)`; `run_langgraph_task` normal completion called only `episodic.store_episode(...)`. Normal completion left zero KG facts.
- Fix: normal-completion branch now calls `extract_and_store_facts(provider, user_prompt=prompt, assistant_response=full_response, task_id)` in a separate `try/except` (same warning shape as resumed path), so a KG failure cannot block `TASK_COMPLETED`. No dedup/pruning (Phase 10.3).
- Test: `tests/test_phase10_1_kg_extraction.py` (1 test: normal completion → ≥1 KG fact). Pre-fix 0 facts; post-fix passes.

### B1. Provider validation ping had no timeout
- Issue: `aether_engine/providers/litellm_provider.py:validate_api_key` called `litellm.acompletion` with no `timeout` anywhere in the file; `orchestration/core.py:_validate_provider_key` awaits it up to 5 times sequentially.
- Fix: `VALIDATION_TIMEOUT = DISCOVERY_TIMEOUT = 10.0s`, injectable `timeout` param, enforced via litellm `timeout` kwarg + `asyncio.wait_for` backstop. Timeout returns `{"status":"network_error","code":None,"message":"Validation timed out after …"}`; generic `except` also gains `"code":None`. `call_stream` not touched — separate call site, streaming legitimately runs >10s.
- Test: `tests/test_phase10_1_ping_timeout.py` (5 tests, mocked hangs, ~0.05s each). Pre-fix hanging mock blocked >5s; post-fix 31 passed (with discovery/provider tests).

### B2. Embedding failure was silent and total
- Issue: `aether_engine/memory/episodic.py:_embed_text()` used `timeout=1.0`, hardcoded `ollama/nomic-embed-text`, bare `except: return None`, zero logging; `store_episode()` left stale embedding rows in place.
- Fix: `EMBEDDING_TIMEOUT_SECONDS = 30.0` (not 10s — a cold Ollama start routinely exceeds 10s), `EMBEDDING_MODEL` named constant. Every failure logged distinctly via existing audit logger (`EPISODIC_EMBEDDING_FAILED` {task_id, context, model, error, error_type}) + module warning. `store_episode()` deletes any stale embedding row on failure, so absence from `episodic_embeddings` is a truthful "not embedded" marker and the existing `EPISODIC_MEMORY_STORED` audit records `embedded=False`. Query path also passes `context="query"`. No backfill / similarity threshold (Phase 10.3).
- Test: `tests/test_phase10_1_embedding_failure.py` (4 tests). Pre-fix 4 failed; post-fix 5 passed (with KG tests).
- Note: pre-existing `tests/test_episodic_memory.py::test_episodic_memory_store_and_query` still fails (audit payload drift — code logs `{task_id, tags, embedded: False}`, test asserts `{task_id, tags}`; critical-doc UNKNOWN item 20). Left untouched as pre-existing; supervisor cleaned 9 `task-embed-*` test rows the worker wrote into real `~/.aether/memory.db`.

### C1. API keys stored in plaintext in `localStorage`
- Issue: `desktop_app/src/components/Settings/ProviderSettings.jsx:83` and duplicate `desktop_app/src/SettingsPanel.jsx:328` wrote `aether_saved_key_<provider>`; `SettingsPanel.jsx:298` pre-filled from it.
- Fix: all three removed. Keys go to the engine via `PROVIDER_SAVE_REQUEST` only; password field starts empty on every load; `Configured`/`Not Configured` comes from engine `has_key`. Whole `desktop_app/src/` audited — no other secret `localStorage` writes (remaining writes: download dir, `reasoning_effort`, `floating_transfer_state` — non-secrets). No replacement store introduced.
- Test: `ProviderSettings.test.jsx` +2 tests (save performs no key-like `localStorage.setItem`; password starts empty even with a stale entry seeded). 7 passed in that file.
- Note: `SettingsPanel.jsx` dead `keys` state/handlers remain (Phase 10.3 C2 scope).

## Out of scope / deferred
- Phase 10.2 (worker backdoor, token exposure, Zip-Slip, single tool pipeline, audit gaps) and Phase 10.3 (memory pruning/dedup, auxiliary placebos, WS facade, lockfile/CI, test gaps) — separate phases.
- Deferred decisions (owner only): real MCP client, specialist→specialist handoff, knowledge-graph edges, audit-log hashing/rotation rebuild, true sandbox isolation.

## Skills used
- Local `~/workspace/claude-skills` only, as read-only guides: `code-reviewer` (SKILL.md + `rules/universal.md` + `languages/python.md` / `typescript.md`), `security-pen-testing` (secret-scanning sections, C1). No online fetch, nothing copied into the repo, no skill added as a dependency.
