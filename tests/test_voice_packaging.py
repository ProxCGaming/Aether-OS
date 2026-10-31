"""Voice runtime packaging (Epic 8 follow-up g): the dependencies
the voice stages probe for are declared in pyproject.toml, so a
standard install can actually run them.

Before this slice the stages were written against optional packages
that nothing declared — every machine reported "voice unavailable:
... is not installed". These tests pin the packaging contract:

  * every distribution the code probes for is a declared project
    dependency, under the name mapping the code expects
    (distribution -> import name);
  * in a synced environment the import names resolve and the real
    stages report themselves available — package presence is the
    whole of their probe, so this is the deps-declared availability
    logic, exercised end to end without loading any model;
  * absence still fails closed honestly: with the packages hidden
    from find_spec (the stages' only probe), the factory error names
    every missing piece;
  * and no placebo: aiortc being installed does NOT make the
    SmallWebRTC transport claim availability — the engine still has
    no RTC negotiation endpoint (no signalling surface exists in
    the contracts or the desktop), and the transport keeps saying
    exactly that.

No models are loaded and nothing touches the network: availability
probes are find_spec-only by design.
"""
import importlib.util
import re
import tomllib
from pathlib import Path

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

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _declared_dependency_names() -> set[str]:
    with PYPROJECT.open("rb") as handle:
        project = tomllib.load(handle)["project"]
    names = set()
    for entry in project.get("dependencies", []):
        match = re.match(r"\s*([A-Za-z0-9._-]+)", entry)
        if match:
            names.add(re.sub(r"[-_.]+", "-", match.group(1)).lower())
    return names


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
    def test_voice_runtime_dependencies_are_declared(self):
        declared = _declared_dependency_names()
        missing = set(VOICE_DEPENDENCIES) - declared
        assert not missing, (
            f"voice runtime deps missing from pyproject dependencies: "
            f"{sorted(missing)}")

    def test_voice_import_names_resolve(self):
        for import_name in VOICE_DEPENDENCIES.values():
            assert importlib.util.find_spec(import_name) is not None, (
                f"{import_name} is declared but not importable in this "
                f"environment (uv sync not run?)")


class TestAvailabilityWithDepsPresent:
    def test_real_stages_report_available(self):
        """Package presence is the whole probe: with the declared
        deps installed, none of the model stages may cry missing."""
        assert SileroVad().unavailable_reason() is None
        assert FasterWhisperStt().unavailable_reason() is None
        assert KokoroTts().unavailable_reason() is None

    def test_webrtc_transport_stays_honest_about_the_missing_endpoint(self):
        """aiortc installed is not an RTC endpoint: the transport
        must keep reporting the real blocker, verbatim."""
        assert WebRtcVoiceTransport().unavailable_reason() == (
            "SmallWebRTC negotiation endpoint is not available in this build")

    def test_default_factory_is_blocked_only_by_the_transport(self):
        """With packages present, the ONLY reason a default voice
        session cannot start is the unbuilt negotiation endpoint —
        no stage may report a missing package any more."""
        pipeline, error = create_voice_pipeline(
            session_id="s", talker=_talker, emit=_emit)
        assert pipeline is None
        assert error is not None and error.startswith("voice unavailable:")
        assert "negotiation endpoint" in error
        assert "not installed" not in error


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
