from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Typed Provider Error Hierarchy (with retriable flag)
# ---------------------------------------------------------------------------
class ProviderError(Exception):
    def __init__(self, message: str, retriable: bool = False):
        super().__init__(message)
        self.message = message
        self.retriable = retriable


class AuthenticationError(ProviderError):
    def __init__(self, message: str = "Invalid API key or authentication failed"):
        super().__init__(message, retriable=False)


class RateLimitError(ProviderError):
    def __init__(self, message: str = "Provider rate limit or quota exceeded"):
        super().__init__(message, retriable=True)


class TimeoutError(ProviderError):
    def __init__(self, message: str = "Request to LLM provider timed out"):
        super().__init__(message, retriable=True)


class NetworkError(ProviderError):
    def __init__(self, message: str = "Network connection to provider failed"):
        super().__init__(message, retriable=True)


class InvalidResponseError(ProviderError):
    def __init__(self, message: str = "Provider returned an invalid or malformed response"):
        super().__init__(message, retriable=False)


# ---------------------------------------------------------------------------
# Message and Chunk Dataclasses
# ---------------------------------------------------------------------------
@dataclass
class ToolCall:
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    call_id: str | None = None
    thought_signature: Any | None = None


@dataclass
class ToolResult:
    name: str
    content: Any
    call_id: str | None = None


@dataclass
class StreamChunk:
    text: str | None = None
    tool_calls: list[ToolCall] | None = None
    finish_reason: str | None = None
    # E1-16: model reasoning/thinking text, when the model exposes it.
    # Always separate from `text`: thinking is displayed in the UI's
    # Thinking collapsible and must never leak into the answer.
    thinking: str | None = None


class BaseProvider(ABC):
    """Abstract interface for LLM providers."""

    @abstractmethod
    async def call_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        node_name: str | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        """Stream response chunks and/or tool call requests for the given message history.

        node_name is the LangGraph node making the call (if any); used for
        audit logging of per-node fallback events.
        """
        yield StreamChunk()
