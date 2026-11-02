"""Universal LLM provider via LiteLLM — routes to any cloud or local model uniformly.

Every model call in AETHER goes through this single provider (ADR 0006 §1).
LiteLLM handles OpenAI, Anthropic, Google, DeepSeek, Ollama, and OpenRouter
with one consistent interface, automatic retries, and structured tool calling.
"""
import asyncio
import logging
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any

from aether_engine.providers.base import (
    AuthenticationError,
    BaseProvider,
    NetworkError,
    ProviderError,
    RateLimitError,
    StreamChunk,
    TimeoutError,
    ToolCall,
)
from aether_engine.providers.discovery import DISCOVERY_TIMEOUT

logger = logging.getLogger("aether_engine.providers.litellm_provider")

# Bounded timeout for the validation ping (Phase 10.1, B1). Same value as
# DISCOVERY_TIMEOUT: the ping is the same class of operation as a model-list
# fetch (one short request to a provider endpoint), so it gets the same bound.
VALIDATION_TIMEOUT = DISCOVERY_TIMEOUT

logging.getLogger("LiteLLM").setLevel(logging.WARNING)


# Epic 9: litellm is imported lazily via module __getattr__ (PEP 562).
# The real import costs ~4s (it eagerly builds the OpenAI type tree)
# and every engine start paid it, although no provider call can
# happen until a key is configured and a task runs. The first
# ``litellm`` reference in this module loads and caches the real
# module in globals; ``warm_litellm()`` lets startup pay the cost in
# a background thread instead. Tests patching
# ``litellm_provider.litellm.acompletion`` still land on the real
# module object; inside this module, call sites bind
# ``litellm = _load_litellm()`` locally as their first step.


def _load_litellm():
    import litellm as real

    # Suppress litellm's noisy debug logs in production.
    real.suppress_debug_info = True
    real.set_verbose = False
    globals()["litellm"] = real
    return real


def __getattr__(name: str):
    if name == "litellm":
        return _load_litellm()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def warm_litellm() -> None:
    """Force the deferred litellm import (background startup warm-up)."""
    _load_litellm()

