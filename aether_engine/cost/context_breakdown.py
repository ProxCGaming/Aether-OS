"""Context breakdown token calculator (Epic 8, E8-A8.1b).

Itemizes context window usage into 7 distinct components:
1. system prompt
2. memory briefing
3. history
4. tool outputs
5. scratchpad
6. repo map
7. skill catalogue

Measurement discipline (review finding M20): every number is
MEASURED from the same builders the request path itself uses, over
the session's real state — nothing is reconstructed from stand-ins
and there are no nominal constants:

- Fast chat assembles ``[system] + history`` only, so its breakdown
  counts the real ``fast_chat._SYSTEM_PROMPT`` and the model-facing
  history from ``session_store.get_chat_history`` (the builder both
  chat paths seed from). The remaining sections are structurally
  absent from that assembly and report a measured 0.
- Graph tasks assemble the supervisor routing prompt
  (``supervisor_model.supervisor_routing_prompt``): the stage-1
  skill catalogue embedded at its head, the routing template, the
  blackboard and memory-briefing sections, the compaction summary
  plus the model-facing message tail, and the ambient repo map.
  The calculator rebuilds those sections from the thread's
  persisted checkpoint state (thread_id == session_id — the same
  state the executor resumes) with the same builder functions and
  counts each section exactly as it is appended.
- The repo map is counted from the real build for the session's
  granted roots (``repo_map_context``): an empty map is 0 tokens.
- Scratchpad is the session's recorded agent thinking (``thoughts``
  on stored messages) — the one component sourced from the session
  record, because model-facing state messages do not carry thoughts.
- A component that has not been assembled yet (e.g. the routing
  prompt of a graph thread that has never run) reports
  ``tokens: None`` with ``unknown: True`` instead of an invented
  number. ``total_tokens`` sums the measured components only, and
  the response carries ``partial: True`` when anything is unknown.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from aether_engine.cost.pricing import _bare_model_id

logger = logging.getLogger("aether_engine.cost.context_breakdown")

# Context window limits for known models. Fallback default is 128,000.
MODEL_CONTEXT_LIMITS: dict[str, int] = {
    "gemini-3.5-flash": 1_048_576,
    "gemini-2.0-flash": 1_048_576,
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "claude-3-5-sonnet": 200_000,
    "claude-3-5-haiku": 200_000,
    "deepseek-chat": 64_000,
    "deepseek-reasoner": 64_000,
}

DEFAULT_CONTEXT_LIMIT = 128_000

_COMPONENT_ORDER = (
    "system prompt",
    "memory briefing",
    "history",
    "tool outputs",
    "scratchpad",
    "repo map",
    "skill catalogue",
)


def get_model_context_limit(model: str | None) -> int:
    """Return context window limit for a model, with LiteLLM lookup and fallback."""
    if not model:
        return DEFAULT_CONTEXT_LIMIT

    bare = _bare_model_id(model)
    if bare in MODEL_CONTEXT_LIMITS:
        return MODEL_CONTEXT_LIMITS[bare]
    if model in MODEL_CONTEXT_LIMITS:
        return MODEL_CONTEXT_LIMITS[model]

    try:
        import litellm

        max_tokens = litellm.get_max_tokens(model)
        if max_tokens and max_tokens > 0:
            return int(max_tokens)
    except Exception:  # noqa: BLE001, S110
        pass

    return DEFAULT_CONTEXT_LIMIT


def count_tokens(text: str | None, model: str = "gemini-3.5-flash") -> int:
    """Count or estimate tokens in text. Returns 0 for empty text."""
    if not text:
        return 0
    try:
        import litellm

        return int(litellm.token_counter(model=model, text=text))
    except Exception:  # noqa: BLE001
        # Standard character-to-token heuristic (approx 4 chars/token, min 1 for non-empty)
        return max(1, len(text) // 4)


def _determine_is_graph_task(
    session_id: str,
    task_id: str | None,
    explicit_graph_task: bool | None,
) -> bool:
    """Determine whether the target is a graph task or fast chat."""
    if explicit_graph_task is not None:
        return explicit_graph_task

    # Check ledger rows if task_id provided
    if task_id:
        try:
            from aether_engine.cost.ledger import get_usage_ledger
            ledger = get_usage_ledger()
            res = ledger.query(task_id=task_id)
            for entry in res.get("entries", []):
                t_class = entry.get("task_class")
                agent = entry.get("agent_name")
                if t_class == "fast_chat" or agent == "fast_chat":
                    return False
                if t_class == "graph" or agent in {"supervisor", "planner", "coder", "researcher", "execute_tool"}:
                    return True
        except Exception:  # noqa: BLE001, S110
            pass

    # Check session messages
    try:
        from aether_engine.memory import session_store
        session = session_store.get_session(session_id)
        if session and session.get("messages"):
            for msg in session["messages"]:
                if msg.get("role") == "tool" or msg.get("thoughts"):
                    return True
    except Exception:  # noqa: BLE001, S110
        pass

    # Default: if task_id starts with fast_chat, False; otherwise True
    return not (task_id and "fast" in task_id.lower())


# ---------------------------------------------------------------------------
# Measurement helpers — each counts REAL assembled text with count_tokens,
# or returns None when the component cannot be measured in this state.
# ---------------------------------------------------------------------------


def _text_of(message: dict) -> str:
    """The text a message carries, mirroring the request path's view:
    plain string content as-is; multimodal parts contribute their
    text parts (media parts have no tokenizer-countable text)."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(str(part.get("text") or ""))
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    if content is None:
        return ""
    return str(content)


