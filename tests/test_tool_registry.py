import asyncio
import unittest
from datetime import datetime

from aether_engine.tools.registry import (
    Tool,
    ToolExecutionError,
    ToolNotFoundError,
    ToolRegistry,
    create_current_time_tool,
    create_file_tools,
)


class TestToolRegistry(unittest.TestCase):
    def setUp(self):
        self.registry = ToolRegistry()

    def test_register_and_get_definitions(self):
        tool = Tool(
            name="test_tool",
            description="A test tool",
            input_schema={"type": "object", "properties": {"x": {"type": "integer"}}},
            execute_fn=lambda x=0: x * 2,
        )
        self.registry.register(tool)
        defs = self.registry.get_definitions()
        self.assertEqual(len(defs), 1)
        self.assertEqual(defs[0]["name"], "test_tool")
        self.assertEqual(defs[0]["description"], "A test tool")
        self.assertEqual(defs[0]["parameters"]["type"], "object")

    def test_sync_tool_execution(self):
        tool = Tool(
            name="multiply",
            description="Multiply numbers",
            execute_fn=lambda a, b: a * b,
        )
        self.registry.register(tool)
        result = asyncio.run(self.registry.execute("multiply", {"a": 6, "b": 7}))
        self.assertEqual(result, 42)

    def test_async_tool_execution(self):
        async def async_echo(msg: str):
            await asyncio.sleep(0.01)
            return f"Echo: {msg}"

        tool = Tool(
            name="async_echo",
            description="Async echo message",
            execute_fn=async_echo,
        )
        self.registry.register(tool)
        result = asyncio.run(self.registry.execute("async_echo", {"msg": "hello"}))
        self.assertEqual(result, "Echo: hello")

    def test_get_current_time_tool(self):
        time_tool = create_current_time_tool()
        self.registry.register(time_tool)
        result = asyncio.run(self.registry.execute("get_current_time"))
        self.assertIsInstance(result, str)
        # Verify it parses as %Y-%m-%d %H:%M:%S
        dt = datetime.strptime(result, "%Y-%m-%d %H:%M:%S")
        self.assertIsNotNone(dt)

    def test_file_tools_are_available(self):
        for tool in create_file_tools():
            self.registry.register(tool)

        self.assertEqual(self.registry.get_tool("write_file").name, "write_file")
        self.assertEqual(self.registry.get_tool("delete_file").name, "delete_file")
        self.assertEqual(self.registry.get_tool("execute_shell").name, "execute_shell")

    def test_unknown_tool_raises_not_found(self):
        with self.assertRaises(ToolNotFoundError):
            asyncio.run(self.registry.execute("non_existent_tool"))

    def test_failing_tool_raises_execution_error(self):
        def failing_fn():
            raise ValueError("Intentional failure")

        tool = Tool(name="fail", description="Fails", execute_fn=failing_fn)
        self.registry.register(tool)
        with self.assertRaises(ToolExecutionError):
            asyncio.run(self.registry.execute("fail"))


if __name__ == "__main__":
    unittest.main()
