"""Universal LLM provider via LiteLLM — routes to any cloud or local model uniformly.

Every model call in AETHER goes through this single provider (ADR 0006 §1).
LiteLLM handles OpenAI, Anthropic, Google, DeepSeek, Ollama, and OpenRouter
with one consistent interface, automatic retries, and structured tool calling.
"""
import asyncio
import logging
from dataclasses import dataclass
from typing import Any, AsyncGenerator, Dict, List, Optional

import litellm

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

logger = logging.getLogger("aether_engine.providers.litellm_provider")

# Suppress litellm's noisy debug logs in production
litellm.suppress_debug_info = True

# ---------------------------------------------------------------------------
# LiteLLM model-name mapping
# ---------------------------------------------------------------------------
# LiteLLM requires provider-prefixed model names for non-OpenAI providers.
# This map translates our internal registry IDs → LiteLLM model strings.
_LITELLM_MODEL_MAP: Dict[str, str] = {
    # Google Gemini (via Google AI Studio)
    "gemini-2.5-flash": "gemini/gemini-2.5-flash",
    "gemini-2.5-pro": "gemini/gemini-2.5-pro",
    "gemini-2.0-flash": "gemini/gemini-2.0-flash",
    "gemini-2.0-flash-lite": "gemini/gemini-2.0-flash-lite",
    # OpenAI (native — no prefix needed)
    "gpt-4o-mini": "gpt-4o-mini",
    "gpt-4o": "gpt-4o",
    # Anthropic
    "claude-3-5-haiku": "anthropic/claude-3-5-haiku-latest",
    "claude-3-5-sonnet": "anthropic/claude-3-5-sonnet-latest",
    # DeepSeek
    "deepseek-chat": "deepseek/deepseek-chat",
    "deepseek-reasoner": "deepseek/deepseek-reasoner",
}

# Provider-specific base URLs (only needed for non-standard endpoints)
_PROVIDER_BASE_URL: Dict[str, str] = {
    "deepseek": "https://api.deepseek.com",
    "openrouter": "https://openrouter.ai/api/v1",
}


_KNOWN_LITELLM_PREFIXES = (
    "openrouter/",
    "gemini/",
    "anthropic/",
    "deepseek/",
    "openai/",
    "groq/",
    "mistral/",
    "ollama/",
    "azure/",
    "bedrock/",
    "vertex_ai/",
    "huggingface/",
    "together_ai/",
    "perplexity/",
    "cohere/",
)


def resolve_litellm_model(model_id: str, provider_name: str = "") -> str:
    """Resolve internal model ID to LiteLLM-compatible model string.

    Ensures that every model passed to LiteLLM has the correct provider prefix,
    even for provider catalogs that use slash-formatted model IDs (e.g. OpenRouter's
    'moonshotai/kimi-k3' -> 'openrouter/moonshotai/kimi-k3').
    """
    if not model_id:
        return model_id

    model_id = model_id.strip().lstrip("~")

    # Check static mapping first
    if model_id in _LITELLM_MODEL_MAP:
        return _LITELLM_MODEL_MAP[model_id]

    # Clean Google Gemini API format 'models/gemini-...'
    if model_id.startswith("models/"):
        model_id = model_id[7:]

    # If provider is explicitly known, apply provider-specific prefix rules:
    if provider_name == "openrouter":
        if not model_id.startswith("openrouter/"):
            return f"openrouter/{model_id}"
        return model_id

    if provider_name in ("google_gemini", "gemini"):
        if not model_id.startswith("gemini/"):
            return f"gemini/{model_id}"
        return model_id

    if provider_name == "anthropic":
        if not model_id.startswith("anthropic/"):
            return f"anthropic/{model_id}"
        return model_id

    if provider_name == "deepseek":
        if not model_id.startswith("deepseek/"):
            return f"deepseek/{model_id}"
        return model_id

    if provider_name == "openai":
        return model_id

    if provider_name in ("custom_openai", "custom") or provider_name.startswith("custom_"):
        if not model_id.startswith("openai/"):
            return f"openai/{model_id}"
        return model_id

    if provider_name in ("groq", "mistral", "ollama"):
        prefix = f"{provider_name}/"
        if not model_id.startswith(prefix):
            return f"{prefix}{model_id}"
        return model_id

    # If provider_name is not specified or unrecognized:
    # 1. If already prefixed with a recognized LiteLLM provider, keep it
    if any(model_id.startswith(p) for p in _KNOWN_LITELLM_PREFIXES):
        return model_id

    # 2. Check model name patterns
    if model_id.startswith("gemini"):
        return f"gemini/{model_id}"
    if model_id.startswith("claude"):
        return f"anthropic/{model_id}"
    if model_id.startswith("deepseek"):
        return f"deepseek/{model_id}"

    # 3. If model has a slash (e.g. 'org/model-name' like 'moonshotai/kimi-k3'),
    # and hasn't matched any known LiteLLM provider prefix, it is an OpenRouter model!
    if "/" in model_id:
        return f"openrouter/{model_id}"

    return model_id


