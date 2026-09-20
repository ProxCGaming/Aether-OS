# Task: Complete the Electron Migration — Tools, MCP, Plugins Port + Memory UI Design

## Your role
The Electron/React frontend (`desktop_app/`) currently has a beautiful,
well-styled Settings panel, but a direct comparison against the working
PySide6 implementation it's replacing found that several tabs are either
reduced to simple on/off toggles or are literal placeholders
(`"coming soon"`) standing in for real, already-built, already-debugged
functionality. This task ports that real functionality forward — not a
redesign from scratch, a port of decisions that were already made and
tested. Where a reference implementation exists, use it as the source of
truth for behavior; only the visual presentation should change.

## Before you start
Fix this first, it's small and it's currently silently broken: in
`aether_engine/langgraph/nodes/supervisor.py`, the call is
`episodic.search_episodic_memory(original_prompt, limit=3)` — that
function doesn't exist. `aether_engine/memory/episodic.py` only defines
`query_episodes`. This is wrapped in a bare `except Exception: pass`, so
every task has been silently failing to retrieve episodic context since
Phase 6 was built, with zero visible error. Fix the call to use the
correct function name, then add a real test that would have caught this
— call the integration point (the Supervisor node), not the underlying
function directly, since the existing test bypasses this exact bug by
importing the correct name directly.

---

## Part 1: Port Tools tab (real per-tool approval policy)

**Reference implementation:** `aether_ui/settings_window.py`'s Tools
tab — per-tool policy dropdown (Always Allow / Require Approval / Deny),
one row per registered tool.

Build the equivalent in `desktop_app/src/SettingsPanel.jsx` (or extract
to its own component file if that keeps things cleaner — your call,
consistent with how `App.jsx`/`DraggableChatWindow.jsx` are already
split). Each tool gets its own row with its current policy, changeable
via the same three-state control. Wire it to the actual engine state —
this needs a real WS round-trip to read and persist policy per tool, not
local-only UI state that resets on reload.

---

## Part 2: Port MCP tab (real server management)

**Currently:** reduced to a single checkbox — `{ id: 'mcp', desc: 'MCP
tool routing' }`. There's no way to add a server at all.

**Reference implementation:** the transplanted MCP management logic
originally built in `mcp_servers_tab.py` and folded into
`settings_window.py`'s MCP tab — add server (name + endpoint),
connection status per server, connect/disconnect, remove.

Port the same real management surface into the Electron settings panel.
This needs real state — a list of configured servers with live status,
not a static list.

---

## Part 3: Port Plugins tab (real install/uninstall, HITL-gated)

**Currently:** `<h3>Plugin manager coming soon...</h3>` — despite the
backend (`aether_engine/plugins/installer.py` — manifest parsing, atomic
install/rollback, uninstall) being real and already built.

**Reference implementation:** `aether_ui/settings/plugins_tab.py`'s
install/uninstall/toggle flow, and ADR 0015/0016 for how plugin approval
is meant to work.

**Important — check before building:** `App.jsx` already handles
`TOOL_APPROVAL_REQUEST`/`GRANTED`/`REJECTED` for tool-execution
approval. ADR 0016 established a *distinct* `PLUGIN_APPROVAL_REQUEST`
event type specifically so plugin installs don't get conflated with
tool-execution approval. Confirm whether `App.jsx` currently handles
`PLUGIN_APPROVAL_REQUEST` at all — if not, extend the same approval-flow
handling to cover it, the same way `main.py` was extended on the
PySide6 side. Don't silently reuse `TOOL_APPROVAL_REQUEST` for this;
that ambiguity was specifically fixed once already.

Build: install (path or URL input) → show exactly what will be added
(skills/tools/MCP connections) → approval → atomic install; list of
installed plugins with toggle and uninstall.

---

## Part 4: Memory UI — this is genuinely new, not a port

