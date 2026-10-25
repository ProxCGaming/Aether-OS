import pytest
from aether_engine.providers.litellm_provider import LiteLLMProvider
from aether_engine.providers.base import ToolCall

def test_convert_messages_prepends_user_turn_if_starts_with_assistant():
    provider = LiteLLMProvider(api_key="test-key", model="gemini/gemini-2.0-flash")
    raw = [
        {"role": "system", "content": "You are a helper."},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_123",
                    "type": "function",
                    "function": {"name": "get_current_time", "arguments": "{}"}
                }
            ]
        },
        {"role": "tool", "name": "get_current_time", "content": "12:00 PM", "tool_call_id": "call_123"}
    ]

    converted = provider._convert_messages(raw)
    
    # First message should be system
    assert converted[0]["role"] == "system"
    # Second message MUST be user to satisfy Gemini turn rules
    assert converted[1]["role"] == "user"
    assert "Please proceed" in converted[1]["content"]
    # Third message is assistant with tool_calls
    assert converted[2]["role"] == "assistant"
    assert converted[2]["tool_calls"][0]["function"]["name"] == "get_current_time"
    # Fourth message is tool response
    assert converted[3]["role"] == "tool"
    assert converted[3]["name"] == "get_current_time"
    assert converted[3]["tool_call_id"] == "call_123"

def test_convert_messages_merges_consecutive_assistant_messages():
    provider = LiteLLMProvider(api_key="test-key", model="gemini/gemini-2.0-flash")
    raw = [
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "Thinking about your request..."},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_abc",
                    "type": "function",
                    "function": {"name": "execute_shell", "arguments": "{\"command\": \"date\"}"}
                }
            ]
        },
        {"role": "tool", "name": "execute_shell", "content": "Wed", "tool_call_id": "call_abc"}
    ]

    converted = provider._convert_messages(raw)
    
    # Should have user, ONE merged assistant, and tool
    assert len(converted) == 3
    assert converted[0]["role"] == "user"
    assert converted[1]["role"] == "assistant"
    assert "Thinking about your request..." in converted[1]["content"]
    assert converted[1]["tool_calls"][0]["function"]["name"] == "execute_shell"
    assert converted[2]["role"] == "tool"

def test_convert_messages_resolves_missing_tool_call_id_and_name():
    provider = LiteLLMProvider(api_key="test-key", model="gemini/gemini-2.0-flash")
    raw = [
        {"role": "user", "content": "Hi"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_unique_456",
                    "type": "function",
                    "function": {"name": "special_tool", "arguments": "{}"}
                }
            ]
        },
        {"role": "tool", "content": "result"}  # no tool_call_id or name
    ]

    converted = provider._convert_messages(raw)
    assert converted[2]["role"] == "tool"
    assert converted[2]["tool_call_id"] == "call_unique_456"
    assert converted[2]["name"] == "special_tool"
