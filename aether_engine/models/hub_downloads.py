"""Model hub downloads (H2): download managers + downloaded inventory.

Builds on H1's catalogue layer (``model_hub.py``). A hub download
routes by the H1 RUNNABILITY label, and the runnability label is
the ONLY routing authority — the hub never downloads something it
labelled as not locally runnable:

- source ``"ollama"`` → the existing Ollama pull machinery
  (``OllamaManager.download_model_task``), by model name.
- source ``"hf"`` labelled ``"ollama"`` (a GGUF repo) → ALSO an
  Ollama pull, of the Hugging Face reference
  ``hf.co/<repo>:<quant>`` (Ollama's HF integration: the tag is
  the GGUF variant's quant label).
- source ``"hf"`` labelled ``"in-app"`` (faster-whisper
  CTranslate2 repos, hexgrad/Kokoro-82M) → the repo is downloaded
  into the standard Hugging Face cache via ``huggingface_hub``,
  so the existing voice code finds it on first load.
- ``"not-runnable"`` / ``"unknown"`` → refused, with the H1 reason.

``huggingface_hub`` ships with the voice extra (faster-whisper
depends on it); it is imported LAZILY and its absence is the
honest "voice components are not installed" refusal — the same
fail-closed shape the voice stages use (find_spec probes in
``voice/stt.py`` / ``voice/tts.py``).

Discipline: ONE download at a time PER STORE (``"ollama"`` /
``"hf"``). A request beyond that is an honest busy refusal —
never a silent queue, and never the pre-emption the legacy
LOCAL_MODEL_DOWNLOAD_START handler applies to ``task_runner``
(hub downloads deliberately do not touch the engine task runner:
starting a model download must not cancel a running agent task).

Progress: Ollama-routed downloads emit the existing pull stream
unchanged (TASK_CREATED / LOCAL_MODEL_DOWNLOAD_PROGRESS /
TASK_COMPLETED / TASK_FAILED from ``OllamaManager``). HF speech
downloads emit the same event types; the percent is computed
from the repo's real file sizes (file-count fraction when HF
reports no sizes) — never simulated.
"""
import asyncio
import logging
import os
import shutil
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from aether_common.contracts import Event, EventType, TaskState
from aether_engine.models.local_models import OllamaManager
from aether_engine.models.model_hub import ModelHub, label_runnability

logger = logging.getLogger("aether_engine.models.hub_downloads")

# Mirrors the voice stages' fail-closed wording: the speech
# download needs the voice components because huggingface_hub
# ships with the voice extra (via faster-whisper).
VOICE_COMPONENTS_MISSING_ERROR = (
    "Voice components are not installed — the speech download needs "
    "them (huggingface_hub ships with the voice extra)."
)

_KOKORO_REPO_ID = "hexgrad/kokoro-82m"


def _load_huggingface_hub() -> Any | None:
    """Import huggingface_hub lazily; None when it isn't installed."""
    try:
        import huggingface_hub
    except ImportError:
        return None
    return huggingface_hub


def _is_hub_speech_repo(repo_id: str) -> bool:
    """HF-cache repos the hub treats as speech inventory — the same
    recognition rule H1's labeller applies (faster-whisper-* repos,
    the Kokoro repo)."""
    leaf = (repo_id or "").rsplit("/", 1)[-1].lower()
    return leaf.startswith("faster-whisper-") or (
        (repo_id or "").lower() == _KOKORO_REPO_ID
    )


# ---------------------------------------------------------------------------
# The hf.co ref builder (Ollama's Hugging Face integration)
# ---------------------------------------------------------------------------


def hf_gguf_ollama_ref(
    repo_id: str,
    variants: list[dict[str, Any]],
    variant: str | None,
) -> tuple[str | None, str | None]:
    """Build the Ollama pull ref for one GGUF variant of an HF repo.

    Returns ``(ref, None)`` or ``(None, error)``. The tag is the
    variant's quant label (``hf.co/<repo>:Q4_K_M``). A repo whose
    single GGUF carries no parseable quant is pulled bare
    (``hf.co/<repo>`` — Ollama's default tag). Ambiguity is a
    refusal naming the available quants, never a guess.
    """
    gguf = [v for v in variants or [] if v.get("kind") == "gguf"]
    if not gguf:
        return None, f"'{repo_id}' has no GGUF variant to pull via Ollama."
    available = ", ".join(
        v.get("quant") or v.get("file") or "?" for v in gguf)
    chosen: dict[str, Any] | None = None
    if variant:
        needle = variant.lower()
        for v in gguf:
            quant = (v.get("quant") or "").lower()
            file_leaf = (v.get("file") or "").rsplit("/", 1)[-1].lower()
            if needle in (quant, file_leaf) or needle == (
                    v.get("file") or "").lower():
                chosen = v
                break
        if chosen is None:
            return None, (
                f"Variant '{variant}' is not available for {repo_id}. "
                f"Available GGUF variants: {available}."
            )
    elif len(gguf) == 1:
        chosen = gguf[0]
    else:
        return None, (
            f"'{repo_id}' has several GGUF variants — choose one: "
            f"{available}."
        )
    quant = chosen.get("quant")
    if quant:
        return f"hf.co/{repo_id}:{quant}", None
    if len(gguf) == 1:
        return f"hf.co/{repo_id}", None
    return None, (
        f"Could not determine the Ollama tag for "
        f"'{chosen.get('file')}' in {repo_id}."
    )