def _count_messages(messages: list[dict], model: str, roles: tuple[str, ...]) -> int:
    return sum(
        count_tokens(_text_of(m), model)
        for m in messages
        if m.get("role") in roles
    )


def _session_messages(session_id: str) -> list[dict[str, Any]]:
    try:
        from aether_engine.memory import session_store
        session = session_store.get_session(session_id)
        if session and session.get("messages"):
            return session["messages"]
    except Exception:
        logger.debug("Could not read session messages for breakdown", exc_info=True)
    return []


def _scratchpad_tokens(store_messages: list[dict], model: str) -> int:
    """The session's recorded agent thinking (the scratchpad segment).

    Thoughts live on the stored session messages; model-facing state
    messages do not carry them, so the session record is the source.
    """
    parts: list[str] = []
    for m in store_messages:
        thoughts = m.get("thoughts")
        if isinstance(thoughts, list):
            parts.extend(str(t) for t in thoughts if t)
        elif isinstance(thoughts, str) and thoughts:
            parts.append(thoughts)
    return count_tokens("\n".join(parts), model) if parts else 0


def _seeded_history_tokens(session_id: str, model: str) -> int | None:
    """History as the model-facing builder produces it.

    ``session_store.get_chat_history`` is the exact history source
    fast chat sends and the graph executor seeds fresh threads from
    (user/assistant turns only, bounded the same way)."""
    try:
        from aether_engine.memory import session_store
        history = session_store.get_chat_history(session_id)
    except Exception:  # noqa: BLE001
        return None
    return sum(count_tokens(m.get("content") or "", model) for m in history)


def _load_thread_state(session_id: str) -> dict[str, Any] | None:
    """The session's persisted graph state (checkpoint channel values).

    The graph executor keys threads by session_id (ADR 0011), so this
    is the exact state the session's next run resumes and assembles
    its requests from. None when no graph run has written a
    checkpoint for the session, or the checkpoint cannot be read.
    """
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver

        from aether_engine.langgraph.checkpointer import DEFAULT_CHECKPOINT_DB

        if not DEFAULT_CHECKPOINT_DB.exists():
            return None
        config = {"configurable": {"thread_id": session_id, "checkpoint_ns": ""}}
        with SqliteSaver.from_conn_string(str(DEFAULT_CHECKPOINT_DB)) as saver:
            tup = saver.get_tuple(config)
        if tup is None:
            return None
        values = (tup.checkpoint or {}).get("channel_values")
        return dict(values) if isinstance(values, dict) else None
    except Exception:
        logger.debug(
            "Could not read graph state for context breakdown", exc_info=True
        )
        return None


def _skill_catalogue(model: str) -> tuple[str, int | None]:
    """The stage-1 skill catalogue, exactly as the supervisor prompt
    embeds it ("" when no skills are enabled). Returns the text (for
    splitting the routing prompt) and its token count, or a None
    count when the catalogue cannot be read."""
    try:
        from aether_engine.skills.manager import GLOBAL_SKILL_MANAGER

        catalogue = GLOBAL_SKILL_MANAGER.catalogue() or ""
    except Exception:  # noqa: BLE001
        return "", None
    return catalogue, count_tokens(catalogue, model)


