# Epic 3 — MCP For Real: PR-style summary

Branch `build/full-build`. Baseline at epic start (Epic 2 close,
`0712ad5`): pytest 791 passed / 13 known-failed / 2 skipped; Jest
216/216. At Epic 3 close: **pytest 799 passed / 13 known-failed /
2 skipped; Jest 216/216 (no desktop changes)**. The 13 failures are
the long-standing pre-existing families — zero regressions.

## Starting point (verified, not assumed)
Most of the master prompt's Epic 3 already existed from the
mcp-001-tracer line, reconciled in Epic 0: official-SDK client
(stdio + Streamable HTTP, timeouts, stdio env allow-list), registry
with validated config, full server lifecycle, auto-connect, tools
as `server__tool` at EXECUTE through the central pipeline,
resources/prompts engine-side and in the UI (resources load into
chat; `/mcp:<server>:<prompt>` slash commands), 13 MCP test files
green. This epic closed what was actually missing:

## What shipped, per issue
- **E3-01 — Demo-001 proof + audible drop** (`a459b1a`, tests in
  `afc893c`): the owner's failed Windows demo is now an automated
  end-to-end test — fake server → registry → supervisor graph →
  planner calls `fake__add` → approval pause → resume → the planner
  sees the result 8. Separately, the supervisor adapter's drop of
  model-emitted non-memory tool calls (issue 012's failure class)
  is no longer silent: names are logged at WARNING and recorded on
  the routing decision (`dropped_tool_calls`, in the delegation
  log). The boundary itself is unchanged and is now ADR 0031 D1:
  the supervisor routes and remembers; specialists execute, through
  the one pipeline.
- **E3-02 — Risk overrides** (`c67a83f`, ADR 0031 D2): the grill
  PRD's last unimplemented proposal. `UserConfig.
  mcp_tool_risk_overrides` maps `server__tool` → risk;
  `get_mcp_tools(risk_overrides=…)` returns overridden copies
  (cache never mutated) at all three websocket build sites, so
  policy, plan mode, and the tool list share the owner's
  classification. Invalid entries are cleaned at load; the key is
  behind the config-layer trust boundary.
- **E3-03 — Flake hardening** (`afc893c`): the intermittent
  full-suite extra failure (~1 in 5 runs; never reproduced in 5
  isolated runs or 4 of 5 full runs during this epic) was chased
  with junit capture. The one latent defect found and fixed: the
  returns-immediately auto-connect test configured a 30s connect
  timeout against a 15s wait budget — a spurious failure anywhere
  a refused connection degrades slowly. Timeout is now 5s, with the
  reasoning in the test. No product-code change was justified by
  the evidence.
- **E3-04 — Misbehaving-server bounds** (tests in `afc893c`): a
  server that accepts but never answers initialize fails connect
  inside its configured timeout with a real error and clean
  registry state; a hanging tool call returns an error string
  inside the tool timeout. Both behaviours verified as already
  correct in the product; the tests now pin them.

## Deliberately not done
OAuth for remote MCP servers and SSE transport (grill exclusions);
per-agent MCP filtering (grill Q10: all agents get all MCP tools);
the owner's Windows re-run of Demo 001 — the automated proof now
covers the flow, and his manual pass remains welcome but is no
longer the only evidence.
