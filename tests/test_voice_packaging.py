"""Voice runtime packaging (Epic 8 follow-up g + S5-M7): the heavy
voice/media stack lives in the ``voice`` optional extra, not in the
hard dependencies, and the engine fails closed without it.

History: follow-up (g) first declared the voice stages' packages so
a standard install could run them at all. S5-M7 (Wave D, mirroring
F-16's ``legacy-ui`` retirement) then moved the heavy stack —
faster-whisper, kokoro, onnxruntime, silero-vad — out of the hard
``[project] dependencies`` into ``[project.optional-dependencies]
voice`` (install: ``uv sync --extra voice``), because every install
paid for them despite the stages' lazy/guarded imports. ``numpy``
and ``aiortc`` stay hard dependencies: they are out of S5-M7's
scope by the task's explicit decision.

These tests pin the packaging contract:

  * the four S5-M7 packages are NOT hard dependencies and ARE in
    the ``voice`` extra; numpy / aiortc remain hard dependencies;
  * every distribution the code probes for is declared somewhere
    (hard deps or the voice extra), under the name mapping the code
    expects (distribution -> import name);
  * the engine and every voice module import cleanly with the four
    packages blocked, and the stages then report themselves
    unavailable (fail closed) instead of crashing;
  * in a synced environment *with* the extra, the import names
    resolve and the real stages report themselves available —
    package presence is the whole of their probe (these tests skip
    when the extra is not installed, so a plain ``uv sync`` run of
    this file stays green);
  * absence still fails closed honestly: with the packages hidden
    from find_spec (the stages' only probe), the factory error names
    every missing piece;
  * and the SmallWebRTC transport's availability is the aiortc
    probe (F-12a): with aiortc installed the transport reports
    available, and with it hidden it names aiortc as missing.

No models are loaded and nothing touches the network: availability
probes are find_spec-only by design.
"""
import importlib.util
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from aether_engine.voice.pipeline import (
    WebRtcVoiceTransport,
    create_voice_pipeline,
)
from aether_engine.voice.stt import FasterWhisperStt
from aether_engine.voice.tts import KokoroTts
from aether_engine.voice.vad import SileroVad

# Distribution name (PEP 503 normalised) -> the import name the code
# probes for with find_spec.
VOICE_DEPENDENCIES = {
    "faster-whisper": "faster_whisper",
    "kokoro": "kokoro",
    "onnxruntime": "onnxruntime",
    "numpy": "numpy",
    "silero-vad": "silero_vad",
    "aiortc": "aiortc",
}

# S5-M7: the heavy stack that must live only in the voice extra.
VOICE_EXTRA_DEPENDENCIES = {
    "faster-whisper": "faster_whisper",
    "kokoro": "kokoro",
    "onnxruntime": "onnxruntime",
    "silero-vad": "silero_vad",
}

# Out of S5-M7's scope: these stay hard dependencies.
VOICE_HARD_DEPENDENCIES = {
    "numpy": "numpy",
    "aiortc": "aiortc",
}

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"
REPO_ROOT = PYPROJECT.parent


def _dep_name(spec: str) -> str:
    match = re.match(r"\s*([A-Za-z0-9._-]+)", spec)
    assert match is not None, f"unparseable dependency spec: {spec!r}"
    return re.sub(r"[-_.]+", "-", match.group(1)).lower()


def _project_data() -> dict:
    with PYPROJECT.open("rb") as handle:
        return tomllib.load(handle)["project"]


def _hard_dependency_names() -> set[str]:
    project = _project_data()
    return {_dep_name(entry) for entry in project.get("dependencies", [])}


def _extra_dependency_names(extra: str) -> set[str]:
    project = _project_data()
    extras = project.get("optional-dependencies", {})
    return {_dep_name(entry) for entry in extras.get(extra, [])}


def _declared_dependency_names() -> set[str]:
    """Every declared distribution: hard deps plus all extras."""
    project = _project_data()
    names = {_dep_name(entry) for entry in project.get("dependencies", [])}
    for entries in project.get("optional-dependencies", {}).values():
        names.update(_dep_name(entry) for entry in entries)
    return names


def _voice_extra_installed() -> bool:
    return all(
        importlib.util.find_spec(import_name) is not None
        for import_name in VOICE_EXTRA_DEPENDENCIES.values()
    )


async def _talker(text: str):
    yield text


async def _emit(event) -> None:
    pass


def _hide_voice_imports(monkeypatch) -> None:
    """Make find_spec report every voice package as missing,
    delegating all other lookups to the real find_spec."""
    hidden = set(VOICE_DEPENDENCIES.values())
    real_find_spec = importlib.util.find_spec

    def hidden_find_spec(name, package=None):
        if name in hidden:
            return None
        return real_find_spec(name, package)

    monkeypatch.setattr(importlib.util, "find_spec", hidden_find_spec)


