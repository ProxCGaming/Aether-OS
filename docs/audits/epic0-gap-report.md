# Epic 0: Aether-OS Audit, Baseline, and Gap Report

**Date:** 2026-10-30  
**Environment:** Linux (x86_64)  
**Base Branch:** `commit-rules-tracer` (`ea6d04577c857d1ef9acb60bda43eb351522345b`) / `orch/agy-epic0-audit`  
**Deliverable Status:** Analysis & Baseline (Epic 0) — completed 30 Oct 2026 (baseline, reconciliation record, and §9 disposition added)

---

## 1. Branch Map

Audit of all local and remote branches (`git branch -a`, `git log --oneline -3 <branch>`, and `git merge-base` against `main` at commit `7811318`).

| Branch | Tip Commit | Commits Ahead of `main` | Merged into `main`? | What it contains that `main` does not |
|---|---|---|---|---|
| `main` | `7811318` | 0 | Baseline trunk | Baseline trunk commit (`Merge remote-tracking branch 'origin/main' into main`). |
| `commit-rules-tracer` | `ea6d045` | 6 | **No** | Python dependency migration to `uv` (`pyproject.toml` + `uv.lock`), commitlint enforcement (Conventional Commits), fast pre-commit hooks (Ruff, oxlint, related Jest, file size check), Gitleaks secret protection, and `.husky/pre-push` baseline-aware test runner. |
| `mcp-001-tracer` | `bf32165` | 9 | **No** | Production-grade MCP client with Streamable HTTP and stdio transports, server lifecycle management in Settings UI, auto-connect with fail-fast status, MCP resource/prompt template injection into chat, and plugin MCP server validation. |
| `phase-10-1-quick-wins` | `bda0dbd` | 1 | **No** | Critical quick wins (ADR 0023): defined `pending_plugin_installs` route, emitted `TASK_INTERRUPTED` on approval pause, KG extraction on normal completion path, 10s timeout on API key validation, 30s embedding timeout, and removal of plaintext API keys from localStorage. |
| `phase-10-2-trust-boundary` | `3d12af0` | 2 | **No** | Trust boundary closures (ADR 0024): engine token authentication for worker process, sandbox wall-clock execution timeouts, header-only WebSocket authentication (removed `get-auth-token` IPC), Zip-Slip safe plugin archive extraction, unification of tool execution through `ToolRegistry.execute()`, and central secret redaction in audit logs. |
| `phase-10-3-correctness` | `8f5fc88` | 3 | **No** | Correctness fixes in memory, providers, UI, and build (ADR 0025): memory pruning & retention policies, episodic deduplication/backfill, single WebSocket facade in `SocketContext`, deletion of dead `CapabilityRouter` & `FloatingChat`, auxiliary models contract fix, electron-builder packaging config, and CI workflow. |
| `phase-9-trust-boundary` | `98128aa` | 0 | **Yes** | Fully merged into `main` (commit `98128aa` is an ancestor of `7811318`). Contains Phase 9 trust-boundary hardening, provider view-all expander, coded discovery errors, and LangGraph 1.2.16 lock. |
| `orch/agy-epic0-audit` | `ea6d045` | 6 | **No** | Active isolated audit worktree branch tracking `commit-rules-tracer`. |

### Remote Branches Summary
- `origin/main`: Identical to local `main` (`7811318`).
- `origin/mcp-001-tracer`: Identical to local `mcp-001-tracer` (`bf32165`).
- `origin/phase-9-trust-boundary`: Ancestor of `main` (`596509c`, merged).
- `origin/feat/agent-skills-spec`: 0 commits ahead of `main` (merged).
- `origin/feature/phase-8-skills-and-optimizations`: 0 commits ahead of `main` (merged).
- `origin/feature/ui-fixes-and-hybrid-retrieval`: 0 commits ahead of `main` (merged).
- `origin/ui-overhaul`: 0 commits ahead of `main` (merged).

### Branch Topology & Divergence Analysis
All unmerged work originates from `main` (`7811318`):
1. **Branch 1 (`commit-rules-tracer`):** Forked at `7811318`. Replaces `requirements.txt` with `uv` (`pyproject.toml` + `uv.lock`) and establishes the pre-commit/pre-push developer guardrails.
2. **Branch 2 (`phase-10-1` -> `phase-10-2` -> `phase-10-3`):** Forked at `7811318` as a linear chain of 3 commits (`bda0dbd` -> `3d12af0` -> `8f5fc88`). Contains 49 modified/added files including core memory, plugin, worker sandbox, and UI fixes.
3. **Branch 3 (`mcp-001-tracer`):** Forked at `7811318`. Contains 9 commits implementing the real MCP subsystem.

### Proposed Reconciliation & Merge Order (For Owner Review)
*Note: Per MASTER-PROMPT §8 Epic 0 and BRIEFING.md, no merges are executed during this task.*

1. **Step 1: Merge `commit-rules-tracer` into `main`**
   - *Rationale:* Establishes `uv` as the single dependency source of truth (`pyproject.toml` + `uv.lock`), commitlint, and test guardrails. Eliminates legacy `requirements.txt` confusion before merging complex application logic.
   - *Risks:* Low. Minimal changes to application source code (primarily tooling, configs, and scripts).
