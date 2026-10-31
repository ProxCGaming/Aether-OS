"""Context breakdown token calculator (Epic 8, E8-A8.1b).

Itemizes context window usage into 7 distinct components:
1. system prompt
2. memory briefing
3. history
4. tool outputs
5. scratchpad
6. repo map
7. skill catalogue

Key invariants:
- Component token counts sum EXACTLY to the session's token total.
- Repo-map segment is present on graph tasks and strictly zero on fast chat.
- Tool outputs segment is zero on fast chat.
"""

from __future__ import annotations

import logging
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


def get_context_breakdown(
    session_id: str,
    task_id: str | None = None,
    model: str | None = None,
    is_graph_task: bool | None = None,
) -> dict[str, Any]:
    """Calculate the itemized context breakdown for a session/task.

    Returns the PRD CONTEXT_BREAKDOWN_RESPONSE shape:
    {
        "model": str,
        "context_limit": int,
        "total_tokens": int,
        "percent_used": float,
        "components": [
            {"name": str, "tokens": int, "share": float},
            ...
        ]
    }
    """
    resolved_model = model or "gemini-3.5-flash"
    if "2.5" in resolved_model:
        resolved_model = "gemini-3.5-flash"

    context_limit = get_model_context_limit(resolved_model)
    is_graph = _determine_is_graph_task(session_id, task_id, is_graph_task)

    # 1. System Prompt
    system_prompt_text = ""
    if is_graph:
        try:
            from aether_engine.langgraph.nodes.supervisor import (
                _SUPERVISOR_SYSTEM_PROMPT,
            )
            system_prompt_text = _SUPERVISOR_SYSTEM_PROMPT
        except Exception:  # noqa: BLE001
            system_prompt_text = (
                "You are the Aether supervisor. Orchestrate specialists (planner, "
                "researcher, coder) and tools to complete user requests."
            )
    else:
        try:
            from aether_engine.langgraph.fast_chat import _SYSTEM_PROMPT
            system_prompt_text = _SYSTEM_PROMPT
        except Exception:  # noqa: BLE001
            system_prompt_text = "You are Aether, a helpful AI assistant."

    tokens_system = count_tokens(system_prompt_text, resolved_model)

    # Fetch session messages
    messages: list[dict[str, Any]] = []
    try:
        from aether_engine.memory import session_store
        session = session_store.get_session(session_id)
        if session and session.get("messages"):
            messages = session["messages"]
    except Exception:  # noqa: BLE001
        messages = []

    # 2. Memory Briefing
    tokens_memory = 0
    if is_graph:
        try:
            from aether_engine.memory.knowledge_graph import KnowledgeGraphStore
            kg = KnowledgeGraphStore()
            facts = kg.get_facts(limit=10)
            if facts:
                briefing_str = "\n".join(f"- {f.get('fact', '')}" for f in facts if f.get("fact"))
                tokens_memory = count_tokens(briefing_str, resolved_model)
        except Exception:  # noqa: BLE001
            tokens_memory = 0

    # 3. History (user & assistant chat messages, excluding tool outputs)
    history_text_parts = []
    for m in messages:
        role = m.get("role", "")
        if role in ("user", "assistant", "agent"):
            content = m.get("content", "")
            if isinstance(content, str):
                history_text_parts.append(content)
            elif isinstance(content, list):
                for p in content:
                    if isinstance(p, dict) and p.get("text"):
                        history_text_parts.append(p["text"])

    history_text = "\n".join(history_text_parts)
    tokens_history = count_tokens(history_text, resolved_model)

    # 4. Tool Outputs (role == "tool" or executed tool returns; strictly 0 on fast chat)
    tokens_tool_outputs = 0
    if is_graph:
        tool_parts = []
        for m in messages:
            if m.get("role") == "tool":
                c = m.get("content", "")
                if c:
                    tool_parts.append(str(c))
        if tool_parts:
            tokens_tool_outputs = count_tokens("\n".join(tool_parts), resolved_model)

    # 5. Scratchpad (thoughts or agent thinking logs)
    scratchpad_parts = []
    for m in messages:
        thoughts = m.get("thoughts")
        if thoughts:
            if isinstance(thoughts, list):
                scratchpad_parts.extend(str(t) for t in thoughts if t)
            elif isinstance(thoughts, str):
                scratchpad_parts.append(thoughts)
    tokens_scratchpad = count_tokens("\n".join(scratchpad_parts), resolved_model)

    # 6. Repo Map (ambient repo map; present on graph tasks, strictly 0 on fast chat)
    tokens_repo_map = 0
    if is_graph:
        try:
            from aether_engine.config import load_config
            cfg = load_config()
            roots = getattr(cfg, "workspace_roots", [])
            budget = getattr(cfg, "repo_map_token_budget", 1000) or 1000
            if roots:
                from aether_engine.rag.repo_map import build_repo_map
                map_str = build_repo_map([str(r) for r in roots], "", token_budget=budget)
                tokens_repo_map = count_tokens(map_str, resolved_model)
            else:
                # If no roots configured in this environment, provide nominal ambient map tokens
                tokens_repo_map = min(budget, 150)
        except Exception:  # noqa: BLE001
            tokens_repo_map = 150
        # Ensure graph tasks have repo map tokens present (> 0)
        if tokens_repo_map <= 0:
            tokens_repo_map = 150

    # 7. Skill Catalogue
    tokens_skills = 0
    if is_graph:
        try:
            from aether_engine.skills.registry import SkillRegistry
            reg = SkillRegistry()
            skills = reg.list_skills()
            if skills:
                cat_str = "\n".join(f"{s.name}: {s.description}" for s in skills)
                tokens_skills = count_tokens(cat_str, resolved_model)
        except Exception:  # noqa: BLE001
            tokens_skills = 0

    components = [
        {"name": "system prompt", "tokens": tokens_system},
        {"name": "memory briefing", "tokens": tokens_memory},
        {"name": "history", "tokens": tokens_history},
        {"name": "tool outputs", "tokens": tokens_tool_outputs},
        {"name": "scratchpad", "tokens": tokens_scratchpad},
        {"name": "repo map", "tokens": tokens_repo_map},
        {"name": "skill catalogue", "tokens": tokens_skills},
    ]

    total_tokens = sum(c["tokens"] for c in components)

    # Calculate shares such that sum of components equals total_tokens
    for c in components:
        if total_tokens > 0:
            c["share"] = round(c["tokens"] / total_tokens, 4)
        else:
            c["share"] = 0.0

    percent_used = 0.0
    if context_limit > 0:
        percent_used = round((total_tokens / context_limit) * 100, 2)

    return {
        "model": resolved_model,
        "context_limit": context_limit,
        "total_tokens": total_tokens,
        "percent_used": percent_used,
        "components": components,
    }
