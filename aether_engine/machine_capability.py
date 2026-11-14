"""Master build S10: hardware capability detection + matrix.

NOT the ``aether_engine/capability/`` package — that one holds
LLM *model* capability benchmarks (MMLU tiers, self-tests). This
module is about the *machine*: what CPU / RAM / GPU / disk /
network this engine is actually running on, and what that means
for the features that care (local voice, local models, cloud
providers, semantic embeddings, browser automation, desktop
control). The name is deliberately different so the two are
never confused again (audit G1, 14 Nov 2026: the misleadingly
named ``capability/`` package made this absence easy to miss).

Design rules, mirroring ``memory/backend_status.py`` (S9):

- Probing is a READ. It never installs, downloads, starts a
  service or opens a device; it measures with the standard
  library plus ``find_spec`` presence checks for packages that
  are already optional dependencies.
- Every probe is individually failure-isolated: a broken probe
  yields an "unknown" field (``None``), never an exception, so
  the report is always complete. "Unknown" is an honest answer;
  a crash or a guessed value is not.
- The probe runs on demand only — never on the engine startup
  path — behind a TTL cache, because the network checks and
  the GPU subprocess cost real time (bounded by the strict
  timeouts below, a few seconds worst case).
- The matrix (:func:`capability_matrix`) is a PURE function of
  the probe dict: same machine in, same verdicts out, no I/O.
  That is what S15 (voice step-down) and S18 (packs) will read,
  and what the tests exercise with synthetic machines.
- The matrix is advisory truth. Nothing in this slice changes
  any feature's actual gating: voice still fails closed on its
  own stage checks, Ollama features still discover Ollama
  themselves. The verdicts explain; they do not (yet) decide.
"""
from __future__ import annotations

import ctypes
import importlib.util
import json
import os
import platform
import shutil
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Probe budgets. Strict on purpose: the probe may run while the
# user is looking at a Settings spinner, and it must never hang.
# ---------------------------------------------------------------------------
REPORT_TTL_SECONDS = 300.0
_NETWORK_TIMEOUT_SECONDS = 1.5
_LOCAL_TIMEOUT_SECONDS = 0.75
_SUBPROCESS_TIMEOUT_SECONDS = 3.0

# Provider endpoints whose reachability defines "online" for the
# cloud-providers verdict. TCP connects only — no HTTP request is
# made and no key is needed to learn whether a route exists.
_NETWORK_ENDPOINTS = (
    ("api.openai.com", 443),
    ("api.anthropic.com", 443),
    ("generativelanguage.googleapis.com", 443),
)

# The local Ollama server: local LLM models (models/local_models.py)
# and the episodic embedding model (memory/episodic.py serves
# qwen3-embedding via 127.0.0.1:11434) both live behind it.
_OLLAMA_HOST = "127.0.0.1"
_OLLAMA_PORT = 11434

# The voice stages' own dependency gates, mirrored from
# voice/stt.py (faster_whisper), voice/tts.py (kokoro),
# voice/vad.py (onnxruntime + numpy) and voice/rtc.py (aiortc).
# If any is absent the pipeline refuses the session today; the
# matrix says the same thing before the user finds out live.
_VOICE_MODULES = ("faster_whisper", "kokoro", "onnxruntime", "numpy", "aiortc")

# The desktop-observe backend's dependency gate, mirrored from
# desktop/windows_uia.py (UIA_PACKAGE_MODULE): the Windows UIA
# provider reads the accessibility tree through the third-party
# "uiautomation" package; absent, observe cannot run (S19).
_DESKTOP_UIA_MODULE = "uiautomation"

_GIB = 1024 ** 3

