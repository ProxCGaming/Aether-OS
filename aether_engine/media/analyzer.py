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
metadata, and stays fully usable in the library. Failed items are
retried by ``backfill()`` up to ``MAX_ANALYSIS_ATTEMPTS`` total
attempts (S4-M11), counted in probe metadata as
``analysis_attempts``. When the
embedding endpoint is down the item still reaches ``analyzed`` —
only its semantic recall degrades until a later re-embed; text
recall (and the store's lexical leg) keeps working.

E8-4.4 adds the video transcript stage on top: a video's audio
track is extracted with ffmpeg and transcribed through the local
STT runner from E8-5.1a (``aether_engine.voice.stt`` — the same
``SttEngine`` protocol the voice cascade uses, no second STT
stack). The transcript is stored on the entry and folded into
the embedded text, so a phrase that is only *spoken* in a video
— never visible in any frame or filename — is findable through
library search and ``media_recall``. The stage is fail-soft and
honest in the same way as the rest of the pipeline: missing STT
runtime or ffmpeg marks the transcript ``unavailable``, an STT
error marks it ``failed``, a video with no audio marks
``no_audio`` — the analysis itself still completes, and no
transcript text is ever fabricated. Videos analysed before this
slice gain transcripts through the same backfill (see
``backfill_transcripts``), resumably and idempotently.
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
from aether_engine.voice import VoiceUnavailableError

logger = logging.getLogger("aether_engine.media.analyzer")

KEYFRAME_CAP = 24
_VISION_TIMEOUT_S = 120.0

# S4-M11: total analysis attempts for one item, including the first
# failure. Failed items below this cap are retried by ``backfill()``;
# at/over it they stay ``failed`` until a human intervenes, so a
# permanently broken provider/file never requeues forever. The
# per-item count lives in probe_metadata["analysis_attempts"].
MAX_ANALYSIS_ATTEMPTS = 3

# E8-4.4: STT consumes raw PCM16 mono (SttEngine.transcribe); the
# extraction below produces exactly that at the rate whisper
# models expect.
AUDIO_SAMPLE_RATE = 16000
_EXTRACT_TIMEOUT_S = 600.0


class AudioExtractionError(RuntimeError):
    """ffmpeg could not extract a video's audio track."""


class NoAudioStreamError(AudioExtractionError):
    """The video has no audio stream to transcribe."""


def extract_audio_pcm(
    file_path: str | Path,
    sample_rate: int = AUDIO_SAMPLE_RATE,
) -> bytes:
    """Extract a video's audio track as raw PCM16 mono samples.

    Raises FileNotFoundError when ffmpeg itself is missing (the
    caller maps that to an honest ``unavailable``), and
    AudioExtractionError / NoAudioStreamError for a file ffmpeg
    cannot read or that carries no audio.
    """
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error",
                "-i", str(file_path),
                "-vn", "-ac", "1", "-ar", str(sample_rate),
                "-f", "s16le", "pipe:1",
            ],
            capture_output=True, timeout=_EXTRACT_TIMEOUT_S, check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise AudioExtractionError("ffmpeg audio extraction timed out") from e
    if proc.returncode != 0:
        stderr = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
        raise AudioExtractionError(
            f"ffmpeg audio extraction failed: {stderr[:200]}")
    if not proc.stdout:
        raise NoAudioStreamError("video has no extractable audio stream")
    return proc.stdout


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

    def _litellm_call_kwargs(self) -> tuple[dict[str, Any], str, str]:
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
        return kwargs, provider_name, model

    async def _ask(self, content: list[dict[str, Any]]) -> dict[str, Any]:
        import litellm

        kwargs, provider_name, model = self._litellm_call_kwargs()
        messages = [{"role": "user", "content": content}]
        response = await asyncio.wait_for(
            litellm.acompletion(
                messages=messages,
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
        # Finding M19: this call bypasses LiteLLMProvider (the
        # recording seam), so record it here — analysis is real model
        # work and PRD A8 counts every call. Observational only: a
        # recording failure must never break the analysis.
        try:
            from aether_engine.cost.ledger import record_provider_call

            usage = getattr(response, "usage", None)
            record_provider_call(
                model_name=model,
                pricing_model=kwargs["model"],
                provider_name=provider_name,
                input_tokens=getattr(usage, "prompt_tokens", None),
                output_tokens=getattr(usage, "completion_tokens", None),
                agent_name="media_analyzer",
                # No messages for the estimation fallback: they carry
                # a base64 image data URL, which a text token counter
                # would price as an enormous prompt. Real responses
                # report usage; a missing one estimates from the
                # reply text alone and is labelled estimated.
                messages=None,
                output_text=text,
            )
        except Exception:  # usage recording is observational only.
            logger.debug("Vision usage recording failed", exc_info=True)
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


def _effective_analysis_attempts(item: dict[str, Any]) -> int:
    """Attempts already spent on a (failed) item, for the retry cap.

    Items that failed before the counter existed carry no
    ``analysis_attempts`` in their probe metadata; they demonstrably
    failed once, so they count as 1. A corrupt counter also reads
    as 1 rather than 0, so it can never buy unlimited retries.
    """
    probe = item.get("probe_metadata") or {}
    if "analysis_attempts" not in probe:
        return 1
    try:
        return int(probe.get("analysis_attempts") or 0)
    except (TypeError, ValueError):
        return 1


def _embed_text_for_item(item: dict[str, Any]) -> str:
    """Rebuild the embedding text for an already-analysed item.

    Used when the transcript stage lands after the main analysis
    (backfill): the same composition ``analyze_item`` embeds —
    summary, keyframe descriptions, transcript, tags.
    """
    probe = item.get("probe_metadata") or {}
    extra = " ".join(
        str(k.get("description") or "")
        for k in probe.get("keyframe_summaries") or []
        if isinstance(k, dict))
    transcript = (item.get("transcript_text") or "").strip()
    if transcript:
        extra = f"{extra} {transcript}".strip()
    return _embed_text_for(
        item.get("summary_text") or "", list(item.get("tags") or []), extra)


class MediaAnalyzer:
    """Analyses MediaStore items in the background.

    ``vision_provider`` is any object with async ``analyze_image``
    and ``analyze_video_frames`` methods (tests inject stubs; the
    default is LiteLLMVisionProvider over the configured auxiliary
    vision model). ``on_event`` receives contracts.Event objects
    (MEDIA_ANALYSIS_PROGRESS / MEDIA_ANALYZED); sync and async
    callbacks are both supported.

    ``stt_engine`` is any ``SttEngine`` (E8-5.1a protocol) for the
    E8-4.4 transcript stage; the default is FasterWhisperStt,
    created lazily so importing or analysing photos never touches
    the voice stack. The engine is opened once and kept loaded —
    this is a background worker, and model load dominates a single
    transcription.
    """

    def __init__(
        self,
        store: MediaStore,
        vision_provider: Any | None = None,
        on_event: Callable[[Event], Any] | None = None,
        config: UserConfig | None = None,
        stt_engine: Any | None = None,
    ):
        self.store = store
        self._vision_provider = vision_provider
        self._on_event = on_event
        self._config = config
        self._stt_engine = stt_engine
        self._stt_opened = False

    @property
    def vision_provider(self) -> Any:
        if self._vision_provider is None:
            self._vision_provider = LiteLLMVisionProvider(config=self._config)
        return self._vision_provider

    @property
    def stt_engine(self) -> Any:
        if self._stt_engine is None:
            from aether_engine.voice.stt import FasterWhisperStt

            self._stt_engine = FasterWhisperStt()
        return self._stt_engine

    async def close_stt(self) -> None:
        """Release the STT model if this analyzer opened one."""
        if self._stt_engine is not None and self._stt_opened:
            try:
                await self._stt_engine.close()
            except Exception:  # release is best-effort
                logger.debug("STT engine close failed", exc_info=True)
            self._stt_opened = False

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
            transcript_text: str | None = None
            transcript_status: str | None = None
            if item.get("media_type") == "video":
                summary, tags, probe, embed_extra = await self._analyze_video(item)
                # E8-4.4 transcript stage — fail-soft by contract:
                # whatever it returns, the analysis above stands.
                await self._emit(
                    EventType.MEDIA_ANALYSIS_PROGRESS,
                    {"media_id": media_id, "status": "transcribing"},
                )
                transcript_text, transcript_status, transcript_error = (
                    await self._transcribe_video(item))
                if transcript_text:
                    embed_extra = f"{embed_extra} {transcript_text}".strip()
                if transcript_error:
                    probe = {**(probe or {}), "transcript_error": transcript_error}
            else:
                summary, tags, probe, embed_extra = await self._analyze_image(item)

            vec = await episodic.embed_text(
                _embed_text_for(summary, tags, embed_extra),
                config=self._config,
                context="media_analysis",
            )
            if vec is not None:
                self.store.store_embedding(media_id, vec)

            # S4-M11: success clears the retry slate. The video path
            # merges the item's previous probe metadata (so stale
            # ``analysis_attempts`` / ``error`` from an earlier failure
            # would survive), and the image path returns probe=None
            # (so the stored probe would be left untouched) — strip
            # both explicitly in either case.
            if probe is not None:
                probe = dict(probe)
                probe.pop("analysis_attempts", None)
                probe.pop("error", None)
            else:
                existing_probe = dict(item.get("probe_metadata") or {})
                if "analysis_attempts" in existing_probe or "error" in existing_probe:
                    existing_probe.pop("analysis_attempts", None)
                    existing_probe.pop("error", None)
                    probe = existing_probe

            updated = self.store.update_analysis(
                media_id,
                status="analyzed",
                summary_text=summary,
                tags=tags,
                probe_metadata=probe,
                analyzed_at=int(time.time() * 1000),
                transcript_text=transcript_text,
                transcript_status=transcript_status,
            )
            await self._emit(EventType.MEDIA_ANALYZED, dict(updated or {}))
            return updated
        except Exception as e:  # noqa: BLE001 — failure is a state, not a crash
            logger.warning(f"Media analysis failed for {media_id}: {e}")
            failed_probe = dict(item.get("probe_metadata") or {})
            failed_probe["error"] = str(e) or repr(e)
            # S4-M11: count this attempt so backfill() can bound
            # retries at MAX_ANALYSIS_ATTEMPTS (see that method).
            try:
                failed_probe["analysis_attempts"] = (
                    int(failed_probe.get("analysis_attempts") or 0) + 1)
            except (TypeError, ValueError):
                failed_probe["analysis_attempts"] = 1
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

    # ------------------------------------------------------------------
    # E8-4.4: video transcript stage (rides the E8-5.1a STT runner)
    # ------------------------------------------------------------------

    async def _transcribe_video(
        self, item: dict[str, Any],
    ) -> tuple[str | None, str, str | None]:
        """Run the transcript stage for one video item.

        Returns ``(transcript_text, transcript_status, error)`` —
        text only on ``done`` (possibly "" for a silent video),
        error text for ``unavailable`` / ``failed``. Never raises:
        every failure mode is a recorded state, so the stage can
        never sink the analysis it rides along with.
        """
        engine = self.stt_engine
        try:
            reason = engine.unavailable_reason()
        except Exception as e:  # noqa: BLE001 — a broken probe is a failure state
            return None, "failed", str(e) or repr(e)
        if reason:
            return None, "unavailable", str(reason)
        try:
            pcm = await asyncio.to_thread(extract_audio_pcm, item["file_path"])
        except FileNotFoundError:
            return None, "unavailable", "ffmpeg is not installed"
        except NoAudioStreamError:
            return None, "no_audio", None
        except AudioExtractionError as e:
            return None, "failed", str(e)
        except Exception as e:  # noqa: BLE001 — extraction must not sink analysis
            return None, "failed", str(e) or repr(e)
        try:
            if not self._stt_opened:
                await engine.open()
                self._stt_opened = True
            text = await engine.transcribe(pcm, sample_rate=AUDIO_SAMPLE_RATE)
        except VoiceUnavailableError as e:
            return None, "unavailable", str(e)
        except Exception as e:  # noqa: BLE001 — STT failure is a state, not a crash
            return None, "failed", str(e) or repr(e)
        return str(text or ""), "done", None

    async def transcribe_item(self, media_id: str) -> dict[str, Any] | None:
        """Transcribe one already-analysed video (the backfill unit).

        Skips non-videos and videos whose transcript is ``done``.
        On success the item's embedding is rebuilt with the
        transcript folded in, so spoken-only phrases become
        semantically recallable, not just lexically. Returns the
        updated item.
        """
        item = self.store.get_media(media_id)
        if item is None or item.get("media_type") != "video":
            return item
        if item.get("transcript_status") == "done":
            return item
        await self._emit(
            EventType.MEDIA_ANALYSIS_PROGRESS,
            {"media_id": media_id, "status": "transcribing"},
        )
        transcript_text, transcript_status, transcript_error = (
            await self._transcribe_video(item))
        probe = dict(item.get("probe_metadata") or {})
        if transcript_error:
            probe["transcript_error"] = transcript_error
        else:
            probe.pop("transcript_error", None)
        updated = self.store.update_analysis(
            media_id,
            transcript_text=transcript_text,
            transcript_status=transcript_status,
            probe_metadata=probe,
        )
        if transcript_status == "done" and transcript_text and updated is not None:
            vec = await episodic.embed_text(
                _embed_text_for_item(updated),
                config=self._config,
                context="media_transcript",
            )
            if vec is not None:
                self.store.store_embedding(media_id, vec)
        await self._emit(EventType.MEDIA_ANALYZED, dict(updated or {}))
        return updated

    async def backfill_transcripts(self, limit: int | None = None) -> int:
        """Transcribe every analysed video still owed a transcript.

        The resumable half of the E8-4.4 backfill: each pass works
        through ``MediaStore.list_videos_missing_transcripts``
        (never-attempted, interrupted, and retryable items), so a
        crash or a missing-deps pass leaves the rest for the next
        one; ``done`` is terminal, so a completed pass followed by
        another is a no-op. Returns how many transcripts reached
        ``done`` in this pass.
        """
        done = 0
        for item in self.store.list_videos_missing_transcripts(limit=limit):
            result = await self.transcribe_item(item["id"])
            if result is not None and result.get("transcript_status") == "done":
                done += 1
        return done

    async def backfill(self, limit: int | None = None) -> int:
        """Analyse every item still awaiting analysis.

        Covers media registered before this pipeline shipped (and
        any item left in a stale ``analyzing`` state by a crash).
        S4-M11: ``failed`` items are retried too, bounded by
        ``MAX_ANALYSIS_ATTEMPTS`` — a transient provider failure no
        longer strands an item forever, and a permanently failing
        one is not requeued forever either. ``limit`` is respected
        across the combined work: pending/analyzing items go first
        and failed retries fill the remainder. Returns how many
        items reached ``analyzed`` (successful retries included).
        The E8-4.4 transcript backfill rides along in the same
        pass, so the one startup call covers pre-slice videos too.
        """
        pending = self.store.list_by_status(("pending", "analyzing"), limit=limit)
        analyzed = 0
        for item in pending:
            result = await self.analyze_item(item["id"])
            if result is not None and result.get("status") == "analyzed":
                analyzed += 1
        remaining = None if limit is None else max(limit - len(pending), 0)
        if remaining is None or remaining > 0:
            # Fetch all failed items rather than capping the query:
            # capped-out items must not consume the retry budget
            # and hide eligible ones behind them.
            retries = [
                item for item in self.store.list_by_status(("failed",))
                if _effective_analysis_attempts(item) < MAX_ANALYSIS_ATTEMPTS
            ]
            if remaining is not None:
                retries = retries[:remaining]
            for item in retries:
                result = await self.analyze_item(item["id"])
                if result is not None and result.get("status") == "analyzed":
                    analyzed += 1
        await self.backfill_transcripts()
        return analyzed


# ----------------------------------------------------------------------
# Process-wide scheduling (arrival trigger + startup backfill)
# ----------------------------------------------------------------------

_default_analyzer: MediaAnalyzer | None = None
_inflight_tasks: set[asyncio.Task] = set()
_inflight_ids: set[str] = set()

# Analysis-event subscribers (N07): analysis is triggered from many
# places (attachment registration, tool execution, startup backfill),
# so progress/results cannot ride the per-task EventBus. Interested
# parties — the websocket handler, one subscription per connection —
# register here and the production analyzer fans every event out to
# them. Sync and async callbacks are both supported; a failing
# subscriber never fails an analysis or starves the others.
_event_subscribers: set[Callable[[Event], Any]] = set()


def subscribe_media_events(callback: Callable[[Event], Any]) -> Callable[[], None]:
    """Register for production-analyzer events; returns an
    unsubscribe callable (idempotent)."""
    _event_subscribers.add(callback)

    def _unsubscribe() -> None:
        _event_subscribers.discard(callback)

    return _unsubscribe


async def _broadcast_media_event(event: Event) -> None:
    """Fan one analysis event out to every subscriber."""
    for callback in list(_event_subscribers):
        try:
            result = callback(event)
            if asyncio.iscoroutine(result):
                await result
        except Exception:  # one bad subscriber must not starve the rest
            logger.debug("Media event subscriber failed", exc_info=True)


def get_media_analyzer() -> MediaAnalyzer:
    """The shared analyzer over the global MediaStore.

    Wired with the subscriber fan-out as its event callback (N07):
    before this, the production analyzer was built with
    ``on_event=None`` and analysis state never reached any client.
    """
    global _default_analyzer
    if _default_analyzer is None:
        _default_analyzer = MediaAnalyzer(
            store=get_media_store(), on_event=_broadcast_media_event
        )
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


def schedule_transcript_backfill() -> None:
    """Kick off a detached transcript-only backfill pass (E8-4.4).

    For when the STT runtime lands after startup (deps installed,
    settings changed): pre-slice videos gain transcripts without
    re-running the full analysis backfill.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def _run() -> None:
        try:
            await get_media_analyzer().backfill_transcripts()
        except Exception:  # detached backfill is best-effort
            logger.debug("Media transcript backfill failed", exc_info=True)

    task = loop.create_task(_run())
    _inflight_tasks.add(task)
    task.add_done_callback(_inflight_tasks.discard)
