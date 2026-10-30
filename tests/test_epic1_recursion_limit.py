"""Epic 1, E1-03 (part 1): the graph recursion limit is real, not a comment.

Before this, graph.py claimed a recursion limit of 25 "set during compile
or run time" but nothing anywhere set it — a supervisor/specialist
ping-pong could loop unboundedly. These tests pin the wiring: the
constant exists, both executor configs (run and resume) carry it, and
LangGraph actually enforces a limit of that size on a looping graph.
"""
import re
from pathlib import Path
from typing import TypedDict

import pytest
from langgraph.errors import GraphRecursionError
from langgraph.graph import START, StateGraph

from aether_engine.langgraph.graph import GRAPH_RECURSION_LIMIT

EXECUTOR = Path(__file__).resolve().parents[1] / "aether_engine" / "langgraph" / "executor.py"


def test_recursion_limit_constant_is_25():
    assert GRAPH_RECURSION_LIMIT == 25


def test_both_executor_configs_carry_the_limit():
    source = EXECUTOR.read_text(encoding="utf-8")
    hits = re.findall(r'"recursion_limit":\s*GRAPH_RECURSION_LIMIT', source)
    # run_langgraph_task and resume_langgraph_task — a limit that only
    # applies before the approval pause is not a limit.
    assert len(hits) == 2


class _LoopState(TypedDict):
    n: int


def _loop_node(state: _LoopState) -> dict:
    return {"n": state["n"] + 1}


@pytest.mark.asyncio
async def test_langgraph_enforces_the_limit_on_a_looping_graph():
    workflow = StateGraph(_LoopState)
    workflow.add_node("loop", _loop_node)
    workflow.add_edge(START, "loop")
    workflow.add_edge("loop", "loop")
    graph = workflow.compile()
    with pytest.raises(GraphRecursionError):
        await graph.ainvoke(
            {"n": 0},
            config={"recursion_limit": GRAPH_RECURSION_LIMIT},
        )
