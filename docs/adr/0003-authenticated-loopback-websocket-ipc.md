# ADR 0003: Authenticated Loopback WebSocket IPC & Audit Logging

## Status
Accepted

## Context
The UI and Engine processes need an efficient, real-time, bi-directional communication channel on the same Windows machine. However, opening local sockets without proper authentication can expose the Engine to unauthorized access by other local processes or malicious scripts.

## Decision
1. **Loopback-Only Binding**: The Engine strictly binds to `127.0.0.1` (never `0.0.0.0`).
2. **Ephemeral Per-Session Token**:
   - On startup, the Engine generates a high-entropy random URL-safe token (`secrets.token_urlsafe(32)`).
   - The token is written to `.run/engine.token`.
   - The UI process reads this file to connect.
3. **Authentication Handshake**:
   - The UI passes the token via `Authorization: Bearer <token>` header or `?token=<token>` query parameter.
   - The Engine validates the token using constant-time comparison (`secrets.compare_digest`).
   - Unauthenticated or invalid connection attempts are immediately rejected with WebSocket close code `1008` (Policy Violation) and logged *before* any message processing occurs.
4. **Structured Audit Logging**:
   - Every connection attempt (success/failure) and every task lifecycle transition is logged as a JSON line to `logs/audit.log`.

## Consequences
### Positive
- Strict security against unauthenticated local access.
- Timing attack mitigation via constant-time token comparison.
- Full traceability through immutable JSONL audit logs.

### Negative / Trade-offs
- Requires file coordination for token discovery between UI and Engine.

### Related ADR
- The approval and worker handshake flow described in [0009-execution-sandbox-and-hitl.md](0009-execution-sandbox-and-hitl.md) uses the same loopback WebSocket contract and per-session token model.
