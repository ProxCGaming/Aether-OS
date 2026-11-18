"""Model hub catalogue layer (H1): live HF catalogue + shipped Ollama catalogue.

Read-only. This module is the engine side of the Local Models tab's
turn into a model hub (owner design, speech-model-picker grill —
hub scope decided 2 Nov 2026): sources are Hugging Face (the live
public API) and the Ollama library ONLY, and the local runtime is
Ollama only. Every model row carries a RUNNABILITY label — how the
model runs, stated honestly:

- ``"in-app"``       — speech models Aether runs in-process
                       (faster-whisper CTranslate2 builds, Kokoro).
- ``"ollama"``       — a GGUF build exists; Ollama runs it.
- ``"not-runnable"`` — weights that need a serving stack Aether
                       doesn't carry (safetensors-only brains).
- ``"unknown"``      — no recognised local run path. Never guess
                       "runnable".

Downloads/pulls are a later slice; nothing here writes to disk.
HF failures (offline, timeout, HTTP error) surface as an honest
error payload — ``{"models": [], "error": ...}`` — never as an
exception and never as fabricated rows.
"""
import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from aether_engine.machine_capability import (
    local_model_fit,
    machine_capability_report,
)

logger = logging.getLogger("aether_engine.models.model_hub")

HF_API_BASE = "https://huggingface.co"

# A catalogue metadata call must never hang the settings surface:
# generous enough for a slow metadata response, short enough that
# an offline machine fails fast and honestly (cf. the local-models
# probes at 2-5s and the pull inactivity bound at 60s).
HF_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)

SEARCH_DEFAULT_LIMIT = 30
SEARCH_MAX_LIMIT = 100

# Detail payloads are the heavy ones (full file lists); cache them
# briefly in-memory so flipping between models in the UI doesn't
# re-fetch. Search results are NEVER cached — freshness is the
# whole point of binding a live API.
DETAIL_CACHE_TTL_SECONDS = 300.0

# E5-G: the detail cache is also size-bounded — a long session
# browsing the catalogue must not grow it monotonically. Expired
# entries are dropped on every insert; beyond that the oldest
# entries are evicted first.
DETAIL_CACHE_MAX_ENTRIES = 128

# ---------------------------------------------------------------------------
# Families
# ---------------------------------------------------------------------------

_PIPELINE_TAG_TO_FAMILY: dict[str, str] = {
    "text-generation": "brain",
    "automatic-speech-recognition": "speech-to-text",
    "text-to-speech": "text-to-speech",
    "feature-extraction": "embedding",
    "sentence-similarity": "embedding",
}

# Reverse map for the API's server-side pipeline_tag filter. The
# embedding family maps to feature-extraction (its primary tag);
# sentence-similarity models still normalise to "embedding" when
# they appear in unfiltered results.
_FAMILY_TO_PIPELINE_TAG: dict[str, str] = {
    "brain": "text-generation",
    "speech-to-text": "automatic-speech-recognition",
    "text-to-speech": "text-to-speech",
    "embedding": "feature-extraction",
}


def family_for_pipeline_tag(tag: str | None) -> str:
    """Map an HF ``pipeline_tag`` to a hub family (default "other")."""
    return _PIPELINE_TAG_TO_FAMILY.get(tag or "", "other")


# ---------------------------------------------------------------------------
# Licence + formats
# ---------------------------------------------------------------------------


def licence_from_hf_model(model: dict[str, Any]) -> str | None:
    """Extract the licence from cardData, falling back to license:* tags.

    Returns None when HF states no licence — never invented.
    """
    card = model.get("cardData") or {}
    licence = card.get("license")
    if isinstance(licence, list):
        licence = licence[0] if licence else None
    if isinstance(licence, str) and licence.strip():
        return licence.strip()
    for tag in model.get("tags") or []:
        if isinstance(tag, str) and tag.startswith("license:"):
            value = tag[len("license:"):].strip()
            if value:
                return value
    return None


def formats_from_tags(tags: Any) -> list[str]:
    """Coarse format detection for search rows, from HF's auto tags."""
    tag_set = set(tags or [])
    formats: list[str] = []
    if "gguf" in tag_set:
        formats.append("gguf")
    if "safetensors" in tag_set:
        formats.append("safetensors")
    return formats


