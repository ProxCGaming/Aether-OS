# Changelog

All notable changes to the **Aether-OS** project will be documented in this file. 

## [Unreleased] (Current Session)

### Added
- **[2026-09-24] Live Tool Activity UI & Event Bus**: Added global event bus `aether_engine/event_bus.py` and `TOOL_ACTIVITY` events to stream tool invocations, parameters, and outputs in real time. Updated `DraggableChatWindow.jsx` with glassmorphic, expandable tool cards with syntax-highlighted payloads.
- **[2026-09-24] Persistent Episodic Thinking History**: Added SQLite episodic thinking log table in `~/.aether/memory.db` and updated `session_store.py`, `executor.py`, and `DraggableChatWindow.jsx` to persist and restore execution pipelines and thinking steps across application restarts.
- **[2026-09-24] Intra-Provider Fallback Resilience**: Extended `_create_provider_instance` in `app.py` to automatically include healthy alternative models from the same provider (e.g. `gemini-2.0-flash` when using `gemini-2.5-flash`) before cross-provider candidates, ensuring single-provider setups have automatic retry resilience.
- **[2026-09-24] Project Guardrails & Git Hooks**: Initialized a root-level `package.json` and configured Husky pre-commit hooks. Commits are now strictly validated against the Conventional Commits format (`type(scope): description`) to prevent vague commit messages and enforce repository standards.
- **[2026-09-24] Mandatory AI Agent Rules**: Created `.agents/rules/01-project-standards.md`. All future AI agents operating in this repository are now strictly bound by rules that prohibit hallucinating changes, mandate frontend testing, and enforce the Conventional Commits format.
- **[2026-09-24] Frontend Test Infrastructure**: Installed `jest` and React Testing Library into the `desktop_app` Electron frontend. Authored the first smoke test (`Dashboard.test.jsx`), completely resolving the previous lack of UI test coverage and creating a safety net for future development.
- **[2026-09-24] ADR Directory Consolidation**: Cleaned up fragmented architecture documentation by moving all 19 floating ADRs from the root `ADR/` folder into `docs/adr/`. The redundant `ADR/` directory was deleted, establishing a single source of truth for design decisions.
- **[2026-09-23 04:22] Environment Diagnostics Engine**: Replaced the static UI mockup in the Settings page with a live, dynamic capability-check runner. Pressing "Run Diagnostics Now" triggers a true backend test for the Python environment, terminal access, SQLite read/write permissions, local Ollama daemon connectivity, and `TAVILY_API_KEY` verification, with live results rendering directly into the UI!
- **[2026-09-23 03:53] Auto-Title Generation**: Newly created conversations (previously generic "New Conversation") are now automatically renamed to a brief summary of the first user prompt sent in the session.

### Fixed
- **[2026-09-24] Gemini Model Discovery & Quota Exhaustion (`gemini-3-pro-image`)**: Filtered out media generation endpoints (`image`, `transcribe`, `lyria`, `audio`) in `discovery.py` that have `limit: 0` on Google AI Studio free tier. Restored `gemini-2.5-flash` as primary default model.
- **[2026-09-24] Capability Misclassification & Route Hijacking**: Fixed `_infer_capabilities` and `select_route` in `aether_engine/routing/` to reject non-chat models, grant `code` to frontier LLMs, and prioritize the user's active/default model instead of diverting to media endpoints.
- **[2026-09-24] Thinking Panel Deduplication & Scrolling Physics**: Deduplicated supervisor progress text and redundant tool cards in the thinking panel, and synchronized scrolling physics with the dropdown list.
- **[2026-09-24] LiteLLM MidStream Fallback Error Handling**: Handled `litellm.MidStreamFallbackError` and retriable quota exhaustion errors during stream execution in `litellm_provider.py` to trigger fallbacks instead of failing the task.
- **[2026-09-23 04:35] Diagnostics Loading State**: Added a dynamic loading state to the "Run Diagnostics Now" button in the Settings UI so that users receive immediate visual feedback (loading spinner, disabled state) while the backend tests are running.
- **Settings API Provider Caching**: API keys are now securely cached in the browser `localStorage` upon connection. If a key is disconnected from the backend, the UI text box remains pre-filled, saving users from needing to re-enter it manually.
- **Model Disconnect Bug**: Fixed a UI bug in `App.jsx` where disconnecting a provider or saving a new configuration failed to instantly refresh the available model dropdown.
- **Capability Auto-Router Priorities**: Fixed the backend `capability_router.py` logic which was ignoring the user's "Global Default Model" choice. The tiebreaker score was increased from `+1.0` to `+100.0`, ensuring standard chats always respect the user's choice instead of silently falling back to OpenRouter.
- **LiteLLM Missing Dependency**: Installed the `google-generativeai` package to resolve unexpected crashes when trying to connect and route to Gemini APIs via LiteLLM.

---

## [2026-09-23]
### Added
- **Auto-Router**: Added a capability-based task auto-router and the main desktop application UI components.

## [2026-09-22]
### Added
- **Memory Graph**: Added single-fact, entity, and full-clear deletion controls to Knowledge Graph UI and backend.
- **Episodic Memory**: Added episodic deletion controls, context menu, and draggable text selection. Implemented autonomous supervisor memory orchestration (ADR 0019).
- **Hybrid Retrieval**: Implemented RRF hybrid retrieval with local SQLite embeddings database. Added ADR 0020 for documentation.
- **Workspace UI**: Added true workspace folder persistence and dialog selection to Projects view.
- **Local Models**: Fetch local model catalog from JSON, added destination path & download cancellation.
- **Metrics**: Wired Dashboard metrics to real WebSocket status, task counts, plugin counts, and memory IPC.

## [2026-09-21]
### Changed
- **Styling**: Updated global glassmorphism tokens, window controls, and ambient animations across secondary tabs.
- **Chat Window**: Polished floating window layout, optimized margins for native resizing, and implemented seamless session state transfer via localStorage.
- **Electron Migration**: Added Electron desktop application with React frontend and migration documentation.

### Fixed
- **Electron Backgrounds**: Removed OS-level vibrancy from floating window to prevent ghost shadow backgrounds.
- **Dropdown UX**: Implemented smooth lerp scrolling, sticky headers, and fixed overflow/drag glitches.

## [2026-09-20]
### Changed
- **Architecture**: Deprecated old Python-based UI components in favor of new Electron/React desktop app.
- **Backend API**: Updated API to support the new React frontend and advanced model providers.
- **Memory Subsystem**: Added ADRs and tests for the memory subsystem.

## [2026-09-18]
### Added
- **Plugins**: Implemented plugin installer, uninstaller, and manifest validation tests.
- **LangGraph Nodes**: Updated nodes, routing, and provider discovery mechanisms.

### Fixed
- **Settings State**: Improved settings tabs, provider state, and secret storage handling.

## [2026-09-16]
### Added
- **Core Architecture**: Implemented complete Aether application architecture including engine, UI, orchestration, and test suite.
- **UI Tabs**: Implemented plugins, agents, and skills tabs; fixed hardcoded model dropdown.

### Fixed
- **Model Discovery**: Disabled exhaustive reachability sweeps to speed up discovery.
- **Routing Loop**: Resolved infinite delegation loops and model discovery 404 errors.
- **Provider UX**: Improved provider config dialog UX with loading states and proper validation routing.
