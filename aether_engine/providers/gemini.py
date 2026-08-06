"""Google Gemini LLM provider implementation with error handling, token caching, and robust model alias resolution."""
import asyncio
import logging
from typing import Any, AsyncGenerator, Dict, List, Optional

from google import genai
from google.genai import errors, types
import httpx

from aether_engine.providers.base import (
    AuthenticationError,
    BaseProvider,
    InvalidResponseError,
    NetworkError,
    ProviderError,
    RateLimitError,
    StreamChunk,
    TimeoutError,
    ToolCall,
)

logger = logging.getLogger("aether_engine.providers.gemini")

DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"

# Map requested UI / prompt model identifiers to active Google GenAI endpoint models
_MODEL_ALIASES: Dict[str, str] = {
    "gemini-2.5-flash": "gemini-2.5-flash",
    "gemini-2.0-flash": "gemini-2.0-flash",
    "gemini-2.5-pro": "gemini-2.5-pro",
    "gemini-flash-latest": "gemini-flash-latest",
    "gemini-2.0-flash-lite": "gemini-2.0-flash-lite",
    "gemini-1.5-flash": "gemini-1.5-flash",
    "gemini-1.5-pro": "gemini-1.5-pro",
}

# Persistent client cache for TCP / TLS connection reuse across turns and tasks
_CLIENT_CACHE: Dict[str, genai.Client] = {}


def _get_cached_client(api_key: str) -> genai.Client:
    if api_key not in _CLIENT_CACHE:
        _CLIENT_CACHE[api_key] = genai.Client(api_key=api_key)
    return _CLIENT_CACHE[api_key]


class GeminiProvider(BaseProvider):
    """Concrete provider implementation for Google Gemini Developer API using the official google-genai SDK."""

    def __init__(self, api_key: str, model: str = DEFAULT_GEMINI_MODEL):
        if not api_key or not api_key.strip():
            raise AuthenticationError("API key cannot be empty.")
        self.api_key = api_key.strip()
        self.model = model or DEFAULT_GEMINI_MODEL
        self.target_model = _MODEL_ALIASES.get(self.model, self.model)
        self.client = _get_cached_client(self.api_key)

    def _convert_messages(self, messages: List[Dict[str, Any]]) -> List[types.Content]:
        contents: List[types.Content] = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            parts = []

            if role == "tool":
                fn_resp_kwargs = {
                    "name": msg.get("name", "tool"),
                    "response": {"result": content},
                }
                if "call_id" in msg and msg["call_id"]:
                    fn_resp_kwargs["id"] = msg["call_id"]

                parts.append(
                    types.Part(
                        function_response=types.FunctionResponse(**fn_resp_kwargs)
                    )
                )
                contents.append(types.Content(role="tool", parts=parts))

            elif role == "assistant":
                if content:
                    parts.append(types.Part(text=content))
                if "tool_calls" in msg and msg["tool_calls"]:
                    for tc in msg["tool_calls"]:
                        fn_call_kwargs = {
                            "name": tc.name if isinstance(tc, ToolCall) else tc.get("name", "tool"),
                            "args": tc.args if isinstance(tc, ToolCall) else tc.get("args", {}),
                        }
                        if isinstance(tc, ToolCall) and tc.call_id:
                            fn_call_kwargs["id"] = tc.call_id
                        elif isinstance(tc, dict) and "id" in tc:
                            fn_call_kwargs["id"] = tc["id"]

                        parts.append(
                            types.Part(
                                function_call=types.FunctionCall(**fn_call_kwargs)
                            )
                        )
                contents.append(types.Content(role="model", parts=parts))

            else:  # user
                if isinstance(content, list):
                    for item in content:
                        if isinstance(item, str):
                            parts.append(types.Part(text=item))
                        elif isinstance(item, dict):
                            if item.get("type") == "text":
                                parts.append(types.Part(text=item.get("text", "")))
                            elif item.get("type") == "image_url":
                                pass
                else:
                    parts.append(types.Part(text=str(content)))
                contents.append(types.Content(role="user", parts=parts))

        return contents

    def _convert_tools(self, tools: Optional[List[Dict[str, Any]]]) -> Optional[List[types.Tool]]:
        if not tools:
            return None
        declarations = []
        for t in tools:
            declarations.append(
                types.FunctionDeclaration(
                    name=t["name"],
                    description=t.get("description", ""),
                    parameters=t.get("parameters", {}),
                )
            )
        return [types.Tool(function_declarations=declarations)]

    async def call_stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        contents = self._convert_messages(messages)
        gemini_tools = self._convert_tools(tools)

        config = types.GenerateContentConfig(
            system_instruction="You are AETHER, a fast desktop assistant. Be concise, direct, and helpful.",
            tools=gemini_tools,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )

        try:
            stream = await self.client.aio.models.generate_content_stream(
                model=self.target_model,
                contents=contents,
                config=config,
            )

            async for chunk in stream:
                if not chunk.candidates:
                    continue

                for candidate in chunk.candidates:
                    if not candidate.content or not candidate.content.parts:
                        continue

                    text_parts = []
                    tool_calls = []

                    for part in candidate.content.parts:
                        if part.text:
                            text_parts.append(part.text)
                        if part.function_call:
                            tool_calls.append(
                                ToolCall(
                                    name=part.function_call.name,
                                    args=dict(part.function_call.args or {}),
                                    call_id=getattr(part.function_call, "id", None),
                                    thought_signature=getattr(part, "thought_signature", None),
                                )
                            )

                    if text_parts or tool_calls or candidate.finish_reason:
                        finish_str = str(candidate.finish_reason) if candidate.finish_reason else None
                        yield StreamChunk(
                            text="".join(text_parts) if text_parts else None,
                            tool_calls=tool_calls if tool_calls else None,
                            finish_reason=finish_str,
                        )

        except errors.APIError as e:
            code = getattr(e, "code", None)
            err_msg = str(e)
            if code in (401, 403) or "API_KEY_INVALID" in err_msg:
                raise AuthenticationError(f"Gemini authentication failed: {err_msg}") from e
            elif code == 429 or "RESOURCE_EXHAUSTED" in err_msg:
                raise RateLimitError(f"Gemini rate limit exceeded: {err_msg}") from e
            elif code == 504:
                raise TimeoutError(f"Gemini request timed out: {err_msg}") from e
            elif code in (500, 502, 503):
                raise NetworkError(f"Gemini service unavailable ({code}): {err_msg}") from e
            raise ProviderError(f"Gemini API error ({code}): {err_msg}", retriable=False) from e
        except (httpx.TimeoutException, asyncio.TimeoutError) as e:
            raise TimeoutError(f"Gemini request timed out: {e}") from e
        except (httpx.NetworkError, httpx.ConnectError, OSError) as e:
            raise NetworkError(f"Network error connecting to Gemini: {e}") from e
        except (AuthenticationError, RateLimitError, TimeoutError, NetworkError, ProviderError):
            raise
        except Exception as e:
            raise ProviderError(f"Unexpected provider error: {e}", retriable=False) from e
