# ADR 0010: Worker Resource Limits and Workspace Restriction

## Status
Accepted

## Context
Aether OS previously ran `aether_worker` sub-processes without resource caps or filesystem boundaries. This allowed user-approved tool executions to potentially cause unbounded damage (e.g., memory leaks, infinite loops, or modifying files across the system) even if the prompt logic was correct. With Phase 5 introducing LangGraph autonomy, these capabilities must be constrained.

## Decision
1. **Task-Class Profiles**: We introduced task classes (`instant`, `quick`, `standard`, `heavy`, `custom`) configured in `~/.aether/config.json`. These set a reasonable upper bound for time and memory limits per task.
2. **Task Class Heuristics**: Added `task_class_router` which infers a task's class from tool names and heuristics (e.g. `gcc` is heavy).
3. **Job Objects**: We used `pywin32`'s `win32job` wrappers. The worker process PID is immediately assigned to the job object after spawning via `asyncio.create_subprocess_exec`. The OS enforces these limits automatically.
4. **Workspace Path Restriction (Validation Layer)**: `aether_engine/validation/pre_flight.py` now resolves target paths for file modification commands and asserts they are relative to declared workspace roots (`~/AetherWorkspace/` by default). **Important scope limitation**: This is a validation-layer check on the initial tool call arguments. It is not an OS-level file system jail or restricted token. A malicious script executing within an approved tool context could still technically attempt out-of-bound file accesses that are not caught by Python's argument validation.
5. **Approval Override**: The UI allows users to override the inferred task class before granting approval.
6. **Audit**: Subprocess termination (by normal completion or limit breach) is recorded in the structured audit log.

## Consequences
- Security and reliability are improved for normal operations.
- Long-running compilations must correctly classify as "heavy", or users must manually override them in the UI.
- The `pywin32` dependency is now required on the backend.
- Workspace bounds are validated but not strictly OS-enforced, representing a balanced step towards security that aligns with ADR 0009.
