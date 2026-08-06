from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Dict, List, Optional


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
    args: Dict[str, Any] = field(default_factory=dict)
    call_id: Optional[str] = None
    thought_signature: Optional[Any] = None


@dataclass
class ToolResult:
    name: str
    content: Any
    call_id: Optional[str] = None


@dataclass
class StreamChunk:
    text: Optional[str] = None
    tool_calls: Optional[List[ToolCall]] = None
    finish_reason: Optional[str] = None


class BaseProvider(ABC):
    """Abstract interface for LLM providers."""

    @abstractmethod
    async def call_stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        """Stream response chunks and/or tool call requests for the given message history."""
        yield StreamChunk()
