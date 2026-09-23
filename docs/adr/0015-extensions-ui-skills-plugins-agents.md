# ADR 0015: Extensions UI, Skills, Plugins, and Agents Architecture

**Date**: 2026-09-18
**Status**: Accepted

## Context
Aether OS requires a way to extend its capabilities beyond built-in tools. In Phase 5.5, we introduce the Extensions UI panel, which allows users to view available skills, install custom plugins, and manage the active LangGraph agent nodes. 
This phase provides the necessary infrastructure for user-driven extension management.

## Decisions

### 1. Unified HITL Approval Flow for Plugins
We decided to reuse the exact same Human-in-the-Loop (HITL) approval drawer mechanism created in Phase 4/5.1 for tool execution to handle plugin installations.
- **Why**: It reduces duplicated UI code and enforces a consistent user experience for any action that requires explicit user consent, whether it's executing a risky shell command or installing a new extension package.
- **How it works**: The UI requests preparation from `POST /plugins/install`, receives the manifest, and triggers a local `TOOL_APPROVAL_REQUEST` WS event. The `ApprovalDrawer` pops up, and when approved, the UI directly intercepts this locally to call the `POST /plugins/confirm` endpoint, keeping the actual installation RESTful.

### 2. Atomic Plugin Installation and Rollback
Plugin installations must be strictly atomic.
- **Why**: Partial installations (e.g. skills copied but tools fail to register) leave the engine in an unstable state. 
- **Implementation**: During `PluginInstaller.install()`, if any step (extracting zip, copying SKILL.md, registering tools, or adding MCP servers) throws an exception, all previous steps are undone. A backup of the previous plugin state (if it was an update/reinstall) is restored.

### 3. Read-Only Agent Nodes Visualization
The Agents tab relies on real-time assignment querying rather than static lists.
- **Why**: The LangGraph supervisor dynamically routes tasks to the `researcher`, `planner`, or `coder` nodes based on capability requirements.
- **Implementation**: The UI queries `/agents/nodes` to fetch the available nodes and their live assigned models based on `GLOBAL_CAPABILITY_ROUTER`. Nodes can be toggled (disabled), which removes them from the Supervisor's available options in its routing prompt.

### 4. Zero Cryptographic Trust (Current Limitation)
For Phase 5.5, plugins are fully trusted once approved by the user. There is no cryptographic signing, registry verification, or isolation sandboxing for plugin code beyond the existing worker sandboxing for tools.
- **Why**: Scope management. A full plugin marketplace and signing infrastructure is out of scope for this foundational phase.

## Consequences
- **Positive**: The unified approval UI makes Aether feel highly cohesive. The atomic rollback ensures users cannot brick their installation by installing malformed plugins.
- **Negative**: The lack of cryptographic signing means users must manually verify the integrity of zip files before approving installation. Future phases will need to introduce package signing or a registry.
