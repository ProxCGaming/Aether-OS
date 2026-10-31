# ADR 0035 — Hooks, TTSR, and what "disabled" means

Status: accepted, 31 Oct 2026. Epic 7.

## Decisions

1. **Hooks are code registrations on the single pipeline.**
   `ToolRegistry.execute` is the one choke point every tool call
   traverses (built-in, MCP, graph node — the node is a thin adapter
   over it). Hooks register in `aether_engine/tools/hooks.py`:
   `tool_call` hooks may pass / rewrite args / block; `tool_result`
   hooks may replace output. Fail-closed is structural: a throwing
   call hook blocks; a throwing result hook withholds the output
   (the hook existed to vet it — unvetted output does not flow).
   No hook code is loaded from config or disk in v1.
2. **TTSR rules are user config, chunk-scoped, off by default.**
   `UserConfig.ttsr_rules` entries are validated at load (invalid
   regex/action dropped with a warning). `redact` and `block` act on
   streamed chunks in the provider. The chunk-boundary limitation
   (a pattern split across chunks can slip) is accepted and
   documented — TTSR is a seatbelt, not a proof.
3. **"Disabled" means absent from the agent's world.**
   - Skills: the manager's index excludes disabled skills, so
     slash resolution, auto-match, and content loading all miss
     them at once. Settings lists them with `enabled=False` purely
     so they can be re-enabled.
   - Plugins: toggle performs teardown (MCP servers removed, skill
     dirs moved out of the scanned tree) and rebuild (configs
     re-validated before re-adding). A flag nothing reads is not a
     disable.
4. **Plugins never contribute executable code.** A plugin package
   carries a manifest, skill files, and MCP server configs. Its
   "tools" entries are declarative metadata; execution happens only
   in MCP server processes (isolated by the MCP client) and in the
   sandboxed worker for built-ins. Install-time code execution does
   not exist and runtime plugin code loading is not added.

## Consequences

- Custom guardrails ship as small registrations beside the engine,
  not edits to the pipeline; each hook outcome is auditable.
- Skill auto-match is live (executor merges matches at task start)
  and the supervisor sees a summaries-only catalogue — progressive
  disclosure now has both stages wired and tested.
