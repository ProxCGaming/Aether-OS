"""Unit tests for context breakdown token calculator (Epic 8, E8-A8.1b).

Verifies:
1. Measured segment counts sum EXACTLY to total_tokens for the session.
2. Repo-map segment is the measured size of the real map the request
   path builds (zero when that map is empty) and strictly zero on
   fast chat; components that cannot be measured report unknown.
3. All 7 required context components are itemised:
   system prompt, memory briefing, history, tool outputs,
   scratchpad, repo map, skill catalogue.
4. Shares and percent_used are calculated accurately.
5. Known model context limits and defaults (gemini-3.5-flash).
"""

import pytest

from aether_engine.cost.context_breakdown import (
    get_context_breakdown,
    get_model_context_limit,
)
from aether_engine.memory import session_store


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    test_db = tmp_path / "test_memory.db"
    monkeypatch.setattr(session_store, "DB_PATH", test_db)
    session_store.init_db()
    yield test_db


def test_model_context_limits_and_fallback():
    assert get_model_context_limit("gemini-3.5-flash") == 1_048_576
    assert get_model_context_limit("gemini-2.0-flash") == 1_048_576
    assert get_model_context_limit("gpt-4o") == 128_000
    assert get_model_context_limit("claude-3-5-sonnet") == 200_000
    assert get_model_context_limit("unknown-custom-model") == 128_000


def test_context_breakdown_components_sum_exactly_to_total(isolated_checkpoints):
    session_id = session_store.create_session("Test Session")
    session_store.add_message(session_id, "user", "Hello, write a script for me")
    session_store.add_message(
        session_id,
        "assistant",
        "Sure, here is the code snippet",
        thoughts=["Thinking through the problem step by step..."],
    )

    breakdown = get_context_breakdown(
        session_id=session_id,
        model="gemini-3.5-flash",
        is_graph_task=True,
    )

    assert breakdown["model"] == "gemini-3.5-flash"
    assert breakdown["context_limit"] == 1_048_576
    assert breakdown["total_tokens"] > 0

    components = breakdown["components"]
    assert len(components) == 7

    component_names = {c["name"] for c in components}
    expected_names = {
        "system prompt",
        "memory briefing",
        "history",
        "tool outputs",
        "scratchpad",
        "repo map",
        "skill catalogue",
    }
    assert component_names == expected_names

    # Hard invariant: the MEASURED (known) parts sum EXACTLY to the
    # session's token total; components that cannot be measured are
    # reported unknown (tokens None), never as invented numbers.
    token_sum = sum(c["tokens"] for c in components if c["tokens"] is not None)
    assert token_sum == breakdown["total_tokens"]

    # Component shares sum to ~1.0 when tokens > 0
    share_sum = sum(c["share"] for c in components)
    assert abs(share_sum - 1.0) < 0.01

    # Percent used reflects total_tokens / context_limit
    expected_pct = round((breakdown["total_tokens"] / breakdown["context_limit"]) * 100, 2)
    assert breakdown["percent_used"] == expected_pct


def test_repo_map_segment_present_on_graph_tasks(tmp_path, monkeypatch, isolated_checkpoints):
    # A REAL workspace with real code: the repo-map segment must be the
    # measured size of the map the request path actually builds for it
    # (via supervisor.repo_map_context), not a nominal constant.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "mod.py").write_text(
        "def alpha():\n    pass\n\nclass Beta:\n    def gamma(self):\n        pass\n"
    )
    _patch_config(monkeypatch, [str(workspace)], budget=1000)

    session_id = session_store.create_session("Graph Task Session")
    session_store.add_message(session_id, "user", "Refactor the module alpha")

    breakdown = get_context_breakdown(
        session_id=session_id,
        model="gemini-3.5-flash",
        is_graph_task=True,
    )

    comp_map = {c["name"]: c["tokens"] for c in breakdown["components"]}
    # repo-map segment present on graph tasks because the real map is
    # non-empty for this workspace
    assert comp_map["repo map"] > 0
    known_sum = sum(c["tokens"] for c in breakdown["components"] if c["tokens"] is not None)
    assert known_sum == breakdown["total_tokens"]

    # ... and it equals the real builder's section, counted as appended
    from aether_engine.config import UserConfig
    from aether_engine.cost.context_breakdown import count_tokens
    from aether_engine.langgraph.nodes.supervisor import repo_map_context

    cfg = UserConfig()
    cfg.workspace_roots = [str(workspace)]
    cfg.repo_map_token_budget = 1000
    section = repo_map_context(
        {"configurable": {"workspace_roots": [str(workspace)], "user_config": cfg}},
        "Refactor the module alpha",
        "Refactor the module alpha",
    )
    assert section  # the real map for this workspace is non-empty
    assert comp_map["repo map"] == count_tokens(f"\n\n{section}", "gemini-3.5-flash")


