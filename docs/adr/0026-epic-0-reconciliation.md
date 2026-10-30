# ADR 0026 — Epic 0 Reconciliation: One Staging Line, Gates Never Waived

- Status: Accepted
- Date: 2026-10-30
- Branch: `build/full-build`, from `main` @ `7811318`
- Context: Four divergent lines had accumulated: `commit-rules-tracer` (the owner's
  commit/test/dependency rule system, local-only), the Phase 10 chain
  (`phase-10-1/2/3`, local-only), and `mcp-001-tracer` (real MCP client, also on
  GitHub). All forked from `main` @ `7811318`. The owner directed (30 Oct 2026):
  reconcile onto a **staging branch** (local `main` stays untouched until he blesses
  the whole build; nothing is pushed without his separate word), Muse is lead
  builder, and the new setup rules (commitlint, hooks, uv-only, test gates) are
  **binding on the reconciliation itself** — no `--no-verify`, no waived gates.
  Full evidence: `docs/audits/epic0-gap-report.md`.

## Decisions

### D1. Merge order: rules first, then Phase 10, then MCP
`commit-rules-tracer` → Phase 10 chain → `mcp-001-tracer`. The rule system lands
first so every later merge is checked by it; MCP lands last so its tools arrive on
top of Phase 10.2's secured `ToolRegistry.execute()` pipeline. Merge commits:
`706eace`, `34fd310`, `6180533`.

### D2. Modify/delete conflicts resolve for the rule system
`requirements.txt`, `requirements-lock.txt`, and the old hand-rolled
`.husky/commit-msg.js` stay **deleted** wherever a feature branch modified them:
uv (`pyproject.toml` + `uv.lock`) is the only dependency system, and commitlint
replaces the hand-rolled hook. The deleted files' unique content was audited first
— everything real (aiosqlite, the LangGraph pins, `mcp>=1.30.0`) already lives in
`pyproject.toml`. One truth the audit surfaced: **pydantic was imported directly by
5+ engine modules but declared only transitively**; it is now a direct dependency
(`151df57`).

### D3. The single tool pipeline survives the MCP merge (highest-stakes conflict)
`execute_tool.py` took the Phase 10.2 side wholesale: the node stays a thin adapter
over `ToolRegistry.execute()`. The MCP branch's inline policy/pre-flight/sandbox
duplication was **not** revived. The one MCP behaviour that had to survive — MCP
tools execute **in-process**, because the sandbox worker cannot hold MCP sessions
(MCP PRD Q4) — already existed inside the MCP branch's own `registry.py` Step 3 and
survives in the merged registry, *after* the shared policy and pre-flight steps.
Net effect: MCP tools now get the central policy gate, pre-flight validation and
audit trail, with session-holding execution where it belongs. Both guard suites
pass: `test_phase10_2_single_pipeline.py`, `test_mcp_websocket_handlers.py`.

### D4. Installer: union of both security reworks, fail-closed wins ties
`plugins/installer.py` had been rewritten by both branches for different security
reasons (Phase 10.2: zip/hash/path hardening; MCP: validate plugin-declared MCP
servers before install). Resolution is a union: both pre-approval validations run
(zip first, then MCP configs), and where the two sides genuinely conflicted, the
fail-closed behaviour won — notably dropping the old silent
`{"command": "dummy"}` server substitution (issue 008: never a silent dummy
server), and using `remove_server_sync` for rollback/uninstall because the merged
registry's `remove_server` is async.

### D5. Gates are complied with, never waived — including by the reconciliation
- The pre-commit Jest-existence rule blocked the Phase 10 merge because
  `DraggableChatWindow.jsx` had no test. The fix was writing the test
  (`dd03648`, 36 tests, all exports covered), committed separately **before** the
  merge so the history stays honest.
- The MCP branch had never lived under Ruff: its code arrived with **311
  violations**. 244 were mechanical (typing modernisation, unused imports) and
  auto-fixed; the remaining ~74 were judgement calls fixed by hand under one
  doctrine: **narrow the exception where the failure modes are knowable; where a
  broad catch is a deliberate boundary (socket handlers, MCP graceful failure,
  atomic rollback), keep it, log it, and mark it `# noqa: BLE001` with the reason
  written next to it.** Silent swallows (S110) were eliminated — every one now
  logs. Final state: `ruff check` on all merged files = 0.

### D6. One test updated for merged error vocabulary
`test_install_rejects_bad_server_name` expected `MCPConfigError` for a bad MCP
server name; in the merged tree the hardened manifest validator rejects the name
earlier, as a pydantic `ValidationError`. The test now accepts either error. The
security contract is unchanged (rejected fail-closed, before approval, nothing
installed) — only the vocabulary that reports it moved. The sibling test in the
same file already used the same either-error pattern.

## Baseline (measured, this branch)
| Stage | pytest | Jest |
|---|---|---|
| commit-rules content (start) | 259 passed / 16 failed / 2 skipped | 30/30, 10 suites |
| after Phase 10 merge | 428 passed / 15 failed / 2 skipped | 146/146, 20 suites |
| after MCP merge (`6180533`) | **533 passed / 15 failed / 2 skipped** | **159/159, 20 suites** |

The 15 failures are the known pre-existing families (startup validation,
workspace containment), unchanged at every step. Zero new failures.

## Residual / carried forward
- MCP issue 002 (owner's 21st.dev key) and issue 005 (owner's risk-override UI
  review) remain owner-gated; the owner's Windows QA of the MCP demos is still his.
- Commit-rules issue 006 still needs the owner on Windows + the GitHub main ruleset.
- The §9 standard-software checklist is applied as acceptance criteria across
  Epics 1–10 (owner's decision), and the full gap addendum (owner: "add all the
  gaps") is planned work assigned per-Epic in the build plan.
