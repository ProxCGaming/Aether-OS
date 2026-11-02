# Epic 1 — Core Chat & Orchestration: PR-style summary

**Branch:** `build/full-build` · **Date:** 2026-10-30 · **Status:** complete

## What shipped

| Issue | Result | Key commits |
|---|---|---|
| E1-01 Parity suite | 8 characterisation tests pinning approval pause/resume, policy gate, briefing, per-agent models, checkpoint durability | `f016e63` |
| E1-02 create_supervisor migration | Routing on LangGraph's prebuilt supervisor; Aether trust wrapper preserved; briefing gap found by parity fixed | `874ca83`, ADR 0027 |
| E1-03 Recursion cap + loop guard | `recursion_limit=25` actually applied; identical-call loop stopped at 5 with a corrective steer | `9d18962` |
| E1-04 Fast chat | Intent split before the graph; chat streams with zero supervisor involvement; ambiguity defaults to work | `7495d6c` |
| E1-05 Per-agent models | Backend proven by parity; Settings → Agents now shows each agent's **effective** model (override or inherit line) | UI commit |
| E1-06 Compaction | Summary-plus-tail model view; full history never rewritten; `compression` auxiliary is a real seam | `41514d6` |
| E1-07 Steering queue | Mid-task messages queue and apply at step boundaries; approval-pause steers apply on resume; Queued badge | `2ded790` |
| E1-08 Conversation QoL | Edit-and-resend hardened (shared ids, occurrence-aware truncate, checkpoint invalidation); regenerate/retry; model-written titles (`title` auxiliary, 4th real seam); session rename; content search with snippets | `278d06f`, `aca1dd2`, UI commit |
| E1-09 Plan mode | READ_ONLY-only tool lists by construction + execute_tool backstop; flag in checkpointed state; UI toggle (per-session) | `d14b86a`, UI commit |
| E1-10 Structured outputs | Per-agent schemas validated pre-return; one corrective retry; loud in-band failure; `structured_outputs` channel | `9f2b8d3` |
| E1-11 Spawn policy | Supervisor allow-list + per-turn delegation cap (default 8, adapted from the PRD's nested-depth 2 for this flat topology); hook-level strip/log/explain | `5d96b59` |
| E1-12 Advisor pass | `advisor` auxiliary (3rd real seam) injects review notes into specialist prompts; best-effort only | `dc2915b` |
| E1-13 Prewalk | Opt-in agents plan on the strong provider, switch to their execution model at the first executed write | `1ede77e` |
| E1-14 Config layers | Global → `.aether/config.json` → per-task overlay; project layer barred from tool_policies/workspace_roots | `dc7627d` |
| E1-15 Blackboard | plan/findings/decisions/open_questions in LangGraph state, bounded briefing, echo-safe merge | `85faf57` |
| E1-16 Thinking stream | `StreamChunk.thinking` end-to-end; rendered only in the Thinking collapsible; never in answers or saved replies | `2f18de7`, UI commit |
| E1-17 Scratchpad | `scratchpad_render` tool (READ_ONLY); engine-sanitised SCRATCHPAD events; Mermaid/SVG/text panel | `1ecb92f`, UI commit |

## Acceptance (from the master prompt)
- Chat round-trip streams — fast-chat and graph paths both stream via
  the shared generator wrapper; covered by fast-chat + steering suites.
- A delegated coding task passes planner → coder → approval → result —
  pinned by the parity suite (approval pause, resume, policy gate).
- Kill/restart/resume — parity test 5: a fresh checkpointer on the
  same DB sees the finished state after interrupt + resume; steering
  suite covers steers across the approval pause.
- pytest + Jest green with no new failures over the Epic 0 baseline —
  pytest: 664 passed, 14 failures, all in the known pre-existing families
  (startup validation, engine_ws provider pins, providers panel,
  phase5 pins, episodic-needs-Ollama, workspace containment);
  `test_mcp_auto_connect`'s dead-server test is a known full-suite
  flake (10/10 in isolation). Jest: 191/191.
- ADR: 0027 (supervisor migration) + 0028 (epic decisions). This
  summary is the PR-style record.

## Known follow-ups (not Epic 1 scope)
- Fast chat does not write episodic/KG memory — Epic 4 parity.
- The supervisor's own routing calls do not stream thinking (E1-16
  covers fast chat + specialists).
- Settings UI for the newer config surfaces (spawn policy, prewalk,
  output schemas, compaction thresholds, layered config) — Epic 6.
