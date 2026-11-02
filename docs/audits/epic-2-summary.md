# Epic 2 — Tools & Validation: PR-style summary

Branch `build/full-build`. Baseline at epic start (Epic 1 close,
`87243d7`): pytest 664 passed / 14 known-failed / 2 skipped; Jest
191/191. At Epic 2 close: **pytest 791 passed / 13 known-failed /
2 skipped; Jest 216/216 (25 new tests over the 191 baseline)**. The 13 failures are the
long-standing pre-existing families (startup validation ×4,
engine_ws ×4, providers panel ×2, phase5 ×2, episodic-needs-Ollama
×1) — zero regressions from this epic; the one pin the epic
deliberately superseded (pre-flight parent-existence, decision D3)
was re-pinned in `46fccd0`. Tool count: 7 built-ins at epic start →
**18** at close (plus MCP tools when servers are connected).

## What shipped, per issue

- **E2-01 `read_file`** (`0a9e7b4`): byte-faithful reads — 1-based
  offset/limit windows, line numbers, a sha256 header over the exact
  bytes (the anchor E2-03 consumes), binary refusal, loud truncation.
  New `tools/paths.py` holds the one containment rule (resolve, then
  inside-a-root; symlink escapes caught), and the registry learned
  `wants_workspace_roots` injection. Pre-flight gained the matching
  read branch.
- **E2-02 `find_files` + `search_text`** (`110609b`): glob over
  contained roots (`.git` always excluded, hidden opt-in, 200-result
  cap) and per-line regex search (`file:line` output, 100-match /
  2000-file scan caps, invalid regex as an honest result string).
  Their pre-flight branch landed early inside the E2-07 commit
  (whole-file staging while the slice was in flight); behaviour and
  tests are this slice's.
- **E2-03 `edit_file` + E2-04 `create_directory`** (`61db720`): the
  edit contract lives in `aether_common/text_edit.py` — every anchor
  occurs exactly once or the edit fails loudly, all edits validate
  before any write, writes are atomic (temp + rename), and both
  executors (registry in-process + worker) verify the read hash
  first. The pause node computes a unified diff at approval time and
  the executor threads it into the approval payload at all three
  emission sites; the desktop card renders it (UI batch, this epic).
  `write_file` creates missing parents inside containment (D3).
- **E2-05 `fetch_url` + E2-10 search providers** (`8f5d95d`):
  trafilatura extraction headed by title + final URL (citations
  survive into answers); SSRF guard on the host and every redirect
  hop (loopback/private/link-local refused unless
  `AETHER_ALLOW_LOCAL_FETCH=1`); timeouts and size caps. Search
  dispatches through `tools/web_search.py`: ddgs default, Exa /
  Brave / Tavily / Firecrawl via `search_provider` with keys from
  the encrypted secret store; fallback to ddgs only when configured.
- **E2-06 arg-aware policy** (`3125362`, ADR 0029): the resolver
  takes the call's args + ordered `path_rules`; both specific-call
  sites (tool_router, registry.execute) pass them. Rules can relax
  the default ask inside a trusted prefix, never override an
  explicit Deny / Require Approval; the built-in floor keeps
  `~/.ssh` / `~/.aether` (all risks) and system prefixes
  (non-read-only) at ask forever. `path_rules` sits behind the
  config-layer trust boundary with `tool_policies`.
- **E2-07 shell rules** (`44f9d8f`): quote-aware segment splitting +
  quoted-literal masking; deny rules (fork bombs, `rm -rf` on
  `/`/`~`/`$HOME`, mkfs/dd/device writes, shutdown family) deny in
  policy AND fail pre-flight; ask rules (`rm -rf` anywhere,
  fetch-then-execute, recursive chmod on system paths) force a
  pause. User regexes append via config.
- **E2-08 approval mode `write`** (`e867234`): `approval_mode`
  `ask` (default, byte-identical behaviour) | `write` relaxes
  exactly the default ask for WRITE tools; explicit settings, path
  rules, the floor, and shell rules still ask. Settings endpoint +
  `TOOL_LIST_RESPONSE` field landed with the close-out commit
  (`72ae8db`).
- **E2-09 approval cache** (`1d0c903`, ADR 0030): scoped approvals
  (once / this file / this folder) persist as (prefix, risk-ceiling)
  grants in `~/.aether/approval-cache.db`; both pause sites consult
  the same cache; hits audited with grant id; grants never override
  deny and never cover floored paths; list/revoke contracts +
  Settings UI.
- **E2-11 code awareness** (`187455e`): `project_tree` (bounded,
  noise-filtered) and `run_checks` (detects the repo's own ruff /
  pytest / jest / oxlint from markers, parsed summaries, shared
  logic in `aether_common/checks.py`, worker-executed).
- **E2-12 escape battery** (`72ae8db`): 9 path-taking tools × 5
  escape vectors (traversal, absolute-outside, symlink file + dir,
  sibling prefix) driven through the real pipeline — all denied,
  outside files untouched; registry pre-flight failures now audit
  `TOOL_VALIDATION_FAILED`.

## Decisions (owner-approved 31 Oct 2026)
D1 context-anchor + read-hash edits · D2 move/copy deferred ·
D3 implicit parent creation inside containment · D4 trafilatura ·
D5 grants as (path prefix × risk ceiling). Recorded in the PRD and
ADRs 0029/0030.

## Deliberately not done
Move/copy (D2, named follow-up); calculator/eval tools; LSP/DAP;
Settings polish beyond functional wiring (Epic 6); path-rule editing
UI (config-file only this epic).
