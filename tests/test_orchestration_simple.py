import asyncio
from typing import Any, AsyncGenerator, Dict, List, Optional
import unittest

from aether_common.contracts import EventType
from aether_engine.orchestration.simple import run_task
from aether_engine.providers.base import (
    BaseProvider,
    RateLimitError,
    StreamChunk,
    ToolCall,
)
from aether_engine.tools.registry import ToolRegistry, create_current_time_tool


class FakeProvider(BaseProvider):
    def __init__(self, script: Optional[List[List[StreamChunk]]] = None):
        self.script = script or []
        self.call_history: List[List[Dict[str, Any]]] = []

    async def call_stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        self.call_history.append(list(messages))
        turn = len(self.call_history) - 1
        if turn < len(self.script):
            for chunk in self.script[turn]:
                await asyncio.sleep(0.01)
                yield chunk
        else:
            yield StreamChunk(text="Default fake response.")


class TestOrchestrationSimple(unittest.IsolatedAsyncioTestCase):
    async def test_straight_through_text(self):
        fake_provider = FakeProvider(
            script=[
                [
                    StreamChunk(text="Hello "),
                    StreamChunk(text="there!"),
                ]
            ]
        )
        registry = ToolRegistry()

        events = []
        async for ev in run_task("Say hello", provider=fake_provider, tools=registry):
            events.append(ev)

        self.assertEqual(events[0].type, EventType.TASK_CREATED)
        self.assertEqual(events[1].type, EventType.TASK_PROGRESS)
        self.assertEqual(events[1].payload["text_delta"], "Hello ")
        self.assertEqual(events[2].type, EventType.TASK_PROGRESS)
        self.assertEqual(events[2].payload["text_delta"], "there!")
        self.assertEqual(events[3].type, EventType.TASK_COMPLETED)
        self.assertEqual(events[3].payload["response"], "Hello there!")

    async def test_multi_turn_tool_calling(self):
        fake_provider = FakeProvider(
            script=[
                # Turn 1: model requests tool execution
                [
                    StreamChunk(tool_calls=[ToolCall(name="get_current_time", args={})])
                ],
                # Turn 2: model generates final answer based on tool result
                [
                    StreamChunk(text="The current time is verified.")
                ],
            ]
        )
        registry = ToolRegistry()
        registry.register(create_current_time_tool())

        events = []
        async for ev in run_task("What time is it?", provider=fake_provider, tools=registry):
            events.append(ev)

        types = [e.type for e in events]
        self.assertEqual(types[0], EventType.TASK_CREATED)
        # Check tool call and tool result events
        tool_call_ev = next(e for e in events if e.payload.get("tool_call") == "get_current_time")
        self.assertIsNotNone(tool_call_ev)

        tool_result_ev = next(e for e in events if e.payload.get("tool_result") == "get_current_time")
        self.assertIsNotNone(tool_result_ev)
        self.assertIn("result", tool_result_ev.payload)

        # Final completion event
        self.assertEqual(types[-1], EventType.TASK_COMPLETED)
        self.assertEqual(events[-1].payload["response"], "The current time is verified.")

        # Verify message history sent to provider in Turn 2 included the tool output
        self.assertEqual(len(fake_provider.call_history), 2)
        turn2_messages = fake_provider.call_history[1]
        self.assertEqual(turn2_messages[0]["role"], "user")
        self.assertEqual(turn2_messages[1]["role"], "assistant")
        self.assertEqual(turn2_messages[2]["role"], "tool")
        self.assertEqual(turn2_messages[2]["name"], "get_current_time")

    async def test_risky_tool_requires_approval_then_executes(self):
        fake_provider = FakeProvider(
            script=[
                [StreamChunk(tool_calls=[ToolCall(name="execute_shell", args={"command": "echo hi"})])],
                [StreamChunk(text="Shell command approved.")],
            ]
        )
        registry = ToolRegistry()
        approvals = []

        async def approval_handler(tool_name, args, request_id):
            approvals.append((tool_name, args))
            return True

        events = []
        async for ev in run_task(
            "Run a shell command",
            provider=fake_provider,
            tools=registry,
            approval_handler=approval_handler,
        ):
            events.append(ev)

        self.assertTrue(approvals)
        self.assertTrue(any(e.type == EventType.TOOL_APPROVAL_REQUEST for e in events))
        self.assertTrue(any(e.type == EventType.TASK_COMPLETED for e in events))

    async def test_provider_error_emits_task_failed(self):
        class FailingProvider(BaseProvider):
            async def call_stream(self, messages, tools=None):
                raise RateLimitError("Rate limit reached")
                yield StreamChunk()

        events = []
        async for ev in run_task("Test prompt", provider=FailingProvider()):
            events.append(ev)

        self.assertEqual(events[0].type, EventType.TASK_CREATED)
        self.assertEqual(events[1].type, EventType.TASK_FAILED)
        self.assertEqual(events[1].payload["retriable"], True)
        self.assertIn("Rate limit reached", events[1].payload["error"])

    async def test_task_cancellation(self):
        class SlowProvider(BaseProvider):
            async def call_stream(self, messages, tools=None):
                while True:
                    await asyncio.sleep(0.5)
                    yield StreamChunk(text="chunk")

        events = []

        async def run_and_collect():
            async for ev in run_task("Slow prompt", provider=SlowProvider()):
                events.append(ev)

        task = asyncio.create_task(run_and_collect())
        await asyncio.sleep(0.05)
        task.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertTrue(any(e.type == EventType.TASK_CANCELLED for e in events))


if __name__ == "__main__":
    unittest.main()
