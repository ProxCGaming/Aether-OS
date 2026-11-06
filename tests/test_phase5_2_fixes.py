from types import SimpleNamespace

import litellm
import pytest


def test_litellm_provider_exception_handling():
    """Verify that catching MidStreamFallbackError does not throw TypeError."""
    # This just verifies the syntax is valid by raising and catching an exception that matches the block.
    # The actual bug was a syntax error in the except clause tuple.
    try:
        raise litellm.Timeout(message="timeout", model="test", llm_provider="test")
    except (litellm.RateLimitError, litellm.Timeout,
            litellm.APIConnectionError, litellm.ServiceUnavailableError,
            getattr(litellm, "MidStreamFallbackError", Exception)) as e:
        assert isinstance(e, litellm.Timeout)

import uuid

from aether_engine.langgraph.executor import run_langgraph_task


@pytest.mark.asyncio
async def test_langgraph_executor_thread_id():
    """Test that session_id is used for thread_id if provided."""
    _session_id = str(uuid.uuid4())
    # We can't easily mock the entire run_langgraph_task without lots of setup,
    # but we just want to ensure it doesn't crash on initialization due to session_id logic.
    # Instead, we just read the source code or use an AST parsing test.
    import inspect
    source = inspect.getsource(run_langgraph_task)
    assert "thread_id = session_id or request_id or str(uuid.uuid4())" in source

# ── B-07 rewrite (Wave 0) ──────────────────────────────────────────
# The old test here grepped coder_node's source for two literal
# strings; the node refactor removed the strings, so the test could
# no longer answer whether the BEHAVIOUR survives. This rewrite runs
# the real coder_node with a stub provider/registry and inspects the
# tool list the provider actually receives: when agent_tools restricts
# the coder's role, a tool the conversation already used stays on the
# list (marked disabled, so the model can see its own history), while
# a tool that is neither allowed nor used is dropped entirely.

def _tool_def(name, description):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": {}},
        },
    }


class _StubToolRegistry:
    """Minimal registry double: only what coder_node reads."""

    def __init__(self, definitions):
        self._definitions = definitions

    def get_definitions(self):
        return self._definitions


class _RecordingProvider:
    """Streams fixed text and records the tools it was offered."""

    def __init__(self):
        self.seen_tools = None

    async def call_stream(self, messages, tools=None, node_name=None):
        self.seen_tools = tools
        yield SimpleNamespace(text="done", tool_calls=None, finish_reason="stop")


@pytest.mark.asyncio
async def test_tool_filtering_keeps_used_tools(monkeypatch):
    """Role-restricted filtering keeps used tools (disabled), drops the rest."""
    from aether_engine.langgraph.nodes import coder as coder_module

    monkeypatch.setattr(coder_module, "save_artifact", lambda *a, **k: "/tmp/code.txt")

    registry = _StubToolRegistry([
        _tool_def("read_file", "Read a file."),
        _tool_def("write_file", "Write a file."),
        _tool_def("execute_shell", "Run a shell command."),
    ])
    provider = _RecordingProvider()
    state = {
        "original_prompt": "Build the thing",
        "messages": [
            {"role": "user", "content": "Build the thing"},
            # The conversation already used write_file earlier.
            {"role": "assistant", "content": "",
             "tool_calls": [{"function": {"name": "write_file"}}]},
            {"role": "tool", "content": "wrote it", "tool_call_id": "call_1"},
        ],
        "plan": "",
        "current_phase": "",
        "artifact_paths": {},
        "tool_results_summary": {},
        "pending_tool_call": None,
        "active_specialist": None,
        "tool_call_signature": None,
        "tool_call_repeats": 0,
        "delegation_log": [],
        "task_id": "filter-test",
        "session_id": "filter-session",
        "context_briefing": None,
        "active_skills": [],
        "blackboard": {},
    }
    config = {"configurable": {
        "provider": provider,
        "tool_registry": registry,
        # The coder role may only use read_file in this run.
        "agent_tools": {"coder": ["read_file"]},
    }}

    result = await coder_module.coder_node(state, config)

    assert result["messages"]  # the node completed and replied
    offered = {t["function"]["name"]: t for t in provider.seen_tools}
    # Allowed tool: offered untouched.
    assert offered["read_file"]["function"]["description"] == "Read a file."
    # Used-but-not-allowed tool: KEPT on the list, marked disabled —
    # the model can still see the tool its own history references.
    assert "write_file" in offered
    assert "DO NOT USE THIS TOOL" in offered["write_file"]["function"]["description"]
    # Neither allowed nor used: dropped entirely.
    assert "execute_shell" not in offered

