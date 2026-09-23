# ADR 0006: Models & Settings UI (Phase 3)

## Status
Accepted

## Context
Phase 3 introduces a user-facing settings interface for managing LLM provider credentials and selecting default models. The UI must remain non-blocking (ADR 0004), communicate over the authenticated loopback WebSocket (ADR 0003), and store secrets via DPAPI (ADR 0005).

Key design questions:
1. Should provider management use new REST endpoints or extend the existing WebSocket?
2. Should the settings UI be a separate window or an overlay within the main window?
3. Where should non-secret configuration (default provider/model) be persisted?

## Decision
1. **WebSocket-only IPC**: Provider management uses new `EventType` members on the existing authenticated WebSocket channel. No REST endpoints are added. This avoids a second authentication surface and keeps all IPC through the established audit-logged channel (ADR 0003).

2. **Overlay drawer system**: The settings UI is a two-stage sliding drawer within the main window:
   - A narrow (~240px) settings menu slides in from the left edge.
   - A wider (~560px) models panel extends sideways from the menu.
   - Both use `QPropertyAnimation` for smooth transitions.
   - The main orb and HUD remain visible and fully functional at all times.
   - All widgets are native PySide6 `QWidget`s with `QPainter`-compatible styling (ADR 0004).

3. **Separate config file for non-secrets**: The user's default provider and model selection are persisted in `~/.aether/config.json` — a plain JSON file. API keys remain encrypted in `~/.aether/secrets.db` via DPAPI (ADR 0005). This separation prevents unnecessary DPAPI overhead for non-sensitive preferences and avoids coupling config reads to Windows user context.

4. **Static provider registry**: Cloud providers are defined in a hardcoded list in `aether_engine/config.py`. No dynamic provider discovery or internet fetching occurs. Local model listings are also static placeholders.

## Consequences
### Positive
- Single authenticated channel for all IPC — no new attack surface.
- Drawer system does not block or replace the main UI.
- Non-secret config is portable and human-readable.
- Static provider list is deterministic and offline-safe.

### Negative / Trade-offs
- Adding new providers requires a code change to the static registry.
- WebSocket message handling grows with each new event type.