# ---------------------------------------------------------------------------
# Capability-matrix thresholds.
#
# HEURISTICS, not measurements. The only measured figure in the
# project is the build VM's: whisper "small" STT took ~5,556 ms
# against a 200 ms stage budget on a contended, GPU-less VM
# (voice audit, 14 Nov 2026) — enough to know CPU-only STT at
# "small" is nowhere near real-time there, not enough to draw a
# precise line for a real Windows PC, whose figures still await.
# These constants are the conservative reading of that one data
# point plus the models' published footprints. Every reason
# string below names the numbers it used, so a wrong threshold
# is visible in the UI, not hidden in code; S15 replaces the
# voice verdict's basis with per-machine measured latency once
# the bench has run on real hardware.
# ---------------------------------------------------------------------------
VOICE_MIN_RAM_BYTES = 2 * _GIB          # below: speech models can't load sanely
VOICE_COMFORTABLE_RAM_BYTES = 8 * _GIB  # whisper + Kokoro + engine headroom
VOICE_COMFORTABLE_CORES = 4             # CPU-only STT parallelism floor
LOCAL_LLM_COMFORTABLE_RAM_BYTES = 16 * _GIB  # mid-size local models fit
LOCAL_LLM_SMALL_RAM_BYTES = 8 * _GIB         # small models fit comfortably


# ---------------------------------------------------------------------------
# Probe seams. Every external interaction goes through one of
# these three so tests can fake the machine without faking the OS.
# ---------------------------------------------------------------------------
def _package_installed(module: str) -> bool:
    """find_spec locates a package WITHOUT importing it."""
    return _package_state(module) is True


def _package_state(module: str) -> bool | None:
    """Tri-state package presence: True (found), False (not
    found — a determination), None (the check itself failed,
    so presence is honestly unknown)."""
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False
    except Exception:  # noqa: BLE001 - a broken import system is "unknown"
        return None


