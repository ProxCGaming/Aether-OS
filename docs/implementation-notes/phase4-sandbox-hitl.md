# Phase 4 Implementation Notes

## Summary
Phase 4 introduces a lightweight execution sandbox with human-in-the-loop approval for risky tools.

## Key implementation points
- The engine validates risky tool requests before execution.
- The orchestration layer can pause for an approval decision before continuing.
- The UI surfaces the approval request in a native drawer and returns the decision over the authenticated loopback websocket.
- The worker process handles approved actions in a separate process and returns structured JSON output.

## Files involved
- [aether_engine/validation/pre_flight.py](../../aether_engine/validation/pre_flight.py)
- [aether_engine/orchestration/simple.py](../../aether_engine/orchestration/simple.py)
- [aether_engine/app.py](../../aether_engine/app.py)
- [aether_ui/components/approval_drawer.py](../../aether_ui/components/approval_drawer.py)
- [aether_ui/main.py](../../aether_ui/main.py)
- [aether_ui/ws_client.py](../../aether_ui/ws_client.py)
- [aether_worker/__main__.py](../../aether_worker/__main__.py)

## Verification
The workflow is covered by regression tests in [tests/test_orchestration_simple.py](../../tests/test_orchestration_simple.py) and [tests/test_pre_flight_validation.py](../../tests/test_pre_flight_validation.py).
