"""Live model discovery service for all supported LLM providers.

Queries provider endpoints dynamically with the user's API key to discover
all available text and chat models instead of relying on static hardcoded lists.

Design notes (per user decision, Oct 2026):
- Discovery returns BOTH the chat-capable (filtered) list and the excluded
  list. The UI shows everything the key gives access to; validation pings
  only run against the filtered list.
- There are NO hardcoded fallback model lists. If the fetch fails, the
  caller gets a DiscoveryError carrying the HTTP status code (or None for
  transport-level failures) so the UI can show the real error.
"""
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
import httpx

logger = logging.getLogger("aether_engine.providers.discovery")

# Default request timeout for model listing calls
DISCOVERY_TIMEOUT = 10.0

# Providers with a dedicated (hardcoded-endpoint) discovery implementation.
_STANDARD_PROVIDERS = {"google_gemini", "openai", "anthropic", "deepseek", "openrouter"}


@dataclass(eq=False)
class DiscoveryError(Exception):
    """A failed model-list fetch. ``code`` is the HTTP status, or None when
    no HTTP response was received at all (DNS, refused, timeout)."""

    code: Optional[int]
    message: str

    def __str__(self) -> str:
        return self.message

    def to_dict(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message}


@dataclass
class DiscoveryResult:
    """Result of a model-list fetch.

    ``models``: chat-capable models after filtering (what selectors show and
    what validation pings).
    ``excluded``: models the provider returned but the filter rejected, as
    ``{"id": ..., "kind": ...}`` dicts where kind is one of
    "embedding" | "image" | "audio" | "other".
    ``error``: set when the fetch itself failed; models/excluded are empty.
    """

    models: List[str] = field(default_factory=list)
    excluded: List[Dict[str, str]] = field(default_factory=list)
    error: Optional[DiscoveryError] = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _classify_excluded(model_id: str) -> str:
    """Best-effort kind tag for a filtered-out model, for UI display."""
    lower = model_id.lower()
    if any(k in lower for k in ("embed", "gecko", "rerank")):
        return "embedding"
    if any(k in lower for k in ("imagen", "image", "dall-e", "dalle", "diffusion")):
        return "image"
    if any(k in lower for k in ("tts", "audio", "whisper", "transcribe", "lyria")):
        return "audio"
    return "other"


def _excluded(model_id: str) -> Dict[str, str]:
    return {"id": model_id, "kind": _classify_excluded(model_id)}


async def fetch_available_models(
    provider_name: str,
    api_key: str,
    base_url: Optional[str] = None,
    filter_reachability: bool = False,
    force_reachability: bool = False,
) -> DiscoveryResult:
    """Dynamically fetch available models for a provider and API key.

    Returns a DiscoveryResult with the filtered chat-capable list, the
    excluded list, and an optional DiscoveryError (with HTTP status code)
    when the fetch failed. Never returns hardcoded fallback models.
    """
    key = api_key.strip() if api_key else ""
    has_custom_url = bool(base_url and base_url.strip())
    # Any provider that is not one of the hardcoded standard providers and exposes
    # a custom base URL is treated as OpenAI-compatible, regardless of its key name.
    is_custom = (
        provider_name in ("custom_openai", "custom")
        or provider_name.startswith("custom_")
        or (has_custom_url and provider_name not in _STANDARD_PROVIDERS)
    )

    if not key and not is_custom:
        return DiscoveryResult()

    try:
        if provider_name == "google_gemini":
            models, excluded = await _discover_gemini_models(key)
        elif provider_name == "openai":
            models, excluded = await _discover_openai_models(key)
        elif provider_name == "anthropic":
            models, excluded = await _discover_anthropic_models(key)
        elif provider_name == "deepseek":
            models, excluded = await _discover_deepseek_models(key)
        elif provider_name == "openrouter":
            models, excluded = await _discover_openrouter_models(key)
        elif is_custom:
            models, excluded = await _discover_openai_compatible_models(base_url=base_url or "", api_key=key)
        else:
            logger.warning(f"Model discovery not implemented for provider '{provider_name}'")
            return DiscoveryResult()

        if filter_reachability and models:
            # Reachability sweep disabled by default based on user request.
            # Returning raw models instantly to prevent 429 Quota errors and hanging.
            pass

        return DiscoveryResult(models=models, excluded=excluded)
    except DiscoveryError as e:
        logger.warning(f"Model discovery failed for {provider_name}: {e.message}")
        return DiscoveryResult(error=e)
    except Exception as e:
        logger.error(f"Error discovering models for {provider_name}: {e}")
        return DiscoveryResult(error=DiscoveryError(code=None, message=str(e) or type(e).__name__))


