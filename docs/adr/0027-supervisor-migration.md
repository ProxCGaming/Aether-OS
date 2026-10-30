# ADR 0027 — Supervisor Migration to create_supervisor() (Epic 1, E1-02)

- Status: Accepted
- Date: 2026-10-30
- Branch: `build/full-build`
- Context: The supervisor routed work with hand-written JSON parsing — its own
  prompt protocol, its own decision parser, and routing edges maintained by
  hand. The owner decided on 26 Oct 2026 (reconfirmed in the full-build
  programme) to replace that routing with LangGraph's prebuilt
  `create_supervisor()` while keeping Aether's trust architecture — the Phase 9
  approval pause, tool-policy gating, the memory briefing, per-agent models —
  as a wrapper around it, never re-implemented inside it. A parity
  characterisation suite (`tests/test_epic1_parity.py`, E1-01) was written
  against the OLD graph first; the migration was accepted only when that suite
  passed against the new one.

## Decisions

### D1. Routing is create_supervisor(); execution stays central
The top-level graph is built by `create_supervisor()` over three compiled
specialist subgraphs sharing `AetherState`. Each subgraph runs
specialist → tool_router → (`pause_for_approval` | `execute_tool`) → back to
the specialist, so a tool call still traverses the single
`ToolRegistry.execute()` pipeline (ADR 0026 D3) and the approval interrupt
still fires from inside the subgraph and resumes at the top level. Specialists
gain no direct tool execution of their own.

### D2. The routing model is an adapter over Aether providers
`AetherSupervisorModel` (`langgraph/supervisor_model.py`) is a `BaseChatModel`
adapter over the provider resolved per run, honouring `agent_models["supervisor"]`.
Native `transfer_to_*` tool calls pass through as handoffs; JSON-text decisions
(the established supervisor prompt protocol) are translated into handoff calls;
a specialist's final text answer ends the run adapter-side, because
langgraph-prebuilt 1.1.1 offers no model-level `jump_to`. Supervisor memory
tools are LangChain wrappers over the existing audited
`_execute_supervisor_memory_tool`, still gated by deny-policy and the
per-agent allow-list at call time. A `post_model_hook` reproduces the old
node's bookkeeping (delegation_log, context_briefing, plan, active_specialist)
for every routing decision.

### D3. The memory briefing now reaches the specialists (parity fix)
The parity suite exposed that `state["context_briefing"]` was written by the
supervisor and read by nobody. All three specialist nodes now inject the
briefing into their system prompt. The parity test that pinned the gap
(strict xfail) passes; its marker is removed.

### D4. Resume carries the full run config
`resume_langgraph_task` previously dropped `user_config`, `agent_models`,
`agent_tools`, `secret_store`, `forced_agent` and `session_id` — behaviour
could change mid-task after an approval. Resume now receives and forwards the
full config (the websocket call site passes it), so what was approved is what
runs. Executor event emission is delta-only, matching the full-history update
shape subgraphs produce.

### D5. supervisor_node is retained as an API, removed as a route
The graph no longer routes through `supervisor_node`. The function remains
because committed suites pin it as a standalone API (parity memory tests,
`test_supervisor_integration.py`), and its logic is not duplicated: provider
resolution, decision parsing and prompt construction are single shared helpers
used by both the node and the adapter. Deleting the function would mean
deleting the tests that guard the memory-tool behaviour.

## Evidence
- Parity: 8/8 green (was 7 pass + 1 strict xfail on the old graph). No test
  body was edited; only the xfail marker was removed, plus docstring updates
  recording the fix.
- Full suite: pytest 547 passed / 15 failed / 2 skipped — the same 15
  pre-existing failures as the Epic 0 baseline; zero regressions. Four
  failures near the changed files were individually re-verified as
  pre-existing (they fail with these changes reverted, or pin source state
  that predates this migration).
- Ruff: zero on all touched files.

## Known wart
A benign runtime warning ("supervisor agent wrote to unknown channel
remaining_steps") can appear: the prebuilt agent tracks that managed value
internally and the workflow ignores it. Harmless today; revisit if a future
langgraph-supervisor release changes channel handling.
