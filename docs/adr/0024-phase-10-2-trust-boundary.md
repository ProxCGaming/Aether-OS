# ADR 0024 — Phase 10.2 Trust Boundary (Part 2)

- Status: Accepted
- Date: 2026-10-25
- Branch: `phase-10-2-trust-boundary`, from `phase-10-1-quick-wins` @ `bda0dbd`
- Context: Phase 9 hardened the documented trust boundary; the critical audit (video-docs, 25 Oct 2026) found holes *behind* it. This phase closes them. All file:line references were re-verified on `main` @ `7811318` before fixing; prompts were written against `phase-9-trust-boundary` @ `596509c`.
- Baseline (main @ 7811318): pytest 221 passed / 15 failed / 3 skipped / 2 errors; Jest 30/30. Phase 10.1 added 18 pytest + 2 Jest (239 / 32).

## Fixes

### A1. Worker was an unauthenticated backdoor — Option 2, token auth
- Issue: `aether_worker/__main__.py:11` dispatched any `--payload` JSON; `read_token` imported but never called. Reproduced: unauthenticated `write_file` wrote a file, exit 0.
- Fix (Option 2 chosen): worker verifies `AETHER_WORKER_TOKEN` (env, never argv) against `.run/engine.token` via `verify_token` before any dispatch; missing/invalid → `WORKER_AUTH_REFUSED` audit, stderr JSON, exit 1, nothing executes. Unknown tool now raises `ValueError` instead of false-success `{"status":"noop"}`. `sandbox.py:execute_tool_in_worker` reads the token after Job Object construction and passes it via env, fail-closed if unavailable. Worker audit is a direct JSONL append in `AuditLogger.log_event` shape — deliberate: importing `aether_engine.audit` costs ~6s + a LiteLLM remote fetch per spawn.
- Why not Option 1: worker has no access to policies/workspace roots/approval state, so the engine would have to pass them inside the argv payload — self-asserted and forgeable. Option 1 cannot distinguish engine-approved calls from an attacker's.
- Honest limit: a same-user process that can read `.run/engine.token` can present it — same trust basis as `/ws/tasks`. True privilege separation (different/low-privilege worker user) is out of scope.
- Test: `tests/test_phase10_2_worker_auth.py` (5 tests). Manual: no-token / wrong-token exit 1, valid-token exit 0, unknown-tool exit 1.

### A2. No wall-clock timeout; `assign_process` failure swallowed
- Issue: `sandbox.py:execute_tool_in_worker` awaited bare `proc.communicate()`; Job Object `PerJobUserTimeLimit` is CPU user-time only, so a sleeping worker is never killed. `assign_process` failure was swallowed at the call site in `sandbox.py` (`except Exception: pass`) — `job_object.py:assign_process` itself logs and re-raises (audit citation corrected).
- Fix: `asyncio.wait_for(proc.communicate(), timeout=time_cap)` with profile caps instant 10s / quick 60s / standard 1800s / heavy 7200s; `proc.kill()` + reap on timeout, distinct `WORKER_EXECUTION_TIMEOUT` audit. `assign_process` failure now kills + reaps the just-spawned worker and raises `RuntimeError` with `WORKER_EXECUTION_REFUSED`.
- Test: `tests/test_phase10_2_worker_timeout.py` (4 tests). Pre-fix: sleeping worker ran full 10s and succeeded; refused worker wrote the file.
- Caveats: Job Object path only faked on Linux; grandchild processes are not killed with the worker; pre-spawn assignment (`CREATE_SUSPENDED`) is a separate change.

### B1. Engine token exposed to renderer, sent in URL query, written plaintext
- Issue: `desktop_app/main.cjs:172` `get-auth-token` IPC handed the full token to the renderer (`preload.cjs:8`); token sent as `?token=` (`main.cjs:24`); `aether_common/auth.py:11` wrote it plaintext with no chmod.
- Fix: `get-auth-token` IPC + preload exposure deleted; `main.cjs:connectEngine` now uses plain WS URL + `Authorization: Bearer` header (ws@8.21.3 headers verified). No React code ever called `getAuthToken`. `auth.py:write_token` now atomic (mkstemp 0600 → fsync → `os.replace` → re-chmod 0600), verified 0600 on Linux. Engine already accepted header tokens (`_extract_token` fallback) — `websocket.py` not touched, so the engine still *accepts* query tokens (follow-up). Retired `aether_ui/ws_client.py:59` still sends `?token=` — out of scope.
- Windows caveat: `os.chmod(path, 0o600)` only toggles the read-only flag; a truly owner-only ACL would need `win32security`/`icacls`. Token file relies on the default per-user ACL of the profile directory on Windows.
- Test: `tests/test_phase10_2_token_header.py` (11 tests) + Jest `preload-no-token.test.js` (5 tests). Jest 37 passed.

