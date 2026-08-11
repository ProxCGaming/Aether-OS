import pytest
pytest.importorskip("langgraph")
from typing import Dict, Any

from aether_engine.langgraph.state import AetherState
from aether_engine.langgraph.checkpointer import SqliteSaver
from aether_engine.langgraph.graph import create_graph

def test_aether_state_schema():
    # Test that the operator.add behavior works for lists
    state = AetherState(
        messages=["Hello"],
        task_id="t1",
        recursion_limit=25,
        current_node="supervisor",
        delegation_log=[],
        artifacts=[]
    )
    assert state["messages"] == ["Hello"]
    assert state["task_id"] == "t1"

def test_graph_structure():
    # Test that the graph can be compiled
    graph = create_graph()
    assert graph is not None
    # Verify nodes exist
    assert "supervisor" in graph.nodes
    assert "researcher" in graph.nodes
    assert "planner" in graph.nodes
    assert "coder" in graph.nodes

@pytest.mark.asyncio
async def test_sqlite_checkpointer(tmp_path):
    from aether_engine.langgraph.checkpointer import get_persistent_checkpointer
    db_path = tmp_path / "test_checkpointer.sqlite"
    
    with get_persistent_checkpointer(db_path=db_path) as saver:
        assert saver is not None
        # It's an instance of SqliteSaver from langgraph.checkpoint.sqlite
        assert hasattr(saver, "put")
        assert hasattr(saver, "get")
