# Epic 7 — Plugins & Skills: summary

Closed 31 Oct 2026. Branch `build/full-build`; nothing pushed,
`main` untouched. ADR: 0035 (hooks, TTSR, disable semantics).

## What shipped

- **E7-01 Hooks** — `aether_engine/tools/hooks.py`: code-registered
  `tool_call` hooks (pass / rewrite args / block) and `tool_result`
  hooks (replace output), consulted by `ToolRegistry.execute`, the
  single pipeline every tool call traverses. Fail-closed by
  construction: a throwing call hook blocks the tool; a throwing
  result hook withholds the unvetted output. All outcomes audited
  (HOOK_BLOCKED / HOOK_REWRITTEN / HOOK_FAILED /
  HOOK_RESULT_REPLACED).
- **E7-02 TTSR** — named regex rules on streamed model output from
  `UserConfig.ttsr_rules`, validated at config load (invalid rules
  dropped with a warning), enforced per chunk in
  `LiteLLMProvider.call_stream`: `redact` replaces matches with a
  rule-tagged marker; `block` stops the stream with a ProviderError
  and a TTSR_BLOCKED audit. Off by default; chunk-boundary
  limitation documented in ADR 0035.
- **E7-03 Skill disable** — persisted disabled set
  (`~/.aether/skills/disabled.json`); disabled skills leave the
  manager index, so slash resolution, auto-match, and content load
  all miss them at once, while Settings still lists them with
  `enabled=False`. SKILL_TOGGLE_REQUEST/RESPONSE contracts +
  Settings toggle UI.
- **E7-04 Auto-match + disclosure** — `match_skills` finally has a
  caller: the executor merges keyword matches into `active_skills`
  at task start. The supervisor prompt carries a bounded,
  summaries-only skill catalogue (stage-1 disclosure); full bodies
  still load only for activated skills.
- **E7-05 Plugin toggle is real** — disabling a plugin removes its
  MCP servers from the global registry and moves its skill dirs to
  `~/.aether/skills/.disabled-by-plugin/<plugin>/`; enabling moves
  them back and re-adds MCP servers only after re-validating the
  stored manifest against the installed package. Previously the
  toggle flipped a flag nothing read.
- Installer stance reaffirmed in ADR 0035: plugins contribute
  skills + MCP configs, never in-process code.

## Associated fixes (found during this epic's verification)

- **Fast chat was silently dead (fixed, `fix(chat)` commit).** The
  full-suite run hung; root cause: the Epic 5 retry-factory
  snapshot reads `task_user_config` unconditionally, but the
  layered config was computed only in the graph branch — every
  fast-chat START_TASK raised UnboundLocalError and the client
  waited forever. On the shipped app, plain chat messages got no
  reply. The layered config is now computed once for both paths.
  `tests/test_epic1_fast_chat.py`: 22/22 (previously hung).
- **Test-button wedge (fixed, `fix(providers)` + `fix(settings)`).**
  Reported by Arindam: the provider Test button gave no visible
  sign of working, and a double-click could freeze the app. One
  validation ran discovery + up to 5 sequential pings inline in the
  websocket loop (up to ~60s; a double-click queued a second scan).
  Now: validation is hard-capped at 30s with an honest timeout
  result, duplicate requests for the same provider+key share one
  in-flight run, the WS handler answers from a background task (the
  socket stays live), the button ignores repeat clicks, shows a
  spinner + elapsed seconds, and reports a plain timeout at 35s.

## Verification

- Epic acceptance: `tests/test_epic7_plugins_skills.py` 12/12;
  `tests/test_validation_hardening.py` 3/3; fast-chat 22/22.
- Full suites: pytest **840 passed / 13 failed / 2 skipped**;
  Jest **247/247 (34 suites)**.
- The 13 pytest failures (test_engine_ws ×4,
  test_startup_validation ×4, test_episodic_memory ×1 — needs
  Ollama, phase5 ×2, providers_panel ×2) were characterised, not
  waved away: each failing group reproduces **identically on the
  pre-Epic-7 tree (Epic 6 close-out commit) in this environment**,
  i.e. they are environment/state effects (accumulated `~/.aether`
  test pollution, timing-sensitive WS families, no local Ollama),
  not regressions from this epic. Epic-attributable tests are green
  in the full run.
