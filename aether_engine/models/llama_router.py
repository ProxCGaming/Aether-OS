"""llama.cpp router client and supervised-process manager.

The local serving backend beside Ollama: a single ``llama-server``
running in router mode (started without a model, discovering the
models in its models directory / preset file), fronted by this
manager for status, model list + load state, explicit load /
unload, and start / stop of the supervised process.

Coded against the official llama.cpp server documentation
(``tools/server/README.md`` in ggml-org/llama.cpp, master,
read 19 Nov 2026). The router-mode surface used here:

- ``GET /health`` — 200 when ready, 503 while loading; any HTTP
  answer means a server is listening at the router URL.
- ``GET /models`` — ``{"data": [{"id", "path", "status":
  {"value": "unloaded" | "loading" | "loaded" | "sleeping" |
  "downloading" | "incomplete", ...}}]}``.
- ``POST /models/load`` / ``POST /models/unload`` with
  ``{"model": "<id>"}`` — ``{"success": true}`` or an
  OpenAI-style error object.
- ``POST /v1/chat/completions`` — the OpenAI-compatible chat
  endpoint. Chat does NOT flow through this module: it goes
  through the engine's existing OpenAI-compatible provider path
  with the router URL as its base (see the smoke suite).

The parsing is defensive, in the OllamaManager idiom: unknown
fields are ignored, and a load state the router did not report
reads as unknown (None) — never guessed. Every method returns
data or an honest error dict; nothing raises across the wire
boundary. The API key is a provider secret: it is resolved from
the SecretStore under the reserved provider id ``llamacpp``,
sent as a Bearer header, and never logged or returned.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from aether_engine.config import DEFAULT_LLAMACPP_ROUTER_URL
from aether_engine.machine_capability import (
    local_model_fit,
    machine_capability_report,
)
from aether_engine.models.llama_presets import (
    TIER_HEAVY,
    match_preset,
    plan_tier_actions,
    preset_rows,
)

logger = logging.getLogger("aether_engine.models.llama_router")

# The reserved SecretStore provider id the router's API key is
# held under — the same store and shape as every cloud
# provider's key, so the key never touches the config file.
LLAMACPP_PROVIDER_ID = "llamacpp"

LLAMACPP_DEFAULT_PORT = 9931

# Named, bounded timeouts (OllamaManager's discipline: an
# unbounded client lets a silent endpoint hang a request
# forever). Health and list reads are panel reads — short. A
# load can legitimately take a while (the router starts a
# child server per model), so its bound is generous but real.
LLAMACPP_HEALTH_TIMEOUT = httpx.Timeout(
    connect=1.0, read=2.0, write=1.0, pool=1.0)
LLAMACPP_MODELS_TIMEOUT = httpx.Timeout(
    connect=1.0, read=3.0, write=1.0, pool=1.0)
LLAMACPP_LOAD_TIMEOUT = httpx.Timeout(
    connect=2.0, read=120.0, write=5.0, pool=2.0)
LLAMACPP_UNLOAD_TIMEOUT = httpx.Timeout(
    connect=2.0, read=15.0, write=5.0, pool=2.0)

# How long a supervised start waits for the router to answer
# before the start is reported as failed-to-become-ready.
LLAMACPP_START_READY_TIMEOUT_S = 30.0
_LLAMACPP_START_POLL_S = 0.5

# The honest refusal for a start on a machine with no usable
# binary — the panel shows this verbatim.
LLAMACPP_NOT_INSTALLED_ERROR = (
    "llama.cpp is not installed or not configured on this machine")


# ------------------------------------------------------------------
# Supervised-process registry. The child handle must outlive any
# single request's manager instance, so it lives at module level,
# keyed by router URL. Tests replace _spawn_router_process — no
# real process is ever spawned by the suite.
# ------------------------------------------------------------------

_SUPERVISED_ROUTERS: dict[str, Any] = {}


def _spawn_router_process(argv: list[str], env: dict[str, str]):
    """Spawn the router child. The one spawn seam."""
    return subprocess.Popen(
        argv, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _supervised_process(router_url: str):
    """The live supervised child for ``router_url``, or None.

    A child that already exited is dropped from the registry —
    a dead process is not a supervised router.
    """
    proc = _SUPERVISED_ROUTERS.get(router_url)
    if proc is None:
        return None
    if proc.poll() is not None:
        _SUPERVISED_ROUTERS.pop(router_url, None)
        return None
    return proc


def _loaded_from_status(status: Any) -> bool | None:
    """Map a router model-status to loaded True / False / None.

    Accepts the documented object form (``{"value": ...}``) and
    a bare string (older builds). ``loaded`` -> True;
    ``unloaded`` and ``sleeping`` (evicted, reloads on demand)
    -> False; transitional states (``loading``,
    ``downloading``), unrecognised values and a missing status
    -> None: in flight or unreported, never guessed.
    """
    value: str | None = None
    if isinstance(status, dict):
        raw = status.get("value")
        value = raw if isinstance(raw, str) else None
    elif isinstance(status, str):
        value = status
    if value is None:
        return None
    value = value.strip().lower()
    if value == "loaded":
        return True
    if value in ("unloaded", "sleeping", "incomplete"):
        return False
    return None


def _entry_size_bytes(entry: dict[str, Any]) -> int | None:
    """A model entry's exact size in bytes, or None.

    Taken from the entry's own size fields when the router
    reports them; otherwise from the model file itself — the
    router runs on this machine, so the entry's ``path`` is a
    local file whose size is a measurement, not a guess.
    """
    for key in ("size_bytes", "size"):
        raw = entry.get(key)
        if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
            return raw
    path = entry.get("path")
    if isinstance(path, str) and path:
        try:
            size = os.path.getsize(path)
        except OSError:
            return None
        return size if size > 0 else None
    return None


def _router_error_message(res: httpx.Response, fallback: str) -> str:
    """The router's own error message when it sent one
    (OpenAI-style ``{"error": {"message": ...}}``), else the
    caller's fallback."""
    try:
        data = res.json()
    except Exception:  # noqa: BLE001 — a non-JSON error body carries no message
        return fallback
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message:
                return message
        elif isinstance(error, str) and error:
            return error
    return fallback


