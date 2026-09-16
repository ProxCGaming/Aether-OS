"""Live model discovery service for all supported LLM providers.

Queries provider endpoints dynamically with the user's API key to discover
all available text and chat models instead of relying on static hardcoded lists.
"""
import logging
from typing import Any, Dict, List, Optional
import httpx

logger = logging.getLogger("aether_engine.providers.discovery")

# Default request timeout for model listing calls
DISCOVERY_TIMEOUT = 10.0


async def fetch_available_models(
    provider_name: str,
    api_key: str,
    base_url: Optional[str] = None,
    filter_reachability: bool = True,
    force_reachability: bool = False,
) -> List[str]:
    """Dynamically fetch all available generation/chat models for a given provider and API key.

    Optionally filters the list to only include models verified to be reachable with the key.
    """
    if not api_key or not api_key.strip():
        return []

    key = api_key.strip()
    raw_models: List[str] = []
    try:
        if provider_name == "google_gemini":
            raw_models = await _discover_gemini_models(key)
        elif provider_name == "openai":
            raw_models = await _discover_openai_models(key)
        elif provider_name == "anthropic":
            raw_models = await _discover_anthropic_models(key)
        elif provider_name == "deepseek":
            raw_models = await _discover_deepseek_models(key)
        elif provider_name == "openrouter":
            raw_models = await _discover_openrouter_models(key)
        elif provider_name in ("custom_openai", "custom") or provider_name.startswith("custom_"):
            raw_models = await _discover_openai_compatible_models(base_url=base_url or "", api_key=key)
        else:
            logger.warning(f"Model discovery not implemented for provider '{provider_name}'")
            return []

        if filter_reachability and raw_models:
            # Skip exhaustive reachability testing if there are too many models (saves time and API limits)
            if len(raw_models) > 30:
                logger.info(f"Skipping reachability sweep for {provider_name} due to large model list ({len(raw_models)} models).")
                return raw_models
                
            from aether_engine.models.reachability import GLOBAL_REACHABILITY_CHECKER
            return await GLOBAL_REACHABILITY_CHECKER.filter_reachable_models(
                provider_name=provider_name,
                model_ids=raw_models,
                api_key=key,
                base_url=base_url,
                force=force_reachability,
            )

        return raw_models
    except Exception as e:
        logger.error(f"Error discovering models for {provider_name}: {e}")
        return []


