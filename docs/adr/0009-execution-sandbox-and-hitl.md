# ADR 0009: Execution Sandbox and Human-in-the-Loop Approval

## Status
Accepted

## Context
AETHER needs to execute restricted local actions such as shell commands and file mutations without undermining the multi-process safety model established in ADR 0001. Direct execution inside the engine or UI would blur security boundaries, make the UI vulnerable to blocking behavior, and prevent safe human review of destructive operations.

## Decision
1. **Process Isolation**: Risky tool execution is delegated to an independent worker process, aether_worker, launched as an OS-level subprocess under the engine lifecycle.
2. **Pre-Flight Validation**: The engine validates file and shell operations locally before approval or execution using read-only checks such as path existence, parent-directory writability, and PATH lookup of the requested executable.
3. **HITL Approval Flow**: When a request is deemed risky, the engine emits a TOOL_APPROVAL_REQUEST event. The UI presents a native PySide6 approval drawer, and the user decision is returned through authenticated loopback WebSocket events.

## Consequences
### Positive
- Tool execution remains isolated from the UI process and can be safely reviewed before mutation.
- Lightweight pre-flight validation catches obvious failures before expensive or destructive behavior.
- The approval flow is observable and auditable through structured event contracts and JSONL audit logs.

### Negative / Trade-offs
- Approval workflows add latency and require explicit user consent for privileged actions.
- Worker execution introduces additional IPC coordination and lifecycle management.
