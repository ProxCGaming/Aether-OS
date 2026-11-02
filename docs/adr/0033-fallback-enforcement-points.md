# ADR 0033 — Fallback enforcement points (§6.4)

Status: accepted, 31 Oct 2026. Epic 5.

## Context

Master prompt §6.4: *"no fallback to a different model once tools have
started executing; only on initial prompt failure, and every fallback
is audit-logged."* Before Epic 5, per-call fallback inside
`LiteLLMProvider.call_stream` retried on ANY retriable error —
including mid-task calls whose conversation already carried tool
results, and even after partial output had streamed — and there was
no task-level fallback at all.

## Decision

Fallback is enforced at exactly two points, both gated, both audited:

1. **Per-call** (`LiteLLMProvider.call_stream`): fallback is
   attempted only when the call has yielded nothing yet AND the
   message list contains no `role: "tool"` result. Otherwise the
   primary error raises (same typed errors as the exhausted path).
   Success on a fallback model audits `NODE_FALLBACK_TRIGGERED`.
2. **Task-level** (`execute_task_generator` + a caller-supplied
   retry factory): a retriably failed task is rebuilt once on the
   top healthy fallback candidate (existing
   `routing.fallback.get_fallback_candidates`), reusing the failed
   task id, only when no `TOOL_ACTIVITY`/`TOOL_APPROVAL_REQUEST`
   event occurred. The switch audits `FALLBACK_TRIGGERED`. The
   approval-resume path supplies no factory — a resumed task has
   history, so §6.4 forbids the switch categorically. The gate is
   the pure function `should_attempt_task_fallback`.

Routing stamps are truth: `TASK_CREATED.routing` carries the
`RoutingDecision`'s real reason and capabilities; the retry attempt
re-stamps with a reason naming the failed provider/model and the
fallback. Provider health (`ProviderHealthManager` snapshot) is
served on `PROVIDER_LIST_RESPONSE` and rendered in Settings.

## Consequences

- A provider dying mid-task surfaces an honest failure instead of a
  silent model switch after side effects.
- Initial-prompt resilience is preserved at both granularities
  (same-provider/cross-provider per call; whole-task retry), each
  distinctly auditable.
- The 10s-bounded `validate_api_key` ping (already wait_for-guarded)
  is regression-pinned; its google_gemini default now targets
  gemini-3.5-flash, matching `select_route`'s corrected default
  argument.
