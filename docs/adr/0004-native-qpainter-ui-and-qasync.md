# ADR 0004: Native QPainter UI Widgets and qasync Integration

## Status
Accepted

## Context
Previous prototypes explored embedding a Chromium web view (`QWebEngineView`) for UI rendering. However:
- `QWebEngineView` brings enormous memory and binary footprint (~150MB+ extra process memory).
- Initializing the web engine adds cold-start latency.
- Seamless desktop transparency, frameless resizing, and ultra-smooth ambient pulsing animations are more efficient, lightweight, and reliable when rendered natively via `QPainter`.

Additionally, the UI must maintain an asynchronous WebSocket connection to the Engine without blocking the UI thread or requiring multi-threaded locks.

## Decision
1. **Native QPainter Widgets**:
   - The Orb (pulsing visual status indicator) and Command HUD are built as native PySide6 `QWidget`s rendered with `QPainter`.
   - The Orb is driven by an independent `QTimer` (~30 FPS) modifying glow and pulse radius.
   - The HUD includes an independent 1.0s heartbeat counter, proving visually that network streaming never blocks the UI thread.
   - Heavier web-based canvases (e.g., artifact rendering) are deferred to future phases.
2. **`qasync` Event Loop**:
   - We merge Python `asyncio` with the Qt event loop using `qasync`.
   - Async network I/O (`websockets`) runs cooperatively on the UI thread without blocking.
3. **Decoupled Qt-Free State Machine**:
   - UI state transitions (`DISCONNECTED`, `CONNECTING`, `CONNECTED`, `TASK_RUNNING`, `ERROR`) live in a pure-Python module (`state.py`) with zero Qt imports, enabling unit testing without a display.

## Consequences
### Positive
- Ultra-low memory and CPU footprint.
- Instant startup time without WebEngine initialization delays.
- Clear visual regression signal: if an async call accidentally blocks, the Orb/heartbeat visibly freezes.
- Fully unit-testable state machine without GUI dependencies.

### Negative / Trade-offs
- UI layout and custom graphics must be defined with Qt stylesheets / `QPainter` instead of HTML/CSS.

### Related ADR
- The approval drawer introduced in [0009-execution-sandbox-and-hitl.md](0009-execution-sandbox-and-hitl.md) follows the same native PySide6/QPainter model and keeps the UI responsive via the existing async event loop.
