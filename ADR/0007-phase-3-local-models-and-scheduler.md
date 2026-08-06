# ADR 0007: Phase 3 Local Models Task Pipeline & Scheduler Plumbing

## Status
Accepted

## Context
Phase 3 requires:
1. Managing local models (Ollama integration) with Engine task-driven downloads streaming progress percentages to the UI.
2. Background capability check scheduling using `APScheduler` with startup misfire recovery and SQLite run history persistence.

Key design questions:
- How should local model downloads be structured without blocking the UI thread or breaking audit logging?
- How should scheduled capability check jobs recover if the Engine was offline during a scheduled trigger?
- Where should capability check execution history be persisted?

## Decision
1. **Engine Task-Driven Local Downloads**:
   - Model downloads execute as real Engine tasks (`run_task` / `TaskState` lifecycle) managed by `OllamaManager` in `aether_engine/models/local_models.py`.
   - Live download percentage ticks are broadcast over WebSocket as `LOCAL_MODEL_DOWNLOAD_PROGRESS` events.
   - The UI launches downloads by prompting for a destination directory via `QFileDialog.getExistingDirectory` and passing it to the Engine.

2. **APScheduler with Startup Misfire Recovery**:
   - Capabilities scheduling uses `AsyncIOScheduler` running in the Engine process (`aether_engine/scheduler/capability_jobs.py`).
   - `misfire_grace_time=None` is configured on periodic jobs so that any run missed while the Engine was offline triggers immediately on next startup.

3. **SQLite Persistence for Run History**:
   - Run history records are stored in `~/.aether/capabilities.db` under table `capability_check_runs(id, method, started_at, finished_at, models_tested, status, cost_estimate, error_log)`.
   - WebSocket events `CAPABILITY_CHECK_SET_SCHEDULE`, `CAPABILITY_CHECK_RUN_NOW`, and `CAPABILITY_CHECK_GET_HISTORY` expose job management to the UI shell.

## Consequences
### Positive
- Local model downloads retain full audit logging and cancellation support through standard Engine tasks.
- Offline scheduled runs are never dropped or silently lost.
- Run history persists across application restarts in lightweight SQLite.

### Negative / Trade-offs
- Requires `apscheduler` dependency in the Engine environment.
- Ollama service must be running locally for live blob pulling; stubs stream gracefully if Ollama is unavailable.