@dataclass
class FallbackModel:
    """Configuration for a fallback model including its provider-specific credentials."""
    model: str
    api_key: str
    provider_name: str
    base_url: Optional[str] = None


class LiteLLMProvider(BaseProvider):
    """Concrete provider that routes any model call through LiteLLM's unified interface.

    Implements BaseProvider.call_stream() using litellm.acompletion(stream=True).
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        provider_name: str = "google_gemini",
        base_url: Optional[str] = None,
        fallback_models: Optional[List[FallbackModel]] = None,
    ):
        if not api_key or not api_key.strip():
            raise AuthenticationError("API key cannot be empty.")
        self.api_key = api_key.strip()
        self.model = model
        self.provider_name = provider_name
        self.base_url = base_url.rstrip("/") if base_url else None
        self.litellm_model = resolve_litellm_model(model, provider_name)
        # Per-node fallback: list of FallbackModel with full provider config for cross-provider fallback
        self.fallback_models: List[FallbackModel] = fallback_models or []

    def _build_call_kwargs(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Build the keyword arguments dict for litellm.acompletion()."""
        kwargs: Dict[str, Any] = {
            "model": self.litellm_model,
            "messages": self._convert_messages(messages),
            "stream": True,
            "api_key": self.api_key,
            "max_tokens": 4096,
        }

        # Set base URL if needed
        base_url = self.base_url or _PROVIDER_BASE_URL.get(self.provider_name)
        if base_url:
            kwargs["api_base"] = base_url

        # Tools (OpenAI-compatible format)
        if tools:
            kwargs["tools"] = self._convert_tools(tools)

        return kwargs

    def _convert_messages(self, messages: Any) -> List[Dict[str, Any]]:
        """Convert internal message format to OpenAI-compatible messages for LiteLLM."""
        if isinstance(messages, str):
            return [{"role": "user", "content": messages}]

        converted: List[Dict[str, Any]] = []

        for msg in messages:
            if isinstance(msg, str):
                converted.append({"role": "user", "content": msg})
                continue
            role = msg.get("role", "user")
            content = msg.get("content", "")

            if role == "tool":
                call_id = msg.get("tool_call_id") or msg.get("call_id")
                tool_name = msg.get("name", "tool")

                # If call_id was not explicitly stored on tool message, resolve it from the preceding assistant message
                if not call_id:
                    for prev_m in reversed(converted):
                        if prev_m.get("role") == "assistant" and prev_m.get("tool_calls"):
                            for prev_tc in prev_m["tool_calls"]:
                                fn = prev_tc.get("function", {})
                                if fn.get("name") == tool_name:
                                    call_id = prev_tc.get("id")
                                    break
                            if call_id:
                                break

                if not call_id:
                    call_id = f"call_{tool_name}"

                converted.append({
                    "role": "tool",
                    "content": str(content),
                    "name": tool_name,
                    "tool_call_id": str(call_id),
                })
            elif role == "assistant":
                entry: Dict[str, Any] = {"role": "assistant"}
                if content:
                    entry["content"] = content

                # Convert tool_calls to OpenAI format
                if "tool_calls" in msg and msg["tool_calls"]:
                    tc_list = []
                    for tc in msg["tool_calls"]:
                        if isinstance(tc, ToolCall):
                            tc_list.append({
                                "id": tc.call_id or f"call_{tc.name}",
                                "type": "function",
                                "function": {
                                    "name": tc.name,
                                    "arguments": _safe_json_dumps(tc.args),
                                },
                            })
                        elif isinstance(tc, dict):
                            tc_list.append({
                                "id": tc.get("id", tc.get("call_id", f"call_{tc.get('name', 'tool')}")),
                                "type": "function",
                                "function": {
                                    "name": tc.get("name", "tool"),
                                    "arguments": _safe_json_dumps(tc.get("args", {})),
                                },
                            })
                    entry["tool_calls"] = tc_list
                    if not content:
                        entry["content"] = None

                converted.append(entry)
            else:
                # user or system
                if isinstance(content, list):
                    parts = []
                    for item in content:
                        if isinstance(item, str):
                            parts.append({"type": "text", "text": item})
                        elif isinstance(item, dict):
                            if item.get("type") == "text":
                                parts.append({"type": "text", "text": item.get("text", "")})
                            elif item.get("type") == "image_url":
                                parts.append(item)
                    converted.append({"role": role, "content": parts})
                else:
                    converted.append({"role": role, "content": str(content)})

        return converted

    def _convert_tools(self, tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Convert internal tool definitions to OpenAI-format function tools.

        Handles both formats:
        1. Flat format: {"name": "...", "description": "...", "parameters": {...}}
        2. OpenAI function calling format: {"type": "function", "function": {"name": "...", ...}}
        """
        result = []
        for t in tools:
            if t.get("type") == "function" and "function" in t:
                # Already in OpenAI function calling format
                fn = t["function"]
                result.append({
                    "type": "function",
                    "function": {
                        "name": fn["name"],
                        "description": fn.get("description", ""),
                        "parameters": fn.get("parameters", {"type": "object", "properties": {}}),
                    },
                })
            else:
                # Flat format - wrap in function calling format
                result.append({
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t.get("description", ""),
                        "parameters": t.get("parameters", {"type": "object", "properties": {}}),
                    },
                })
        return result

    async def call_stream(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        """Stream response chunks from any LiteLLM-supported provider.

        If the primary model fails with a retriable error and fallback_models
        are configured, automatically retries with each fallback in order.
        Logs NODE_FALLBACK_TRIGGERED (distinct from task-level FALLBACK_TRIGGERED)
        when a fallback model succeeds.
        """
        # Build the list of models to try: primary first, then fallbacks
        # Each entry is a tuple of (model_string, api_key, base_url, provider_name)
        models_to_try: List[tuple] = [
            (self.litellm_model, self.api_key, self.base_url, self.provider_name)
        ] + [
            (fm.model, fm.api_key, fm.base_url, fm.provider_name)
            for fm in self.fallback_models
        ]

        last_error: Optional[Exception] = None
        for model_idx, (current_model, current_api_key, current_base_url, current_provider) in enumerate(models_to_try):
            is_fallback = model_idx > 0
            try:
                kwargs = self._build_call_kwargs(messages, tools)
                # Override model and credentials for fallback attempts
                kwargs["model"] = current_model
                kwargs["api_key"] = current_api_key
                
                # Correctly resolve api_base for the current provider
                resolved_base_url = current_base_url or _PROVIDER_BASE_URL.get(current_provider)
                if resolved_base_url:
                    kwargs["api_base"] = resolved_base_url
                else:
                    kwargs.pop("api_base", None)

                response = await litellm.acompletion(**kwargs)

                accumulated_tool_calls: Dict[int, Dict[str, Any]] = {}

                async for chunk in response:
                    delta = chunk.choices[0].delta if chunk.choices else None
                    finish_reason = chunk.choices[0].finish_reason if chunk.choices else None

                    if not delta and not finish_reason:
                        continue

                    text = getattr(delta, "content", None) if delta else None
                    tool_call_deltas = getattr(delta, "tool_calls", None) if delta else None

                    # Accumulate tool call chunks (they arrive incrementally)
                    if tool_call_deltas:
                        for tc_delta in tool_call_deltas:
                            idx = tc_delta.index
                            if idx not in accumulated_tool_calls:
                                accumulated_tool_calls[idx] = {
                                    "id": "",
                                    "name": "",
                                    "arguments": "",
                                }
                            acc = accumulated_tool_calls[idx]
                            if tc_delta.id:
                                acc["id"] = tc_delta.id
                            if tc_delta.function:
                                if tc_delta.function.name:
                                    acc["name"] += tc_delta.function.name
                                if tc_delta.function.arguments:
                                    acc["arguments"] += tc_delta.function.arguments

                    # Emit text chunks as they arrive
                    if text:
                        yield StreamChunk(text=text, finish_reason=finish_reason)
                    elif finish_reason and not accumulated_tool_calls:
                        yield StreamChunk(finish_reason=finish_reason)

                    # When stream is done with tool calls, emit them all at once
                    if finish_reason and accumulated_tool_calls:
                        parsed_calls = []
                        for _idx, acc in sorted(accumulated_tool_calls.items()):
                            parsed_args = _safe_json_loads(acc["arguments"])
                            call_id = acc["id"] or f"call_{acc['name']}"
                            parsed_calls.append(
                                ToolCall(
                                    name=acc["name"],
                                    args=parsed_args,
                                    call_id=call_id,
                                )
                            )
                        yield StreamChunk(
                            tool_calls=parsed_calls,
                            finish_reason=finish_reason,
                        )

                # If we got here, the call succeeded
                if is_fallback:
                    logger.info(
                        f"NODE_FALLBACK_TRIGGERED: primary model '{self.litellm_model}' failed, "
                        f"succeeded with fallback model '{current_model}'"
                    )
                return  # Success — exit the retry loop

            except litellm.AuthenticationError as e:
                raise AuthenticationError(f"Authentication failed: {e}") from e
            except litellm.BadRequestError as e:
                # Check if it's a "model not found" / "invalid model" error - retriable for fallback
                err_str = str(e)
                model_not_found_indicators = [
                    "not a valid model",
                    "model not found",
                    "invalid model",
                    "does not exist",
                    "unknown model",
                    "model.*not.*support",
                    "no such model",
                ]
                import re
                is_model_not_found = any(re.search(pattern, err_str, re.IGNORECASE) for pattern in model_not_found_indicators)
                last_error = e
                if is_model_not_found and (model_idx < len(models_to_try) - 1):
                    logger.warning(
                        f"Model '{current_model}' failed with retriable error: {e}. "
                        f"Trying next fallback..."
                    )
                    continue
                raise ProviderError(f"Bad request: {e}", retriable=is_model_not_found) from e
            except litellm.NotFoundError as e:
                last_error = e
                if model_idx < len(models_to_try) - 1:
                    logger.warning(
                        f"Model '{current_model}' failed with retriable error: {e}. "
                        f"Trying next fallback..."
                    )
                    continue
                raise ProviderError(f"Model not found: {e}", retriable=True) from e
            except (litellm.RateLimitError, litellm.Timeout,
                    litellm.APIConnectionError, litellm.ServiceUnavailableError) as e:
                # Retriable errors — try next fallback model if available
                last_error = e
                if is_fallback or model_idx < len(models_to_try) - 1:
                    logger.warning(
                        f"Model '{current_model}' failed with retriable error: {e}. "
                        f"{'Trying next fallback...' if model_idx < len(models_to_try) - 1 else 'No more fallbacks.'}"
                    )
                    continue
                # No more fallbacks — raise the appropriate typed exception
                if isinstance(e, litellm.RateLimitError):
                    raise RateLimitError(f"Rate limit exceeded: {e}") from e
                elif isinstance(e, litellm.Timeout):
                    raise TimeoutError(f"Request timed out: {e}") from e
                else:
                    raise NetworkError(f"Network/service error: {e}") from e
            except (AuthenticationError, RateLimitError, TimeoutError, NetworkError, ProviderError):
                raise
            except Exception as e:
                # Non-retriable unknown errors — don't try fallbacks
                raise ProviderError(f"Unexpected LiteLLM error: {e}", retriable=False) from e

        # All models exhausted
        if last_error:
            raise NetworkError(f"All models failed. Last error: {last_error}") from last_error


# ---------------------------------------------------------------------------
# Validation helper — lightweight model list call to verify a key works
# ---------------------------------------------------------------------------
async def validate_api_key(
    provider_name: str,
    api_key: str,
    model: str = "",
    base_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate a provider API key by making a minimal completion call.

    Returns dict with 'status' ('connected', 'invalid_key', 'network_error')
    and 'message' explaining the result.
    """
    import time

    # Pick a cheap model for the validation ping
    if not model:
        _default_models = {
            "google_gemini": "gemini-2.0-flash",
            "openai": "gpt-4o-mini",
            "anthropic": "claude-3-5-haiku",
            "deepseek": "deepseek-chat",
            "openrouter": "openrouter/auto",
            "custom_openai": "gpt-3.5-turbo",
        }
        model = _default_models.get(provider_name, "gpt-3.5-turbo")

    litellm_model = resolve_litellm_model(model, provider_name=provider_name)

    kwargs: Dict[str, Any] = {
        "model": litellm_model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 5,
        "api_key": api_key,
    }

    effective_base = base_url.rstrip("/") if base_url else _PROVIDER_BASE_URL.get(provider_name)
    if effective_base:
        kwargs["api_base"] = effective_base

    start = time.time()
    try:
        resp = await litellm.acompletion(**kwargs)
        latency = (time.time() - start) * 1000.0
        return {
            "status": "connected",
            "message": "API key is valid.",
            "latency_ms": round(latency, 2),
        }
    except litellm.AuthenticationError:
        return {"status": "invalid_key", "message": "Invalid API key."}
    except (litellm.RateLimitError,):
        # Rate limit means the key is valid, just throttled
        latency = (time.time() - start) * 1000.0
        return {
            "status": "connected",
            "message": "API key is valid (rate limited).",
            "latency_ms": round(latency, 2),
        }
    except Exception as e:
        return {"status": "network_error", "message": f"Validation failed: {e}"}


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------
import json


def _safe_json_dumps(obj: Any) -> str:
    """Safely serialize to JSON string."""
    if isinstance(obj, str):
        return obj
    try:
        return json.dumps(obj)
    except (TypeError, ValueError):
        return str(obj)


def _safe_json_loads(s: str) -> Dict[str, Any]:
    """Safely parse a JSON string into a dict."""
    if not s:
        return {}
    try:
        result = json.loads(s)
        return result if isinstance(result, dict) else {"value": result}
    except (json.JSONDecodeError, ValueError):
        return {"raw": s}
