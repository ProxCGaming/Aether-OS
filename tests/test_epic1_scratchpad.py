"""Epic 1, E1-17 (engine side): the scratchpad tool.

scratchpad_render is a READ_ONLY built-in in the central registry.
Executing it publishes a SCRATCHPAD event (sanitised, length-capped)
for the display-only UI panel; the tool result itself is a plain
confirmation. Unsafe SVG markup can never reach the client.
"""
import asyncio

import pytest

from aether_common.contracts import EventType
from aether_engine.event_bus import event_bus
from aether_engine.langgraph.nodes.execute_tool import execute_tool_node
from aether_engine.tools.registry import ToolRisk, build_tool_registry
from aether_engine.tools.scratchpad import (
    MAX_CONTENT_CHARS,
    sanitise_scratchpad,
    scratchpad_event_payload,
)


def test_sanitiser_strips_scripts_handlers_and_js_urls():
    dirty = (
        '<svg onload="alert(1)"><script>alert(2)</script>'
        '<a href="javascript:alert(3)"><text>hi</text></a>'
        '<foreignObject><div>html</div></foreignObject>'
        '<rect onclick="boom()" width="10"/></svg>'
    )
    clean = sanitise_scratchpad("svg", dirty)
    assert "<script" not in clean and "onload" not in clean
    assert "javascript:" not in clean and "onclick" not in clean
    assert "foreignObject" not in clean
    assert "<rect" in clean and 'width="10"' in clean  # legit content survives


def test_sanitiser_caps_length_and_leaves_other_kinds_alone():
    long_text = "x" * (MAX_CONTENT_CHARS + 500)
    assert len(sanitise_scratchpad("text", long_text)) == MAX_CONTENT_CHARS
    assert sanitise_scratchpad("mermaid", "graph TD; A-->B") == "graph TD; A-->B"


def test_event_payload_validates_kind_and_content():
    assert scratchpad_event_payload({"kind": "html", "content": "<b>x</b>"}, "coder") is None
    assert scratchpad_event_payload({"kind": "svg", "content": "  "}, "coder") is None
    payload = scratchpad_event_payload({"kind": "text", "content": "hello"}, "coder")
    assert payload == {"kind": "text", "content": "hello", "agent": "coder"}


def test_tool_is_registered_read_only_in_the_central_registry():
    registry = build_tool_registry(include_file_tools=False, include_web_tools=False)
    tool = registry.get_tool("scratchpad_render")
    assert tool.risk == ToolRisk.READ_ONLY
    assert "scratchpad_render" in registry.read_only_names()  # offered in plan mode too


@pytest.mark.asyncio
async def test_execution_publishes_a_sanitised_scratchpad_event():
    registry = build_tool_registry(include_file_tools=False, include_web_tools=False)
    queue: asyncio.Queue = asyncio.Queue()
    event_bus.subscribe("task-scratch", queue)
    try:
        state = {
            "pending_tool_call": {
                "name": "scratchpad_render",
                "args": {"kind": "svg", "content": '<svg><script>alert(1)</script><circle r="5"/></svg>'},
                "call_id": "c1",
            },
            "active_specialist": "coder",
            "task_id": "task-scratch",
            "messages": [],
        }
        result = await execute_tool_node(state, {"configurable": {"tool_registry": registry}})
    finally:
        event_bus.unsubscribe("task-scratch", queue)
    assert "Scratchpad updated" in result["messages"][0]["content"]
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    pads = [ev for ev in events if getattr(ev, "type", None) == EventType.SCRATCHPAD]
    assert len(pads) == 1
    assert pads[0].payload["kind"] == "svg"
    assert "<script" not in pads[0].payload["content"]
    assert "<circle" in pads[0].payload["content"]
    assert pads[0].payload["agent"] == "coder"
