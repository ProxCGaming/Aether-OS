# P1d Step 0 spike — verdict (19 Nov 2026)

**VERDICT: PASS — Route A (hand-rolled subagents-as-tools) is viable on
langgraph 1.2.16. No Route B (Deep Agents) needed.**

Spike: `spike_p1d/spike.py` (`uv run python spike_p1d/spike.py`, exit 0).
A `create_react_agent` supervisor (AetherState schema, post-model hook)
delegates through a prototype `delegate_to_researcher(briefing)` tool
that invokes the REAL researcher subgraph (`graph._build_specialist_graph`:
runner → tool_router → approval pause → execute_tool) with the agent's
current state and the tool's runtime config. The researcher calls an
ask-policy tool ("Require Approval"), so the run exercises the full
pause/resume path.

Both checkpointer variants pass every check:

- **Variant A** — specialist subgraph compiled WITH the shared
  checkpointer (exactly today's `_build_specialist_graph` usage).
- **Variant B** — specialist subgraph compiled without one (only the
  outer graph holds the checkpointer).

Checks (all true, both variants):

- `a_interrupt_bubbled` — the approval `interrupt()` inside the
  specialist surfaces on the OUTER graph's invoke as `__interrupt__`
  with the production payload (`approval_request`, tool name, args,
  request_id). Inside the tool it arrives as a `GraphInterrupt`
  exception out of the subgraph `ainvoke` — it must be allowed to
  propagate (never caught/converted by the delegation tool).
- `a_resume_completed` / `a_probe_executed_once` /
  `a_resume_into_specialist` — resuming the outer graph with
  `Command(resume={"approved": True})` re-enters the tool, which
  re-invokes the subgraph; the subgraph resumes INTO the same
  specialist (its second model round sees the probe's tool result),
  the probe executes exactly once, and the run completes.
- `b_specialist_saw_briefing` / `b_specialist_saw_history` — the
  specialist's first model round receives the full conversation and
  the `context_briefing` the post-model hook wrote immediately before
  the tool ran (hook → ToolNode state flow confirmed).
- `b_supervisor_saw_answer` / `b_supervisor_saw_full_history` — the
  supervisor's next model step sees the specialist's final answer as
  an assistant message and its internal tool trail (full_history
  semantics on the return path).
- `b_delegation_count` — the hook's counter increment lands exactly
  once across the delegation (and, per the resume checks, is not
  double-counted across the pause).

## Framework constraint discovered (shapes the build)

Aether's `collapse_subgraph_echo` reducer converts any multi-message
channel write to plain dicts, and `create_react_agent`'s
`_validate_chat_history` only recognises `BaseMessage` instances: a
`ToolMessage` batched into the same write as the specialist's dict
messages becomes a dict and the delegate call then reads as
unanswered (`INVALID_CHAT_HISTORY`). The delegation tool therefore
returns **two** `Command`s: first `{"messages": [ToolMessage]}`
(a single-message write, which stays a `ToolMessage`), then
`{"messages": [<specialist delta>], ...merged channels}`.

## Build choice

Variant A for the real build: the specialist subgraphs keep the exact
construction they have today (`_build_specialist_graph(name, node_fn,
checkpointer)`), so the migration changes only the supervisor's
topology, not the specialists'.
