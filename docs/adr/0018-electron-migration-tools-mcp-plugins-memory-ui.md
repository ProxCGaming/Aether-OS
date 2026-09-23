# ADR 0018: Electron Migration - Tools, MCP, Plugins, and Memory UI

## Status
Accepted

## Context
During the migration of Aether-OS from a PySide6 interface to an Electron-based React frontend, we needed to port the remaining advanced settings functionality: Tools policy management, MCP server configuration, Plugin management, and Memory viewers (Knowledge Graph and Episodic Memory). The backend architecture also needed to be updated to expose these capabilities via WebSocket rather than relying on HTTP polling or static PySide6 bindings.

## Decision
We decided to implement the following features directly in the React frontend (`SettingsPanel.jsx`) communicating over WebSocket (`app.py`):
1. **Tools:** Added a WebSocket endpoint to request the list of tools and set policies. The frontend displays tools in a list with a dropdown for "Always Allow," "Require Approval," and "Deny." 
2. **MCP (Model Context Protocol):** Added UI for managing and adding local MCP servers via WebSocket requests to `GLOBAL_MCP_REGISTRY`. 
3. **Plugins:** Migrated plugin installation and management to use the same WebSocket-based async approval workflow as Tools, utilizing `PLUGIN_APPROVAL_REQUEST` and `App.jsx` side effects to prompt the user before finalizing installation. 
4. **Memory:** Introduced a tabbed interface inside the Memory tab:
   - **Episodic Memory:** A Master-Detail view showing a list of conversation turns, allowing the user to click into specific tasks to see prompts, responses, timestamps, and task IDs. 
   - **Knowledge Graph:** A grid of cards grouped by `entity_name` representing the extracted facts about the user or the environment.

## Consequences
- **Positive:** Full feature parity with the PySide6 implementation in the new Electron architecture. The asynchronous nature of WebSockets provides a much smoother UI for Plugin installation and MCP configuration. 
- **Negative:** `app.py` continues to grow as the central monolithic router for all WebSocket events. Future refactoring should consider splitting WebSocket event handlers into modular routers based on `EventType`. 
