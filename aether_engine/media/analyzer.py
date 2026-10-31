"""Background media analysis pipeline (Epic 8, E8-4.3).

Every media item registered in the MediaStore (E8-4.2) is analysed
on arrival — fully async, never blocking chat:

- photos get a vision description + tags from the configured
  auxiliary vision model;
- videos get technical probe metadata (ffprobe) plus sampled
  keyframe summaries (ffmpeg, adaptive sampling under a hard
  24-frame cap);
- the resulting metadata is embedded through the unified embedder
  (E8-3.2: ``episodic.embed_text`` — one model, one circuit
  breaker) so the library and the agent ``media_recall`` tool can
  find media by what it shows, not what it is named.

Failure is honest and non-destructive: an item whose analysis
fails is marked ``failed`` with the error recorded in its probe
metadata, and stays fully usable in the library. When the
embedding endpoint is down the item still reaches ``analyzed`` —
only its semantic recall degrades until a later re-embed; text
recall (and the store's lexical leg) keeps working.

Transcripts for video are E8-4.4, deliberately not here.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from aether_common.contracts import Event, EventType
from aether_engine.config import UserConfig, load_config
from aether_engine.media.store import MediaStore, get_media_store
from aether_engine.memory import episodic

logger = logging.getLogger("aether_engine.media.analyzer")

KEYFRAME_CAP = 24
_VISION_TIMEOUT_S = 120.0


# ----------------------------------------------------------------------
# Video probe + adaptive keyframe sampling
# ----------------------------------------------------------------------

def _parse_fps(rate: str | None) -> float | None:
    if not rate:
        return None
    try:
        if "/" in rate:
            num, _, den = rate.partition("/")
            d = float(den)
            return float(num) / d if d else None
        return float(rate)
    except (TypeError, ValueError):
        return None


def probe_video(file_path: str | Path) -> dict[str, Any]:
    """Probe a video file with ffprobe.

    Returns duration/codec/resolution/fps/format on success. Any
    failure — missing file, missing ffprobe, unparseable output —
    returns an ``error`` entry with ``duration`` 0.0 and ``codec``
    "unknown" instead of raising: callers mark the item honestly
    rather than crashing the pipeline.
    """
    path = Path(file_path)
    error_probe: dict[str, Any] = {
        "duration": 0.0,
        "codec": "unknown",
        "width": None,
        "height": None,
        "fps": None,
        "format_name": "unknown",
    }
    if not path.exists() or not path.is_file():
        return {**error_probe, "error": f"file not found: {path}"}
    try:
        proc = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-print_format", "json",
                "-show_format", "-show_streams",
                str(path),
            ],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except FileNotFoundError:
        return {**error_probe, "error": "ffprobe is not installed"}
    except subprocess.TimeoutExpired:
        return {**error_probe, "error": "ffprobe timed out"}
    if proc.returncode != 0:
        return {**error_probe, "error": f"ffprobe failed: {(proc.stderr or '').strip()[:200]}"}
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {**error_probe, "error": "ffprobe output was not valid JSON"}

    fmt = data.get("format") or {}
    try:
        duration = float(fmt.get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    probe: dict[str, Any] = {
        **error_probe,
        "duration": duration,
        "format_name": fmt.get("format_name") or "unknown",
    }
    if fmt.get("size") is not None:
        try:
            probe["size"] = int(fmt["size"])
        except (TypeError, ValueError):
            pass
    if fmt.get("bit_rate") is not None:
        try:
            probe["bit_rate"] = int(fmt["bit_rate"])
        except (TypeError, ValueError):
            pass
    for stream in data.get("streams") or []:
        if stream.get("codec_type") == "video":
            probe["codec"] = stream.get("codec_name") or "unknown"
            probe["width"] = stream.get("width")
            probe["height"] = stream.get("height")
            probe["fps"] = _parse_fps(stream.get("avg_frame_rate") or stream.get("r_frame_rate"))
            break
    return probe


def adaptive_keyframe_timestamps(duration: float, cap: int = KEYFRAME_CAP) -> list[float]:
    """Sample timestamps for keyframe extraction, hard-capped.

    Density adapts to length — every 2s up to 30s, every 5s up to
    3 minutes, evenly spread beyond that — and the count NEVER
    exceeds ``cap`` (24 by design, grill Q24): past the 3-minute
    band the step grows with the duration so the cap holds for
    arbitrarily long videos.
    """
    if duration <= 0 or cap <= 0:
        return []
    if duration <= 30:
        step = 2.0
    elif duration <= 180:
        step = 5.0
    else:
        step = duration / cap
    step = max(step, duration / cap)
    timestamps: list[float] = []
    t = 0.0
    while t < duration and len(timestamps) < cap:
        timestamps.append(round(t, 3))
        t += step
    return timestamps


def extract_keyframes(
    file_path: str | Path,
    timestamps: list[float],
    out_dir: str | Path,
) -> list[dict[str, Any]]:
    """Extract one JPEG per timestamp with ffmpeg (best-effort).

    Returns ``[{"timestamp": float, "path": str | None}]`` — a frame
    whose extraction failed carries ``path=None`` rather than
    aborting the whole analysis.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    frames: list[dict[str, Any]] = []
    for i, ts in enumerate(timestamps):
        target = out / f"frame_{i:02d}_{ts:.3f}.jpg"
        extracted: str | None = None
        try:
            proc = subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-ss", str(ts),
                    "-i", str(file_path),
                    "-frames:v", "1",
                    "-q:v", "3",
                    str(target),
                ],
                capture_output=True, text=True, timeout=30, check=False,
            )
            if proc.returncode == 0 and target.exists() and target.stat().st_size > 0:
                extracted = str(target)
        except Exception:  # noqa: BLE001 — one bad frame never sinks the analysis
            extracted = None
        frames.append({"timestamp": ts, "path": extracted})
    return frames


