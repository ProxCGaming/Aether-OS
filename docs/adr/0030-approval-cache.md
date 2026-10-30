# ADR 0030 — Approval Cache Grants (Epic 2, E2-09)

- Status: Accepted
- Date: 2026-10-31
- Branch: `build/full-build`
- Context: Every non-read-only tool call paused for approval, every
  time — including the fiftieth write to the same project folder in
  one sitting. Users either rubber-stamp (defeating the pause) or
  abandon the tool. The approval needed a memory, with the same care
  as the policy work in ADR 0029: scoped, revocable, auditable, and
  never stronger than the policy it sits on.

## Decisions

### D1. A grant is (path prefix, risk ceiling)
Scoped approvals offered on the approval card — once (nothing
stored), this file, this folder — persist as rows in
`~/.aether/approval-cache.db` (SQLite, WAL). A grant covers a later
call when the call's path is inside the prefix and its risk is at or
below the ceiling: a write grant covers reads under it; it never
covers a riskier call, a path outside the prefix, or a call with no
path (shell commands are ungrantable by construction).

### D2. The cache overlays the verdict; it is not a policy
Grants are consulted only where a pause would otherwise happen —
the graph's `tool_router` and `ToolRegistry.execute` ask the same
question and must agree. A grant can turn "ask" into "allow"; it can
never turn "deny" into anything, and it never applies to floored
paths (ADR 0029 D3 names standing configuration explicitly, so
`~/.ssh` / `~/.aether` and non-read-only system paths are
ungrantable even if a grant row names them).

### D3. Every transition is audited and revocable
Grant creation (from the websocket approval handler, which reads the
pending call's tool and path from the checkpointer),
`APPROVAL_CACHE_HIT` on use, and revocation all write audit events.
Settings lists grants and revokes by id; revocation is a row delete
and takes effect on the next call. Grant recording is best-effort
beside an approval already given — a storage failure never blocks
the user's approval, it just means the next call asks again.