def _repo_map_tokens(
    model: str,
    state: dict[str, Any] | None,
    store_messages: list[dict],
) -> int | None:
    """The ambient repo-map section, from the real builder.

    Mirrors the run config the websocket hands the executor
    (configured workspace roots, falling back to the default
    workspace) and the prompt/context the routing prompt builds the
    map from. An unbuilt or empty map is a measured 0."""
    try:
        from aether_engine.config import load_config

        cfg = load_config()
    except Exception:  # noqa: BLE001
        return None
    roots = getattr(cfg, "workspace_roots", None) or [
        str(Path.home() / "AetherWorkspace")
    ]
    if state is not None:
        prompt = state.get("original_prompt") or ""
        try:
            from aether_engine.langgraph.state import message_dicts

            msgs = message_dicts(state.get("messages", []))
        except Exception:  # noqa: BLE001
            msgs = []
    else:
        msgs = store_messages
        prompt = ""
        for m in reversed(msgs):
            if m.get("role") == "user":
                prompt = _text_of(m)
                break
    if not isinstance(prompt, str):
        prompt = str(prompt)
    context_text = " ".join(str(m.get("content", "")) for m in msgs)
    try:
        from aether_engine.langgraph.nodes.supervisor import repo_map_context

        section = repo_map_context(
            {
                "configurable": {
                    "workspace_roots": [str(r) for r in roots],
                    "user_config": cfg,
                }
            },
            prompt,
            context_text,
        )
    except Exception:  # noqa: BLE001
        return None
    if not section:
        return 0
    # The routing prompt appends the section after a blank line.
    return count_tokens(f"\n\n{section}", model)


def _graph_components(
    session_id: str,
    store_messages: list[dict],
    model: str,
) -> dict[str, int | None]:
    """Component token counts for a graph session, measured from the
    thread's persisted state and the real assembly builders."""
    state = _load_thread_state(session_id)
    catalogue, tokens_skills = _skill_catalogue(model)
    tokens_repo_map = _repo_map_tokens(model, state, store_messages)
    tokens_scratchpad = _scratchpad_tokens(store_messages, model)

    if state is None:
        # No graph run has assembled this thread yet: the routing
        # prompt and the memory briefing do not exist, so they are
        # reported unknown rather than invented. History is what a
        # fresh thread would be seeded with; seeded history carries
        # no tool outputs by construction.
        return {
            "system prompt": None,
            "memory briefing": None,
            "history": _seeded_history_tokens(session_id, model),
            "tool outputs": 0,
            "scratchpad": tokens_scratchpad,
            "repo map": tokens_repo_map,
            "skill catalogue": tokens_skills,
        }

    # System prompt: the real routing-prompt builder (the
    # create_supervisor path, tool_mode=True), split at the skill
    # catalogue it embeds at its head.
    tokens_system: int | None = None
    try:
        from aether_engine.langgraph.nodes.supervisor import (
            _available_specialists,
            _supervisor_system_prompt,
        )

        original_prompt = state.get("original_prompt", "None")
        if not isinstance(original_prompt, str):
            original_prompt = str(original_prompt)
        plan = state.get("plan", "No plan yet.")
        if not isinstance(plan, str):
            plan = str(plan)
        full_prompt = _supervisor_system_prompt(
            original_prompt,
            plan,
            _available_specialists({"configurable": {}}),
            tool_mode=True,
        )
        remainder = full_prompt
        if catalogue and full_prompt.startswith(catalogue):
            remainder = full_prompt[len(catalogue):]
        remainder = remainder.removeprefix("\n")
        tokens_system = count_tokens(remainder, model)
    except Exception:
        logger.debug("Routing prompt build failed for breakdown", exc_info=True)
        tokens_system = None

    # Memory briefing: the briefing sections the assembly appends —
    # the shared team blackboard (every agent) and the supervisor's
    # memory briefing (specialists), rendered exactly as appended.
    tokens_memory: int | None = None
    try:
        from aether_engine.langgraph.state import blackboard_briefing

        briefing_tokens = 0
        board = blackboard_briefing(state)
        if board:
            briefing_tokens += count_tokens(
                "\n\nShared team blackboard (written by all agents in "
                f"this task):\n{board}",
                model,
            )
        briefing = state.get("context_briefing")
        briefing = briefing.strip() if isinstance(briefing, str) else ""
        if briefing:
            briefing_tokens += count_tokens(
                "\n\nMemory briefing from the supervisor (facts "
                f"retrieved from long-term memory):\n{briefing}",
                model,
            )
        tokens_memory = briefing_tokens
    except Exception:  # noqa: BLE001
        tokens_memory = None

    # History and tool outputs: the compaction-aware model-facing
    # view of the thread (summary section + verbatim tail), via the
    # same helpers the routing prompt and specialists consume.
    tokens_history: int | None = None
    tokens_tool_outputs: int | None = None
    try:
        from aether_engine.langgraph.compaction import (
            context_summary_section,
            model_facing_messages,
        )

        facing = model_facing_messages(state)
        history_tokens = 0
        summary_section = context_summary_section(state)
        if summary_section:
            history_tokens += count_tokens(f"\n\n{summary_section}", model)
        history_tokens += _count_messages(facing, model, ("user", "assistant"))
        tokens_history = history_tokens
        tokens_tool_outputs = _count_messages(facing, model, ("tool",))
    except Exception:  # noqa: BLE001
        tokens_history = None
        tokens_tool_outputs = None

    return {
        "system prompt": tokens_system,
        "memory briefing": tokens_memory,
        "history": tokens_history,
        "tool outputs": tokens_tool_outputs,
        "scratchpad": tokens_scratchpad,
        "repo map": tokens_repo_map,
        "skill catalogue": tokens_skills,
    }