# ----------------------------------------------------------------------
# Default vision provider (configured auxiliary vision model)
# ----------------------------------------------------------------------

def _resolve_vision_model(config: UserConfig) -> tuple[str, str]:
    """(provider_name, model) for media analysis.

    The auxiliary vision override (Settings, stored as
    "provider:::model") wins — the same precedence the routing
    policy gives it for image tasks (E8-4.1); otherwise the user's
    default provider/model, which is vision-capable by selection.
    """
    override = (getattr(config, "auxiliary_models", None) or {}).get("vision")
    if override and override != "auto":
        if ":::" in override:
            provider, _, model = override.partition(":::")
            return provider, model
        if "/" in override and not override.startswith("models/"):
            provider, _, model = override.partition("/")
            return provider, model
        return "", override
    return (
        getattr(config, "default_provider", "") or "",
        getattr(config, "default_model", "") or "",
    )


def _image_data_url(file_path: str | Path, mime_type: str | None) -> str:
    path = Path(file_path)
    mime = mime_type or "image/png"
    if not mime.startswith("image/"):
        mime = "image/jpeg" if path.suffix.lower() in (".jpg", ".jpeg") else "image/png"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _parse_vision_json(text: str) -> dict[str, Any]:
    """Extract the JSON object from a vision model's reply.

    Models wrap JSON in prose or code fences often enough that a
    strict parse would fail honest answers; take the outermost
    object if one exists, else an empty dict (the caller degrades
    to an empty summary rather than inventing one).
    """
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(text[start:end + 1])
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


