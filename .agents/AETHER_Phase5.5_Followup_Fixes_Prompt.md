# Task: Investigate and Fix — Provider/Model Mismatch, Missing Approval Audit Trail, Unverified Plugin Flow, Outstanding Explanations

## Your role
This prompt covers four distinct items found by directly analyzing
`logs/audit.log` (1076 lines, spanning many engine restarts) rather than
by reading code or trusting prior summaries. Two are real bugs with
concrete evidence attached. One is a verification gap — something
claimed as done but with zero supporting evidence across substantial
real activity. One is a set of previously-requested items that were
never addressed. Investigate and fix all four. Do not report anything
as "done" without pasting the actual evidence this prompt asks for.

---

## Issue 1: Provider/model mismatch on Engine restart (new bug, found via log analysis)

**Evidence, directly from the log:**
```
{"action": "STARTUP_FALLBACK", "details": {"reason": "Model 'deepseek/deepseek-v4.1-flash' not found for provider 'google_gemini'; using default 'gemini-3.1-flash-image'."}}
```
```
{"action": "STARTUP_FALLBACK", "details": {"reason": "Model 'gemini-3.5-flash-lite' not found for provider 'openrouter'; using default 'stealth/union-alpha'."}}
```
Two separate occurrences, in both directions — a model belonging to one
provider is being checked against a completely different provider on
Engine startup. This is not a one-off fluke; it happened twice with
different provider/model pairs.

**Required investigation:** trace exactly how the "default provider" /
"default model" pair gets persisted (likely in `~/.aether/config.json`)
and exactly how it gets read back and validated on `ENGINE_STARTED`.
Determine specifically:
- Is the model being saved without its associated provider, so a lookup
  later checks it against whatever the *current* default provider
  happens to be instead of the provider it was actually selected under?
