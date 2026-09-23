# ADR 0016: Provider/Model Persistence and Approval Audit Fixes

## Date
2026-09-18

## Context

During a post-Phase 5.5 stability review, multiple issues were observed regarding the persistence of default provider/model selections, the completeness of the audit trail during tool approvals, and the reuse of legacy approval events for the new plugin installation flow.

1. **Provider/Model Mismatch**:
   When users selected a new model from the HUD combobox, the UI only emitted the model name in its `model_changed_by_user` signal. The main application window subsequently paired this new model with whatever it considered the `_current_default_provider`. This led to invalid pairs being persisted to `config.json` (e.g., `default_provider: "google_gemini"` paired with `default_model: "deepseek-v4.1-flash"`). Upon engine restart, the validation logic correctly flagged this mismatch but incorrectly initiated a fallback, discarding the user's intent.

2. **Approval Audit Trail Completeness**:
   The LangGraph-based tool approval loop (introduced in Phase 5) relies on native checkpointing. While execution events were logged, the exact moments when a human-in-the-loop (HITL) approval was requested, and subsequently granted or rejected, were absent from the `audit.log`.

3. **Plugin Approval Ambiguity**:
   The Phase 5.5 plugin installer triggered the `ApprovalDrawer` by emitting a mock `TOOL_APPROVAL_REQUEST`. While functional, reusing an execution-level event for administrative package installation violates the separation of concerns and creates potential vectors for confusing or bypassing the sandbox.

## Decision

1. **Fix HUD Dropdown Signals**:
   We modified the `model_changed_by_user` signal in `aether_ui/hud.py` to emit both the provider and the model name `(str, str)`. The main listener now respects the provided provider, ensuring that `config.json` is always updated with valid, internally consistent provider/model pairs. This effectively resolves the "hardcoded models dropdown" issue where the provider was implicitly hardcoded to the previous default.

2. **Introduce Explicit Audit Logging for Approvals**:
   We updated `aether_engine/app.py` to intercept `EventType.TOOL_APPROVAL_REQUEST` coming from the LangGraph executor and log an `APPROVAL_REQUESTED` event to the `audit_logger`. Similarly, `TOOL_APPROVAL_GRANTED` and `TOOL_APPROVAL_REJECTED` messages from the client now trigger corresponding `APPROVAL_GRANTED` and `APPROVAL_REJECTED` audit log entries before execution resumes.

3. **Isolate Plugin Approval Flow**:
   We introduced a distinct `EventType.PLUGIN_APPROVAL_REQUEST`. The plugin tab now emits this event, and the main window's routing logic has been updated to explicitly recognize and route this event to the `ApprovalDrawer`. This ensures plugin installations are distinct from standard execution approvals while still benefiting from the same native UI components.

## Consequences

- **Positive**:
  - `config.json` state remains consistent, preventing unexpected fallbacks on engine restart.
  - The `audit.log` now provides a comprehensive, end-to-end view of the HITL approval lifecycle.
  - Plugin installations have a dedicated event type, enabling future architectural divergence (e.g., different UI representations or elevated auth requirements) without impacting the LangGraph executor.
- **Negative**:
  - Requires UI and Engine to stay in sync regarding the new `PLUGIN_APPROVAL_REQUEST` event type.