# ---------------------------------------------------------------------------
# The manager
# ---------------------------------------------------------------------------


class ModelHubDownloads:
    """Coordinates hub downloads across the Ollama and HF stores."""

    def __init__(
        self,
        hub: ModelHub,
        ollama: OllamaManager,
        hf_cache_dir: str | Path | None = None,
        hf_loader: Any = None,
    ):
        self._hub = hub
        self._ollama = ollama
        self._hf_cache_dir = Path(hf_cache_dir) if hf_cache_dir else None
        self._hf_loader = hf_loader or _load_huggingface_hub
        self._busy: set[str] = set()

    # -- lifecycle ------------------------------------------------------

    def release(self, plan: dict[str, Any]) -> None:
        """Free the plan's store slot. Idempotent."""
        store = (plan or {}).get("store")
        if store:
            self._busy.discard(store)

    async def begin(
        self,
        source: str,
        model_id: str,
        variant: str | None = None,
    ) -> dict[str, Any]:
        """Resolve a download request into a plan, or an honest refusal.

        On success the plan's store slot is CLAIMED; the caller
        must consume ``run(plan)`` (which releases on completion)
        or call ``release(plan)``. Refusals claim nothing.
        """
        if source not in ("ollama", "hf"):
            return {"success": False,
                    "error": f"Unknown model source '{source}'."}
        if not model_id:
            return {"success": False, "error": "No model id was given."}

        if source == "ollama":
            plan: dict[str, Any] = {
                "kind": "ollama-pull",
                "store": "ollama",
                "key": model_id,
                "model": model_id,
                "ref": model_id,
                "repo_id": None,
            }
        else:
            detail_result = await self._hub.detail(
                source="hf", repo_id=model_id)
            detail = detail_result.get("detail")
            if detail is None:
                return {"success": False,
                        "error": detail_result.get("error")
                        or f"Could not load '{model_id}'."}
            label = detail.get("runnability")
            if label == "ollama":
                ref, error = hf_gguf_ollama_ref(
                    model_id, detail.get("variants") or [], variant)
                if error is not None:
                    return {"success": False, "error": error}
                plan = {
                    "kind": "ollama-pull",
                    "store": "ollama",
                    "key": ref,
                    "model": ref,
                    "ref": ref,
                    "repo_id": model_id,
                }
            elif label == "in-app":
                if self._hf_loader() is None:
                    return {"success": False,
                            "error": VOICE_COMPONENTS_MISSING_ERROR}
                plan = {
                    "kind": "hf-speech",
                    "store": "hf",
                    "key": model_id,
                    "model": model_id,
                    "ref": None,
                    "repo_id": model_id,
                }
            else:
                reason = detail.get("runnability_reason") or (
                    "Aether can't confirm this runs locally.")
                return {"success": False,
                        "error": f"Cannot download '{model_id}': {reason}"}

        store = plan["store"]
        if store in self._busy:
            return {
                "success": False,
                "error": f"A download is already in progress for the "
                         f"{store} store — wait for it to finish or "
                         f"cancel it first.",
            }
        self._busy.add(store)
        plan["success"] = True
        plan["error"] = None
        plan["task_id"] = f"hub-dl-{uuid.uuid4().hex[:8]}"
        return plan

    async def run(
        self,
        plan: dict[str, Any],
        request_id: str | None = None,
    ) -> AsyncGenerator[Event, None]:
        """Execute a claimed plan, yielding the download event stream.

        Releases the store slot when the stream ends — completion,
        failure, cancellation, or the consumer walking away.
        """
        try:
            if plan.get("kind") == "ollama-pull":
                async for ev in self._ollama.download_model_task(
                    model_name=plan["ref"],
                    task_id=plan["task_id"],
                    request_id=request_id,
                ):
                    yield ev
            else:
                async for ev in self._run_hf_speech(plan, request_id):
                    yield ev
        finally:
            self.release(plan)

    # -- HF speech downloads ---------------------------------------------

    async def _run_hf_speech(
        self,
        plan: dict[str, Any],
        request_id: str | None,
    ) -> AsyncGenerator[Event, None]:
        repo_id = plan["repo_id"]
        t_id = plan["task_id"]

        yield Event(
            type=EventType.TASK_CREATED,
            request_id=request_id,
            payload={
                "task_id": t_id,
                "prompt": f"Download speech model {repo_id}",
                "state": TaskState.RUNNING.value,
                "model": repo_id,
            },
        )

        def _failed(error: str, retriable: bool = True) -> Event:
            return Event(
                type=EventType.TASK_FAILED,
                request_id=request_id,
                payload={
                    "task_id": t_id,
                    "model": repo_id,
                    "state": TaskState.FAILED.value,
                    "error": error,
                    "retriable": retriable,
                },
            )

        hf = self._hf_loader()
        if hf is None:
            yield _failed(VOICE_COMPONENTS_MISSING_ERROR, retriable=False)
            return

        cache_dir = str(self._hf_cache_root())
        try:
            info = await asyncio.to_thread(
                hf.HfApi().model_info, repo_id, files_metadata=True)
        except Exception as e:  # noqa: BLE001 — a catalogue/read failure surfaces as TASK_FAILED with the real cause, never raised across the socket
            yield _failed(
                f"Could not read the file list for {repo_id}: {e}")
            return
        files: list[tuple[str, int | None]] = []
        for sibling in getattr(info, "siblings", None) or []:
            name = getattr(sibling, "rfilename", None)
            if not name:
                continue
            size = getattr(sibling, "size", None)
            files.append((name, size if isinstance(size, int) else None))
        if not files:
            yield _failed(
                f"Hugging Face lists no files for {repo_id}.",
                retriable=False,
            )
            return

        total_bytes = (
            sum(size for _, size in files)
            if all(size is not None for _, size in files)
            else None
        )
        yield Event(
            type=EventType.LOCAL_MODEL_DOWNLOAD_PROGRESS,
            request_id=request_id,
            payload={
                "task_id": t_id,
                "model": repo_id,
                "download_percent": 0.0,
                "status": "preparing",
            },
        )

        done_files = 0
        done_bytes = 0
        try:
            for name, size in files:
                # One file per to_thread hop: the event loop stays
                # responsive and cancellation lands BETWEEN files
                # (a thread mid-file cannot be killed; it finishes
                # into the cache, where HF resumes from it later).
                await asyncio.to_thread(
                    hf.hf_hub_download,
                    repo_id=repo_id,
                    filename=name,
                    cache_dir=cache_dir,
                )
                done_files += 1
                done_bytes += size or 0
                if total_bytes:
                    pct = round(done_bytes / total_bytes * 100, 1)
                else:
                    pct = round(done_files / len(files) * 100, 1)
                yield Event(
                    type=EventType.LOCAL_MODEL_DOWNLOAD_PROGRESS,
                    request_id=request_id,
                    payload={
                        "task_id": t_id,
                        "model": repo_id,
                        "download_percent": pct,
                        "status": f"{done_files}/{len(files)} files",
                    },
                )
        except asyncio.CancelledError:
            yield Event(
                type=EventType.TASK_CANCELLED,
                request_id=request_id,
                payload={"task_id": t_id, "model": repo_id},
            )
            raise
        except Exception as e:  # noqa: BLE001 — a per-file download failure surfaces as TASK_FAILED with the real cause; files already fetched stay in the cache for resume
            yield _failed(f"Downloading {repo_id} failed: {e}")
            return

        yield Event(
            type=EventType.TASK_COMPLETED,
            request_id=request_id,
            payload={
                "task_id": t_id,
                "model": repo_id,
                "path": str(self._hf_repo_dir(repo_id)),
                "response": f"Downloaded {repo_id} into the "
                            f"Hugging Face cache",
            },
        )

    # -- HF cache layout ---------------------------------------------------
    # Standard huggingface_hub cache: <root>/models--<org>--<repo>/
    # with blobs/ (real files), snapshots/<rev>/ (symlinks into
    # blobs) and refs/. The voice code resolves repos through the
    # same layout, which is why downloads land here and nowhere else.

    def _hf_cache_root(self) -> Path:
        if self._hf_cache_dir is not None:
            return self._hf_cache_dir
        hf = self._hf_loader()
        if hf is not None:
            try:
                return Path(hf.constants.HF_HUB_CACHE)
            except Exception as e:  # noqa: BLE001 — fall through to the env/default mirror of the same constant
                logger.debug(f"HF cache constant unreadable, using env/default: {e}")
        if os.environ.get("HF_HUB_CACHE"):
            return Path(os.environ["HF_HUB_CACHE"])
        if os.environ.get("HF_HOME"):
            return Path(os.environ["HF_HOME"]) / "hub"
        return Path.home() / ".cache" / "huggingface" / "hub"

    def _hf_repo_dir(self, repo_id: str) -> Path:
        return self._hf_cache_root() / (
            "models--" + repo_id.replace("/", "--"))

    @staticmethod
    def _repo_id_from_cache_dirname(dirname: str) -> str:
        body = dirname[len("models--"):]
        parts = body.split("--")
        if len(parts) >= 2:
            return parts[0] + "/" + "--".join(parts[1:])
        return body

    @staticmethod
    def _dir_size_bytes(root: Path) -> int:
        """Sum real files under ``root``; symlinks are skipped so a
        snapshot link is never counted on top of its blob."""
        total = 0
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                path = Path(dirpath) / name
                try:
                    if not path.is_symlink():
                        total += path.stat().st_size
                except OSError:
                    continue
        return total

    def _scan_hf_speech_cache(self) -> list[dict[str, Any]]:
        root = self._hf_cache_root()
        rows: list[dict[str, Any]] = []
        if not root.is_dir():
            return rows
        for child in sorted(root.iterdir()):
            if not child.is_dir() or not child.name.startswith("models--"):
                continue
            repo_id = self._repo_id_from_cache_dirname(child.name)
            if not _is_hub_speech_repo(repo_id):
                continue
            leaf = repo_id.rsplit("/", 1)[-1]
            family = (
                "text-to-speech"
                if repo_id.lower() == _KOKORO_REPO_ID
                else "speech-to-text"
            )
            formats = ["ctranslate2"] if family == "speech-to-text" else []
            label, reason = label_runnability(family, formats, repo_id)
            rows.append({
                "source": "hf",
                "id": repo_id,
                "name": leaf,
                "family": family,
                "downloads": None,
                "licence": None,
                "formats": formats,
                "runnability": label,
                "runnability_reason": reason,
                "size_bytes": self._dir_size_bytes(child),
                "downloaded": True,
                "path": str(child),
            })
        return rows

    # -- Downloaded inventory ----------------------------------------------

    async def downloaded(self) -> dict[str, Any]:
        """One listing merging both stores' downloaded models.

        Rows carry the H1 normalised shape plus ``downloaded: True``
        and ``size_bytes`` measured from the store itself (the
        daemon's /api/tags sizes; on-disk bytes for the HF cache).
        """
        rows: list[dict[str, Any]] = []
        for model in await self._ollama.list_downloaded_models():
            label, reason = label_runnability(
                "brain", ["gguf"], model["name"])
            rows.append({
                "source": "ollama",
                "id": model["name"],
                "name": model["name"],
                "family": "brain",
                "downloads": None,
                "licence": None,
                "formats": ["gguf"],
                "runnability": label,
                "runnability_reason": reason,
                "size_bytes": model.get("size_bytes"),
                "downloaded": True,
                "path": model.get("path"),
            })
        rows.extend(self._scan_hf_speech_cache())
        rows.sort(key=lambda r: (r["source"], r["name"]))
        return {"models": rows, "error": None}

    # -- Delete --------------------------------------------------------------

    async def delete(self, source: str, model_id: str) -> dict[str, Any]:
        """Delete one downloaded model from its store.

        Deleting something that isn't there is an honest no-op
        (``success: True, deleted: False``), not an error.
        """
        if source == "ollama":
            if not await self._ollama.is_ollama_running():
                return {
                    "success": False,
                    "deleted": False,
                    "error": "Ollama is not running — hub deletes "
                             "need the daemon to verify and remove "
                             "its models.",
                }
            present = {
                m["name"]
                for m in await self._ollama.list_downloaded_models()
            }
            if model_id not in present:
                return {
                    "success": True,
                    "deleted": False,
                    "error": None,
                    "message": f"'{model_id}' is not downloaded — "
                               f"nothing to delete.",
                }
            if await self._ollama.delete_model(model_id):
                return {"success": True, "deleted": True, "error": None}
            return {
                "success": False,
                "deleted": False,
                "error": f"Ollama could not delete '{model_id}'.",
            }
        if source == "hf":
            repo_dir = self._hf_repo_dir(model_id)
            if not repo_dir.is_dir():
                return {
                    "success": True,
                    "deleted": False,
                    "error": None,
                    "message": f"'{model_id}' is not in the Hugging "
                               f"Face cache — nothing to delete.",
                }
            try:
                # RUX-B-08: the tree removal is synchronous bulk
                # work inside an async delete — run it on a worker
                # thread so a large cached repo cannot stall the
                # caller's event loop.
                await asyncio.to_thread(shutil.rmtree, repo_dir)
            except OSError as e:
                return {
                    "success": False,
                    "deleted": False,
                    "error": f"Could not remove {repo_dir}: {e}",
                }
            return {"success": True, "deleted": True, "error": None}
        return {
            "success": False,
            "deleted": False,
            "error": f"Unknown model source '{source}'.",
        }
