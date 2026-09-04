import pytest
pytest.importorskip("langgraph")
from typing import Dict, Any
from pathlib import Path

from aether_engine.langgraph.state import AetherState
from aether_engine.langgraph.checkpointer import DEFAULT_CHECKPOINT_DB
from aether_engine.langgraph.graph import create_graph

def test_aether_state_schema():
    """Test that the AetherState TypedDict has the expected keys."""
    # Verify expected keys exist in the schema
    expected_keys = {
        "original_prompt", "messages", "plan", "current_phase",
        "artifact_paths", "tool_results_summary", "pending_tool_call",
        "active_specialist", "delegation_log"
    }
    assert expected_keys.issubset(AetherState.__annotations__.keys())

def test_graph_structure():
    # Test that the graph can be compiled
    graph = create_graph()
    assert graph is not None
    # Verify nodes exist
    assert "supervisor" in graph.nodes
    assert "researcher" in graph.nodes
    assert "planner" in graph.nodes
    assert "coder" in graph.nodes

def test_checkpoint_path_is_persistent():
    """Verify the default checkpoint path points to a real file, not :memory:."""
    assert DEFAULT_CHECKPOINT_DB == Path.home() / ".aether" / "checkpoints.db"
    assert str(DEFAULT_CHECKPOINT_DB) != ":memory:"