- Does this only affect models under dynamically-added custom providers
  (ADR 0013's `custom_provider_names`/`custom_provider_types`), or does
  it affect static `CLOUD_PROVIDERS` models too? Test both.
- This is very likely the same root cause behind the still-unanswered
  question from the last investigation request: whether
  `CapabilityRouter` actually integrates with dynamically-added custom
  providers correctly. Resolve that question as part of this
  investigation, with the same rigor originally requested (trace the
  actual code, cite exact functions/lines, don't infer from the ADR's
  description).

**Required fix:** the provider/model pair must be persisted and
validated together, not independently. Fix the actual bug, then add a
regression test that reproduces this exact scenario (save a model under
provider A, restart with provider A no longer default, confirm no
mismatch/fallback occurs incorrectly).

---

## Issue 2: No audit trail for the normal tool-approval happy path

**Evidence:** across the entire 1076-line log — 160 `RUNNING` task
transitions, 46 `WORKER_EXECUTION_COMPLETED` events — there is not one
`PENDING_APPROVAL` task state, and not one distinct event marking "a
risky tool call requested approval" or "approval was granted." The only
approval-related event present is `APPROVAL_HANDLER_MISSING` (the
fail-closed rejection path, confirmed working). The normal path — human
sees the drawer, approves, execution proceeds — leaves **zero trace** in
the audit log.

Given this project has already found one real fail-open bug in exactly
this mechanism (Phase 5.1, `pause.py`), an approval mechanism with no
audit visibility into its normal operation is a real gap, not a nitpick
— there's currently no way to prove, after the fact, that a given
execution actually went through human review rather than some other
path.

**Required fix:** add distinct audit log events for both sides of the
normal approval cycle — e.g. `APPROVAL_REQUESTED` (when a risky tool
call reaches the pause point, with tool name and args) and
`APPROVAL_GRANTED` (when the user approves, before execution proceeds).
Use whatever naming is consistent with the rest of the system, your
call, but document the exact names chosen. Verify these actually fire by
triggering a real approval cycle and checking the log — don't just add
the logging call and assume it works.

---

## Issue 3: Plugin install/uninstall flow — never demonstrated with real evidence

Phase 5.5's walkthrough describes the plugin flow as working, and ADR
0015 describes it in detail, but the audit log — covering substantial
real activity across 135 engine starts — contains **zero** `PLUGIN_*`
events of any kind. The required manual acceptance test (install a real
plugin, confirm `PLUGIN_INSTALLED` in the audit log) was never actually
performed, or was performed through a path that doesn't log correctly.

**Also:** ADR 0015 states plugin installs "trigger a local
`TOOL_APPROVAL_REQUEST` WS event" — that exact event type appears zero
times anywhere in the log. `TOOL_APPROVAL_REQUEST` was the event type
explicitly superseded by ADR 0011 and confirmed removed in Phase 5.1.
Reusing that specific name for a new, different purpose (plugin
approval, which isn't a LangGraph node's tool call at all) is confusing
at best and a sign of resurrected dead code at worst.

**Required fix:**
- Do not reuse `TOOL_APPROVAL_REQUEST` for plugin-install approval. Use
  a distinct, new event/request type (e.g. `PLUGIN_APPROVAL_REQUEST`) so
  it's unambiguous this is a separate mechanism, not a revival of what
  Phase 5.1 removed.
- Actually install a real, well-formed test plugin through the running
  UI, end to end — not a unit test, an actual manual run.
- Actually uninstall it afterward.
- Paste the real resulting audit log lines for both `PLUGIN_INSTALLED`
  and `PLUGIN_UNINSTALLED`, exactly as they appear in the log file, not
  a description of what they should contain.

---

## Issue 4: Outstanding items from before, still not addressed

Two things were asked for across the last two verification rounds and
have not been answered either time:

1. **The "hardcoded models dropdown" fix**, mentioned in the Phase 5.5
   completion summary — this was out of scope for Phase 5.5. Explain
   precisely what was actually broken, and why it was fixed under this
   task instead of reported as a separate finding. If it's related to
   Issue 1's provider/model mismatch, say so explicitly.
2. **The real checkpoint durability kill-test** — requested twice now.
   Actually do this: pause a task for approval, forcibly kill the Engine
   process (`Stop-Process -Force`, not a graceful shutdown), restart it,
   confirm the task resumes from the persistent checkpoint. Paste the
   real audit log lines from before the kill and after the restart. If
   this has never actually been done, say so plainly rather than
   describe it as if it had been — an honest "not done yet, here's what
   happened when I just tried it" is the only acceptable answer at this
   point.

---

## Explicitly out of scope
- No new features beyond what's needed to fix the four issues above.
- No changes to the Skills or Agents tabs — they're not implicated in
  anything found here.
- No plugin marketplace, signing, or discovery — unrelated to what was
  found.

## Required process
For each of the four issues: reproduce the problem first (don't assume
this prompt's description is still accurate — re-check against current
code/logs), then fix, then produce the specific evidence requested.

**Write one new ADR: `ADR/0016-provider-model-persistence-and-approval-audit-fixes.md`.**
Cover: the exact root cause of the provider/model mismatch and the fix,
resolution of the ADR 0013/CapabilityRouter integration question (with
citations to the exact code, not a restated assumption), the new
approval-audit event names and when they fire, and confirmation that
plugin-install approval now uses a distinct event type from the removed
`TOOL_APPROVAL_REQUEST`.

## Acceptance tests
1. Regression test reproducing the exact provider/model mismatch
   scenario from Issue 1, now passing.
2. A real approval cycle (approve a risky tool call normally) produces
   `APPROVAL_REQUESTED`/`APPROVAL_GRANTED` (or your chosen names) in the
   audit log — paste the real lines.
3. A real plugin install and uninstall, end-to-end, with real
   `PLUGIN_INSTALLED`/`PLUGIN_UNINSTALLED` audit log lines pasted.
4. A real Engine kill-and-restart during a paused approval, with real
   before/after audit log lines pasted.
5. Full `pytest tests/ -v` raw output, not summarized.

## When you're done
Report against each of the four issues separately, with the exact
evidence requested for each. Do not summarize as "all fixed" without the
underlying proof for every one. Stop here — do not proceed to any other
phase without being asked.