class TestPackagingDeclarations:
    def test_voice_extra_deps_are_not_hard_dependencies(self):
        hard = _hard_dependency_names()
        leaked = set(VOICE_EXTRA_DEPENDENCIES) & hard
        assert not leaked, (
            f"voice-extra dependencies still hard runtime deps: "
            f"{sorted(leaked)}")

    def test_voice_extra_deps_live_in_the_voice_extra(self):
        project = _project_data()
        extras = project.get("optional-dependencies", {})
        assert "voice" in extras, "pyproject.toml has no voice extra"
        extra = _extra_dependency_names("voice")
        missing = set(VOICE_EXTRA_DEPENDENCIES) - extra
        assert not missing, (
            f"voice dependencies missing from the voice extra: "
            f"{sorted(missing)}")

    def test_voice_hard_deps_remain_hard_dependencies(self):
        hard = _hard_dependency_names()
        missing = set(VOICE_HARD_DEPENDENCIES) - hard
        assert not missing, (
            f"numpy/aiortc must stay hard dependencies (out of "
            f"S5-M7 scope), missing: {sorted(missing)}")

    def test_voice_runtime_dependencies_are_declared(self):
        declared = _declared_dependency_names()
        missing = set(VOICE_DEPENDENCIES) - declared
        assert not missing, (
            f"voice runtime deps missing from pyproject (hard deps "
            f"or the voice extra): {sorted(missing)}")

    def test_voice_hard_import_names_resolve(self):
        for import_name in VOICE_HARD_DEPENDENCIES.values():
            assert importlib.util.find_spec(import_name) is not None, (
                f"{import_name} is a hard dependency but not "
                f"importable in this environment (uv sync not run?)")

    def test_voice_extra_import_names_resolve_when_installed(self):
        if not _voice_extra_installed():
            pytest.skip(
                "voice extra not installed (uv sync --extra voice) — "
                "extra import names cannot resolve in this environment")
        for import_name in VOICE_EXTRA_DEPENDENCIES.values():
            assert importlib.util.find_spec(import_name) is not None


class TestEngineImportsWithoutVoiceExtra:
    def test_engine_imports_with_voice_stack_blocked(self):
        """Mirror of the legacy-ui blocked-import test: with the
        four voice packages made unimportable, the engine and the
        voice modules still import, and every stage reports an
        honest unavailable reason instead of crashing."""
        blocked = sorted(VOICE_EXTRA_DEPENDENCIES.values())
        script = (
            "import sys\n"
            f"for _name in {blocked!r}:\n"
            "    sys.modules[_name] = None\n"
            "import aether_engine\n"
            "import aether_engine.voice\n"
            "import aether_engine.voice.pipeline\n"
            "import aether_engine.voice.rtc\n"
            "import aether_engine.voice.stt\n"
            "import aether_engine.voice.tts\n"
            "import aether_engine.voice.vad\n"
            "from aether_engine.voice.stt import FasterWhisperStt\n"
            "from aether_engine.voice.tts import KokoroTts\n"
            "from aether_engine.voice.vad import SileroVad\n"
            "assert FasterWhisperStt().unavailable_reason() == "
            "'faster-whisper is not installed (local STT)'\n"
            "assert KokoroTts().unavailable_reason() == "
            "'kokoro is not installed (local TTS)'\n"
            "assert SileroVad().unavailable_reason() == "
            "'onnxruntime is not installed (Silero VAD runtime)'\n"
            "print('blocked-import check OK')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            check=False,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert result.returncode == 0, (
            "engine / voice modules failed to import with the voice "
            f"stack blocked:\n{result.stdout}\n{result.stderr}")

    def test_stages_report_unavailable_when_extra_absent(self):
        """In a real no-extra environment (no mocking) the stages
        fail closed. Skipped when the extra IS installed — the
        present-case is covered by TestAvailabilityWithDepsPresent."""
        if _voice_extra_installed():
            pytest.skip("voice extra installed in this environment")
        assert FasterWhisperStt().unavailable_reason() == (
            "faster-whisper is not installed (local STT)")
        assert KokoroTts().unavailable_reason() == (
            "kokoro is not installed (local TTS)")
        assert SileroVad().unavailable_reason() == (
            "onnxruntime is not installed (Silero VAD runtime)")


class TestAvailabilityWithDepsPresent:
    def test_real_stages_report_available(self):
        """Package presence is the whole probe: with the voice
        extra installed, none of the model stages may cry missing."""
        if not _voice_extra_installed():
            pytest.skip(
                "voice extra not installed (uv sync --extra voice)")
        assert SileroVad().unavailable_reason() is None
        assert FasterWhisperStt().unavailable_reason() is None
        assert KokoroTts().unavailable_reason() is None

    def test_webrtc_transport_available_with_aiortc(self):
        """F-12a built the negotiation endpoint: with aiortc
        installed the transport reports available (its media proof
        lives in test_voice_rtc.py's loopback tests)."""
        assert WebRtcVoiceTransport().unavailable_reason() is None

    def test_webrtc_transport_names_aiortc_when_missing(self, monkeypatch):
        _hide_voice_imports(monkeypatch)
        assert WebRtcVoiceTransport().unavailable_reason() == (
            "aiortc is not installed (SmallWebRTC transport)")

    def test_default_factory_builds_with_all_deps_present(self):
        """With packages present, the default factory builds a
        pipeline — construction opens nothing (models and the peer
        connection load in start())."""
        if not _voice_extra_installed():
            pytest.skip(
                "voice extra not installed (uv sync --extra voice)")
        pipeline, error = create_voice_pipeline(
            session_id="s", talker=_talker, emit=_emit)
        assert error is None
        assert pipeline is not None


class TestAvailabilityWithDepsAbsent:
    def test_factory_fails_closed_naming_every_missing_package(self, monkeypatch):
        _hide_voice_imports(monkeypatch)
        pipeline, error = create_voice_pipeline(
            session_id="s", talker=_talker, emit=_emit)
        assert pipeline is None
        assert error is not None and error.startswith("voice unavailable:")
        for fragment in (
            "onnxruntime is not installed",
            "faster-whisper is not installed",
            "kokoro is not installed",
            "aiortc is not installed",
        ):
            assert fragment in error