2. **Step 2: Rebase / Merge `phase-10-3-correctness` (which includes 10-1 and 10-2)**
   - *Rationale:* Incorporates critical trust-boundary, security, memory, and UI bug fixes. Must be adapted to use `uv run` and respect the newly merged hook rules.
   - *Risks:* Moderate. Touches `aether_common/contracts.py`, `aether_engine/memory/`, `aether_engine/routes/websocket.py`, and `desktop_app/src/App.jsx`. Requires verifying that `phase-10-3` tests conform to `commit-rules-tracer`'s hook checks.
3. **Step 3: Rebase / Merge `mcp-001-tracer`**
   - *Rationale:* Adds MCP client features on top of a secured and corrected engine core.
   - *Risks:* Moderate. Needs verification against `phase-10-2`'s single execution pipeline (`ToolRegistry.execute()`) to ensure MCP tools are routed through policy, pre-flight, and sandbox without bypass.

---

## 2. Test baseline (measured 30 Oct 2026)

Measured on the `commit-rules-tracer` content (the base the reconciliation starts from):

- **pytest: 259 passed / 16 failed / 2 skipped.** The 16 failures are the branch's known pre-existing failures — the startup-validation and workspace-containment tests are among them — and are the same failures the branch's own pre-push baseline already tracks.
- **Jest: 30/30 passed across 10 suites.**

The baseline was measured in the audit worktree and re-confirmed identical on the staging branch after merge step 1 (see §3), so these numbers are the honest starting point every later build step is compared against.

---

## 3. Reconciliation execution

At the owner's direction, the build runs on a staging branch, `build/full-build`, created from `main`. Local `main` stays untouched, and nothing is pushed without the owner's separate word.

The merge order was executed as proposed in §1:

- **Step 1 — `commit-rules-tracer`: done**, as merge commit `706eace`. The repo's own hooks are active and enforced the new rules on the reconciliation itself: commitlint rejected a first non-conforming merge message (rewritten to comply — no bypass used), and Gitleaks scanned the merge clean.
- **Step 2 — the phase-10 chain (`phase-10-3-correctness`):** the merge produced exactly two conflicts, both modify/delete cases — `requirements.txt` and `.husky/commit-msg.js`. Both were resolved in favour of the commit-rules side, per the owner's settled decisions: `uv` is the only dependency system (so the deleted `requirements.txt` stays deleted), and the commitlint architecture replaces the old hand-rolled commit-msg hook (so the deleted `.husky/commit-msg.js` stays deleted).
- **One truth found by the merge:** `pydantic` was imported directly by 5+ engine modules but declared only as a transitive dependency — it worked by accident of other packages pulling it in. It is now declared as a direct dependency.

---

## 4. Standard-software checklist (§9)

Per the owner's decisions, the master prompt's §9 standard-software checklist is **applied as acceptance criteria throughout Epics 1–10 of the build**, rather than graded as a standalone audit table in this report. An epic is not done until the relevant §9 criteria pass for it.

Two owner directives sharpen that further:

- **Production standard across the board.** Every subsystem gets a real, production-grade implementation — no scaffolds or placeholders left standing. The owner named the **memory subsystem** and **plugins** specifically.
- **Switchable memory backends are a decided Epic 4 requirement:** the native memory engine is the default, with Mem0 and other memory systems pluggable behind a common facade, selectable by configuration.

---

## §5 Completion record (appended at Epic 0 close, 30 Oct 2026)

The reconciliation was executed on `build/full-build` exactly in the §1 order:

| Step | Commit | Result |
|---|---|---|
| Merge commit-rules-tracer | `706eace` | Hooks live; commitlint rejected the first non-conforming merge message |
| Jest gate compliance | `dd03648` | `DraggableChatWindow.test.jsx` written (36 tests) instead of waiving the missing-test rule |
| Merge phase-10-3 chain | `34fd310` | 2 modify/delete conflicts (requirements.txt, .husky/commit-msg.js), both resolved for the commit-rules side per settled decisions |
| Dependency truth | `151df57` | pydantic declared as a direct dependency (was imported by 5+ modules, only transitive) |
| Merge mcp-001-tracer | `6180533` | 6 conflicts; the single-pipeline shape preserved (see ADR 0026) |

**Final baseline on `build/full-build` @ `6180533`:** pytest **533 passed / 15 failed /
2 skipped**; Jest **159/159 across 20 suites**. The 15 failures are the known
pre-existing families (startup validation, workspace containment) carried unchanged
through every merge — zero new failures were introduced by the reconciliation.
The MCP merge also passed a full Ruff compliance pass (311 violations on arrival →
0; doctrine in ADR 0026) because the MCP branch had never lived under the gate.

**Carried into later Epics (not Epic 0 blockers):** MCP issue 002 (owner's 21st.dev
key) and issue 005 (owner's risk-override UI review); commit-rules issue 006
(owner on Windows + GitHub main ruleset); the owner's Windows QA of the MCP demos.
