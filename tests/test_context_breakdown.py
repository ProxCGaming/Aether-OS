"""Unit tests for context breakdown token calculator (Epic 8, E8-A8.1b).

Verifies:
1. Segment counts sum EXACTLY to total_tokens for the session.
2. Repo-map segment is present on graph tasks and strictly zero on fast chat.
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


def test_context_breakdown_components_sum_exactly_to_total():
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

    # Hard invariant: itemised parts sum EXACTLY to the session's token total
    token_sum = sum(c["tokens"] for c in components)
    assert token_sum == breakdown["total_tokens"]

    # Component shares sum to ~1.0 when tokens > 0
    share_sum = sum(c["share"] for c in components)
    assert abs(share_sum - 1.0) < 0.01

    # Percent used reflects total_tokens / context_limit
    expected_pct = round((breakdown["total_tokens"] / breakdown["context_limit"]) * 100, 2)
    assert breakdown["percent_used"] == expected_pct


def test_repo_map_segment_present_on_graph_tasks():
    session_id = session_store.create_session("Graph Task Session")
    session_store.add_message(session_id, "user", "Refactor the module")

    breakdown = get_context_breakdown(
        session_id=session_id,
        model="gemini-3.5-flash",
        is_graph_task=True,
    )

    comp_map = {c["name"]: c["tokens"] for c in breakdown["components"]}
    # repo-map segment present on graph tasks
    assert comp_map["repo map"] > 0
    assert sum(c["tokens"] for c in breakdown["components"]) == breakdown["total_tokens"]


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
