from aether_engine.providers.litellm_provider import LiteLLMProvider


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

def test_convert_messages_closes_a_trailing_model_turn():
    """16 Nov 2026, owner's live failure: a specialist's text output
    became the last message of the supervisor's next decision call,
    and Vertex rejected the request (400, "Requests ending with a
    model turn are not supported"). The converted conversation must
    end with a user turn, the model turn's content preserved."""
    provider = LiteLLMProvider(api_key="test-key", model="gemini/gemini-2.0-flash")
    raw = [
        {"role": "user", "content": "Create plan.txt with 3 names, then read it back."},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "find_files", "arguments": "{}"}},
        ]},
        {"role": "tool", "name": "find_files", "content": "No files matched.", "tool_call_id": "call_1"},
        {"role": "assistant", "content": "I'll create the plan.txt file with 3 names now."},
    ]

    converted = provider._convert_messages(raw)

    assert converted[-1]["role"] == "user"
    assert "Please proceed" in converted[-1]["content"]
    assert converted[-2]["role"] == "assistant"
    assert "plan.txt" in converted[-2]["content"]


def test_convert_messages_leaves_a_trailing_tool_call_turn_alone():
    """A trailing assistant message WITH tool_calls is not a
    completed model turn (its results are missing) — the closer
    must not fire and mask that different problem."""
    provider = LiteLLMProvider(api_key="test-key", model="gemini/gemini-2.0-flash")
    raw = [
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_9", "type": "function",
             "function": {"name": "get_current_time", "arguments": "{}"}},
        ]},
    ]

    converted = provider._convert_messages(raw)

    assert converted[-1]["role"] == "assistant"
    assert converted[-1]["tool_calls"][0]["function"]["name"] == "get_current_time"


def test_convert_messages_ending_with_user_or_tool_is_unchanged():
    provider = LiteLLMProvider(api_key="test-key", model="gemini/gemini-2.0-flash")
    raw_user = [
        {"role": "user", "content": "Hi"},
        {"role": "assistant", "content": "Hello!"},
        {"role": "user", "content": "Thanks"},
    ]
    converted = provider._convert_messages(raw_user)
    assert len(converted) == 3
    assert converted[-1] == {"role": "user", "content": "Thanks"}


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
