"""Run-configuration snapshots and checkpoint history repair.

Split out of ``langgraph/executor.py`` in the Wave 5 craft pass:
the run-config snapshot (seeded into graph state so a resume can
rebuild the paused task's own configuration, E1-F04), its
checkpoint read-back, and the session-store history-repair walk
(E1-F05) are one job, distinct from run/resume orchestration.
``langgraph.executor`` re-exports every name defined here; its
namespace is the historical import and patch surface (the routes
layer imports ``get_persisted_run_config`` through it).
"""

import logging
from typing import Any

try:
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    from aether_engine.langgraph.state import message_dicts
except ImportError:
    AsyncSqliteSaver = Any  # type: ignore
    message_dicts = None  # type: ignore

from aether_engine.config import UserConfig
from aether_engine.providers.base import BaseProvider

logger = logging.getLogger(__name__)


def _run_config_snapshot(
    *,
    provider: BaseProvider,
    session_id: str | None,
    agent_models: dict[str, str] | None,
    agent_tools: dict[str, list[str]] | None,
    workspace_roots: list[str] | None,
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    """The run's effective configuration as plain JSON-safe data.

    Seeded into the graph state (``run_config``) so the checkpointer
    carries it across an approval pause: E1-F04 — a resume rebuilds
    the paused task's OWN provider, per-agent models/tools, forced
    agent, workspace roots and layered user config from this
    snapshot instead of discarding them for the current globals.
    Only names and values are recorded; live objects (provider
    instance, tool registry, secret store) are rebuilt at resume
    time and never enter the checkpoint.
    """
    user_config = kwargs.get("user_config")
    return {
        "provider_name": getattr(provider, "provider_name", None),
        "model": getattr(provider, "model", None),
        "session_id": session_id,
        "agent_models": dict(agent_models or {}),
        "agent_tools": {
            name: list(tools) for name, tools in (agent_tools or {}).items()
        },
        "forced_agent": kwargs.get("forced_agent"),
        "workspace_roots": [str(root) for root in (workspace_roots or [])],
        "user_config": (
            user_config.to_dict() if isinstance(user_config, UserConfig) else None
        ),
        "local_only": bool(kwargs.get("local_only")),
        "require_vision": bool(kwargs.get("require_vision")),
    }


async def get_persisted_run_config(thread_id: str) -> dict[str, Any] | None:
    """Read the run-config snapshot checkpointed with a thread's state.

    Returns None when the thread has no snapshot (a task paused
    before snapshots existed, or an unreadable checkpoint) — callers
    fall back to their previous behaviour for those threads.
    """
    # compile_graph and DEFAULT_CHECKPOINT_DB resolve through the
    # executor module at call time: the suites patch both on the
    # executor namespace (their historical home), and rewind.service
    # treats the executor-bound DEFAULT_CHECKPOINT_DB as the operative
    # value — a direct import here would bind a private copy those
    # patches never reach.
    from aether_engine.langgraph import executor as executor_module

    db_path = executor_module.DEFAULT_CHECKPOINT_DB
    if not db_path.exists():
        return None
    try:
        async with AsyncSqliteSaver.from_conn_string(str(db_path)) as saver:
            await saver.setup()
            graph = executor_module.compile_graph(checkpointer=saver)
            state = await graph.aget_state(
                {"configurable": {"thread_id": thread_id}})
            snapshot = (state.values or {}).get("run_config")
            return snapshot if isinstance(snapshot, dict) else None
    except Exception:  # best-effort read: a resume must never fail because the snapshot could not be loaded
        logger.debug(
            f"Could not read persisted run config for thread {thread_id}",
            exc_info=True)
        return None


def _turn_text(content: Any) -> str:
    """Plain text of a message content: strings pass through;
    multimodal part lists contribute their text parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ).strip()
    return ""


def _turn_key(role: str, text: str) -> tuple[str, str]:
    return role, " ".join(text.split())


def _missing_store_turns(
    checkpoint_messages: Any, store_history: list[dict[str, str]]
) -> list[dict[str, str]]:
    """Session-store turns the graph checkpoint does not hold (E1-F05).

    The session store is the visible conversation: it also holds the
    fast-chat turns, which never enter the graph checkpoint. Matching
    is an in-order subsequence walk — each store turn consumes the
    first identical checkpoint turn at or after the previous match —
    so graph turns are recognised as present (never duplicated) while
    turns that happened outside the graph are returned, in store
    order, for the caller to append ahead of the new prompt.
    """
    checkpoint_keys: list[tuple[str, str]] = []
    for msg in message_dicts(checkpoint_messages):
        role = msg.get("role")
        if role not in ("user", "assistant"):
            continue
        text = _turn_text(msg.get("content"))
        if text.strip():
            checkpoint_keys.append(_turn_key(role, text))
    missing: list[dict[str, str]] = []
    cursor = 0
    for turn in store_history:
        key = _turn_key(turn["role"], turn["content"])
        try:
            cursor = checkpoint_keys.index(key, cursor) + 1
        except ValueError:
            missing.append({"role": turn["role"], "content": turn["content"]})
    return missing
