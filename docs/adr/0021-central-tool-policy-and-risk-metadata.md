# 0021. Central Tool Policy and Risk Metadata

## Status
Accepted

## Context
Aether OS executes tools in a sandboxed worker (`execute_tool_in_worker`) or in-process depending on their risk. Previously, this routing relied on a hardcoded set of tool names (`RISKY_TOOLS`), duplicated across `execute_tool.py` and `graph.py`. User tool policies (e.g., "Deny", "Require Approval") set in the frontend were saved but ignored during agent execution. Supervisor memory tools also bypassed policies. This resulted in a disjointed user experience where the frontend promised control but the backend ignored it.

## Decision
1. **Tool Risk Metadata:** Extend the `Tool` dataclass with a `ToolRisk` enum (`read_only`, `write`, `destructive`, `execute`). 
2. **Central Policy Engine:** Introduce `policy.py` to resolve the effective policy for any tool. The policy is determined by the explicit user setting, or if unset, defaults based on `ToolRisk`: `read_only` defaults to "allow", while all others default to "ask" (Require Approval). 
3. **Sandbox Routing by Risk:** The `tool_router` and `execute_tool_node` now use `tool.risk` rather than a hardcoded list of names to route risky tools to the sandbox worker.
4. **Three-Layer Policy Enforcement:**
   - **Layer 1 (Offer):** Supervisor nodes filter out tools that resolve to "deny" before providing the schema to the model.
   - **Layer 2 (Route):** `tool_router` in `graph.py` routes to `pause_for_approval` if the resolved policy is "ask".
   - **Layer 3 (Execute):** `execute_tool_node` rejects execution if the policy is "deny" or if an "ask" tool was called but `pending_tool_call.approved` is false.
5. **Security Downgrade:** "Always Allow" is explicitly prohibited for risky tools. If requested via websocket, the backend downgrades it to "Require Approval" and logs an audit event.

## Consequences
- **Positive:** True human-in-the-loop control over all agent capabilities. Sandbox routing is robust to new tools since it checks risk metadata rather than names.
- **Negative:** `tool_router` now requires access to the `config` object to read policies, coupling graph structure slightly more to user configuration. Memory tools (which don't reside in `ToolRegistry`) require a fallback mechanism in `policy.py` (`KNOWN_NON_REGISTRY_TOOLS`) to enforce policies.