def test_repo_map_segment_zero_on_fast_chat():
    session_id = session_store.create_session("Fast Chat Session")
    session_store.add_message(session_id, "user", "What is the capital of France?")
    session_store.add_message(session_id, "assistant", "The capital of France is Paris.")

    breakdown = get_context_breakdown(
        session_id=session_id,
        model="gemini-3.5-flash",
        is_graph_task=False,
    )

    comp_map = {c["name"]: c["tokens"] for c in breakdown["components"]}
    # repo-map segment strictly zero on fast chat
    assert comp_map["repo map"] == 0
    # tool outputs strictly zero on fast chat
    assert comp_map["tool outputs"] == 0
    # Hard invariant: sum is exact
    assert sum(c["tokens"] for c in breakdown["components"]) == breakdown["total_tokens"]


def test_empty_session_handles_honestly():
    session_id = session_store.create_session("Empty Session")
    breakdown = get_context_breakdown(
        session_id=session_id,
        model="gemini-3.5-flash",
        is_graph_task=False,
    )

    comp_map = {c["name"]: c["tokens"] for c in breakdown["components"]}
    # Fast chat system prompt exists, so total_tokens matches system prompt
    assert comp_map["system prompt"] > 0
    assert comp_map["history"] == 0
    assert comp_map["repo map"] == 0
    assert sum(c["tokens"] for c in breakdown["components"]) == breakdown["total_tokens"]


def test_context_breakdown_and_budget_websocket_flow(tmp_path, monkeypatch):
    from starlette.testclient import TestClient

    from aether_common.contracts import Event, EventType
    from aether_engine.app import app, engine_state

    session_id = session_store.create_session("WS Session")
    session_store.add_message(session_id, "user", "Hello websocket")

    with TestClient(app) as client:
        token = engine_state.auth_token
        with client.websocket_connect(f"/ws/tasks?token={token}") as ws:
            # Receive HELLO
            hello = ws.receive_json()
            assert hello["type"] == EventType.HELLO.value

            # Send CONTEXT_BREAKDOWN_REQUEST
            ws.send_text(
                Event(
                    type=EventType.CONTEXT_BREAKDOWN_REQUEST,
                    request_id="req_cb_1",
                    payload={"session_id": session_id, "model": "gemini-3.5-flash"},
                ).to_json()
            )
            resp = ws.receive_json()
            assert resp["type"] == EventType.CONTEXT_BREAKDOWN_RESPONSE.value
            assert resp["request_id"] == "req_cb_1"
            assert resp["payload"]["model"] == "gemini-3.5-flash"
            assert resp["payload"]["total_tokens"] > 0
            assert len(resp["payload"]["components"]) == 7

            # Send DAILY_BUDGET_SET_REQUEST
            ws.send_text(
                Event(
                    type=EventType.DAILY_BUDGET_SET_REQUEST,
                    request_id="req_budget_1",
                    payload={"daily_budget_usd": 10.0},
                ).to_json()
            )
            b_resp = ws.receive_json()
            assert b_resp["type"] == EventType.DAILY_BUDGET_SET_RESPONSE.value
            assert b_resp["payload"]["daily_budget_usd"] == 10.0
            assert b_resp["payload"]["ok"] is True
            assert engine_state.user_config.daily_budget_usd == 10.0


# ---------------------------------------------------------------------------
# M20: the breakdown must MEASURE the real request assembly, never invent
# numbers. An empty repo map reports 0 (not the old nominal 150), known
# components are counted from the same builders the request path uses,
# and components that have not been assembled yet report unknown.
# ---------------------------------------------------------------------------

import uuid
from datetime import UTC, datetime

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from aether_engine import config as config_module
from aether_engine.config import UserConfig
from aether_engine.cost.context_breakdown import count_tokens
from aether_engine.langgraph import checkpointer as checkpointer_module


@pytest.fixture
def isolated_checkpoints(tmp_path, monkeypatch):
    """Point the graph checkpointer at a fresh temp DB for this test."""
    db_path = tmp_path / "checkpoints.db"
    monkeypatch.setattr(checkpointer_module, "DEFAULT_CHECKPOINT_DB", db_path)
    return db_path


def _patch_config(monkeypatch, roots, budget=1000):
    cfg = UserConfig()
    cfg.workspace_roots = list(roots)
    cfg.repo_map_token_budget = budget
    monkeypatch.setattr(config_module, "load_config", lambda *a, **k: cfg)
    return cfg


