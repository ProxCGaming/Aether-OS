# ADR 0001: Multi-Process Architecture and Process Isolation

## Status
Accepted

## Context
AETHER is a local-first Windows AI desktop assistant. The application consists of distinct functional responsibilities:
1. **User Interface (UI)**: Desktop shell responsible for rendering ambient visual feedback (Orb, Command HUD), handling user input, and displaying task progress with 60 FPS fluidity.
2. **Engine**: Core business logic coordinator managing task lifecycles, WebSocket IPC, and worker orchestration, memory, and LLM integrations.
3. **Execution Workers** (future phase): Sandboxed child processes executing tools, file operations, and code in restricted environments.

Running these components in a single process (or relying on Python threads) introduces critical vulnerabilities:
- Python's Global Interpreter Lock (GIL) and long-running synchronous or heavy async tasks can freeze the UI thread.
- Memory corruption or crashes in tool execution or engine logic would terminate the entire desktop assistant.
- Privilege leakage: the UI shell would share the same security context and memory space as task execution.

## Decision
We enforce strict **OS process isolation**:
- The UI and the Engine run as separate OS processes.
- The processes **never share memory**.
- All inter-process communication occurs strictly over authenticated loopback WebSockets (`127.0.0.1`).
- The UI never performs network scraping, heavy computation, or disk modifications directly.

## Consequences
### Positive
- **Fault Isolation**: A crash or high-load operation in the Engine or Worker cannot crash or freeze the UI desktop shell.
- **Responsiveness**: The UI Qt event loop remains completely fluid and responsive.
- **Clear Boundaries**: IPC messages must be explicitly defined and validated via schema-versioned event contracts.

### Related ADR
- See [0009-execution-sandbox-and-hitl.md](0009-execution-sandbox-and-hitl.md) for the worker-based sandbox and approval flow derived from this isolation model.

### Negative / Trade-offs
- Requires IPC serialization overhead (JSON over WebSocket).
- Requires process lifecycle management (startup and graceful shutdown synchronization).
