# ADR 0031 — Supervisor Tool Boundary & MCP Risk Overrides

**Status:** Accepted
**Date:** 2026-10-31
**Branch:** build/full-build (Epic 3)

## Context

The owner's Windows Demo 001 (Oct 2026) failed with "Unknown tool:
Fake__add": the pre-migration supervisor routed its tool calls to a
memory-only executor, so an MCP tool call died inside the wrong
executor. Epic 1's create_supervisor migration changed the shape —
the supervisor is now offered only memory tools and handoff tools
(`AetherSupervisorModel._offered_tools`) — but two traces of the old
failure class remained in Epic 3's audit:

1. A model can still *emit* a non-memory, non-handoff call at the
   supervisor level (models hallucinate calls they were not
   offered). The adapter dropped those calls silently: the tool use
   vanished and the reply was read as a text routing decision, with
   no trace of what was lost.
2. MCP tools all carried risk EXECUTE (approval-required) with no
   way for the owner to classify a trusted read-only MCP tool
   differently — the grill PRD (26 Oct 2026) had proposed a per-tool
   risk override map that was never implemented.

## Decisions

**D1 — The supervisor never executes non-memory tools, and drops are
audible.** Tool execution belongs to specialists, through the
central pipeline (policy → pre-flight → sandbox/in-process). The
supervisor's job is routing and memory. Rather than giving the
supervisor a second execution path (the pre-migration design that
produced issue 012), the drop stays — but it is never silent: the
adapter logs the dropped tool names at WARNING and records them on
the routing decision (`dropped_tool_calls`, persisted in the
delegation log; `aether_dropped_tool_calls` on memory-step
messages). A vanished tool call is now diagnosable from logs and
state. E3-01 proves the supported path end-to-end: a specialist
calls `fake__add` through the full graph with the approval pause.

**D2 — `mcp_tool_risk_overrides` classifies MCP tools at registry
build.** `UserConfig.mcp_tool_risk_overrides` maps `server__tool`
to a `ToolRisk` value. `MCPRegistry.get_mcp_tools(risk_overrides=…)`
returns overridden tools as copies (the connect-time cache is never
mutated), so policy, plan mode (a read_only-overridden MCP tool
becomes plan-eligible), and the tool list all see the owner's
classification. The websocket build sites pass the user config's
map at every registry build. Invalid entries are cleaned at config
load and ignored at application. The key sits in the config-layer
trust boundary (`PROJECT_FORBIDDEN_KEYS`): a cloned workspace
cannot reclassify MCP tools to relax its own approvals.

## Consequences

- Issue 012's failure mode cannot recur invisibly: either a tool
  call runs through the one pipeline (specialists) or its drop is
  logged and recorded (supervisor).
- Owners can make a trusted MCP reader frictionless (read_only →
  no approval pause, plan-mode eligible) or lock a dangerous one
  down (destructive), per tool, without touching server code.
- Overrides take effect at the next registry build (task start,
  resume, tool list) — no reconnect needed.
