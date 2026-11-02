# Epic 5 — Providers: PR-style summary

**Branch:** `build/full-build` · **Date:** 31 Oct 2026 · **Verdict:** §6.4 is now
enforced at both fallback points, routing stamps tell the truth, and
provider health is visible in Settings. Most of the epic's surface
(discovery honesty, health manager, local models, scheduler) already
existed and was verified rather than rebuilt.

## What shipped

- **E5-01 — per-call §6.4 gate** (`280fe24`): `call_stream` falls
  back only on a clean initial failure — never when the conversation
  carries tool results, never after partial output streamed. Blocked
  fallbacks log why and raise the usual typed errors.
- **E5-02 — task-level fallback**: one retry on the top healthy
  candidate via a retry factory (same task id, same path), only
  before any tool activity; audited `FALLBACK_TRIGGERED`; the
  approval-resume path is categorically excluded. Gate extracted as
  the pure, truth-table-tested `should_attempt_task_fallback`.
- **E5-03 — routing truth**: `TASK_CREATED` stamps the decision's
  real reason + capabilities (was a hard-coded "Default Routing" with
  empty capabilities); `select_route`'s dead `gemini-2.5-flash`
  default argument corrected; routing contract pinned by tests
  (requested model wins, default wins, unhealthy default falls back
  with a reason naming it). No "+100 tiebreak"/scoring exists in the
  code — the rule-based contract is what was pinned (PRD Q2).
- **E5-04 — validation pins**: the already-bounded ping hang fix is
  regression-tested (a hanging completion returns an honest failure
  in bounded time); google_gemini ping default → gemini-3.5-flash.
- **E5-05 — health surface**: `PROVIDER_LIST_RESPONSE` carries the
  health snapshot; Settings provider cards render state + last
  latency chips, Jest-pinned.

## Tests

- New: `tests/test_epic5_providers.py` — 10 tests.
- Review caught and fixed three masked defects before close-out
  (`c3dc819`): the curated registry lacked gemini-3.5-flash entirely
  (select_route could not honour the configured default pre-discovery),
  the validation tests targeted an imagined method API instead of the
  real module function, and the partial-stream fake never raised,
  making the §6.4 stream test vacuous.
- Baselines after Epic 5: **pytest 836 passed / 3 failed / 2 skipped in the final full run (the 3 failures are the known timing-sensitive families: test_engine_ws x1, test_startup_validation x2; those families swing between runs — an earlier full run showed 827/8 — and are unrelated to Epic 5's files; every Epic 5 test passes in the full run); Jest 223/223**
  (known-failure families unchanged: 13).

## Records

- ADR 0033 (fallback enforcement points).
- PRD + progress: `workspace/goals/aether-os-desktop-app-build/files/epic5-prd-and-issues.md`.