async def _discover_gemini_models(api_key: str) -> Tuple[List[str], List[Dict[str, str]]]:
    """Fetch available models from Google Gemini API and filter for content generation."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
    async with httpx.AsyncClient(timeout=DISCOVERY_TIMEOUT) as client:
        resp = await client.get(url)
        if resp.status_code != 200:
            raise DiscoveryError(
                code=resp.status_code,
                message=f"Gemini model list returned HTTP {resp.status_code}: {resp.text[:200]}",
            )
        data = resp.json()
        raw_models = data.get("models", [])

    discovered: List[str] = []
    excluded: List[Dict[str, str]] = []
    # Keywords to exclude (non-chat / image / embedding / audio / special purpose models)
    exclude_keywords = (
        "embedding", "imagen", "image", "aqa", "tts", "whisper", "audio",
        "gecko", "bison", "learnlm", "transcribe", "lyria", "diffusion",
        "2.5-computer", "preview"
    )

    for m in raw_models:
        name = m.get("name", "")  # format: "models/gemini-2.0-flash"
        model_id = name[7:] if name.startswith("models/") else name
        if not model_id:
            continue

        # Check supported methods
        methods = m.get("supportedGenerationMethods", [])
        if "generateContent" not in methods:
            excluded.append(_excluded(model_id))
            continue

        if any(kw in model_id.lower() for kw in exclude_keywords):
            excluded.append(_excluded(model_id))
            continue

        discovered.append(model_id)

    # Filter out alias/latest models that LiteLLM can't reliably resolve
    # (e.g. gemini-flash-latest, gemini-pro-latest are Vertex AI aliases, not AI Studio IDs)
    alias_blocklist = {"gemini-flash-latest", "gemini-pro-latest", "gemini-flash-lite-latest"}
    kept: List[str] = []
    for m in discovered:
        if m in alias_blocklist:
            excluded.append({"id": m, "kind": "other"})
        else:
            kept.append(m)
    discovered = kept

    # Sort so newer and popular stable models appear near top
    def _gemini_sort_key(m_id: str) -> tuple:
        lower = m_id.lower()
        priority = 99
        if lower == "gemini-2.5-flash":
            priority = 1
        elif lower == "gemini-2.0-flash":
            priority = 2
        elif lower == "gemini-2.5-pro":
            priority = 3
        elif lower == "gemini-2.0-flash-lite":
            priority = 4
        elif "2.5" in lower:
            priority = 5
        elif "2.0" in lower:
            priority = 6
        elif "flash" in lower:
            priority = 7
        elif "pro" in lower:
            priority = 8
        elif "1.5" in lower:
            priority = 9
        return (priority, lower)

    discovered.sort(key=_gemini_sort_key)
    return discovered, excluded


async def _discover_openai_models(api_key: str) -> Tuple[List[str], List[Dict[str, str]]]:
    """Fetch available models from OpenAI /v1/models endpoint and filter for chat models."""
    url = "https://api.openai.com/v1/models"
    headers = {"Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(timeout=DISCOVERY_TIMEOUT) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            raise DiscoveryError(
                code=resp.status_code,
                message=f"OpenAI model list returned HTTP {resp.status_code}: {resp.text[:200]}",
            )
        data = resp.json()
        raw_models = data.get("data", [])

    discovered: List[str] = []
    excluded: List[Dict[str, str]] = []
    # Target chat / reasoning prefixes
    target_prefixes = ("gpt-4", "gpt-3.5", "o1", "o3", "chatgpt")
    exclude_keywords = ("audio", "realtime", "embed", "moderation", "tts", "dall-e", "instruct", "transcription")

    for m in raw_models:
        model_id = m.get("id", "")
        if not model_id:
            continue
        lower = model_id.lower()

        if any(lower.startswith(p) for p in target_prefixes):
            if not any(kw in lower for kw in exclude_keywords):
                discovered.append(model_id)
                continue
        excluded.append(_excluded(model_id))

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
    return discovered, excluded


async def _discover_anthropic_models(api_key: str) -> Tuple[List[str], List[Dict[str, str]]]:
    """Fetch available models from Anthropic /v1/models endpoint."""
    url = "https://api.anthropic.com/v1/models"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }
    async with httpx.AsyncClient(timeout=DISCOVERY_TIMEOUT) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            raise DiscoveryError(
                code=resp.status_code,
                message=f"Anthropic model list returned HTTP {resp.status_code}: {resp.text[:200]}",
            )
        data = resp.json()
        raw_models = data.get("data", [])

    discovered: List[str] = []
    excluded: List[Dict[str, str]] = []
    for m in raw_models:
        model_id = m.get("id", "")
        if not model_id:
            continue
        if model_id.startswith("claude-"):
            discovered.append(model_id)
        else:
            excluded.append(_excluded(model_id))
    return discovered, excluded


async def _discover_deepseek_models(api_key: str) -> Tuple[List[str], List[Dict[str, str]]]:
    """Fetch available models from DeepSeek /models endpoint."""
    url = "https://api.deepseek.com/models"
    headers = {"Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(timeout=DISCOVERY_TIMEOUT) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            raise DiscoveryError(
                code=resp.status_code,
                message=f"DeepSeek model list returned HTTP {resp.status_code}: {resp.text[:200]}",
            )
        data = resp.json()
        raw_models = data.get("data", [])

    discovered = [m.get("id", "") for m in raw_models if m.get("id")]
    return discovered, []


async def _discover_openrouter_models(api_key: str) -> Tuple[List[str], List[Dict[str, str]]]:
    """Fetch top text/chat models from OpenRouter /api/v1/models endpoint."""
    url = "https://openrouter.ai/api/v1/models"
    headers = {"Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(timeout=DISCOVERY_TIMEOUT) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            raise DiscoveryError(
                code=resp.status_code,
                message=f"OpenRouter model list returned HTTP {resp.status_code}: {resp.text[:200]}",
            )
        data = resp.json()
        raw_models = data.get("data", [])

    discovered: List[str] = []
    excluded: List[Dict[str, str]] = []
    exclude_openrouter = ("image", "embedding", "audio", "whisper", "tts", "moderation", "diffusion")
    for m in raw_models[:50]:  # Limit top 50
        model_id = m.get("id", "")
        if not model_id:
            continue
        # Skip tilde-prefixed aliases (temporary/unstable IDs that don't resolve)
        if model_id.startswith("~"):
            excluded.append({"id": model_id, "kind": "other"})
            continue
        # Skip batch-only variants (don't support streaming)
        if model_id.endswith(":batch"):
            excluded.append({"id": model_id, "kind": "other"})
            continue
        lower = model_id.lower()
        if any(kw in lower for kw in exclude_openrouter):
            excluded.append(_excluded(model_id))
            continue
        discovered.append(model_id)

    return discovered, excluded


async def _discover_openai_compatible_models(
    base_url: str, api_key: str
) -> Tuple[List[str], List[Dict[str, str]]]:
    """Fetch available models from an OpenAI-compatible /models endpoint."""
    if not base_url or not base_url.strip():
        return [], []

    base_url = base_url.strip().rstrip("/")
    headers: Dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    # Spoof User-Agent to bypass strict WAF filters (e.g. AgentRouter)
    headers["User-Agent"] = "Cline/1.0.0"
    candidates: List[str] = []
    if base_url.endswith("/models"):
        candidates.append(base_url)
    else:
        candidates.append(f"{base_url}/models")
        if not base_url.endswith("/v1"):
            candidates.append(f"{base_url}/v1/models")

    raw_models: List[Any] = []
    tried: List[str] = []
    last_error: Optional[DiscoveryError] = None
    async with httpx.AsyncClient(timeout=DISCOVERY_TIMEOUT) as client:
        for url in candidates:
            tried.append(url)
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
                else:
                    last_error = DiscoveryError(
                        code=resp.status_code,
                        message=f"Model list request to {url} returned HTTP {resp.status_code}: {resp.text[:200]}",
                    )
            except DiscoveryError:
                raise
            except Exception as e:
                logger.debug(f"Failed querying {url}: {e}")
                last_error = DiscoveryError(code=None, message=f"Request to {url} failed: {e}")
                continue

    if not raw_models:
        raise last_error or DiscoveryError(
            code=None,
            message=f"No model list returned by any candidate endpoint: {', '.join(tried)}",
        )

    discovered: List[str] = []
    excluded: List[Dict[str, str]] = []
    exclude_keywords = ("audio", "realtime", "embed", "moderation", "tts", "dall-e", "image", "transcription", "rerank", "diffusion")

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
        else:
            excluded.append(_excluded(model_id))

    return discovered, excluded