def _seed_thread(db_path, session_id, values):
    """Write one graph checkpoint for thread=session_id, as a run would."""
    config = {"configurable": {"thread_id": session_id, "checkpoint_ns": ""}}
    checkpoint = {
        "v": 1,
        "id": str(uuid.uuid4()),
        "ts": datetime.now(UTC).isoformat(),
        "channel_values": values,
        "channel_versions": {},
        "versions_seen": {},
    }
    with SqliteSaver.from_conn_string(str(db_path)) as saver:
        saver.put(config, checkpoint, {"source": "test", "step": 1, "writes": {}}, {})


def _components(breakdown):
    return {c["name"]: c for c in breakdown["components"]}


def test_repo_map_empty_workspace_is_zero_never_nominal(
    tmp_path, monkeypatch, isolated_checkpoints
):
    # (a) The audit's repro: the real map is EMPTY, so the meter must
    # say 0 — the old calculator forced a nominal 150 here.
    empty_ws = tmp_path / "empty-ws"
    empty_ws.mkdir()
    _patch_config(monkeypatch, [str(empty_ws)], budget=1000)

    session_id = session_store.create_session("Empty Map Session")
    session_store.add_message(session_id, "user", "Hello")

    breakdown = get_context_breakdown(
        session_id=session_id, model="gemini-3.5-flash", is_graph_task=True
    )
    repo = _components(breakdown)["repo map"]
    assert repo["unknown"] is False
    assert repo["tokens"] == 0


def test_repo_map_zero_budget_is_zero_never_nominal(
    tmp_path, monkeypatch, isolated_checkpoints
):
    # (a) variant: a real workspace but a zero map budget disables the
    # map in the real assembly (build_repo_map returns ""), so the
    # meter must say 0, not the old nominal 150.
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "mod.py").write_text("def alpha():\n    pass\n")
    _patch_config(monkeypatch, [str(workspace)], budget=0)

    session_id = session_store.create_session("Zero Budget Session")
    session_store.add_message(session_id, "user", "Hello")

    breakdown = get_context_breakdown(
        session_id=session_id, model="gemini-3.5-flash", is_graph_task=True
    )
    repo = _components(breakdown)["repo map"]
    assert repo["unknown"] is False
    assert repo["tokens"] == 0


def test_fast_chat_components_are_the_real_assembly():
    # (b) Fast chat's request is [system] + history: the system segment
    # must be the token count of fast_chat's actual _SYSTEM_PROMPT and
    # history the count of the model-facing history builder's output.
    from aether_engine.langgraph.fast_chat import _SYSTEM_PROMPT

    session_id = session_store.create_session("Fast Measured Session")
    session_store.add_message(session_id, "user", "What is the capital of France?")
    session_store.add_message(session_id, "assistant", "The capital of France is Paris.")

    breakdown = get_context_breakdown(
        session_id=session_id, model="gemini-3.5-flash", is_graph_task=False
    )
    comps = _components(breakdown)
    assert comps["system prompt"]["tokens"] == count_tokens(
        _SYSTEM_PROMPT, "gemini-3.5-flash"
    )
    expected_history = sum(
        count_tokens(m["content"], "gemini-3.5-flash")
        for m in session_store.get_chat_history(session_id)
    )
    assert comps["history"]["tokens"] == expected_history
    # Structural zeros: the fast-chat assembly contains no such sections.
    for name in ("memory briefing", "tool outputs", "repo map", "skill catalogue"):
        assert comps[name]["tokens"] == 0
        assert comps[name]["unknown"] is False
    assert breakdown["partial"] is False


