"""Streaming voice conversation (Epic 8, E8-5.1a + E8-5.1b).

Voice mode is fast chat with ears and a mouth: an owned cascade —
Silero VAD + endpointing (``vad.py``) -> local STT (``stt.py``) ->
the fast-chat talker -> local Kokoro TTS (``tts.py``) — coordinated
by ``pipeline.VoicePipeline``. STT and TTS are local; the brain is
the already-configured chat model behind ``run_fast_chat``.
E8-5.1b adds full-duplex barge-in (playback stops at once, only
actually-heard words are committed) and supervisor escalation:
work turns launch background tasks from the voice session, and a
finished task's summary is spoken back as an announcement.

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
