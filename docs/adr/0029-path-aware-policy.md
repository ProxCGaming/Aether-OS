# ADR 0029 — Path-Aware Tool Policy (Epic 2, E2-06)

- Status: Accepted
- Date: 2026-10-31
- Branch: `build/full-build`
- Context: Tool policy resolved by tool NAME only (ADR 0021): a
  `write_file` call was "ask" everywhere or, for read-only tools,
  allowed everywhere. Real work needs the verdict to depend on the
  call: writing inside the project the user is working on is routine;
  writing to `~/.ssh` never is. The master prompt directs that the
  existing policy engine be extended, not replaced.

## Decisions

### D1. One resolver, refined by arguments
`resolve_tool_policy` / `resolve_policy_by_name` gain optional
`args` and `path_rules`. Call sites that judge a SPECIFIC call — the
graph's `tool_router` and `ToolRegistry.execute` — pass both; call
sites that judge a tool in the abstract (tool offering, tool lists)
pass neither and keep the legacy name-level answer. With no args or
no rules the verdicts are byte-identical to the old logic.

### D2. Path rules are ordered, first-match, and cannot override explicit per-tool settings
A rule is `{prefix, verdict, tools?, risks?}` in
`UserConfig.path_rules`. A matching rule can relax the DEFAULT ask
for non-read-only tools (its purpose) or tighten anything, but an
explicit per-tool `Deny` still denies and an explicit per-tool
`Require Approval` still asks. Precedence: deny > explicit ask >
path verdict > legacy defaults.

### D3. A built-in sensitive floor sits under every allow
No configuration — not Always Allow, not a path rule — can auto-allow
a call whose path resolves under `~/.ssh` or `~/.aether` (any risk:
reading a key into a model context is exfiltration), or a
non-read-only call under system prefixes (`/etc`, `/usr`, …). The
floor raises allow → ask; it never denies, and the user can still
approve per call.

### D4. Path rules inherit the config-layer trust boundary
`path_rules` joins `tool_policies` and `workspace_roots` in
`config_layers.PROJECT_FORBIDDEN_KEYS`: a cloned workspace's
`.aether/config.json` cannot relax the rules that govern it. The
per-task overlay from the user's own client keeps full power.

## Consequences
- Approvals become rarer exactly where the user declared trust, and
  guaranteed exactly where they didn't think to look.
- The resolver stays the single policy seam (ADR 0026 D3): approval
  modes (E2-08) and the approval cache (E2-09) layer onto its verdict,
  not around it.
