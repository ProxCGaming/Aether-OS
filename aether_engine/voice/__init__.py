"""Streaming voice conversation (Epic 8, E8-5.1a).

Voice mode is fast chat with ears and a mouth: an owned cascade —
Silero VAD + endpointing (``vad.py``) -> local STT (``stt.py``) ->
the fast-chat talker -> local Kokoro TTS (``tts.py``) — coordinated
by ``pipeline.VoicePipeline``. STT and TTS are local; the brain is
the already-configured chat model behind ``run_fast_chat``.

Every stage sits behind a small protocol and lazy-imports its
runtime dependency, so this package imports cleanly on machines
without any voice packages installed: a stage whose dependency is
missing reports ``unavailable_reason()`` and the session refuses to
start with an honest "voice unavailable: <reason>" instead of
crashing — and no audio device, model, or transport is touched
before a session actually starts.
"""


class VoiceUnavailableError(RuntimeError):
    """A voice stage or transport cannot run on this machine.

    The message always names the missing piece; session start
    surfaces it verbatim after the "voice unavailable: " prefix.
    """


class VoiceTalkerError(RuntimeError):
    """The fast-chat talker failed a voice turn (TASK_FAILED)."""


__all__ = ["VoiceTalkerError", "VoiceUnavailableError"]