**No PySide6 reference exists for this** — Phase 6 (episodic memory +
semantic knowledge graph) was built after the frontend split was already
underway, so there's nothing to port. This needs real design thinking,
not implementation-first coding. `PRODUCT.md` already states a relevant
product principle worth honoring: *"Memory is the core: the UI should
make the agent's memory (graphs, logs) visible and tangible, not
hidden."*

**What actually exists to show, be precise about the real data shapes:**
- **Episodic memory** (`~/.aether/memory.db`): completed tasks — prompt,
  summary, tags, outcome, timestamp. Searchable via SQLite FTS5/BM25,
  not vector similarity — relevance ranking is lexical/keyword-based,
  not semantic distance.
- **Knowledge graph**: `entities` (name, type, first_seen,
  last_updated) and `facts` (entity reference, fact text, source task,
  timestamp, confidence) — a real but simple relational structure, not
  a rich graph database. Don't over-design for graph complexity that
  doesn't exist yet (no relationship edges between entities currently,
  just entity→fact).

**Required process — research and propose before building:**
1. Before writing any component code, research how comparable
   memory-centric AI tools present this kind of data (chat history
   search, "memory" or "facts" panels, activity timelines — whatever's
   genuinely comparable) and identify 2-3 candidate UI patterns that
   would suit these specific data shapes (e.g., a searchable
   chronological list/timeline for episodic memory vs. a
   chronological+full-text-search hybrid; a simple entity-grouped list
   vs. a lightweight graph/network visualization for the knowledge
   graph, given there currently are no entity-to-entity relationships to
   visualize — a network graph may be premature given the actual data
   shape, but make that call explicitly rather than defaulting to one
   pattern without considering the alternative).
2. Write a short design rationale — which pattern for which tier, why,
   and what you explicitly decided against and why — before
   implementing. This mirrors how every other phase in this project
   required a design summary before code.
3. Build it. Wire to real data — the actual `memory.db` contents via a
   new read-only query endpoint on the engine if one doesn't exist yet
   (check `app.py` first; don't duplicate if something's already there
   from Phase 6's build).
4. This replaces the current `"Storage manager coming soon..."`
   placeholder in the Memory tab.

---

## Priority order
Fix the episodic bug first — small, and it's the actual point of Tier 2.
Then Tools and MCP, in either order — both are real regressions of
safety/functionality-relevant management surfaces. Then Plugins. Memory
UI last, since it's net-new design work rather than restoring something
that already existed.

## Explicitly out of scope
- No changes to `aether_engine`'s actual plugin/MCP/tool backend logic
  beyond the one bug fix above — this is a frontend port, the backends
  already exist and work.
- No changes to the PySide6 `aether_ui/` side — it's being retired, not
  maintained in parallel going forward (flag if you disagree with this
  given what you find, but don't silently keep extending both).
- No Advanced/Security tab work this round — separate task if needed.

## Required process
Design summary before code, same as every phase before this one —
particularly for Part 4, where the research/rationale step is not
optional.

**Write one ADR: `ADR/0018-electron-migration-tools-mcp-plugins-memory-ui.md`.**
Document: the episodic bug and fix, confirmation each ported tab now
reads/writes real engine state (not local-only UI state), whether
`PLUGIN_APPROVAL_REQUEST` needed adding to `App.jsx` or already existed,
and the full design rationale for the Memory UI's chosen patterns.

## Acceptance tests — real evidence, not descriptions
1. The episodic bug fix: paste a real Supervisor run showing episodic
   context actually gets retrieved and used (previously silently empty).
2. Tools tab: change a tool's policy in the UI, restart the app, confirm
   it persisted — not just that the dropdown showed a value.
3. MCP tab: actually add a server through the UI, confirm it shows
   connected, confirm it persists across a restart.
4. Plugins tab: install a real test plugin end-to-end through the UI,
   confirm the approval step shows correctly, confirm
   `PLUGIN_INSTALLED` appears in the audit log.
5. Memory UI: paste the actual rendered view with real data from a
   populated `memory.db`, not an empty state.

## When you're done
Report each part separately with its real evidence. Stop here — don't
touch Advanced/Security or start anything else without being asked.