def test_graph_breakdown_measures_seeded_thread_state(
    tmp_path, monkeypatch, isolated_checkpoints
):
    # (b) With real graph state in the checkpointer (as a completed run
    # leaves it), every segment must be counted from the SAME builders
    # the request path uses, over that real state.
    from aether_engine.langgraph.compaction import (
        context_summary_section,
        model_facing_messages,
    )
    from aether_engine.langgraph.nodes.supervisor import (
        _available_specialists,
        _supervisor_system_prompt,
    )
    from aether_engine.langgraph.state import blackboard_briefing
    from aether_engine.skills.manager import GLOBAL_SKILL_MANAGER

    empty_ws = tmp_path / "ws"
    empty_ws.mkdir()
    _patch_config(monkeypatch, [str(empty_ws)], budget=1000)

    session_id = session_store.create_session("Graph Measure Session")
    session_store.add_message(
        session_id,
        "assistant",
        "Store-side reply",
        thoughts=["Thinking through the problem step by step..."],
    )

    state = {
        "messages": [
            {"role": "user", "content": "First request: build the importer"},
            {"role": "assistant", "content": "Working on the importer now."},
            {
                "role": "tool",
                "content": "importer.py contents: def import_session(): ...",
                "tool_call_id": "t1",
            },
            {"role": "user", "content": "Now add CRC checks"},
        ],
        "context_summary": "The user asked for an importer and work began.",
        "context_summary_upto": 1,
        "blackboard": {
            "plan": "Importer plan",
            "findings": ["CRC matters"],
            "decisions": [],
            "open_questions": [],
        },
        "plan": "Importer plan v1",
        "original_prompt": "First request: build the importer",
        "context_briefing": "The user prefers transactional imports.",
    }
    _seed_thread(isolated_checkpoints, session_id, state)

    breakdown = get_context_breakdown(
        session_id=session_id, model="gemini-3.5-flash", is_graph_task=True
    )
    comps = _components(breakdown)
    model = "gemini-3.5-flash"

    # System prompt + skill catalogue: the real routing prompt, split
    # at the catalogue the builder embeds at its head.
    catalogue = GLOBAL_SKILL_MANAGER.catalogue()
    full_prompt = _supervisor_system_prompt(
        "First request: build the importer",
        "Importer plan v1",
        _available_specialists({"configurable": {}}),
        tool_mode=True,
    )
    remainder = full_prompt[len(catalogue):]
    remainder = remainder.removeprefix("\n")
    assert comps["skill catalogue"]["tokens"] == count_tokens(catalogue, model)
    assert comps["system prompt"]["tokens"] == count_tokens(remainder, model)

    # History: the compaction summary section plus the model-facing
    # user/assistant messages (the folded first message is excluded).
    facing = model_facing_messages(state)
    expected_history = count_tokens(
        f"\n\n{context_summary_section(state)}", model
    ) + sum(
        count_tokens(m["content"], model)
        for m in facing
        if m["role"] in ("user", "assistant")
    )
    assert comps["history"]["tokens"] == expected_history

    # Tool outputs: the tool message inside the model-facing history.
    assert comps["tool outputs"]["tokens"] == count_tokens(
        "importer.py contents: def import_session(): ...", model
    )

    # Memory briefing: the blackboard section and the supervisor's
    # memory briefing, rendered exactly as specialists receive them.
    expected_briefing = count_tokens(
        "\n\nShared team blackboard (written by all agents in this task):\n"
        f"{blackboard_briefing(state)}",
        model,
    ) + count_tokens(
        "\n\nMemory briefing from the supervisor (facts retrieved from "
        "long-term memory):\nThe user prefers transactional imports.",
        model,
    )
    assert comps["memory briefing"]["tokens"] == expected_briefing

    # Repo map: real build for an empty workspace is empty -> measured 0.
    assert comps["repo map"]["tokens"] == 0
    assert comps["repo map"]["unknown"] is False

    # Scratchpad: the session's recorded thoughts.
    assert comps["scratchpad"]["tokens"] == count_tokens(
        "Thinking through the problem step by step...", model
    )

    assert breakdown["partial"] is False
    assert all(c["tokens"] is not None for c in breakdown["components"])
    assert (
        sum(c["tokens"] for c in breakdown["components"])
        == breakdown["total_tokens"]
    )


def test_unassembled_graph_components_report_unknown(
    tmp_path, monkeypatch, isolated_checkpoints
):
    # (c) A graph session whose thread has never been assembled (no
    # checkpoint): the routing prompt and the memory briefing do not
    # exist yet, so they must report unknown — never invented numbers.
    empty_ws = tmp_path / "ws"
    empty_ws.mkdir()
    _patch_config(monkeypatch, [str(empty_ws)], budget=1000)

    session_id = session_store.create_session("Unassembled Session")
    session_store.add_message(session_id, "user", "Hello graph")

    breakdown = get_context_breakdown(
        session_id=session_id, model="gemini-3.5-flash", is_graph_task=True
    )
    comps = _components(breakdown)

    for name in ("system prompt", "memory briefing"):
        assert comps[name]["unknown"] is True
        assert comps[name]["tokens"] is None

    # What CAN be measured honestly still is: the history a fresh
    # thread would be seeded with, and the (empty) real repo map.
    assert comps["history"]["tokens"] == count_tokens("Hello graph", "gemini-3.5-flash")
    assert comps["history"]["unknown"] is False
    assert comps["repo map"]["tokens"] == 0
    assert comps["tool outputs"]["tokens"] == 0

    assert breakdown["partial"] is True
    known_sum = sum(
        c["tokens"] for c in breakdown["components"] if c["tokens"] is not None
    )
    assert known_sum == breakdown["total_tokens"]