# ---------------------------------------------------------------------------
# LiteLLM model-name mapping
# ---------------------------------------------------------------------------
# LiteLLM requires provider-prefixed model names for non-OpenAI providers.
# This map translates our internal registry IDs → LiteLLM model strings.
_LITELLM_MODEL_MAP: dict[str, str] = {
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
_PROVIDER_BASE_URL: dict[str, str] = {
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
    model_id = model_id.removeprefix("models/")

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
    base_url: str | None = None


class LiteLLMProvider(BaseProvider):
    """Concrete provider that routes any model call through LiteLLM's unified interface.

    Implements BaseProvider.call_stream() using litellm.acompletion(stream=True).
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        provider_name: str = "google_gemini",
        base_url: str | None = None,
        fallback_models: list[FallbackModel] | None = None,
        reasoning_effort: str = "standard",
        ttsr_rules: list | None = None,
    ):
        api_key = api_key.strip() if api_key else ""
        if not api_key and provider_name not in ("custom_openai", "openai_compatible", "ollama"):
            raise AuthenticationError("API key cannot be empty.")
        
        self.api_key = api_key if api_key else "sk-no-key-required"
        self.model = model
        self.provider_name = provider_name
        self.base_url = base_url.rstrip("/") if base_url else None
        self.litellm_model = resolve_litellm_model(model, provider_name)
        # Per-node fallback: list of FallbackModel with full provider config for cross-provider fallback
        self.fallback_models: list[FallbackModel] = fallback_models or []
        # Phase 9, B2: user-selected reasoning depth, forwarded to litellm for
        # reasoning-capable models ("standard" | "medium" | "high").
        self.reasoning_effort = reasoning_effort or "standard"
        # Epic 7, E7-02: TTSR stream rules (empty = off).
        from aether_engine.providers.ttsr import StreamRules

        self.stream_rules = StreamRules(ttsr_rules)

    def _build_call_kwargs(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Build the keyword arguments dict for litellm.acompletion()."""
        kwargs: dict[str, Any] = {
            "model": self.litellm_model,
            "messages": self._convert_messages(messages),
            "stream": True,
            "api_key": self.api_key,
            "max_tokens": 4096,
            "num_retries": 0,
        }

        # Set base URL if needed
        base_url = self.base_url or _PROVIDER_BASE_URL.get(self.provider_name)
        if base_url:
            kwargs["api_base"] = base_url
            
        # Spoof User-Agent to bypass strict WAF filters (e.g. AgentRouter)
        kwargs["extra_headers"] = {"User-Agent": "Cline/1.0.0"}

        # Tools (OpenAI-compatible format)
        if tools:
            kwargs["tools"] = self._convert_tools(tools)

        # Phase 9, B2: forward the user's reasoning-effort preference. Only sent
        # when non-default so models without reasoning support are unaffected.
        if self.reasoning_effort and self.reasoning_effort != "standard":
            kwargs["reasoning_effort"] = self.reasoning_effort

        return kwargs

    def _convert_messages(self, messages: Any) -> list[dict[str, Any]]:
        """Convert internal message format to OpenAI-compatible messages for LiteLLM.

        Also normalizes turn structure for strict providers like Google Gemini / Vertex AI
        which enforce that functionCall turns must immediately follow a user turn or functionResponse.
        """
        if isinstance(messages, str):
            return [{"role": "user", "content": messages}]

        converted: list[dict[str, Any]] = []

        for msg in messages:
            if isinstance(msg, str):
                converted.append({"role": "user", "content": msg})
                continue
            role = msg.get("role", "user")
            content = msg.get("content", "")

            if role in ("tool", "function"):
                call_id = msg.get("tool_call_id") or msg.get("call_id") or msg.get("id")
                tool_name = msg.get("name")

                # If call_id or tool_name missing, resolve from preceding assistant message
                if not call_id or not tool_name:
                    for prev_m in reversed(converted):
                        if prev_m.get("role") == "assistant" and prev_m.get("tool_calls"):
                            for prev_tc in prev_m["tool_calls"]:
                                fn = prev_tc.get("function", {})
                                if not tool_name or fn.get("name") == tool_name:
                                    if not call_id:
                                        call_id = prev_tc.get("id")
                                    if not tool_name:
                                        tool_name = fn.get("name")
                                    break
                                elif call_id and prev_tc.get("id") == call_id:
                                    if not tool_name:
                                        tool_name = fn.get("name")
                                    break
                            if call_id and tool_name:
                                break

                if not tool_name:
                    tool_name = "tool"
                if not call_id:
                    call_id = f"call_{tool_name}"

                converted.append({
                    "role": "tool",
                    "content": str(content),
                    "name": str(tool_name),
                    "tool_call_id": str(call_id),
                })
            elif role == "assistant":
                entry: dict[str, Any] = {"role": "assistant"}
                if content:
                    entry["content"] = content

                # Convert tool_calls to OpenAI format
                if msg.get("tool_calls"):
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
                            fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                            tool_name = tc.get("name") or fn.get("name") or "tool"
                            tool_args = tc.get("args") or fn.get("arguments") or {}
                            call_id = tc.get("id") or tc.get("call_id") or f"call_{tool_name}"
                            tc_list.append({
                                "id": str(call_id),
                                "type": "function",
                                "function": {
                                    "name": str(tool_name),
                                    "arguments": _safe_json_dumps(tool_args),
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

        # ── Turn Normalization for Gemini / Vertex AI strict constraints ──
        # Gemini requires that the first non-system turn is "user", and functionCall turns
        # must immediately follow a user turn or function response turn.
        system_msgs = [m for m in converted if m.get("role") == "system"]
        chat_msgs = [m for m in converted if m.get("role") != "system"]

        if not chat_msgs:
            return system_msgs + [{"role": "user", "content": "Hello"}]

        # Ensure first chat message is user
        if chat_msgs[0].get("role") in ("assistant", "tool"):
            chat_msgs.insert(0, {"role": "user", "content": "Please proceed with the task."})

        # Merge adjacent assistant messages to prevent consecutive model turns
        normalized_chat: list[dict[str, Any]] = []
        for m in chat_msgs:
            if normalized_chat and normalized_chat[-1].get("role") == "assistant" and m.get("role") == "assistant":
                prev = normalized_chat[-1]
                c1 = prev.get("content") or ""
                c2 = m.get("content") or ""
                merged_content = f"{c1}\n{c2}".strip() if (c1 or c2) else None
                prev["content"] = merged_content
                tc1 = prev.get("tool_calls") or []
                tc2 = m.get("tool_calls") or []
                if tc1 or tc2:
                    prev["tool_calls"] = tc1 + tc2
            else:
                normalized_chat.append(m)

        return system_msgs + normalized_chat

    def _convert_tools(self, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        node_name: str | None = None,
    ) -> AsyncGenerator[StreamChunk, None]:
        """Stream response chunks from any LiteLLM-supported provider.

        If the primary model fails with a retriable error and fallback_models
        are configured, automatically retries with each fallback in order.
        Logs NODE_FALLBACK_TRIGGERED (distinct from task-level FALLBACK_TRIGGERED)
        when a fallback model succeeds.
        """
        litellm = _load_litellm()  # Epic 9: deferred import, bound locally
        # Build the list of models to try: primary first, then fallbacks
        # Each entry is a tuple of (model_string, api_key, base_url, provider_name)
        models_to_try: list[tuple] = [
            (self.litellm_model, self.api_key, self.base_url, self.provider_name)
        ] + [
            (fm.model, fm.api_key, fm.base_url, fm.provider_name)
            for fm in self.fallback_models
        ]

        # Epic 5, E5-01 — the §6.4 gate on per-call fallback. Fallback
        # is forbidden (the primary error raises instead) when:
        # (a) tools have already executed in this task — the
        #     conversation carries tool results, and switching models
        #     mid-task after side effects is unsafe; or
        # (b) this call already yielded output — a restarted stream
        #     would duplicate text and repeat tool calls.
        tools_already_executed = any(
            isinstance(m, dict) and m.get("role") == "tool" for m in (messages or [])
        )
        yielded_any = False

        def _fallback_blocked() -> str | None:
            if tools_already_executed:
                return "tools have already executed in this task (no mid-task model switch)"
            if yielded_any:
                return "this call already streamed output (a restart would duplicate it)"
            return None

        def _record_attempt_usage(
            recorded_flag: list[bool],
            model_idx: int,
            current_model: str,
            current_provider: str,
            usage: tuple[int, int] | None,
            text_parts: list[str],
        ) -> None:
            """Record one attempt's call in the usage ledger, once.

            Finding M19: fires on the success path AND when the
            consumer closes the generator early (GeneratorExit at a
            yield — the intent classifier stops reading at its
            verdict). The call happened either way; the flag keeps
            the two paths from both recording it.
            """
            if recorded_flag[0]:
                return
            recorded_flag[0] = True
            self._record_usage(
                model_name=self.model if model_idx == 0 else current_model,
                pricing_model=current_model,
                provider_name=current_provider,
                usage=usage,
                messages=messages,
                output_text="".join(text_parts),
                node_name=node_name,
            )

        last_error: Exception | None = None
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

                accumulated_tool_calls: dict[int, dict[str, Any]] = {}
                # Epic 8, E8-A8.1a: per-attempt usage capture for the
                # cost ledger. Providers report token usage on the
                # stream's chunks (typically the last one); text is
                # accumulated only so token counts can be estimated —
                # and labelled estimated — when none are reported.
                call_usage: tuple[int, int] | None = None
                call_text_parts: list[str] = []

                # Finding M19: per-attempt recording flag for
                # _record_attempt_usage (success path + GeneratorExit
                # close path at the yields below share it).
                attempt_recorded = [False]

                async for chunk in response:
                    parsed_usage = _parse_usage(getattr(chunk, "usage", None))
                    if parsed_usage is not None:
                        call_usage = parsed_usage
                    delta = chunk.choices[0].delta if chunk.choices else None
                    finish_reason = chunk.choices[0].finish_reason if chunk.choices else None

                    if not delta and not finish_reason:
                        continue

                    text = getattr(delta, "content", None) if delta else None
                    # E1-16: reasoning-capable models expose their
                    # thinking on the delta (LiteLLM normalises it to
                    # reasoning_content; some providers use thinking).
                    thinking = None
                    if delta:
                        thinking = getattr(delta, "reasoning_content", None) or getattr(delta, "thinking", None)
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

                    # Emit text chunks as they arrive (thinking rides
                    # along on its own field, never inside the text)
                    if text:
                        call_text_parts.append(text)
                    if text or thinking:
                        # Epic 7, E7-02: TTSR rules vet the text
                        # before it leaves the provider. A block rule
                        # stops the stream (audited); redactions are
                        # applied in place.
                        if text and self.stream_rules.active:
                            text, blocked_rule = self.stream_rules.apply(text)
                            if blocked_rule is not None:
                                from aether_engine.audit import AuditLogger

                                AuditLogger().log_event("TTSR_BLOCKED", {
                                    "node": node_name,
                                    "rule": blocked_rule,
                                    "model": self.litellm_model,
                                })
                                raise ProviderError(
                                    f"Stream blocked by TTSR rule '{blocked_rule}'",
                                    retriable=False,
                                )
                        yielded_any = True
                        try:
                            yield StreamChunk(text=text, thinking=thinking, finish_reason=finish_reason)
                        except GeneratorExit:
                            _record_attempt_usage(
                                attempt_recorded, model_idx, current_model,
                                current_provider, call_usage, call_text_parts)
                            raise
                    elif finish_reason and not accumulated_tool_calls:
                        yielded_any = True
                        try:
                            yield StreamChunk(finish_reason=finish_reason)
                        except GeneratorExit:
                            _record_attempt_usage(
                                attempt_recorded, model_idx, current_model,
                                current_provider, call_usage, call_text_parts)
                            raise

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
                        yielded_any = True
                        try:
                            yield StreamChunk(
                                tool_calls=parsed_calls,
                                finish_reason=finish_reason,
                            )
                        except GeneratorExit:
                            _record_attempt_usage(
                                attempt_recorded, model_idx, current_model,
                                current_provider, call_usage, call_text_parts)
                            raise

                # If we got here, the call succeeded
                if is_fallback:
                    # Phase 9, A4: this must be visible in the audit log as its own
                    # event, distinct from task-level FALLBACK_TRIGGERED (Phase 5.1
                    # acceptance test #4). stdlib logging alone is not enough.
                    from aether_engine.audit import AuditLogger
                    AuditLogger().log_event("NODE_FALLBACK_TRIGGERED", {
                        "node": node_name,
                        "primary_model": self.litellm_model,
                        "failed_provider": self.provider_name,
                        "fallback_model": current_model,
                        "fallback_provider": current_provider,
                    })
                    logger.info(
                        f"NODE_FALLBACK_TRIGGERED: primary model '{self.litellm_model}' failed, "
                        f"succeeded with fallback model '{current_model}'"
                    )
                # Epic 8, E8-A8.1a: one ledger row per completed
                # call, attributed to the model that actually ran
                # (the fallback's own id when a fallback succeeded).
                _record_attempt_usage(
                    attempt_recorded, model_idx, current_model,
                    current_provider, call_usage, call_text_parts)
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
                if is_model_not_found and (model_idx < len(models_to_try) - 1) and _fallback_blocked() is None:
                    logger.warning(
                        f"Model '{current_model}' failed with retriable error: {e}. "
                        f"Trying next fallback..."
                    )
                    continue
                if is_model_not_found and _fallback_blocked() is not None:
                    logger.warning(f"Fallback blocked for model '{current_model}': {_fallback_blocked()}")
                raise ProviderError(f"Bad request: {e}", retriable=is_model_not_found) from e
            except litellm.NotFoundError as e:
                last_error = e
                if _fallback_blocked() is not None and model_idx < len(models_to_try) - 1:
                    logger.warning(f"Fallback blocked for model '{current_model}': {_fallback_blocked()}")
                if model_idx < len(models_to_try) - 1 and _fallback_blocked() is None:
                    logger.warning(
                        f"Model '{current_model}' failed with retriable error: {e}. "
                        f"Trying next fallback..."
                    )
                    continue
                raise ProviderError(f"Model not found: {e}", retriable=True) from e
            except (litellm.RateLimitError, litellm.Timeout,
                    litellm.APIConnectionError, litellm.ServiceUnavailableError,
                    getattr(litellm, "MidStreamFallbackError", Exception)) as e:
                # Retriable errors — try next fallback model if available
                last_error = e
                if _fallback_blocked() is not None and model_idx < len(models_to_try) - 1:
                    logger.warning(f"Fallback blocked for model '{current_model}': {_fallback_blocked()}")
                    blocked = _fallback_blocked()
                    if isinstance(e, litellm.RateLimitError) or "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                        raise RateLimitError(f"Rate limit exceeded: {e} (fallback blocked: {blocked})") from e
                    elif isinstance(e, litellm.Timeout):
                        raise TimeoutError(f"Request timed out: {e} (fallback blocked: {blocked})") from e
                    else:
                        raise NetworkError(f"Network/service error: {e} (fallback blocked: {blocked})") from e
                if is_fallback or model_idx < len(models_to_try) - 1:
                    logger.warning(
                        f"Model '{current_model}' failed with retriable error: {e}. "
                        f"{'Trying next fallback...' if model_idx < len(models_to_try) - 1 else 'No more fallbacks.'}"
                    )
                    continue
                # No more fallbacks — raise the appropriate typed exception
                err_str = str(e)
                if isinstance(e, litellm.RateLimitError) or "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                    raise RateLimitError(f"Rate limit exceeded: {e}") from e
                elif isinstance(e, litellm.Timeout):
                    raise TimeoutError(f"Request timed out: {e}") from e
                else:
                    raise NetworkError(f"Network/service error: {e}") from e
            except (AuthenticationError, RateLimitError, TimeoutError, NetworkError, ProviderError):
                raise
            except Exception as e:
                err_str = str(e)
                if ("429" in err_str or "RESOURCE_EXHAUSTED" in err_str or "quota" in err_str.lower()) and (is_fallback or model_idx < len(models_to_try) - 1) and _fallback_blocked() is None:
                    logger.warning(
                        f"Model '{current_model}' failed with rate limit/quota error: {e}. Trying next fallback..."
                    )
                    last_error = e
                    continue
                # Non-retriable unknown errors — don't try fallbacks
                raise ProviderError(f"Unexpected LiteLLM error: {e}", retriable=False) from e

        # All models exhausted
        if last_error:
            raise NetworkError(f"All models failed. Last error: {last_error}") from last_error

    def _record_usage(
        self,
        *,
        model_name: str,
        pricing_model: str,
        provider_name: str,
        usage: tuple[int, int] | None,
        messages: list[dict[str, Any]],
        output_text: str,
        node_name: str | None,
    ) -> None:
        """Epic 8, E8-A8.1a: record one completed call in the usage
        ledger (see aether_engine.cost.ledger). Observational only —
        a recording failure must never break the call that produced
        it, so this never raises."""
        try:
            from aether_engine.cost.ledger import record_provider_call

            record_provider_call(
                model_name=model_name,
                pricing_model=pricing_model,
                provider_name=provider_name,
                input_tokens=usage[0] if usage else None,
                output_tokens=usage[1] if usage else None,
                agent_name=node_name,
                messages=messages,
                output_text=output_text,
            )
        except Exception:  # usage recording is observational only.
            logger.debug("Usage recording failed", exc_info=True)


# ---------------------------------------------------------------------------
# Validation helper — lightweight model list call to verify a key works
# ---------------------------------------------------------------------------
async def validate_api_key(
    provider_name: str,
    api_key: str,
    model: str = "",
    base_url: str | None = None,
    timeout: float = VALIDATION_TIMEOUT,
) -> dict[str, Any]:
    """Validate a provider API key by making a minimal completion call.

    Returns dict with 'status' ('connected', 'invalid_key', 'network_error')
    and 'message' explaining the result. On a timeout the result carries the
    DiscoveryResult-style error shape: status 'network_error' with a 'code'
    of None (transport-level failure — no HTTP response was received, same
    convention as DiscoveryError) and a message naming the bound. A timeout
    is never reported as a success.

    ``timeout`` bounds the ping in seconds (default VALIDATION_TIMEOUT, 10s,
    matching DISCOVERY_TIMEOUT). It is enforced twice: passed to LiteLLM so
    the underlying HTTP client enforces it, and via ``asyncio.wait_for`` so
    the bound holds even if LiteLLM drops or ignores the kwarg for a provider.
    """
    import time

    litellm = _load_litellm()  # Epic 9: deferred import, bound locally

    # Pick a cheap model for the validation ping
    if not model:
        _default_models = {
            "google_gemini": "gemini-3.5-flash",
            "openai": "gpt-4o-mini",
            "anthropic": "claude-3-5-haiku",
            "deepseek": "deepseek-chat",
            "openrouter": "openrouter/auto",
            "custom_openai": "gpt-3.5-turbo",
        }
        model = _default_models.get(provider_name, "gpt-3.5-turbo")

    litellm_model = resolve_litellm_model(model, provider_name=provider_name)

    # LiteLLM's OpenAI client requires a non-empty key to bypass client-side validation.
    effective_api_key = api_key if api_key else "sk-no-key-required"

    kwargs: dict[str, Any] = {
        "model": litellm_model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 5,
        "api_key": effective_api_key,
        "timeout": timeout,
        "extra_headers": {"User-Agent": "Cline/1.0.0"}
    }

    effective_base = base_url.rstrip("/") if base_url else _PROVIDER_BASE_URL.get(provider_name)
    if effective_base:
        kwargs["api_base"] = effective_base

    start = time.time()
    try:
        # wait_for is the backstop: LiteLLM honours `timeout` for most
        # providers, but a stalled provider must not block validation
        # indefinitely if the kwarg is dropped anywhere along the path.
        await asyncio.wait_for(litellm.acompletion(**kwargs), timeout=timeout)
        latency = (time.time() - start) * 1000.0
        return {
            "status": "connected",
            "message": "API key is valid.",
            "latency_ms": round(latency, 2),
        }
    except litellm.AuthenticationError:
        return {"status": "invalid_key", "message": "Invalid API key."}
    except litellm.RateLimitError:
        # Rate limit means the key is valid, just throttled
        latency = (time.time() - start) * 1000.0
        return {
            "status": "connected",
            "message": "API key is valid (rate limited).",
            "latency_ms": round(latency, 2),
        }
    except (asyncio.TimeoutError, litellm.Timeout):  # noqa: UP041 — qualified on purpose: bare TimeoutError in this module is the domain error imported from providers.base, not the builtin asyncio raises.
        # Phase 10.1, B1: a stalled provider previously blocked validation
        # indefinitely (no timeout anywhere on this call). Return a real
        # error in the DiscoveryResult shape — code None means no HTTP
        # response was received, exactly like DiscoveryError — never a
        # silent success and never an empty success.
        latency = (time.time() - start) * 1000.0
        return {
            "status": "network_error",
            "message": f"Validation timed out after {timeout}s: provider did not respond.",
            "code": None,
            "latency_ms": round(latency, 2),
        }
    except Exception as e:  # noqa: BLE001 — validation boundary: any provider failure becomes a structured network_error result, never a crash.
        return {"status": "network_error", "message": f"Validation failed: {e}", "code": None}


# ---------------------------------------------------------------------------
# Usage helpers (Epic 8, E8-A8.1a)
# ---------------------------------------------------------------------------


def _usage_value(usage: Any, *names: str) -> int | None:
    for name in names:
        value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
    return None


def _parse_usage(usage: Any) -> tuple[int, int] | None:
    """Extract (input_tokens, output_tokens) from a LiteLLM usage
    payload (object or dict; providers spell the fields both ways).
    Returns None when the payload carries no token counts at all."""
    if usage is None:
        return None
    input_tokens = _usage_value(usage, "prompt_tokens", "input_tokens")
    output_tokens = _usage_value(usage, "completion_tokens", "output_tokens")
    if input_tokens is None and output_tokens is None:
        return None
    return (input_tokens or 0, output_tokens or 0)


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


def _safe_json_loads(s: str) -> dict[str, Any]:
    """Safely parse a JSON string into a dict."""
    if not s:
        return {}
    try:
        result = json.loads(s)
        return result if isinstance(result, dict) else {"value": result}
    except (json.JSONDecodeError, ValueError):
        return {"raw": s}
