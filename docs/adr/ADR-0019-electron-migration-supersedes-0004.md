# ADR 0019: Full Frontend Migration from PySide6 to Electron/React

## Status
Accepted (Supersedes ADR 0004: "Native QPainter UI and qasync")

## Context
ADR 0004 established PySide6/Qt with native QPainter rendering as the
frontend, explicitly rejecting a web-based UI. That decision was later
revisited when a React/Electron rewrite was evaluated on its own merits
and rejected again, for three concrete reasons on record: bundle size,
loss of direct Windows API interop for DPAPI secret encryption and Job
Object process sandboxing, and an estimated 2–4 week rewrite cost with
no clear product benefit to justify it at the time.

That evaluation was correct given what existed then. What changed: a
separate design tool was used to generate a new Electron/React frontend
from scratch (`desktop_app/`), and the product decision was made to
adopt it as a full replacement for `aether_ui/`, not a parallel
experiment. This ADR documents that decision formally, and — critically
— verifies whether the original rejection reasons still apply.

## Why this is safe: the process-isolation boundary was never crossed
The three-process architecture (`aether_ui` ↔ `aether_engine` ↔
`aether_worker`, ADR 0001) already communicated exclusively over an
authenticated loopback WebSocket (ADR 0003). This was a deliberate
choice specifically so the frontend technology would not matter to the
backend's security guarantees.

That design assumption has now been exercised, not just theorized:
- `desktop_app/main.cjs` is a thin Electron shell — it does not
  reimplement DPAPI encryption, Job Object limits, or worker sandboxing.
  It reads the session token and renders a window, with
  `contextIsolation: true` and `nodeIntegration: false` — the correct,
  locked-down Electron configuration.
- `desktop_app/src/App.jsx` connects to `ws://127.0.0.1:8000/ws/tasks`
  using the same authenticated protocol the PySide6 client used.
- `aether_engine` — where DPAPI (ADR 0005/0012), pre-flight validation
  and Job Object sandboxing (ADR 0009/0010), and the LangGraph
  checkpoint/approval mechanism (ADR 0011, fixed in 5.1) all live — is
  entirely untouched by this migration. It does not know or care what
  kind of client is on the other end of the socket.

**This means the second rejection reason (loss of Windows API interop)
does not actually apply here.** That interop was never going to live in
the frontend. The first reason (bundle size) is a real, accepted
trade-off of this decision. The third (rewrite cost) was paid.

## Decision
1. `desktop_app/` (Electron + React) is the frontend going forward.
2. `aether_ui/` (PySide6) is retired, not maintained in parallel. See
   the companion retirement task for what "retired" means concretely —
   this ADR records the decision, it does not itself complete the
   retirement.
3. Feature parity is being restored incrementally, tracked here rather
   than assumed complete:

| Feature | Status as of this ADR |
|---|---|
| Tools policy (Always Allow/Approval/Deny) | Ported, real WS-backed state |
| MCP server management (add/connect/disconnect) | Ported, real WS-backed state |
| Plugins (install/uninstall, HITL-gated) | Ported, distinct `PLUGIN_APPROVAL_REQUEST` event, not conflated with tool approval |
| Memory UI (episodic + knowledge graph) | Built new — no PySide6 equivalent existed; Master-Detail view for episodic, entity-grouped cards for knowledge graph |
| Local model download (Ollama) | Ported |
| Skills | **Not yet ported** — currently a single capability toggle, not the honest-empty-state browsing tab that existed in PySide6 |
| Advanced / Security settings | **Not yet ported** — placeholder |
| Engine lifecycle management (spawn/monitor `aether_engine` from the app itself) | **Not yet built** — `main.cjs` currently assumes the engine is already running, started manually. Required before this can be a real standalone executable, per `PRODUCT.md`'s own stated goal |
| Automated test coverage on the Electron/React side | **None yet** — zero `.test.jsx` files exist |

## Consequences
### Positive
- The backend's security-critical mechanisms are provably unaffected —
  verified by direct inspection, not assumed from the protocol design.
- The frontend can now be revised, restyled, or further redesigned
  without touching `aether_engine` at all.

### Negative / open risk
- The four "not yet" rows above are real, tracked gaps, not
  hypothetical ones. Until engine lifecycle management is built, this
  is a developer-mode app, not a shippable one.
- Zero automated test coverage on the frontend means regressions here
  are currently only caught by manual review, the same failure mode
  that let the episodic memory retrieval bug ship silently on the
  backend earlier in this project's history.
