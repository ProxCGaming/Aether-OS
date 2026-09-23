# ADR 0002: Stdlib-Only Shared Contracts (`aether_common`)

## Status
Accepted

## Context
Cross-process IPC messages must be structured and validated on both the sending and receiving sides. Frameworks like `pydantic` are commonly used in FastAPI backends, but adding Pydantic to a shared contract package imposes heavy external dependencies onto the UI client process.

The UI process needs to remain minimal, fast to initialize, and independent of backend web frameworks.

## Decision
We define all shared contracts in a dedicated package named `aether_common` built exclusively with Python Standard Library modules:
- Standard `dataclasses` for contract records (`Event`).
- Standard `enum.Enum` for event types (`EventType`) and task states (`TaskState`).
- Standard `json`, `uuid`, `time`, and `secrets` for serialization, identification, timestamps, and authentication.

Pydantic may be used internally within the Engine at its HTTP/WS boundary if needed, but must convert immediately into `aether_common` standard dataclasses.

## Consequences
### Positive
- Zero external dependencies for `aether_common`.
- The UI client process does not require FastAPI, Pydantic, or heavy web dependencies.
- Unit tests for contracts and state transitions run out of the box with standard `python -m unittest`.

### Negative / Trade-offs
- Manual validation logic (e.g., schema version checks, transition validation) must be explicitly implemented instead of relying on Pydantic validators.