async def _discover_gemini_models(api_key: str) -> List[str]:
    """Fetch available models from Google Gemini API and filter for content generation."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
    async with httpx.AsyncClient(timeout=DISCOVERY_TIMEOUT) as client:
        resp = await client.get(url)
        if resp.status_code != 200:
            logger.warning(f"Gemini model list returned status {resp.status_code}: {resp.text[:200]}")
            return []

        data = resp.json()
        raw_models = data.get("models", [])

    discovered = []
    # Keywords to exclude (non-chat / embedding / special purpose models / deprecated 2.5 preview endpoints / broken previews)
    exclude_keywords = ("embedding", "imagen", "aqa", "tts", "whisper", "gecko", "bison", "learnlm", "2.5-flash", "2.5-pro", "2.5-computer", "preview")

    for m in raw_models:
        name = m.get("name", "")  # format: "models/gemini-2.0-flash"
        model_id = name[7:] if name.startswith("models/") else name

        # Check supported methods
        methods = m.get("supportedGenerationMethods", [])
        if "generateContent" not in methods:
            continue

        if any(kw in model_id.lower() for kw in exclude_keywords):
            continue

        discovered.append(model_id)

    # Filter out alias/latest models that LiteLLM can't reliably resolve
    # (e.g. gemini-flash-latest, gemini-pro-latest are Vertex AI aliases, not AI Studio IDs)
    alias_blocklist = {"gemini-flash-latest", "gemini-pro-latest", "gemini-flash-lite-latest"}
    discovered = [m for m in discovered if m not in alias_blocklist]

    # Sort so newer and popular models appear near top
    def _gemini_sort_key(m_id: str) -> tuple:
        lower = m_id.lower()
        priority = 99
        if lower == "gemini-2.0-flash":
            priority = 1
        elif lower == "gemini-2.0-flash-lite":
            priority = 2
        elif "2.0" in lower:
            priority = 3
        elif "1.5" in lower:
            priority = 6
        elif "flash" in lower:
            priority = 7
        elif "pro" in lower:
            priority = 8
        return (priority, lower)

    discovered.sort(key=_gemini_sort_key)
    return discovered


async def _discover_openai_models(api_key: str) -> List[str]:
    """Fetch available models from OpenAI /v1/models endpoint and filter for chat models."""
    url = "https://api.openai.com/v1/models"
    headers = {"Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(timeout=DISCOVERY_TIMEOUT) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            logger.warning(f"OpenAI model list returned status {resp.status_code}: {resp.text[:200]}")
            return []

        data = resp.json()
        raw_models = data.get("data", [])

    discovered = []
    # Target chat / reasoning prefixes
    target_prefixes = ("gpt-4", "gpt-3.5", "o1", "o3", "chatgpt")
    exclude_keywords = ("audio", "realtime", "embed", "moderation", "tts", "dall-e", "instruct", "transcription")

    for m in raw_models:
        model_id = m.get("id", "")
        lower = model_id.lower()

        if any(lower.startswith(p) for p in target_prefixes):
            if not any(kw in lower for kw in exclude_keywords):
                discovered.append(model_id)

    def _openai_sort_key(m_id: str) -> tuple:
        lower = m_id.lower()
        if lower.startswith("gpt-4o"):
            return (1, lower)
        if lower.startswith("o1") or lower.startswith("o3"):
            return (2, lower)
        if lower.startswith("gpt-4"):
            return (3, lower)
        return (4, lower)

    discovered.sort(key=_openai_sort_key)
    return discovered


async def _discover_anthropic_models(api_key: str) -> List[str]:
    """Fetch available models from Anthropic /v1/models endpoint."""
    url = "https://api.anthropic.com/v1/models"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    async with httpx.AsyncClient(timeout=DISCOVERY_TIMEOUT) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            logger.warning(f"Anthropic model list returned status {resp.status_code}: {resp.text[:200]}")
            # Fallback to standard Claude 3.5/3.7 models if endpoint is not accessible
            return [
                "claude-3-7-sonnet-latest",
                "claude-3-5-sonnet-latest",
                "claude-3-5-haiku-latest",
                "claude-3-opus-latest",
            ]

        data = resp.json()
        raw_models = data.get("data", [])

    discovered = [m.get("id", "") for m in raw_models if m.get("id", "").startswith("claude-")]
    if not discovered:
        discovered = [
            "claude-3-7-sonnet-latest",
            "claude-3-5-sonnet-latest",
            "claude-3-5-haiku-latest",
            "claude-3-opus-latest",
        ]
    return discovered


async def _discover_deepseek_models(api_key: str) -> List[str]:
    """Fetch available models from DeepSeek /models endpoint."""
    url = "https://api.deepseek.com/models"
    headers = {"Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(timeout=DISCOVERY_TIMEOUT) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            logger.warning(f"DeepSeek model list returned status {resp.status_code}: {resp.text[:200]}")
            return ["deepseek-chat", "deepseek-reasoner"]

        data = resp.json()
        raw_models = data.get("data", [])

    discovered = [m.get("id", "") for m in raw_models if m.get("id")]
    return discovered if discovered else ["deepseek-chat", "deepseek-reasoner"]


async def _discover_openrouter_models(api_key: str) -> List[str]:
    """Fetch top text/chat models from OpenRouter /api/v1/models endpoint."""
    url = "https://openrouter.ai/api/v1/models"
    headers = {"Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(timeout=DISCOVERY_TIMEOUT) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            logger.warning(f"OpenRouter model list returned status {resp.status_code}: {resp.text[:200]}")
            return []

        data = resp.json()
        raw_models = data.get("data", [])

    discovered = []
    for m in raw_models[:50]:  # Limit top 50
        model_id = m.get("id", "")
        if not model_id:
            continue
        # Skip tilde-prefixed aliases (temporary/unstable IDs that don't resolve)
        if model_id.startswith("~"):
            continue
        # Skip batch-only variants (don't support streaming)
        if model_id.endswith(":batch"):
            continue
        discovered.append(model_id)

    return discovered


async def _discover_openai_compatible_models(base_url: str, api_key: str) -> List[str]:
    """Fetch available models from an OpenAI-compatible /models endpoint."""
    if not base_url or not base_url.strip():
        return []

    base_url = base_url.strip().rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    candidates = []
    if base_url.endswith("/models"):
        candidates.append(base_url)
    else:
        candidates.append(f"{base_url}/models")
        if not base_url.endswith("/v1"):
            candidates.append(f"{base_url}/v1/models")

    raw_models = []
    async with httpx.AsyncClient(timeout=DISCOVERY_TIMEOUT) as client:
        for url in candidates:
            try:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, dict):
                        if "data" in data and isinstance(data["data"], list):
                            raw_models = data["data"]
                            break
                        elif "models" in data and isinstance(data["models"], list):
                            raw_models = data["models"]
                            break
                    elif isinstance(data, list):
                        raw_models = data
                        break
            except Exception as e:
                logger.debug(f"Failed querying {url}: {e}")

    discovered = []
    exclude_keywords = ("audio", "realtime", "embed", "moderation", "tts", "dall-e", "transcription", "rerank")

    for m in raw_models:
        if isinstance(m, str):
            model_id = m
        elif isinstance(m, dict):
            model_id = m.get("id") or m.get("name") or m.get("model") or ""
        else:
            continue

        if not model_id:
            continue

        lower = model_id.lower()
        if not any(kw in lower for kw in exclude_keywords):
            discovered.append(model_id)

    return discovered