class LlamaRouterManager:
    """Status, models, load/unload and process control for one
    llama.cpp router, mirroring OllamaManager's honesty idioms.
    """

    def __init__(
        self,
        router_url: str = DEFAULT_LLAMACPP_ROUTER_URL,
        *,
        binary_path: str = "",
        models_dir: str = "",
        secret_store: Any = None,
        api_key: str | None = None,
    ):
        self.router_url = (router_url or DEFAULT_LLAMACPP_ROUTER_URL)
        self.router_url = self.router_url.strip().rstrip("/") \
            or DEFAULT_LLAMACPP_ROUTER_URL
        self.binary_path = binary_path or ""
        self.models_dir = models_dir or ""
        self._secret_store = secret_store
        self._api_key = api_key
        # Tier-policy bookkeeping: last use per model (monotonic
        # clock) and the job state of heavy models. In-memory
        # only — after an engine restart the policy simply has
        # no idleness evidence yet, which it treats honestly as
        # "unknown", never as "idle since forever".
        self._last_used: dict[str, float] = {}
        self._job_state: dict[str, str] = {}

    @classmethod
    def from_config(cls, config: Any, secret_store: Any = None
                    ) -> LlamaRouterManager:
        """Build the manager for the live user config.

        Attribute access is defensive (getattr with the field
        defaults): a config object predating these fields must
        not crash a panel read.
        """
        return cls(
            router_url=getattr(
                config, "llamacpp_router_url",
                DEFAULT_LLAMACPP_ROUTER_URL),
            binary_path=getattr(config, "llamacpp_binary_path", ""),
            models_dir=getattr(config, "llamacpp_models_dir", ""),
            secret_store=secret_store,
        )

    # ----------------------------------------------------------
    # HTTP plumbing
    # ----------------------------------------------------------

    def _resolve_api_key(self) -> str | None:
        """The router's API key from the SecretStore, or None.

        Resolved per call so a key saved after this manager was
        built is picked up. The value is only ever placed in an
        Authorization header or the child's environment — never
        logged, never returned.
        """
        if self._api_key is not None:
            return self._api_key or None
        store = self._secret_store
        if store is None:
            return None
        try:
            key = store.load_provider(LLAMACPP_PROVIDER_ID)
        except Exception:  # noqa: BLE001 — no stored key is a normal state, not a failure
            return None
        return key or None

    def _client(self, timeout: httpx.Timeout) -> httpx.AsyncClient:
        headers: dict[str, str] = {}
        key = self._resolve_api_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        # trust_env=False: the router is a configured local
        # endpoint — ambient proxy variables must never reroute
        # (or, with a malformed no_proxy entry, break) its calls.
        return httpx.AsyncClient(
            timeout=timeout, headers=headers, trust_env=False)

    async def _fetch_model_entries(self) -> list[dict[str, Any]] | None:
        """The router's raw /models entries, or None when the
        read itself failed (unreachable, non-200, malformed)."""
        try:
            async with self._client(LLAMACPP_MODELS_TIMEOUT) as client:
                res = await client.get(f"{self.router_url}/models")
            if res.status_code != 200:
                return None
            data = res.json()
        except Exception as e:  # noqa: BLE001 — a failed read is reported as None, never raised
            logger.debug(f"Router model list read failed: {e}")
            return None
        if isinstance(data, dict):
            entries = data.get("data")
            if isinstance(entries, list):
                return [e for e in entries if isinstance(e, dict)]
            return None
        if isinstance(data, list):  # defensive: a bare-list build
            return [e for e in data if isinstance(e, dict)]
        return None

    # ----------------------------------------------------------
    # Status + model list
    # ----------------------------------------------------------

    async def _router_up(self) -> bool:
        """True when the router is up and answering as a router.

        ``/health`` reads 200 when ready and 503 while a model
        is loading; in router mode the model list is served by
        the main process throughout, so a non-ready health is
        confirmed against ``/models`` before the router is
        called down. A transport failure is simply not-up.
        """
        try:
            async with self._client(LLAMACPP_HEALTH_TIMEOUT) as client:
                res = await client.get(f"{self.router_url}/health")
            if res.status_code == 200:
                return True
        except Exception:  # noqa: BLE001 — any transport failure means not up
            return False
        return await self._fetch_model_entries() is not None

    async def router_status(self) -> dict[str, Any]:
        """The router's state: {running, version, models_loaded,
        error}.

        ``running`` is False with NO error when nothing answers
        at the router URL — the router simply isn't up, which is
        the default on every machine without it. ``error`` is set
        only when a read itself failed (the router answered but
        its model list could not be read). ``version`` and
        ``models_loaded`` are None when unreported.
        """
        result: dict[str, Any] = {
            "running": False,
            "version": None,
            "models_loaded": None,
            "error": None,
        }
        if not await self._router_up():
            return result
        result["running"] = True

        # Version is advisory: some builds report one on /props,
        # many do not. Unreported reads as None, never invented.
        try:
            async with self._client(LLAMACPP_HEALTH_TIMEOUT) as client:
                res = await client.get(f"{self.router_url}/props")
            if res.status_code == 200:
                data = res.json()
                if isinstance(data, dict):
                    for key in ("version", "build_info"):
                        value = data.get(key)
                        if isinstance(value, str) and value:
                            result["version"] = value
                            break
        except Exception as e:  # noqa: BLE001 — version is best-effort only
            logger.debug(f"Router version read failed: {e}")

        entries = await self._fetch_model_entries()
        if entries is None:
            result["error"] = (
                "the router answered but its model list "
                "could not be read")
        else:
            result["models_loaded"] = sum(
                1 for e in entries
                if _loaded_from_status(e.get("status")) is True)
        return result

    async def list_models(self) -> dict[str, Any]:
        """The router's models: {models: [rows], error}.

        Rows carry {id, name, size_bytes, loaded, preset, tier,
        fit} with the honesty rules of the local-model surface:
        ``loaded`` is None when the router did not report the
        state, ``size_bytes`` None when no size is known, and
        ``fit`` the shared local-model fit verdict's tier (None
        when size or machine budget is unknown). A router that
        is not running yields an empty list and NO error — its
        absence is a state, not a failure.
        """
        if not await self._router_up():
            return {"models": [], "error": None}

        entries = await self._fetch_model_entries()
        if entries is None:
            return {
                "models": [],
                "error": "the router answered but its model list "
                         "could not be read",
            }

        # Fit verdicts are advisory (OllamaManager's rule): a
        # probe failure leaves every verdict unjudged (None),
        # never breaks the list.
        fit_machine: dict[str, Any] = {}
        try:
            report = await asyncio.to_thread(machine_capability_report)
            fit_machine = report.get("machine") or {}
        except Exception:  # noqa: BLE001 — advisory only, see above
            fit_machine = {}

        rows: list[dict[str, Any]] = []
        for entry in entries:
            model_id = entry.get("id")
            if not isinstance(model_id, str) or not model_id:
                continue
            size_bytes = _entry_size_bytes(entry)
            fit = local_model_fit(size_bytes, fit_machine)
            preset = match_preset(model_id)
            rows.append({
                "id": model_id,
                "name": model_id.rsplit("/", 1)[-1],
                "size_bytes": size_bytes,
                "loaded": _loaded_from_status(entry.get("status")),
                "preset": preset.id if preset else None,
                "tier": preset.tier if preset else None,
                "fit": fit["tier"] if fit else None,
            })
        rows.sort(key=lambda r: r["id"])
        return {"models": rows, "error": None}

    # ----------------------------------------------------------
    # Load / unload
    # ----------------------------------------------------------

    async def load_model(self, model_id: str) -> dict[str, Any]:
        """Load one model on the router: {success, error}.

        Success only when the router confirms it; an unreachable
        router, a refusal and a malformed answer all report
        False with the reason.
        """
        model_id = (model_id or "").strip()
        if not model_id:
            return {"success": False, "error": "no model was specified"}
        try:
            async with self._client(LLAMACPP_LOAD_TIMEOUT) as client:
                res = await client.post(
                    f"{self.router_url}/models/load",
                    json={"model": model_id})
        except Exception as e:  # noqa: BLE001 — reported, never raised
            logger.warning(f"Router load of {model_id} failed: {e}")
            return {
                "success": False,
                "error": f"the llama.cpp router is not reachable "
                         f"at {self.router_url}",
            }
        if res.status_code != 200:
            return {
                "success": False,
                "error": _router_error_message(
                    res, f"the router refused to load {model_id} "
                         f"(HTTP {res.status_code})"),
            }
        try:
            data = res.json()
        except Exception:  # noqa: BLE001 — a 200 with no body still confirms the load
            data = {}
        if isinstance(data, dict) and data.get("success") is False:
            return {
                "success": False,
                "error": _router_error_message(
                    res, f"the router refused to load {model_id}"),
            }
        self._last_used[model_id] = time.monotonic()
        return {"success": True, "error": None}

    async def unload_model(self, model_id: str) -> dict[str, Any]:
        """Unload one model from the router: {success, error}.

        The model files stay on disk — this is residency only,
        with load_model's honesty contract.
        """
        model_id = (model_id or "").strip()
        if not model_id:
            return {"success": False, "error": "no model was specified"}
        try:
            async with self._client(LLAMACPP_UNLOAD_TIMEOUT) as client:
                res = await client.post(
                    f"{self.router_url}/models/unload",
                    json={"model": model_id})
        except Exception as e:  # noqa: BLE001 — reported, never raised
            logger.warning(f"Router unload of {model_id} failed: {e}")
            return {
                "success": False,
                "error": f"the llama.cpp router is not reachable "
                         f"at {self.router_url}",
            }
        if res.status_code != 200:
            return {
                "success": False,
                "error": _router_error_message(
                    res, f"the router refused to unload {model_id} "
                         f"(HTTP {res.status_code})"),
            }
        self._last_used.pop(model_id, None)
        self._job_state.pop(model_id, None)
        return {"success": True, "error": None}

    # ----------------------------------------------------------
    # Tier policy application
    # ----------------------------------------------------------

    def note_model_used(self, model_id: str) -> None:
        """Record that a model just did work (a chat or a job).

        Refreshes the daily tier's idle clock and marks a heavy
        model as mid-job, so the policy never unloads it early.
        The executor-side call sites are an integration step;
        the manager's own loads already record use.
        """
        self._last_used[model_id] = time.monotonic()
        preset = match_preset(model_id)
        if preset is not None and preset.tier == TIER_HEAVY:
            self._job_state[model_id] = "active"

    async def note_job_completed(self, model_id: str) -> dict[str, Any]:
        """The after-job hook for the tier policy.

        A heavy model whose job completed is unloaded
        immediately (its verdict is returned); every other tier
        is a no-op success — resident and daily models follow
        their own rules.
        """
        self._job_state[model_id] = "done"
        self._last_used[model_id] = time.monotonic()
        preset = match_preset(model_id)
        if preset is None or preset.tier != TIER_HEAVY:
            return {"success": True, "error": None}
        return await self.unload_model(model_id)

    async def apply_tier_policy(
        self, *, router_just_started: bool = False,
    ) -> dict[str, Any]:
        """Run one tier-policy pass against the live router.

        Gathers the router's model states plus this manager's
        use bookkeeping, asks the pure policy
        (:func:`plan_tier_actions`) and performs the calls.
        Returns {load, unload, errors} — the actions attempted
        and any per-model failures, so a caller can report
        exactly what happened.
        """
        listing = await self.list_models()
        rows = [{
            "id": m["id"],
            "loaded": m["loaded"],
            "last_used": self._last_used.get(m["id"]),
            "job_state": self._job_state.get(m["id"]),
        } for m in listing["models"]]
        plan = plan_tier_actions(
            rows, now=time.monotonic(),
            router_just_started=router_just_started)
        errors: dict[str, str] = {}
        for model_id in plan["load"]:
            res = await self.load_model(model_id)
            if not res["success"]:
                errors[model_id] = res["error"] or "load failed"
        for model_id in plan["unload"]:
            res = await self.unload_model(model_id)
            if not res["success"]:
                errors[model_id] = res["error"] or "unload failed"
        return {"load": plan["load"], "unload": plan["unload"],
                "errors": errors}

    # ----------------------------------------------------------
    # Supervised process control
    # ----------------------------------------------------------

    def supervised_running(self) -> bool:
        """True while a router child Aether spawned is alive."""
        return _supervised_process(self.router_url) is not None

    async def start_router(self) -> dict[str, Any]:
        """Start the supervised router: {success, running, error}.

        Spawns ``llama-server`` in router mode ONLY when a
        binary path is configured AND exists on disk; without
        one, the start refuses with
        :data:`LLAMACPP_NOT_INSTALLED_ERROR` — never a fake
        start. A router that is already answering (whoever
        started it) is a successful, truthful "running". After a
        successful spawn the resident tier is loaded per the
        tier policy.
        """
        if self.supervised_running():
            return {"success": True, "running": True, "error": None}
        status = await self.router_status()
        if status["running"]:
            return {"success": True, "running": True, "error": None}

        binary = (self.binary_path or "").strip()
        if not binary or not Path(binary).is_file():
            return {
                "success": False,
                "running": False,
                "error": LLAMACPP_NOT_INSTALLED_ERROR,
            }

        parsed = urlparse(self.router_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or LLAMACPP_DEFAULT_PORT
        argv = [binary, "--host", host, "--port", str(port)]
        if (self.models_dir or "").strip():
            argv += ["--models-dir", self.models_dir.strip()]
        env = dict(os.environ)
        key = self._resolve_api_key()
        if key:
            # llama.cpp's common arguments read LLAMA_ARG_* env
            # vars; the key stays off the command line, where a
            # process listing would show it to any local user.
            env["LLAMA_ARG_API_KEY"] = key
        try:
            proc = _spawn_router_process(argv, env)
        except Exception as e:  # noqa: BLE001 — a spawn failure is a verdict, not an exception
            logger.error(f"Could not spawn the llama.cpp router: {e}")
            return {
                "success": False,
                "running": False,
                "error": f"the llama.cpp router process could not "
                         f"be started: {e}",
            }
        _SUPERVISED_ROUTERS[self.router_url] = proc

        deadline = time.monotonic() + LLAMACPP_START_READY_TIMEOUT_S
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                _SUPERVISED_ROUTERS.pop(self.router_url, None)
                return {
                    "success": False,
                    "running": False,
                    "error": f"the llama.cpp router exited during "
                             f"startup (exit code {proc.returncode})",
                }
            status = await self.router_status()
            if status["running"]:
                # Resident tier loads on router start (tier
                # policy); failures ride the log, not the start
                # verdict — the router itself is up.
                try:
                    await self.apply_tier_policy(router_just_started=True)
                except Exception as e:  # noqa: BLE001 — policy is best-effort after a good start
                    logger.warning(f"Tier policy after router start: {e}")
                return {"success": True, "running": True, "error": None}
            await asyncio.sleep(_LLAMACPP_START_POLL_S)
        return {
            "success": False,
            "running": True,
            "error": "the llama.cpp router process started but "
                     "did not answer in time",
        }

    async def stop_router(self) -> dict[str, Any]:
        """Stop the supervised router: {success, running, error}.

        Only a child Aether spawned is terminated. With no
        supervised child, an already-quiet machine is an
        idempotent success, and a router someone else started is
        reported honestly as not Aether's to stop.
        """
        proc = _supervised_process(self.router_url)
        if proc is None:
            status = await self.router_status()
            if not status["running"]:
                return {"success": True, "running": False, "error": None}
            return {
                "success": False,
                "running": True,
                "error": "the running llama.cpp router was not "
                         "started by Aether, so it cannot be "
                         "stopped here",
            }
        try:
            proc.terminate()
            try:
                await asyncio.to_thread(proc.wait, 5.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                await asyncio.to_thread(proc.wait)
        except Exception as e:  # noqa: BLE001 — a stop failure is a verdict, not an exception
            logger.error(f"Could not stop the llama.cpp router: {e}")
            return {
                "success": False,
                "running": self.supervised_running(),
                "error": f"stopping the llama.cpp router failed: {e}",
            }
        _SUPERVISED_ROUTERS.pop(self.router_url, None)
        return {"success": True, "running": False, "error": None}

    async def router_control(self, action: str) -> dict[str, Any]:
        """Dispatch a router control action: {success, running,
        error} — the control wire shape."""
        if action == "start":
            return await self.start_router()
        if action == "stop":
            return await self.stop_router()
        status = await self.router_status()
        return {
            "success": False,
            "running": bool(status["running"]),
            "error": f"unknown router action {action!r} "
                     f"(expected 'start' or 'stop')",
        }

    # ----------------------------------------------------------
    # Wire payloads (the shapes the panel consumes)
    # ----------------------------------------------------------

    async def status_payload(self, enabled: bool) -> dict[str, Any]:
        """The status wire shape: {enabled, running, url,
        version, models_loaded, supervised} plus ``error`` only
        when a read itself failed."""
        status = await self.router_status()
        payload: dict[str, Any] = {
            "enabled": bool(enabled),
            "running": status["running"],
            "url": self.router_url,
            "version": status["version"],
            "models_loaded": status["models_loaded"],
            "supervised": self.supervised_running(),
        }
        if status["error"] is not None:
            payload["error"] = status["error"]
        return payload

    async def models_payload(self) -> dict[str, Any]:
        """The models wire shape: {models, presets} plus
        ``error`` only when a read itself failed. Preset
        availability is judged against the models the router
        actually reported."""
        listing = await self.list_models()
        payload: dict[str, Any] = {
            "models": listing["models"],
            "presets": preset_rows(m["id"] for m in listing["models"]),
        }
        if listing["error"] is not None:
            payload["error"] = listing["error"]
        return payload
