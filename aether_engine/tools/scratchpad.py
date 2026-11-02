"""Epic 1, E1-17: the agent scratchpad tool.

``scratchpad_render`` lets an agent show the user a diagram or sketch
in the chat's scratchpad panel — a Mermaid diagram, an SVG sketch,
Markdown, or plain text — using far fewer tokens than describing it
in prose. The panel is display-only: rendering never feeds back into
execution.

Safety: content is sanitised HERE, before it is published to the UI —
scripts, event-handler attributes, javascript: URLs and foreignObject
embeds are stripped from SVG, and every kind is length-capped — so
the client can render the SVG payload as markup without ever giving
it script capability.
"""
from __future__ import annotations

import re
from typing import Any

from aether_engine.tools.registry import Tool, ToolRisk

SCRATCHPAD_TOOL_NAME = "scratchpad_render"
SCRATCHPAD_KINDS = ("mermaid", "svg", "markdown", "text")
MAX_CONTENT_CHARS = 20_000

_SCRIPT_BLOCK = re.compile(r"<script\b[^>]*>.*?</script\s*>", re.IGNORECASE | re.DOTALL)
_FOREIGN_OBJECT = re.compile(r"<foreignObject\b[^>]*>.*?</foreignObject\s*>", re.IGNORECASE | re.DOTALL)
_EVENT_ATTR = re.compile(r"""\son\w+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""", re.IGNORECASE)
_JS_URL = re.compile(r"""(href|xlink:href)\s*=\s*("\s*javascript:[^"]*"|'\s*javascript:[^']*')""", re.IGNORECASE)


def sanitise_scratchpad(kind: str, content: str) -> str:
    """Make scratchpad content safe for the display-only panel."""
    text = (content or "")[:MAX_CONTENT_CHARS]
    if kind == "svg":
        text = _SCRIPT_BLOCK.sub("", text)
        text = _FOREIGN_OBJECT.sub("", text)
        text = _EVENT_ATTR.sub("", text)
        text = _JS_URL.sub(r"\1=\"#\"", text)
    return text


def _render_scratchpad(kind: str = "text", content: str = "") -> str:
    if kind not in SCRATCHPAD_KINDS:
        return f"Error: kind must be one of {', '.join(SCRATCHPAD_KINDS)}."
    if not (content or "").strip():
        return "Error: content is empty."
    return (
        f"Scratchpad updated with a {kind} rendering "
        f"({len(content)} characters). It is visible to the user in the "
        "scratchpad panel; it is display-only and nothing was executed."
    )


def create_scratchpad_tool() -> Tool:
    return Tool(
        name=SCRATCHPAD_TOOL_NAME,
        description=(
            "Show the user a visual in the scratchpad panel: a Mermaid diagram "
            "(kind='mermaid'), an SVG sketch (kind='svg'), a Markdown document "
            "(kind='markdown'), or formatted plain text (kind='text'). Use it "
            "to explain structure, flows, or plans visually instead of "
            "describing them at length. Display-only."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": list(SCRATCHPAD_KINDS),
                         "description": "Rendering kind: mermaid, svg, markdown, or text"},
                "content": {"type": "string", "description": "The diagram/sketch/text content"},
            },
            "required": ["kind", "content"],
        },
        execute_fn=_render_scratchpad,
        risk=ToolRisk.READ_ONLY,
    )


def scratchpad_event_payload(args: dict[str, Any], agent: str | None) -> dict[str, Any] | None:
    """The SCRATCHPAD event payload for a tool call, or None if invalid."""
    kind = args.get("kind")
    content = args.get("content")
    if kind not in SCRATCHPAD_KINDS or not isinstance(content, str) or not content.strip():
        return None
    return {"kind": kind, "content": sanitise_scratchpad(kind, content), "agent": agent}
