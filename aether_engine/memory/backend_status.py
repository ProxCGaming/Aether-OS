"""Master build S9: honest availability for the memory backends.

The Settings memory picker lists ALL four backends (owner
decision, 14 Nov 2026 — never installed-only filtering), so each
entry needs a truthful state: available, package not installed,
or installed but not configured, with the reason in plain words.

Probing NEVER constructs a backend. Construction has side
effects — Mem0 initialises its stores and LLM clients, Graphiti
opens a Neo4j driver and starts a dedicated event-loop thread —
so a status check that built one just to look would not be a
read. Instead each probe mirrors the adapter's own construction
gates, in the adapter's own order (see backends/*.py): the
package import first (via find_spec, which locates without
executing), then the configuration the adapter itself demands
before it will build (Mem0's LLM key, Graphiti's NEO4J_PASSWORD;
LangMem's adapter needs nothing beyond the package).

Where a probe cannot see further — Neo4j actually answering,
Mem0's own initialisation succeeding — the entry says exactly
that in its ``detail`` instead of claiming "available" means
"proven working".
"""
from __future__ import annotations

import importlib.util
import os
from collections.abc import Mapping
from typing import Any

from aether_engine.memory.facade import VALID_BACKENDS

# Facade backend name -> the import whose presence gates it.
_PACKAGE_MODULES = {
    "mem0": "mem0",
    "langmem": "langmem",
    "graphiti": "graphiti_core",
}

# Every non-native adapter is exercised in Aether's test suite
# against stand-in doubles, not the live package/service — the
# picker says so rather than dressing them as proven.
_EXPERIMENTAL_WARNING = (
    "Experimental adapter: Aether's tests exercise it with "
    "stand-in doubles, not against the live package or service."
)

_BACKEND_COPY: dict[str, dict[str, Any]] = {
    "native": {
        "name": "Native (built-in)",
        "description": (
            "Aether's own memory: the episodic store and knowledge "
            "graph in local storage under ~/.aether."
        ),
        "needs": "Nothing — built into Aether.",
        "warnings": [],
        "detail": "The default backend, fully tested with Aether.",
    },
    "mem0": {
        "name": "Mem0",
        "description": (
            "The Mem0 service library (mem0ai) as the memory "
            "store: it extracts facts and ranks search results "
            "with its own models."
        ),
        "needs": (
            "The mem0ai package, plus an LLM provider key in the "
            "engine's environment."
        ),
        "warnings": [
            _EXPERIMENTAL_WARNING,
            ("Mem0 extracts and ranks with its own models, so what "
            "gets stored and how results rank differ from the "
            "native backend."),
        ],
        "detail": None,
    },
    "langmem": {
        "name": "LangMem",
        "description": (
            "A LangMem-style adapter: a local JSON file store "
            "with LangGraph-Store-shaped items; facts are "
            "extracted with Aether's own provider."
        ),
        "needs": "The langmem package. Storage is a local file — no external service.",
        "warnings": [
            _EXPERIMENTAL_WARNING,
            ("LangMem's managed extraction pipelines are not wired "
            "in; this adapter keeps its own store "
            "(~/.aether/langmem_store.json)."),
            "Search is word-matching (substring), not semantic.",
            ("Facts are not grouped by entity — deleting a whole "
            "entity at once is not supported."),
        ],
        "detail": None,
    },
    "graphiti": {
        "name": "Graphiti",
        "description": (
            "Graphiti's temporal knowledge graph in a Neo4j "
            "database: it extracts entities and edges from "
            "episodes itself."
        ),
        "needs": (
            "The graphiti-core package, plus a running Neo4j "
            "database configured via NEO4J_URI / NEO4J_USER / "
            "NEO4J_PASSWORD."
        ),
        "warnings": [
            _EXPERIMENTAL_WARNING,
            ("Facts and episodes cannot be edited, deleted or "
            "cleared from Aether on this backend — those "
            "operations are refused by design; manage them in "
            "Neo4j directly."),
            ("The fact list Aether shows for this backend is a "
            "broad-search approximation, not a complete listing."),
            ("The adapter runs a dedicated background thread "
            "while the engine is running."),
        ],
        "detail": None,
    },
}