def formats_from_files(filenames: list[str]) -> list[str]:
    """Exact format detection for detail rows, from the file list."""
    formats: list[str] = []
    lowered = [f.lower() for f in filenames]
    if any(f.endswith(".gguf") for f in lowered):
        formats.append("gguf")
    if any(f.endswith(".safetensors") for f in lowered):
        formats.append("safetensors")
    if any(f.rsplit("/", 1)[-1] == "model.bin" for f in lowered):
        formats.append("ctranslate2")
    if any(f.endswith(".onnx") for f in lowered):
        formats.append("onnx")
    return formats


# ---------------------------------------------------------------------------
# Runnability labeller (pure)
# ---------------------------------------------------------------------------

_KOKORO_REPO_ID = "hexgrad/kokoro-82m"


def _is_faster_whisper_repo(repo_id: str) -> bool:
    leaf = (repo_id or "").rsplit("/", 1)[-1].lower()
    return leaf.startswith("faster-whisper-")


def _is_kokoro_repo(repo_id: str) -> bool:
    return (repo_id or "").lower() == _KOKORO_REPO_ID


def label_runnability(
    family: str, formats: list[str], repo_id: str
) -> tuple[str, str]:
    """Label how a model runs locally, with an honest one-line reason.

    Rules, in order:
    1. Speech-to-text with a CTranslate2/faster-whisper build →
       in-app (faster-whisper runs it inside the engine).
    2. The Kokoro TTS repo → in-app.
    3. A GGUF build → via Ollama — EXCEPT for speech families:
       Ollama serves no speech models for Aether and there is no
       in-app GGUF speech runtime, so a GGUF-only speech repo is
       "unknown", not a false "ollama".
    4. A safetensors-only brain → not-runnable (needs a serving
       stack — vLLM/Transformers class — that Aether doesn't carry).
    5. Anything else → unknown; a run path is never guessed.
    """
    fmt = set(formats or [])
    if family == "speech-to-text" and (
        "ctranslate2" in fmt or _is_faster_whisper_repo(repo_id)
    ):
        return (
            "in-app",
            "Runs in-app via faster-whisper (CTranslate2) — no Ollama needed.",
        )
    if family == "text-to-speech" and _is_kokoro_repo(repo_id):
        return "in-app", "Runs in-app via Kokoro — no Ollama needed."
    if "gguf" in fmt:
        if family in ("speech-to-text", "text-to-speech"):
            return (
                "unknown",
                "GGUF speech build — Aether has no local runtime for speech GGUFs.",
            )
        return "ollama", "GGUF build — runs via Ollama."
    if family == "brain" and "safetensors" in fmt:
        return (
            "not-runnable",
            (
                "Safetensors weights need a serving stack (e.g. vLLM/Transformers) "
                "that Aether doesn't carry."
            ),
        )
    return (
        "unknown",
        "No recognised local run path — Aether can't confirm this runs locally.",
    )


# ---------------------------------------------------------------------------
# GGUF variants + detail grouping
# ---------------------------------------------------------------------------

_QUANT_RE = re.compile(
    r"(IQ\d+_[A-Z0-9]+|Q\d+_K_[SML]|Q\d+_K|Q\d+_\d+|BF16|F16|F32)",
    re.IGNORECASE,
)


def parse_gguf_quant(filename: str) -> str | None:
    """Parse the quant label (Q4_K_M, IQ2_XS, F16…) from a GGUF filename."""
    leaf = (filename or "").rsplit("/", 1)[-1]
    match = _QUANT_RE.search(leaf)
    return match.group(1).upper() if match else None


# Files that travel with a CTranslate2 model.bin as one loadable set.
_CT2_COMPANIONS = {
    "config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "vocabulary.txt",
    "vocabulary.json",
    "special_tokens_map.json",
    "generation_config.json",
    "normalizer.json",
    "preprocessor_config.json",
}


