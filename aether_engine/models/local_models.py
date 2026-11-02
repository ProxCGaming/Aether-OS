"""Ollama local models manager and Engine task-driven download pipeline."""
import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import httpx

from aether_common.contracts import Event, EventType, TaskState

logger = logging.getLogger("aether_engine.models.local_models")

OLLAMA_BASE_URL = "http://127.0.0.1:11434"

# Curated library catalog of common local models with sizes
DEFAULT_LOCAL_CATALOG: list[dict[str, Any]] = [
    {"name": "llama3:8b", "size": "~4.7GB", "downloaded": False, "path": ""},
    {"name": "mistral:7b", "size": "~4.1GB", "downloaded": False, "path": ""},
    {"name": "phi3:mini", "size": "~2.3GB", "downloaded": False, "path": ""},
    {"name": "gemma2:2b", "size": "~1.6GB", "downloaded": False, "path": ""},
]


class OllamaManager:
    """Manages local model queries, deletion, and Engine task-driven downloads."""

    def __init__(self, base_url: str = OLLAMA_BASE_URL):
        self.base_url = base_url
        self._downloaded_paths: dict[str, str] = {}

    async def is_ollama_running(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                res = await client.get(f"{self.base_url}/")
                return res.status_code == 200
        except Exception:  # noqa: BLE001 — reachability probe: any failure means Ollama is not running
            return False

    async def list_models(self) -> list[dict[str, Any]]:
        """List local models, placing downloaded models sorted at the top."""
        downloaded_names = set()
        models_result: list[dict[str, Any]] = []

        if await self.is_ollama_running():
            try:
                async with httpx.AsyncClient(timeout=3.0) as client:
                    res = await client.get(f"{self.base_url}/api/tags")
                    if res.status_code == 200:
                        data = res.json()
                        for m in data.get("models", []):
                            name = m.get("name", "")
                            size_bytes = m.get("size", 0)
                            size_gb = round(size_bytes / (1024**3), 1)
                            path = self._downloaded_paths.get(name, f"~/.ollama/models/manifests/{name}")
                            downloaded_names.add(name)
                            models_result.append({
                                "name": name,
                                "size": f"~{size_gb}GB",
                                "downloaded": True,
                                "path": path,
                            })
            except Exception as e:  # noqa: BLE001 — best-effort delete: any failure reports False  # noqa: BLE001 — download failure is reported as a TASK_FAILED event, never raised  # noqa: BLE001 — best-effort model listing: a dead Ollama yields an empty list
                logger.warning(f"Error fetching Ollama tags: {e}")

        # Add remaining catalog entries below downloaded models
        for item in DEFAULT_LOCAL_CATALOG:
            if item["name"] not in downloaded_names:
                models_result.append({
                    "name": item["name"],
                    "size": item["size"],
                    "downloaded": False,
                    "path": self._downloaded_paths.get(item["name"], ""),
                })

        # Ensure downloaded models sort to top
        models_result.sort(key=lambda x: (not x["downloaded"], x["name"]))
        return models_result

    async def download_model_task(
        self,
        model_name: str,
        destination_dir: str | None = None,
        task_id: str | None = None,
        request_id: str | None = None,
    ) -> AsyncGenerator[Event, None]:
        """Engine task stream for model downloads, reporting live progress percentages."""
        t_id = task_id or "dl-task"
        dest = destination_dir or str(Path.home() / ".ollama" / "models")

        yield Event(
            type=EventType.TASK_CREATED,
            request_id=request_id,
            payload={
                "task_id": t_id,
                "prompt": f"Download local model {model_name}",
                "state": TaskState.RUNNING.value,
                "destination_dir": dest,
            },
        )

        def _failed(error: str, retriable: bool) -> Event:
            return Event(
                type=EventType.TASK_FAILED,
                request_id=request_id,
                payload={
                    "task_id": t_id,
                    "model": model_name,
                    "state": TaskState.FAILED.value,
                    "error": error,
                    "retriable": retriable,
                },
            )

        # E5-F09: the lifecycle is truthful end to end. There is no simulated
        # progress fallback: a pull counts as downloaded only when the daemon
        # confirms it (HTTP 200 and a terminal "success" status in the pull
        # stream). Every other outcome — daemon offline, connection failure,
        # non-200 status, a streamed error event, or a stream that ends
        # before success — yields TASK_FAILED with the real cause and records
        # no downloaded-path metadata. Partial blobs from an interrupted pull
        # belong to the daemon's own store; this layer simply never claims
        # the model exists until the daemon says the pull succeeded.
        if not await self.is_ollama_running():
            yield _failed(
                f"Ollama is not running (daemon unreachable at {self.base_url}); "
                f"cannot download {model_name}.",
                retriable=True,
            )
            return

        saw_success = False
        try:
            async with (
                httpx.AsyncClient(timeout=None) as client,
                client.stream(
                    "POST",
                    f"{self.base_url}/api/pull",
                    json={"name": model_name, "stream": True},
                ) as resp,
            ):
                if resp.status_code != 200:
                    body = (await resp.aread()).decode("utf-8", errors="replace").strip()
                    detail = f": {body}" if body else ""
                    yield _failed(
                        f"Ollama pull for {model_name} failed with "
                        f"HTTP {resp.status_code}{detail}",
                        retriable=resp.status_code >= 500,
                    )
                    return

                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    try:
                        payload_data = json.loads(line)
                    except json.JSONDecodeError:
                        yield _failed(
                            f"Ollama pull for {model_name} returned a malformed "
                            f"progress line: {line!r}",
                            retriable=False,
                        )
                        return

                    streamed_error = payload_data.get("error")
                    if streamed_error:
                        yield _failed(
                            f"Ollama pull for {model_name} failed: {streamed_error}",
                            retriable=False,
                        )
                        return

                    if payload_data.get("status") == "success":
                        # Terminal marker: TASK_COMPLETED below supersedes a
                        # progress event for this line.
                        saw_success = True
                        continue

                    total = payload_data.get("total", 0)
                    completed = payload_data.get("completed", 0)
                    pct = round((completed / total) * 100, 1) if total > 0 else 0.0

                    yield Event(
                        type=EventType.LOCAL_MODEL_DOWNLOAD_PROGRESS,
                        request_id=request_id,
                        payload={
                            "task_id": t_id,
                            "model": model_name,
                            "download_percent": pct,
                            "status": payload_data.get("status", "downloading"),
                        },
                    )
        except asyncio.CancelledError:
            yield Event(
                type=EventType.TASK_CANCELLED,
                request_id=request_id,
                payload={"task_id": t_id, "model": model_name},
            )
            raise
        except Exception as e:  # noqa: BLE001 — download failure is reported as a TASK_FAILED event, never raised
            logger.error(f"Error downloading {model_name} via Ollama: {e}")
            yield _failed(
                f"Ollama pull for {model_name} failed: {e}",
                retriable=True,
            )
            return

        if not saw_success:
            yield _failed(
                f"Ollama pull for {model_name} ended before the daemon "
                "confirmed success (stream truncated).",
                retriable=True,
            )
            return

        self._downloaded_paths[model_name] = str(Path(dest) / model_name)
        yield Event(
            type=EventType.TASK_COMPLETED,
            request_id=request_id,
            payload={
                "task_id": t_id,
                "model": model_name,
                "path": str(Path(dest) / model_name),
                "response": f"Downloaded {model_name} to {dest}",
            },
        )

    async def delete_model(self, model_name: str) -> bool:
        # E5-F09: metadata is cleared and success returned ONLY when the
        # daemon confirms the deletion (HTTP 200/204). Offline, non-200 and
        # exception paths return False and RETAIN the recorded path — the
        # model is still on disk as far as anyone knows.
        if not await self.is_ollama_running():
            logger.error(f"Cannot delete {model_name}: Ollama is not running")
            return False
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                res = await client.delete(
                    f"{self.base_url}/api/delete",
                    json={"name": model_name},
                )
                if res.status_code in (200, 204):
                    self._downloaded_paths.pop(model_name, None)
                    return True
                logger.error(
                    f"Failed to delete model {model_name}: "
                    f"Ollama returned HTTP {res.status_code}"
                )
                return False
        except Exception as e:  # noqa: BLE001 — delete failure reports False; metadata retained
            logger.error(f"Failed to delete model {model_name}: {e}")
            return False