def _package_installed(module: str) -> bool:
    """find_spec locates a package WITHOUT importing it — probing
    must not execute the package or build anything."""
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _probe(name: str, env: Mapping[str, str]) -> tuple[str, str | None, str | None]:
    """(state, reason, detail) for one backend.

    state is "available", "not_installed" or "not_configured";
    reason is set exactly when the backend is not available.
    """
    if name == "native":
        return "available", None, None
    module = _PACKAGE_MODULES[name]
    package = {"mem0": "mem0ai", "langmem": "langmem",
               "graphiti": "graphiti-core"}[name]
    if not _package_installed(module):
        return (
            "not_installed",
            (f"The {package} package is not installed in the "
            f"engine's environment (pip install {package})."),
            None,
        )
    if name == "mem0":
        # Mem0MemoryBackend's own gate after the import:
        # Memory() with the default config needs an LLM
        # provider key; its error names OPENAI_API_KEY.
        if not env.get("OPENAI_API_KEY"):
            return (
                "not_configured",
                ("The mem0ai package is installed, but Mem0's "
                "default config needs an LLM provider key "
                "(e.g. OPENAI_API_KEY) and none is set in the "
                "engine's environment."),
                None,
            )
        return (
            "available",
            None,
            ("Checked: the package is installed and an LLM key "
            "is set in the engine's environment. Mem0 itself "
            "initialises on first use — Aether does not start "
            "it just to check."),
        )
    if name == "graphiti":
        # GraphitiMemoryBackend's own gate after the import:
        # NEO4J_PASSWORD is required before it will construct.
        if not env.get("NEO4J_PASSWORD"):
            return (
                "not_configured",
                ("The graphiti-core package is installed, but "
                "NEO4J_PASSWORD is not set in the engine's "
                "environment — Graphiti needs a Neo4j database "
                "(NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD)."),
                None,
            )
        return (
            "available",
            None,
            ("Checked: the package is installed and "
            "NEO4J_PASSWORD is set. Whether Neo4j is actually "
            "reachable is only known when the backend first "
            "runs — Aether does not connect just to check."),
        )
    # langmem: the package is the adapter's only gate.
    return "available", None, None


def probe_backend(name: str, *, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """The status entry for one backend (without ``current`` —
    :func:`memory_backend_statuses` adds it)."""
    env = os.environ if env is None else env
    copy = _BACKEND_COPY[name]
    state, reason, probed_detail = _probe(name, env)
    return {
        "id": name,
        "name": copy["name"],
        "description": copy["description"],
        "needs": copy["needs"],
        "state": state,
        "available": state == "available",
        "reason": reason,
        "warnings": list(copy["warnings"]),
        "detail": probed_detail if probed_detail is not None else copy["detail"],
    }


def memory_backend_statuses(
    user_config=None, *, env: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """The picker's full report: every backend in
    VALID_BACKENDS order, each flagged when it is the configured
    selection, plus the selection itself. An unknown configured
    value is reported as-is in ``current`` with no entry flagged
    — the picker then shows the truth (nothing claims to be
    current) instead of guessing."""
    current = str(
        getattr(user_config, "memory_backend", None) or "native"
    ).strip().lower()
    backends = []
    for name in VALID_BACKENDS:
        entry = probe_backend(name, env=env)
        entry["current"] = name == current
        backends.append(entry)
    return {"current": current, "backends": backends}


def validate_memory_backend_switch(
    name: Any, *, env: Mapping[str, str] | None = None
) -> str | None:
    """None when ``name`` may be selected, else the refusal
    reason — the same words the picker shows for that backend,
    so the server's refusal and the UI's reason never diverge.
    This is the server-side guard behind the picker's disabled
    options: a hand-sent config update gets exactly the refusal
    the UI was showing."""
    normalised = str(name).strip().lower()
    if normalised not in VALID_BACKENDS:
        return (
            f"Unknown memory backend {name!r}; valid backends: "
            f"{', '.join(VALID_BACKENDS)}."
        )
    entry = probe_backend(normalised, env=env)
    if not entry["available"]:
        return entry["reason"]
    return None
