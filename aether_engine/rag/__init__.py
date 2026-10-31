"""Workspace RAG (Epic 8, Item 3).

Two deliberately separate layers over the granted workspace:

* ``repo_map`` — the ambient code layer for graph tasks: tree-sitter
  signatures ranked by a defs/refs PageRank, rebuilt from live files.
  Code is never embedded (grill Q16); the map is computed locally and
  only ever enters the supervisor's context.
* ``prose_indexer`` / ``store`` (E8-3.2) — the embedded prose index
  behind the deliberate ``workspace_search`` tool. Its own store: it
  does NOT switch when the Epic 4 memory backend switches.
"""