class LiteLLMVisionProvider:
    """Vision analysis through the configured model via LiteLLM.

    Implements the two-method surface MediaAnalyzer consumes.
    LiteLLM is imported lazily (Epic 9: the import costs seconds
    and analysis is a background path).
    """

    def __init__(self, config: UserConfig | None = None):
        self._config = config

    def _litellm_call_kwargs(self) -> dict[str, Any]:
        config = self._config if self._config is not None else load_config()
        provider_name, model = _resolve_vision_model(config)
        from aether_engine.providers.litellm_provider import resolve_litellm_model

        kwargs: dict[str, Any] = {"model": resolve_litellm_model(model, provider_name)}
        try:
            from aether_engine.secrets.storage import SecretStore

            key = SecretStore().load_provider(provider_name) if provider_name else ""
            if key:
                kwargs["api_key"] = key
        except Exception:  # key lookup is best-effort; litellm may still find env credentials
            logger.debug("Vision provider key lookup failed", exc_info=True)
        base_url = (getattr(config, "custom_base_urls", None) or {}).get(provider_name)
        if base_url:
            kwargs["api_base"] = base_url
        return kwargs

    async def _ask(self, content: list[dict[str, Any]]) -> dict[str, Any]:
        import litellm

        kwargs = self._litellm_call_kwargs()
        response = await asyncio.wait_for(
            litellm.acompletion(
                messages=[{"role": "user", "content": content}],
                num_retries=0,
                **kwargs,
            ),
            timeout=_VISION_TIMEOUT_S,
        )
        text = ""
        try:
            text = response.choices[0].message.content or ""
        except (AttributeError, IndexError, TypeError):
            text = ""
        return _parse_vision_json(text)

    async def analyze_image(self, file_path: str, mime_type: str | None = None) -> dict[str, Any]:
        data_url = _image_data_url(file_path, mime_type)
        result = await self._ask([
            {
                "type": "text",
                "text": (
                    "Describe this image for a media library index. Reply with ONLY a JSON "
                    'object: {"summary": "one or two sentences describing what the image '
                    'shows", "tags": ["3-8 short topical tags"]}. Name the concrete '
                    "objects, colours, text, and setting you can see."
                ),
            },
            {"type": "image_url", "image_url": {"url": data_url}},
        ])
        return {
            "summary": str(result.get("summary") or ""),
            "tags": [str(t) for t in (result.get("tags") or []) if str(t).strip()],
        }

    async def analyze_video_frames(
        self,
        frames: list[dict[str, Any]],
        probe: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        content: list[dict[str, Any]] = [{
            "type": "text",
            "text": (
                "These are sampled keyframes from one video"
                + (f" (duration {probe.get('duration')}s, codec {probe.get('codec')})" if probe else "")
                + ". Reply with ONLY a JSON object: {\"summary\": \"one or two sentences "
                "summarising the whole video\", \"keyframe_summaries\": [{\"timestamp\": "
                "<seconds>, \"description\": \"what this frame shows\"}] with one entry per "
                "image in order, \"tags\": [\"3-8 short topical tags\"]}."
            ),
        }]
        for frame in frames:
            if frame.get("path"):
                content.append({
                    "type": "image_url",
                    "image_url": {"url": _image_data_url(frame["path"], "image/jpeg")},
                })
        result = await self._ask(content)
        keyframes: list[dict[str, Any]] = []
        for entry in result.get("keyframe_summaries") or []:
            if isinstance(entry, dict):
                keyframes.append({
                    "timestamp": entry.get("timestamp"),
                    "description": str(entry.get("description") or ""),
                })
        return {
            "summary": str(result.get("summary") or ""),
            "keyframe_summaries": keyframes,
            "tags": [str(t) for t in (result.get("tags") or []) if str(t).strip()],
        }


# ----------------------------------------------------------------------
# The analyzer worker
# ----------------------------------------------------------------------

def _embed_text_for(item_summary: str, tags: list[str], extra: str = "") -> str:
    """The text whose embedding represents this media item.

    Summary first (it carries the semantics), tags appended in the
    same "Tags:" shape the library's own fixtures use, plus any
    extra descriptive text (keyframe descriptions for video).
    """
    parts = [item_summary.strip()]
    if extra.strip():
        parts.append(extra.strip())
    if tags:
        parts.append("Tags: " + ", ".join(tags))
    return " ".join(p for p in parts if p)


class MediaAnalyzer:
    """Analyses MediaStore items in the background.

    ``vision_provider`` is any object with async ``analyze_image``
    and ``analyze_video_frames`` methods (tests inject stubs; the
    default is LiteLLMVisionProvider over the configured auxiliary
    vision model). ``on_event`` receives contracts.Event objects
    (MEDIA_ANALYSIS_PROGRESS / MEDIA_ANALYZED); sync and async
    callbacks are both supported.
    """

    def __init__(
        self,
        store: MediaStore,
        vision_provider: Any | None = None,
        on_event: Callable[[Event], Any] | None = None,
        config: UserConfig | None = None,
    ):
        self.store = store
        self._vision_provider = vision_provider
        self._on_event = on_event
        self._config = config

    @property
    def vision_provider(self) -> Any:
        if self._vision_provider is None:
            self._vision_provider = LiteLLMVisionProvider(config=self._config)
        return self._vision_provider

    async def _emit(self, event_type: EventType, payload: dict[str, Any]) -> None:
        if self._on_event is None:
            return
        try:
            result = self._on_event(Event(type=event_type, payload=payload))
            if asyncio.iscoroutine(result):
                await result
        except Exception:  # event delivery never fails an analysis
            logger.debug("Media analysis event callback failed", exc_info=True)

    async def analyze_item(self, media_id: str) -> dict[str, Any] | None:
        """Analyse one item end-to-end. Returns the updated item.

        Never raises for analysis failures: the item is marked
        ``failed`` with the error in its probe metadata and the
        (still usable) item is returned.
        """
        item = self.store.get_media(media_id)
        if item is None:
            return None
        self.store.update_analysis(media_id, status="analyzing")
        await self._emit(
            EventType.MEDIA_ANALYSIS_PROGRESS,
            {"media_id": media_id, "status": "analyzing"},
        )
        try:
            if item.get("media_type") == "video":
                summary, tags, probe, embed_extra = await self._analyze_video(item)
            else:
                summary, tags, probe, embed_extra = await self._analyze_image(item)

            vec = await episodic.embed_text(
                _embed_text_for(summary, tags, embed_extra),
                config=self._config,
                context="media_analysis",
            )
            if vec is not None:
                self.store.store_embedding(media_id, vec)

            updated = self.store.update_analysis(
                media_id,
                status="analyzed",
                summary_text=summary,
                tags=tags,
                probe_metadata=probe,
                analyzed_at=int(time.time() * 1000),
            )
            await self._emit(EventType.MEDIA_ANALYZED, dict(updated or {}))
            return updated
        except Exception as e:  # noqa: BLE001 — failure is a state, not a crash
            logger.warning(f"Media analysis failed for {media_id}: {e}")
            failed_probe = dict(item.get("probe_metadata") or {})
            failed_probe["error"] = str(e) or repr(e)
            updated = self.store.update_analysis(
                media_id,
                status="failed",
                probe_metadata=failed_probe,
            )
            await self._emit(
                EventType.MEDIA_ANALYSIS_PROGRESS,
                {"media_id": media_id, "status": "failed", "error": failed_probe["error"]},
            )
            return updated

    async def _analyze_image(
        self, item: dict[str, Any],
    ) -> tuple[str, list[str], dict[str, Any] | None, str]:
        result = await self.vision_provider.analyze_image(
            item["file_path"], mime_type=item.get("mime_type"))
        summary = str(result.get("summary") or "")
        tags = [str(t) for t in (result.get("tags") or [])]
        # Images keep their registration probe metadata untouched.
        return summary, tags, None, ""

    async def _analyze_video(
        self, item: dict[str, Any],
    ) -> tuple[str, list[str], dict[str, Any], str]:
        probe = await asyncio.to_thread(probe_video, item["file_path"])
        if probe.get("error"):
            raise RuntimeError(str(probe["error"]))
        timestamps = adaptive_keyframe_timestamps(float(probe.get("duration") or 0.0))
        with tempfile.TemporaryDirectory(prefix="aether-keyframes-") as tmp:
            frames = await asyncio.to_thread(
                extract_keyframes, item["file_path"], timestamps, tmp)
            result = await self.vision_provider.analyze_video_frames(frames, probe=probe)
        summary = str(result.get("summary") or "")
        tags = [str(t) for t in (result.get("tags") or [])]
        keyframes = result.get("keyframe_summaries") or []
        merged_probe = dict(item.get("probe_metadata") or {})
        merged_probe.update(probe)
        merged_probe["keyframe_summaries"] = keyframes
        # Keyframe descriptions join the embedded text so a moment
        # inside the video is findable by description.
        embed_extra = " ".join(
            str(k.get("description") or "") for k in keyframes if isinstance(k, dict))
        return summary, tags, merged_probe, embed_extra

    async def backfill(self, limit: int | None = None) -> int:
        """Analyse every item still awaiting analysis.

        Covers media registered before this pipeline shipped (and
        any item left in a stale ``analyzing`` state by a crash).
        Returns how many items reached ``analyzed``.
        """
        pending = self.store.list_by_status(("pending", "analyzing"), limit=limit)
        analyzed = 0
        for item in pending:
            result = await self.analyze_item(item["id"])
            if result is not None and result.get("status") == "analyzed":
                analyzed += 1
        return analyzed


# ----------------------------------------------------------------------
# Process-wide scheduling (arrival trigger + startup backfill)
# ----------------------------------------------------------------------

_default_analyzer: MediaAnalyzer | None = None
_inflight_tasks: set[asyncio.Task] = set()
_inflight_ids: set[str] = set()


def get_media_analyzer() -> MediaAnalyzer:
    """The shared analyzer over the global MediaStore."""
    global _default_analyzer
    if _default_analyzer is None:
        _default_analyzer = MediaAnalyzer(store=get_media_store())
    return _default_analyzer


def schedule_media_analysis(media_id: str) -> None:
    """Fire-and-forget analysis for a freshly registered item.

    No-ops outside a running event loop (sync registration paths)
    and for ids already in flight. Analysis failures land on the
    item itself (status ``failed``), never on the caller.
    """
    if media_id in _inflight_ids:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _inflight_ids.add(media_id)

    async def _run() -> None:
        try:
            await get_media_analyzer().analyze_item(media_id)
        except Exception:  # the item carries the failure
            logger.debug(f"Scheduled media analysis failed for {media_id}", exc_info=True)
        finally:
            _inflight_ids.discard(media_id)

    task = loop.create_task(_run())
    _inflight_tasks.add(task)
    task.add_done_callback(_inflight_tasks.discard)


def schedule_media_backfill() -> None:
    """Kick off a detached backfill pass (engine startup)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def _run() -> None:
        try:
            await get_media_analyzer().backfill()
        except Exception:  # startup backfill is best-effort
            logger.debug("Media analysis backfill failed", exc_info=True)

    task = loop.create_task(_run())
    _inflight_tasks.add(task)
    task.add_done_callback(_inflight_tasks.discard)
