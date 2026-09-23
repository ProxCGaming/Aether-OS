# ADR 0011: LangGraph Orchestration and Native Approval

## Status
Accepted

## Context
AETHER is transitioning its core orchestration layer to a multi-agent setup using LangGraph (Phase 5). The previous mechanism for pausing execution to request human-in-the-loop (HITL) approval relied on a bespoke `TOOL_APPROVAL_REQUEST` event flow and a hand-rolled blocking pattern (`wait_for_approval`) inside the old orchestration loop. While functional for in-memory execution, this meant that an Engine crash during a pending approval lost the task's context permanently. LangGraph provides native conditional-edge pause and resume mechanisms, combined with persistent checkpointing, enabling durability across Engine restarts.

Furthermore, we need to introduce specialized agent nodes (Supervisor, Researcher, Planner, Coder) and integrate with external MCP servers for extended capabilities.

## Decision
1. **Supersession of Approval Flow**: This ADR explicitly **supersedes the approval-flow portion of ADR 0009** (specifically the custom event types and blocking-wait pattern in the orchestration loop).
2. **Preservation of Execution Sandbox**: The process-isolation (`aether_worker`), pre-flight validation logic (from ADR 0009), and Windows Job Object task-class resource limits (from ADR 0010) **remain fully in effect, unmodified**. LangGraph governs *whether and when* execution is paused for human review, but it does not replace *how* that execution runs once approved.
3. **LangGraph Pause/Resume**: We will use LangGraph's native conditional-edge interrupt mechanism for risky tool calls. Safe tools (e.g., `read_file`, `web_search`) continue automatically; risky tools (e.g., `write_file`, `execute_shell`, `delete_file`) trigger a pause. The engine will translate this pause into a WebSocket event for the UI and use the UI's response to resume the graph.
4. **Persistent Checkpointing**: We will use `langgraph-checkpoint-sqlite` (or `sqlite3` depending on the adapter) to durably checkpoint graph state to disk, ensuring that paused tasks survive Engine restarts.
5. **Graph Structure and Recursion Limit**: A supervisor node delegates tasks to specialized nodes without fan-out. We impose a strict **recursion limit of 25** to prevent pathological unbounded execution loops.
6. **MCP Integration**: We will use the officially recommended standard MCP adapter for Python to load tools from connected MCP servers. Access is strictly governed by a user-managed allow-list in the UI.
7. **Approval Drawer Modifiability**: For this phase, we will stub the "edit-before-approve" functionality to focus on architectural stability, reserving full edit support for a later iteration.
8. **Delegation Logging**: Supervisor delegation logs will be written to a dedicated `delegation_log.jsonl` to prepare for Phase 8 skill distillation.

## Consequences
### Positive
- Tasks pending approval survive Engine process restarts due to persistent checkpointing.
- The orchestration layer is standardized on LangGraph, simplifying multi-node communication.
- The architecture correctly separates the concern of *workflow orchestration* from the concern of *safe execution*.

### Negative / Trade-offs
- Increased dependency surface (LangGraph, SQLite checkpointer, MCP adapter).
- The transition requires careful mapping of LangGraph interrupts to the existing WebSocket event contracts.