def group_variants(siblings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group a repo's files into downloadable variants.

    Each .gguf file is its own variant (one quant each); the
    safetensors shards form one variant; a CTranslate2 model.bin
    plus its tokenizer/config companions form one variant; anything
    left over is grouped as "other". A file appears in exactly one
    variant. ``size`` may be None when HF didn't report it; sizes
    are summed over the files that carry one.
    """
    files = [
        (s.get("rfilename") or "", s.get("size"))
        for s in siblings
        if s.get("rfilename")
    ]

    def _size(size: Any) -> int:
        return size if isinstance(size, int) else 0

    variants: list[dict[str, Any]] = []
    claimed: set[str] = set()

    gguf_files = sorted(f for f, _ in files if f.lower().endswith(".gguf"))
    size_by_name = dict(files)
    for name in gguf_files:
        claimed.add(name)
        variants.append({
            "kind": "gguf",
            "file": name,
            "size_bytes": _size(size_by_name.get(name)),
            "quant": parse_gguf_quant(name),
        })

    st_files = sorted(
        f for f, _ in files
        if f not in claimed and f.lower().endswith(".safetensors")
    )
    if st_files:
        claimed.update(st_files)
        variants.append({
            "kind": "safetensors",
            "files": st_files,
            "size_bytes": sum(_size(size_by_name.get(f)) for f in st_files),
        })

    leaves = {f.rsplit("/", 1)[-1]: f for f, _ in files if f not in claimed}
    if "model.bin" in leaves:
        ct2_files = [leaves["model.bin"]] + sorted(
            f for leaf, f in leaves.items() if leaf in _CT2_COMPANIONS
        )
        claimed.update(ct2_files)
        variants.append({
            "kind": "ctranslate2",
            "files": ct2_files,
            "size_bytes": sum(_size(size_by_name.get(f)) for f in ct2_files),
        })

    rest = sorted(f for f, _ in files if f not in claimed)
    if rest:
        variants.append({
            "kind": "other",
            "files": rest,
            "size_bytes": sum(_size(size_by_name.get(f)) for f in rest),
        })
    return variants


# ---------------------------------------------------------------------------
# HF normalisers
# ---------------------------------------------------------------------------


def _base_row(model: dict[str, Any], formats: list[str]) -> dict[str, Any]:
    repo_id = model.get("id") or model.get("modelId") or ""
    family = family_for_pipeline_tag(model.get("pipeline_tag"))
    label, reason = label_runnability(family, formats, repo_id)
    return {
        "source": "hf",
        "id": repo_id,
        "name": repo_id.rsplit("/", 1)[-1] if repo_id else "",
        "family": family,
        "downloads": model.get("downloads") or 0,
        "licence": licence_from_hf_model(model),
        "formats": formats,
        "runnability": label,
        "runnability_reason": reason,
        "size_bytes": None,
    }


def normalise_hf_search_row(model: dict[str, Any]) -> dict[str, Any]:
    """Normalise one /api/models list entry into a hub row.

    Formats come from HF's tags at this level (coarse); the detail
    call re-derives them exactly from the file list.
    """
    return _base_row(model, formats_from_tags(model.get("tags")))


def normalise_hf_detail(model: dict[str, Any]) -> dict[str, Any]:
    """Normalise one /api/models/{id}?blobs=true payload into a hub detail."""
    siblings = model.get("siblings") or []
    filenames = [s.get("rfilename") or "" for s in siblings if s.get("rfilename")]
    detail = _base_row(model, formats_from_files(filenames))
    detail["variants"] = group_variants(siblings)
    detail["size_bytes"] = sum(
        s["size"] for s in siblings if isinstance(s.get("size"), int)
    )
    detail["pipeline_tag"] = model.get("pipeline_tag")
    detail["library_name"] = model.get("library_name")
    return detail


# ---------------------------------------------------------------------------
# Ollama source — the shipped catalogue (there is NO official Ollama
# catalogue API and ollama.com is never scraped; the app ships this
# curated list as desktop_app/public/ollama-catalog.json).
# ---------------------------------------------------------------------------

# Mirror of the shipped JSON, used when the JSON file isn't
# reachable from the engine (packaged layout). A test pins this
# copy to the JSON so the two cannot silently drift.
_FALLBACK_OLLAMA_CATALOG: list[dict[str, Any]] = [
    {"name": "llama3:8b", "size": 4.7, "params": "8B", "quant": "Q4_K_M", "description": "Meta's powerful 8B model. Excellent for general chat and reasoning."},
    {"name": "mistral:7b", "size": 4.1, "params": "7B", "quant": "Q4_K_M", "description": "Fast, extremely capable open model by Mistral AI."},
    {"name": "qwen2.5:7b", "size": 4.2, "params": "7B", "quant": "Q4_K_M", "description": "Alibaba's latest high-performance reasoning model."},
    {"name": "phi3:mini", "size": 2.3, "params": "3.8B", "quant": "Q4_K_M", "description": "Microsoft's highly efficient small model. Perfect for low RAM."},
    {"name": "gemma2:2b", "size": 1.6, "params": "2B", "quant": "Q4_K_M", "description": "Google's lightweight model based on Gemini."},
    {"name": "deepseek-coder-v2:16b", "size": 8.9, "params": "16B", "quant": "Q4_K_M", "description": "Exceptional coding and logic capabilities."},
    {"name": "qwen3-embedding:0.6b", "size": 0.6, "params": "0.6B", "quant": "Q8_0", "family": "embedding", "description": "Qwen3 embedding model — the embedder Aether uses for memory and workspace search."},
    {"name": "nomic-embed-text", "size": 0.27, "params": "137M", "quant": "F16", "family": "embedding", "description": "Lightweight English embedding model by Nomic."},
]


def _load_shipped_ollama_catalog() -> list[dict[str, Any]]:
    candidate = (
        Path(__file__).resolve().parents[2]
        / "desktop_app"
        / "public"
        / "ollama-catalog.json"
    )
    try:
        data = json.loads(candidate.read_text(encoding="utf-8"))
        if isinstance(data, list) and data:
            return data
    except (OSError, ValueError):
        pass
    return [dict(e) for e in _FALLBACK_OLLAMA_CATALOG]


def ollama_catalogue_rows() -> list[dict[str, Any]]:
    """Hub rows for the shipped Ollama catalogue.

    ``size`` in the catalogue is GB (float). Licence is None: the
    catalogue carries none and one is never invented.
    """
    rows: list[dict[str, Any]] = []
    for entry in _load_shipped_ollama_catalog():
        name = entry.get("name") or ""
        size = entry.get("size")
        size_bytes = (
            round(size * 1024**3) if isinstance(size, (int, float)) else None
        )
        rows.append({
            "source": "ollama",
            "id": name,
            "name": name,
            # The catalogue's own family when it declares one (the
            # embedding models); chat entries predate the field and
            # are brains.
            "family": entry.get("family") or "brain",
            "downloads": None,
            "licence": entry.get("licence") or entry.get("license"),
            "formats": ["gguf"],
            "runnability": "ollama",
            "runnability_reason": "GGUF build — runs via Ollama.",
            "size_bytes": size_bytes,
            "params": entry.get("params"),
            "quant": entry.get("quant"),
            "description": entry.get("description"),
        })
    return rows


def _ollama_detail(row: dict[str, Any]) -> dict[str, Any]:
    detail = dict(row)
    detail["variants"] = [{
        "kind": "gguf",
        "file": row["id"],
        "size_bytes": row.get("size_bytes"),
        "quant": row.get("quant"),
    }]
    detail["pipeline_tag"] = "text-generation"
    detail["library_name"] = None
    return detail


# ---------------------------------------------------------------------------
# The hub
# ---------------------------------------------------------------------------


class ModelHub:
    """Read-only catalogue over the HF API and the shipped Ollama list."""

    def __init__(self, cache_ttl: float = DETAIL_CACHE_TTL_SECONDS):
        self._cache_ttl = cache_ttl
        self._detail_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
        self._ollama_rows: list[dict[str, Any]] | None = None

    # -- sources ------------------------------------------------------

    def _ollama(self) -> list[dict[str, Any]]:
        if self._ollama_rows is None:
            self._ollama_rows = ollama_catalogue_rows()
        return self._ollama_rows

    async def _hf_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        async with httpx.AsyncClient(timeout=HF_TIMEOUT) as client:
            resp = await client.get(f"{HF_API_BASE}{path}", params=params)
        if resp.status_code != 200:
            raise _HfHttpError(resp.status_code)
        return resp.json()

    # -- machine fit (Spec D) ------------------------------------------

    async def _fit_machine(self) -> dict[str, Any]:
        """The capability probe dict fit verdicts are judged
        against. The report is TTL-cached engine-side, so this
        costs a dict copy once warm. Fit is advisory: ANY failure
        here yields {} — every verdict then honestly comes back
        unjudged (None) instead of breaking a catalogue answer."""
        try:
            report = await asyncio.to_thread(machine_capability_report)
            return report.get("machine") or {}
        except Exception:  # noqa: BLE001 — advisory only, see docstring
            return {}

    async def _with_fit(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Attach each row's fit verdict as a NEW dict per row
        (the shipped Ollama catalogue rows are cached and shared
        — never mutated). Rows are returned untouched when no
        row reports a size: there is nothing to judge."""
        if not any(r.get("size_bytes") for r in rows):
            return rows
        machine = await self._fit_machine()
        return [
            {**r, "fit": local_model_fit(r.get("size_bytes"), machine)}
            for r in rows
        ]

    async def _detail_with_fit(self, detail: dict[str, Any]) -> dict[str, Any]:
        """Attach fit verdicts to a detail dict and its variants
        (same rule as search rows), as copies — the detail cache
        keeps the raw detail."""
        if not detail.get("size_bytes") and not any(
                isinstance(v, dict) and v.get("size_bytes")
                for v in detail.get("variants") or []):
            return detail
        machine = await self._fit_machine()
        out = {**detail,
               "fit": local_model_fit(detail.get("size_bytes"), machine)}
        variants = detail.get("variants")
        if isinstance(variants, list):
            out["variants"] = [
                {**v, "fit": local_model_fit(v.get("size_bytes"), machine)}
                if isinstance(v, dict) else v
                for v in variants
            ]
        return out

    # -- search ---------------------------------------------------------

    async def search(
        self,
        source: str,
        query: str = "",
        family: str | None = None,
        limit: int = SEARCH_DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        """Return ``{"models": [rows], "error": None | str}``. Never raises."""
        if source == "ollama":
            rows = self._ollama()
            if query:
                needle = query.lower()
                rows = [r for r in rows if needle in r["name"].lower()]
            if family:
                rows = [r for r in rows if r["family"] == family]
            return {"models": await self._with_fit(rows), "error": None}
        if source != "hf":
            return {
                "models": [],
                "error": f"Unknown model source '{source}'.",
            }
        # E5-Q: the "Never raises" contract starts here — a non-int
        # limit from a caller used to raise TypeError while building
        # params, before the guarded region below. Validate first
        # and answer with an error payload, like every other bad
        # input this function reports.
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            return {
                "models": [],
                "error": f"Invalid search limit: {limit!r}.",
            }
        params: dict[str, Any] = {
            "sort": "downloads",
            "direction": -1,
            "limit": max(1, min(limit, SEARCH_MAX_LIMIT)),
            "full": "true",
        }
        if query:
            params["search"] = query
        tag = _FAMILY_TO_PIPELINE_TAG.get(family) \
            if isinstance(family, str) else None
        if tag:
            params["pipeline_tag"] = tag
        try:
            data = await self._hf_get("/api/models", params=params)
        except _HfHttpError as e:
            return {
                "models": [],
                "error": f"Hugging Face catalogue returned HTTP {e.status_code}.",
            }
        except httpx.TimeoutException:
            return {
                "models": [],
                "error": "Hugging Face catalogue timed out — "
                         "check the connection and retry.",
            }
        except httpx.HTTPError:
            return {
                "models": [],
                "error": "Hugging Face catalogue is unreachable — "
                         "check the connection and retry.",
            }
        except ValueError:
            return {
                "models": [],
                "error": "Hugging Face catalogue returned an unreadable response.",
            }
        except Exception as e:  # noqa: BLE001 — incl. client-construction failures (httpx.InvalidURL from a malformed proxy env is NOT an HTTPError): a catalogue failure must surface as a payload, never raise across the socket
            logger.warning(f"Model hub HF search failed unexpectedly: {e}")
            return {
                "models": [],
                "error": "Hugging Face catalogue is unreachable — "
                         "check the connection and retry.",
            }
        if not isinstance(data, list):
            return {
                "models": [],
                "error": "Hugging Face catalogue returned an unreadable response.",
            }
        rows = [normalise_hf_search_row(m) for m in data if isinstance(m, dict)]
        if family:
            # The API filter is the primary cut; normalisation is the
            # authority (e.g. sentence-similarity → embedding).
            rows = [r for r in rows if r["family"] == family]
        return {"models": await self._with_fit(rows), "error": None}

    # -- detail ---------------------------------------------------------

    async def detail(self, source: str, repo_id: str) -> dict[str, Any]:
        """Return ``{"detail": {...} | None, "error": None | str}``. Never raises."""
        if source == "ollama":
            for row in self._ollama():
                if row["id"] == repo_id:
                    return {"detail": await self._detail_with_fit(
                        _ollama_detail(row)), "error": None}
            return {
                "detail": None,
                "error": f"'{repo_id}' is not in the shipped Ollama catalogue.",
            }
        if source != "hf":
            return {
                "detail": None,
                "error": f"Unknown model source '{source}'.",
            }
        if not repo_id:
            return {"detail": None, "error": "No model id was given."}
        key = (source, repo_id)
        cached = self._detail_cache.get(key)
        if cached and time.monotonic() - cached[0] < self._cache_ttl:
            return {"detail": await self._detail_with_fit(cached[1]),
                    "error": None}
        try:
            data = await self._hf_get(
                f"/api/models/{quote(repo_id, safe='/')}",
                params={"blobs": "true"},
            )
        except _HfHttpError as e:
            if e.status_code == 404:
                return {
                    "detail": None,
                    "error": f"Hugging Face has no model '{repo_id}'.",
                }
            return {
                "detail": None,
                "error": f"Hugging Face catalogue returned HTTP {e.status_code}.",
            }
        except httpx.TimeoutException:
            return {
                "detail": None,
                "error": "Hugging Face catalogue timed out — "
                         "check the connection and retry.",
            }
        except httpx.HTTPError:
            return {
                "detail": None,
                "error": "Hugging Face catalogue is unreachable — "
                         "check the connection and retry.",
            }
        except ValueError:
            return {
                "detail": None,
                "error": "Hugging Face catalogue returned an unreadable response.",
            }
        except Exception as e:  # noqa: BLE001 — see search(): never raise across the socket
            logger.warning(f"Model hub HF detail failed unexpectedly: {e}")
            return {
                "detail": None,
                "error": "Hugging Face catalogue is unreachable — "
                         "check the connection and retry.",
            }
        if not isinstance(data, dict):
            return {
                "detail": None,
                "error": "Hugging Face catalogue returned an unreadable response.",
            }
        detail = normalise_hf_detail(data)
        self._cache_detail(key, detail)
        return {"detail": await self._detail_with_fit(detail), "error": None}

    def _cache_detail(
        self, key: tuple[str, str], detail: dict[str, Any],
    ) -> None:
        """Insert into the detail cache, keeping it bounded (E5-G):
        expired entries are dropped first, then the oldest entries
        are evicted so the cache never exceeds
        ``DETAIL_CACHE_MAX_ENTRIES``."""
        now = time.monotonic()
        expired = [
            k for k, (cached_at, _) in self._detail_cache.items()
            if now - cached_at >= self._cache_ttl
        ]
        for k in expired:
            del self._detail_cache[k]
        self._detail_cache.pop(key, None)
        while len(self._detail_cache) >= DETAIL_CACHE_MAX_ENTRIES:
            oldest = next(iter(self._detail_cache))
            del self._detail_cache[oldest]
        self._detail_cache[key] = (now, detail)


class _HfHttpError(Exception):
    """Internal: HF answered with a non-200 status."""

    def __init__(self, status_code: int):
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code