def _run_command(argv: list[str], timeout: float) -> str | None:
    """Run a probe command; stdout on success, None on any
    failure (missing binary, timeout, non-zero exit)."""
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout,
            check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _connect_error(host: str, port: int, timeout: float) -> BaseException | None:
    """None when a TCP connection was established, else the
    exception that prevented it. Lets the network probe tell a
    clean "nothing answered" (OSError) from a broken socket
    layer (anything else -> the state is honestly "unknown")."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return None
    except BaseException as exc:  # noqa: BLE001 - classified by the caller
        return exc


# ---------------------------------------------------------------------------
# Individual probes. Each returns its slice of the machine dict
# and swallows its own failures into None / "unknown" values.
# ---------------------------------------------------------------------------
def _probe_platform() -> dict[str, Any]:
    return {
        "system": platform.system() or None,
        "release": platform.release() or None,
        "architecture": platform.machine() or None,
        "python": platform.python_version(),
    }


def _cpu_model() -> str | None:
    system = platform.system()
    if system == "Linux":
        try:
            for line in Path("/proc/cpuinfo").read_text(
                    encoding="utf-8", errors="replace").splitlines():
                if line.lower().startswith("model name") and ":" in line:
                    return line.split(":", 1)[1].strip() or None
        except OSError:
            pass
    elif system == "Windows":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
            ) as key:
                name, _ = winreg.QueryValueEx(key, "ProcessorNameString")
                if name:
                    return str(name).strip()
        except OSError:
            pass
    elif system == "Darwin":
        out = _run_command(
            ["sysctl", "-n", "machdep.cpu.brand_string"],
            _SUBPROCESS_TIMEOUT_SECONDS)
        if out and out.strip():
            return out.strip()
    return platform.processor() or None


def _probe_cpu() -> dict[str, Any]:
    try:
        cores = os.cpu_count()
    except Exception:  # noqa: BLE001 - a probe must not raise
        cores = None
    try:
        model = _cpu_model()
    except Exception:  # noqa: BLE001 - a probe must not raise
        model = None
    return {"logical_cores": cores, "model": model}


def _ram_total_bytes() -> int | None:
    system = platform.system()
    if system == "Linux":
        try:
            for line in Path("/proc/meminfo").read_text(
                    encoding="utf-8", errors="replace").splitlines():
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
        except (OSError, ValueError, IndexError):
            pass
    elif system == "Windows":
        try:
            class _MemoryStatusEx(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = _MemoryStatusEx()
            status.dwLength = ctypes.sizeof(_MemoryStatusEx)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullTotalPhys)
        except Exception:  # noqa: BLE001 - a probe must not raise
            return None
    elif system == "Darwin":
        out = _run_command(
            ["sysctl", "-n", "hw.memsize"], _SUBPROCESS_TIMEOUT_SECONDS)
        if out:
            try:
                return int(out.strip())
            except ValueError:
                pass
    return None


def _probe_ram() -> dict[str, Any]:
    try:
        return {"total_bytes": _ram_total_bytes()}
    except Exception:  # noqa: BLE001 - a probe must not raise
        return {"total_bytes": None}


def _data_volume() -> Path:
    """The Aether data volume: ~/.aether when it exists (the
    stores live there), else the home directory it would be
    created on — disk space is judged for that volume."""
    data = Path.home() / ".aether"
    return data if data.exists() else Path.home()


def _probe_disk() -> dict[str, Any]:
    try:
        target = _data_volume()
        usage = shutil.disk_usage(target)
        return {
            "path": str(target),
            "total_bytes": usage.total,
            "free_bytes": usage.free,
        }
    except Exception:  # noqa: BLE001 - a probe must not raise
        return {"path": None, "total_bytes": None, "free_bytes": None}


def _probe_gpu() -> dict[str, Any] | None:
    """Best-effort discrete-GPU detection. Returns a dict when
    a GPU was identified, None when none was — None means "not
    detected by this probe" (an integrated GPU may still
    exist); the matrix words its reasons accordingly."""
    out = _run_command(
        ["nvidia-smi", "--query-gpu=name,memory.total",
         "--format=csv,noheader,nounits"],
        _SUBPROCESS_TIMEOUT_SECONDS)
    if out:
        first = out.strip().splitlines()[0] if out.strip() else ""
        parts = [p.strip() for p in first.split(",")]
        if parts and parts[0]:
            memory = None
            if len(parts) > 1:
                try:
                    memory = int(float(parts[1])) * 1024 * 1024  # MiB -> bytes
                except ValueError:
                    memory = None
            return {"name": parts[0], "vendor": "nvidia", "memory_bytes": memory}
    if platform.system() == "Windows":
        out = _run_command(
            ["wmic", "path", "win32_VideoController", "get", "name"],
            _SUBPROCESS_TIMEOUT_SECONDS)
        if out:
            names = [ln.strip() for ln in out.splitlines()
                     if ln.strip() and ln.strip().lower() != "name"]
            if names:
                return {"name": names[0], "vendor": None, "memory_bytes": None}
    return None


def _probe_microphone() -> dict[str, Any]:
    """Microphone presence. The microphone is captured by the
    desktop shell (WebRTC), not by the engine — the engine has
    no audio-input library of its own, so unless sounddevice
    happens to be importable the honest answer is "unknown",
    with the reason attached."""
    if not _package_installed("sounddevice"):
        return {
            "state": "unknown",
            "detail": ("sounddevice is not installed in the engine's "
                       "environment, and the microphone is captured by "
                       "the desktop app — the engine cannot see it."),
        }
    try:
        import sounddevice

        device = sounddevice.query_devices(kind="input")
        name = device.get("name") if isinstance(device, dict) else None
        return {"state": "present", "detail": name or "An input device answers."}
    except Exception:  # noqa: BLE001 - absence of a mic must not raise
        return {
            "state": "absent",
            "detail": "sounddevice is importable but reports no input device.",
        }


def _probe_network() -> dict[str, Any]:
    """Reachability of the provider endpoints, probed
    concurrently so the whole check costs one timeout, not
    three. Any success -> "online"; all failures through the
    socket layer (OSError) -> "offline"; a failure of the
    socket layer itself -> "unknown"."""
    with ThreadPoolExecutor(max_workers=len(_NETWORK_ENDPOINTS)) as pool:
        errors = list(pool.map(
            lambda ep: _connect_error(ep[0], ep[1], _NETWORK_TIMEOUT_SECONDS),
            _NETWORK_ENDPOINTS))
    reachable = [host for (host, _port), err
                 in zip(_NETWORK_ENDPOINTS, errors) if err is None]
    if reachable:
        return {"state": "online", "reachable": reachable}
    if all(isinstance(err, OSError) for err in errors):
        return {"state": "offline", "reachable": []}
    return {"state": "unknown", "reachable": []}


def _probe_ollama() -> bool | None:
    err = _connect_error(_OLLAMA_HOST, _OLLAMA_PORT, _LOCAL_TIMEOUT_SECONDS)
    if err is None:
        return True
    if isinstance(err, OSError):
        return False
    return None


def _playwright_browsers_dir() -> Path:
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if override:
        return Path(override)
    system = platform.system()
    if system == "Windows":
        local = os.environ.get("LOCALAPPDATA")
        base = Path(local) if local else Path.home() / "AppData" / "Local"
        return base / "ms-playwright"
    if system == "Darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    return Path.home() / ".cache" / "ms-playwright"


def _probe_voice_dependencies() -> dict[str, Any]:
    return {module: _package_state(module) for module in _VOICE_MODULES}


def _probe_desktop() -> dict[str, Any]:
    """Desktop-observe readiness: whether the UIA package the
    Windows backend needs is present (tri-state, find_spec —
    the package is located, never imported, by a probe)."""
    return {"uia_package": _package_state(_DESKTOP_UIA_MODULE)}


def _probe_browser() -> dict[str, Any]:
    installed = _package_state("playwright")
    browsers_found = False
    if installed:
        try:
            browsers_dir = _playwright_browsers_dir()
            browsers_found = browsers_dir.is_dir() and any(
                child.is_dir() and child.name.startswith("chromium")
                for child in browsers_dir.iterdir())
        except OSError:
            browsers_found = False
    return {"playwright_installed": installed, "chromium_found": browsers_found}


def probe_machine() -> dict[str, Any]:
    """Run every probe and return the machine dict. Always
    returns a complete dict: a probe that fails contributes
    its "unknown" values, never an exception."""
    def _isolated(fn, fallback):
        try:
            return fn()
        except Exception:  # noqa: BLE001 - isolation IS the contract
            return fallback

    return {
        "platform": _isolated(_probe_platform,
                              {"system": None, "release": None,
                               "architecture": None, "python": None}),
        "cpu": _isolated(_probe_cpu, {"logical_cores": None, "model": None}),
        "ram": _isolated(_probe_ram, {"total_bytes": None}),
        "disk": _isolated(_probe_disk,
                          {"path": None, "total_bytes": None, "free_bytes": None}),
        "gpu": _isolated(_probe_gpu, None),
        "microphone": _isolated(
            _probe_microphone,
            {"state": "unknown", "detail": "The microphone probe failed."}),
        "network": _isolated(_probe_network, {"state": "unknown", "reachable": []}),
        "ollama_reachable": _isolated(_probe_ollama, None),
        "voice_dependencies": _isolated(
            _probe_voice_dependencies,
            {module: None for module in _VOICE_MODULES}),
        "browser": _isolated(
            _probe_browser, {"playwright_installed": None, "chromium_found": False}),
        "desktop": _isolated(_probe_desktop, {"uia_package": None}),
    }


# ---------------------------------------------------------------------------
# The capability matrix — a pure function of the machine dict.
# ---------------------------------------------------------------------------
def _gb(value: int | None) -> str:
    if value is None:
        return "an unmeasured amount of"
    # One decimal, trimmed: "8" for exactly 8 GiB, "7.7" for
    # 7.7 GiB — a whole-number rounding made a 7.7 GiB machine
    # read as "8 GB (under the 8 GB line)".
    return f"{value / _GIB:.1f}".rstrip("0").rstrip(".")


def _verdict(feature_id: str, name: str, status: str, reason: str) -> dict[str, Any]:
    return {"id": feature_id, "name": name, "status": status, "reason": reason}


def _local_voice(machine: dict[str, Any]) -> dict[str, Any]:
    name = "Local voice (speech-to-text + text-to-speech)"
    deps = machine.get("voice_dependencies") or {}
    missing = [mod for mod in _VOICE_MODULES if deps.get(mod) is False]
    if missing:
        return _verdict(
            "local_voice", name, "unavailable",
            "The local voice stack is not installed (missing: "
            + ", ".join(missing)
            + ") — voice sessions refuse to start for exactly this "
              "reason today. Install it with: uv sync --extra voice.")
    unjudged = [mod for mod in _VOICE_MODULES if deps.get(mod) is None]
    if unjudged:
        return _verdict(
            "local_voice", name, "unknown",
            "Whether the voice stack's packages are installed could "
            "not be determined ("
            + ", ".join(unjudged)
            + "), so local voice is unjudged.")
    ram = (machine.get("ram") or {}).get("total_bytes")
    cores = (machine.get("cpu") or {}).get("logical_cores")
    gpu = machine.get("gpu")
    mic = machine.get("microphone") or {}
    if ram is not None and ram < VOICE_MIN_RAM_BYTES:
        return _verdict(
            "local_voice", name, "unavailable",
            f"This machine measured {_gb(ram)} GB of RAM — under the "
            f"{VOICE_MIN_RAM_BYTES // _GIB} GB the local speech models "
            "need just to load, before the engine's own footprint.")
    mic_note = ""
    if mic.get("state") == "unknown":
        mic_note = (" Microphone presence is unknown to the engine "
                    "(it is captured by the desktop app), so that part "
                    "is unjudged here.")
    if mic.get("state") == "absent":
        return _verdict(
            "local_voice", name, "degraded",
            "The speech stack is installed, but no microphone input "
            "device was found, so Aether could speak but not hear.")
    if gpu:
        return _verdict(
            "local_voice", name, "available",
            f"A GPU was detected ({gpu.get('name') or 'unnamed'}), so "
            "the local speech models can run accelerated instead of "
            "on CPU." + mic_note)
    # CPU-only speech: the one configuration the project has a
    # measurement for, and it is bad (see the thresholds comment).
    # CPU-only is therefore never "available" in this slice — it
    # is degraded when the hardware is measurable, unknown when
    # it is not; S15's measured step-down refines this verdict.
    concerns = []
    if cores is not None and cores < VOICE_COMFORTABLE_CORES:
        concerns.append(f"only {cores} logical CPU cores")
    if ram is not None and ram < VOICE_COMFORTABLE_RAM_BYTES:
        concerns.append(f"{_gb(ram)} GB of RAM (under the "
                        f"{VOICE_COMFORTABLE_RAM_BYTES // _GIB} GB comfort line)")
    if concerns:
        return _verdict(
            "local_voice", name, "degraded",
            "Speech runs on CPU here, and this machine is light for "
            "it: " + " and ".join(concerns) + ". Expect slow turns; "
            "a smaller speech-to-text model helps." + mic_note)
    if cores is None or ram is None:
        return _verdict(
            "local_voice", name, "unknown",
            "The speech stack is installed, but CPU cores and RAM "
            "could not both be measured, so CPU speech performance "
            "is unjudged." + mic_note)
    return _verdict(
        "local_voice", name, "degraded",
        "Speech runs on CPU here (no discrete GPU detected). On the "
        "build VM, CPU-only speech-to-text at the default 'small' "
        "model measured ~5.6 s per turn against a 200 ms budget — "
        "this machine is unmeasured, so expect slow turns until a "
        "per-machine measurement says otherwise; a smaller "
        "speech-to-text model helps." + mic_note)


def _local_models(machine: dict[str, Any]) -> dict[str, Any]:
    name = "Local LLM models (Ollama)"
    reachable = machine.get("ollama_reachable")
    if reachable is None:
        return _verdict(
            "local_models", name, "unknown",
            "Whether Ollama is answering at 127.0.0.1:11434 could "
            "not be determined, so local models are unjudged.")
    if not reachable:
        return _verdict(
            "local_models", name, "unavailable",
            "Ollama is not answering at 127.0.0.1:11434 — local "
            "models are served by Ollama, so none can run until it "
            "is started.")
    gpu = machine.get("gpu")
    ram = (machine.get("ram") or {}).get("total_bytes")
    if gpu:
        return _verdict(
            "local_models", name, "available",
            f"Ollama is running and a GPU was detected "
            f"({gpu.get('name') or 'unnamed'}), so local models can "
            "run accelerated.")
    if ram is not None and ram >= LOCAL_LLM_COMFORTABLE_RAM_BYTES:
        return _verdict(
            "local_models", name, "available",
            f"Ollama is running and this machine has {_gb(ram)} GB "
            "of RAM — enough for mid-size local models on CPU.")
    if ram is not None and ram >= LOCAL_LLM_SMALL_RAM_BYTES:
        return _verdict(
            "local_models", name, "degraded",
            f"Ollama is running, but {_gb(ram)} GB of RAM (and no "
            "detected GPU) limits local models to small ones, at "
            "CPU speeds.")
    if ram is not None:
        return _verdict(
            "local_models", name, "degraded",
            f"Ollama is running, but only {_gb(ram)} GB of RAM was "
            "measured — only the smallest local models fit "
            "comfortably, and they will be slow on CPU.")
    return _verdict(
        "local_models", name, "degraded",
        "Ollama is running, but RAM could not be measured, so "
        "local-model size headroom is unproven.")


def _cloud_providers(machine: dict[str, Any]) -> dict[str, Any]:
    name = "Cloud model providers"
    state = (machine.get("network") or {}).get("state")
    if state == "online":
        reachable = (machine.get("network") or {}).get("reachable") or []
        return _verdict(
            "cloud_providers", name, "available",
            "Provider endpoints are reachable from this machine ("
            + ", ".join(reachable) + ").")
    if state == "offline":
        return _verdict(
            "cloud_providers", name, "unavailable",
            "None of the provider endpoints (OpenAI, Anthropic, "
            "Google) could be reached — cloud models cannot be "
            "used until a network route exists.")
    return _verdict(
        "cloud_providers", name, "unknown",
        "Network reachability could not be determined, so cloud "
        "providers are unjudged.")


def _semantic_embeddings(machine: dict[str, Any]) -> dict[str, Any]:
    name = "Semantic memory embeddings"
    reachable = machine.get("ollama_reachable")
    if reachable is None:
        return _verdict(
            "semantic_embeddings", name, "unknown",
            "Whether Ollama is answering at 127.0.0.1:11434 could "
            "not be determined, so embedding-based recall is "
            "unjudged.")
    if not reachable:
        return _verdict(
            "semantic_embeddings", name, "unavailable",
            "Ollama is not answering at 127.0.0.1:11434, where the "
            "embedding model is served — semantic (meaning-based) "
            "memory recall is off; keyword recall still works.")
    return _verdict(
        "semantic_embeddings", name, "available",
        "Ollama is answering at 127.0.0.1:11434, where the "
        "embedding model (qwen3-embedding:0.6b) is served.")


def _browser_automation(machine: dict[str, Any]) -> dict[str, Any]:
    name = "Browser automation"
    browser = machine.get("browser") or {}
    if browser.get("playwright_installed") is None:
        return _verdict(
            "browser_automation", name, "unknown",
            "Whether the playwright package is installed could "
            "not be determined, so browser automation is "
            "unjudged.")
    if not browser.get("playwright_installed"):
        return _verdict(
            "browser_automation", name, "unavailable",
            "The playwright package is not installed in the "
            "engine's environment — the browser agent cannot "
            "start until it is.")
    if browser.get("chromium_found"):
        return _verdict(
            "browser_automation", name, "available",
            "Playwright is installed and a Chromium build was "
            "found in its browsers directory.")
    return _verdict(
        "browser_automation", name, "degraded",
        "Playwright is installed, but no Chromium build was found "
        "in the default browsers directory — the first browser "
        "task will fail until one is installed "
        "(playwright install chromium). A custom "
        "PLAYWRIGHT_BROWSERS_PATH location is checked when set.")


def _desktop_control(machine: dict[str, Any]) -> dict[str, Any]:
    name = "Desktop control (Computer Use)"
    system = (machine.get("platform") or {}).get("system")
    if system == "Windows":
        # S19: the verdict goes live for OBSERVE — desktop
        # observe (reading the foreground window's accessibility
        # tree as text, when the user enables it) is built; what
        # it still cannot do is ACT (click/type), which is S20.
        # The remaining gate is the UIA package the backend
        # reads through, probed like every other dependency.
        # S20: actuation is now built too — UIA invoke/set-value
        # by element, window and clipboard primitives, vision
        # grounding and the pixel fallback, all behind the
        # Computer Use switches (master + Control + the one-time
        # acknowledgement) and the desktop confirmation policy.
        # The verdict speaks for the MACHINE's readiness, so the
        # reason names the switches as the remaining gate.
        uia = (machine.get("desktop") or {}).get("uia_package")
        if uia is True:
            return _verdict(
                "desktop_control", name, "available",
                "Desktop observe and actuation are built on this "
                "platform (Windows UI Automation): with Computer "
                "Use enabled in Settings, Aether can read the "
                "foreground window's accessibility tree as text "
                "when a task asks, and — with Control on and "
                "acknowledged — click and type through the "
                "desktop confirmation policy (closing/quitting "
                "and system actions always confirm).")
        if uia is False:
            return _verdict(
                "desktop_control", name, "degraded",
                "Desktop observe and actuation are built on this "
                "platform (Windows UI Automation), but the "
                "'uiautomation' package is not installed in the "
                "engine's environment, so the accessibility tree "
                "cannot be read or actuated until it is.")
        return _verdict(
            "desktop_control", name, "unknown",
            "Desktop observe is built on this platform (Windows "
            "UI Automation), but whether the 'uiautomation' "
            "package is installed could not be determined, so "
            "readiness is unjudged.")
    if system:
        return _verdict(
            "desktop_control", name, "unavailable",
            f"Desktop control is built on Windows UI Automation; "
            f"{system} is not a supported platform for it.")
    return _verdict(
        "desktop_control", name, "unknown",
        "The operating system could not be identified, so "
        "desktop-control platform support is unjudged.")


def capability_matrix(machine: dict[str, Any]) -> list[dict[str, Any]]:
    """PURE: probe dict -> per-feature verdicts. No I/O, no
    clock, no randomness — the same machine always yields the
    same verdicts, which is what makes the matrix testable
    with synthetic machines."""
    return [
        _local_voice(machine),
        _local_models(machine),
        _cloud_providers(machine),
        _semantic_embeddings(machine),
        _browser_automation(machine),
        _desktop_control(machine),
    ]


# ---------------------------------------------------------------------------
# The report: probe + matrix, behind a TTL cache, persisted to
# ~/.aether/machine_capability.json so the stored matrix the
# rest of the programme reads survives a restart (its age is
# always visible via probed_at, and a copy older than the TTL
# is never served as current).
# ---------------------------------------------------------------------------
_REPORT_PATH = Path.home() / ".aether" / "machine_capability.json"
_cache_lock = threading.Lock()
_cached_report: dict[str, Any] | None = None


def _report_age(report: dict[str, Any], now: float) -> float | None:
    probed_at = report.get("probed_at")
    if not isinstance(probed_at, (int, float)):
        return None
    return now - float(probed_at)


def _persist(report: dict[str, Any]) -> None:
    """Best-effort atomic write of the stored matrix. A failed
    write loses the record, never the answer."""
    try:
        _REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _REPORT_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(report), encoding="utf-8")
        os.replace(tmp, _REPORT_PATH)
    except OSError:
        pass


def _load_persisted() -> dict[str, Any] | None:
    try:
        data = json.loads(_REPORT_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if not isinstance(data.get("machine"), dict):
        return None
    if not isinstance(data.get("features"), list):
        return None
    return data


def build_report() -> dict[str, Any]:
    """Probe now and build the full report (no caching)."""
    machine = probe_machine()
    return {
        "probed_at": time.time(),
        "machine": machine,
        "features": capability_matrix(machine),
    }


def machine_capability_report(force: bool = False) -> dict[str, Any]:
    """The full report for the WS surface and in-process
    readers. Serves the in-memory cache, then the persisted
    copy, while either is younger than REPORT_TTL_SECONDS;
    re-probes when stale or when ``force`` is set. The
    returned dict carries ``cached`` so a reader can tell a
    fresh measurement from a served copy."""
    global _cached_report
    now = time.time()
    with _cache_lock:
        if not force and _cached_report is not None:
            age = _report_age(_cached_report, now)
            if age is not None and age < REPORT_TTL_SECONDS:
                return {**_cached_report, "cached": True}
        if not force and _cached_report is None:
            persisted = _load_persisted()
            if persisted is not None:
                age = _report_age(persisted, now)
                if age is not None and age < REPORT_TTL_SECONDS:
                    _cached_report = persisted
                    return {**persisted, "cached": True}
        report = build_report()
        _cached_report = report
    _persist(report)
    return {**report, "cached": False}