def _fast_chat_components(
    session_id: str,
    store_messages: list[dict],
    model: str,
) -> dict[str, int | None]:
    """Component token counts for fast chat. Its assembly is exactly
    ``[system] + history`` (tools=None, no map/catalogue/briefing
    sections), so the absent sections are structural, measured zeros."""
    try:
        from aether_engine.langgraph.fast_chat import _SYSTEM_PROMPT

        tokens_system: int | None = count_tokens(_SYSTEM_PROMPT, model)
    except Exception:  # noqa: BLE001
        tokens_system = None
    return {
        "system prompt": tokens_system,
        "memory briefing": 0,
        "history": _seeded_history_tokens(session_id, model),
        "tool outputs": 0,
        "scratchpad": _scratchpad_tokens(store_messages, model),
        "repo map": 0,
        "skill catalogue": 0,
    }


def get_context_breakdown(
    session_id: str,
    task_id: str | None = None,
    model: str | None = None,
    is_graph_task: bool | None = None,
) -> dict[str, Any]:
    """Calculate the itemized context breakdown for a session/task.

    Returns the PRD CONTEXT_BREAKDOWN_RESPONSE shape, extended
    additively with an ``unknown`` flag per component and a
    top-level ``partial`` flag:
    {
        "model": str,
        "context_limit": int,
        "total_tokens": int,          # sum of the MEASURED components
        "percent_used": float,        # of total_tokens (a lower bound
                                      # when partial is True)
        "partial": bool,
        "components": [
            {"name": str, "tokens": int | None, "share": float,
             "unknown": bool},
            ...
        ]
    }
    """
    resolved_model = model or "gemini-3.5-flash"
    if "2.5" in resolved_model:
        resolved_model = "gemini-3.5-flash"

    context_limit = get_model_context_limit(resolved_model)
    is_graph = _determine_is_graph_task(session_id, task_id, is_graph_task)

    store_messages = _session_messages(session_id)
    if is_graph:
        values = _graph_components(session_id, store_messages, resolved_model)
    else:
        values = _fast_chat_components(session_id, store_messages, resolved_model)

    components: list[dict[str, Any]] = []
    total_tokens = 0
    partial = False
    for name in _COMPONENT_ORDER:
        tokens = values[name]
        if tokens is None:
            partial = True
            components.append(
                {"name": name, "tokens": None, "share": 0.0, "unknown": True}
            )
        else:
            total_tokens += tokens
            components.append(
                {"name": name, "tokens": tokens, "share": 0.0, "unknown": False}
            )

    # Shares are over the measured total; unknown components carry 0.
    for c in components:
        if c["tokens"] is not None and total_tokens > 0:
            c["share"] = round(c["tokens"] / total_tokens, 4)

    percent_used = 0.0
    if context_limit > 0:
        percent_used = round((total_tokens / context_limit) * 100, 2)

    return {
        "model": resolved_model,
        "context_limit": context_limit,
        "total_tokens": total_tokens,
        "percent_used": percent_used,
        "partial": partial,
        "components": components,
    }
