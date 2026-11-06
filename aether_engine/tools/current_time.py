"""Current-time tool: get_current_time.

One of the built-in tool factories split out of ``tools/registry.py``
(Wave 5 registry split): the registry keeps the tool types, the
assembly point and the execution pipeline; each family module owns
its factories. The registry re-exports :func:`create_current_time_tool`
lazily, so existing imports from it keep working.
"""

from datetime import datetime

from aether_engine.tools.registry import Tool, ToolRisk


def _get_current_time_fn(**_kwargs) -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def create_current_time_tool() -> Tool:
    return Tool(
        name="get_current_time",
        description="Get the current local date and time on the user's system.",
        input_schema={
            "type": "object",
            "properties": {},
        },
        execute_fn=_get_current_time_fn,
        risk=ToolRisk.READ_ONLY,
    )