### B2. Plugin install: Zip-Slip, traversal, unsigned code
- Issue: `plugins/installer.py:84` `z.extractall(target_dir)` with no sanitisation; manifest/skill names unvalidated; no hash/signature mechanism existed anywhere (grep: no `sha256`/`signature` in `aether_engine/` + `aether_common/`). Repro: manifest `"name": "../../escape"` installed outside the plugins directory.
- Fix: `extractall` replaced with member-by-member `_safe_extract` (resolve containment check). `_validate_zip_archive` runs before any write/backup: root `manifest.json` required, rejects absolute/`..`/backslash/NUL/symlink/non-regular members, allowlists every member against `manifest.files`, SHA-256 (`hmac.compare_digest`) per member, empty `files` map refused as unsigned. `manifest.py` Pydantic validators: names `^[A-Za-z0-9][A-Za-z0-9._-]*$`, `file_path` relative only. Refusal = `ValueError` + `PLUGIN_INSTALL_REFUSED` audit. Supervisor addition: `state.py:91` passes `audit_logger=self.audit_logger` into `PluginInstaller` (worker left production refusals unaudited).
- Hash/trust model: SHA-256 manifest hash check, no PKI invented. `files` is a publisher-declared integrity manifest — detects post-packaging tampering, does **not** authenticate the publisher; the trust gate for *who* may install remains the user's explicit approval (Phase 10.1 A1 flow). Zip-only: directory installs need no hashes, existing fixtures unchanged.
- Test: `tests/test_phase10_2_plugin_zipslip.py` (14 tests). Pre-fix: 12 failed / 2 passed; post-fix 24 passed (with plugin tests).
- Follow-up (not done): no zip size / zip-bomb limit.

### C1. Hardened `ToolRegistry.execute()` was dead; live path duplicated it
- Issue: `tools/registry.py:180` `execute()` (Phase 9 pipeline) had no production caller; `langgraph/nodes/execute_tool.py:23` duplicated enforcement inline and called the worker with no `audit_logger`, so the live path produced zero `WORKER_*` audit events.
- Fix: `execute_tool_node` is now a thin adapter over `ToolRegistry.execute()`; inline policy/pre-flight/worker dispatch deleted. Preserved: per-specialist `agent_tools` allow-list, tool-message return shape, exact denial/approval/validation wording (translated from `ToolExecutionError`). `tools/registry.py` needed no change. No per-tool exceptions.
- Notes: there is no `WORKER_EXECUTION_STARTED` event in production code (only an enum value in `contracts.py`); the unified pipeline emits `WORKER_EXECUTION_COMPLETED` / `TERMINATED` / `TIMEOUT` / `REFUSED` plus `TOOL_BLOCKED_BY_POLICY`. The old node's `TOOL_BLOCKED_BY_POLICY` event carried an `agent` field; the registry's does not — candidate one-line follow-up (optional `agent` kwarg on `execute()`).
- Test: `tests/test_phase10_2_single_pipeline.py` (5 tests). Pre-fix 3 failed; post-fix 42 passed (with registry/permissions/langgraph/Phase 9 tests).

### C2. Audit gaps — key-reveal / destructive actions unaudited, plaintext payloads
- Issue: `secrets/storage.py:load_provider` had no audit call (websocket reveal path logged nothing); approved `delete_file` through `ToolRegistry.execute()` produced no registry audit; `AuditLogger.log_event("APPROVAL_REQUESTED", {"args": ...})` and `log_task_transition(..., full payload)` wrote caller dicts verbatim — secret values present in `logs/audit.log` in repro.
- Fix (bounded): redaction centralised in `audit.py:_append` (every `log_*` funnels through it) — sensitive-key values → `"[REDACTED]"`, `args`/`payload`/`manifest` → `{"keys": [...]}`, `content`/`command` → `"[REDACTED]"`, narrow bare-string secret patterns scrubbed (`sk-…`, `AKIA…`, `ghp_…`, `xox…`, `BEGIN … PRIVATE KEY`). `SecretStore` now emits `SECRET_LOADED` (provider only) on successful decryption and `SECRET_DELETED` on row deletion (failed loads emit nothing; audit is best-effort). `ToolRegistry.execute` emits `TOOL_EXECUTED` {tool_name, risk, arg_keys only} after successful non-`READ_ONLY` execution; `READ_ONLY` tools emit nothing. `scripts/export_keys.py` already audited `SECRETS_EXPORTED` correctly — no change.
- Test: `tests/test_phase10_2_audit_gaps.py` (6 tests). Pre-fix 5 failed; post-fix 22 passed (with secret-store/registry tests).
- Not built (follow-up): audit-log hashing/hash-chain, rotation, fsync, locking, sequencing, `0600`. `SECRET_LOADED` volume from routine internal loads may warrant a reveal-vs-internal distinction later.

## Out of scope / deferred
- Phase 10.3 (memory, providers, UI, build/tests) — separate phase.
- Deferred decisions (owner only): real MCP client, specialist→specialist handoff, knowledge-graph edges, audit-log hashing/rotation rebuild, true sandbox isolation.

## Skills used
- Local `~/workspace/claude-skills` only, as read-only guides: `security-pen-testing` (OWASP / API security / secret-scanning), `code-reviewer` (SKILL.md + `rules/universal.md` + `languages/python.md` / `typescript.md`). No online fetch, nothing copied into the repo, no skill added as a dependency.
