"""FF3-01 — the Kokoro pipeline must be built with the selected
voice's lang_code, at every construction path.

Before the fix, KokoroTts built ONE pipeline with the default
``lang_code="a"`` and spoke every voice through it: the British
and Hindi voices the picker offers were phonemised with American
English rules. These tests pin the corrected behaviour with a
stub ``kokoro`` module (the real one is a voice extra):

- a voice id's first letter is its lang_code;
- synthesize() loads (and caches) the pipeline for the voice's
  language, not the engine default;
- a voice whose language pack cannot load fails honestly with
  VoiceUnavailableError — never silently spoken through the
  wrong language's pipeline.
"""

from __future__ import annotations

import importlib.machinery
import sys
import types

import pytest

from aether_engine.voice import VoiceUnavailableError
from aether_engine.voice.tts import KokoroTts, lang_code_for_voice


@pytest.fixture
def fake_kokoro(monkeypatch):
    """A stub kokoro module recording every KPipeline build."""
    built: list[str] = []
    fail_langs: set[str] = set()

    class FakePipeline:
        def __init__(self, lang_code: str):
            if lang_code in fail_langs:
                raise RuntimeError(f"no voice pack for {lang_code}")
            self.lang_code = lang_code
            built.append(lang_code)

        def __call__(self, text, voice=None):
            yield ("g", "p", [0.0, 0.05, -0.05])

    module = types.ModuleType("kokoro")
    module.__spec__ = importlib.machinery.ModuleSpec("kokoro", loader=None)
    module.KPipeline = FakePipeline
    monkeypatch.setitem(sys.modules, "kokoro", module)
    return {"built": built, "fail_langs": fail_langs}


@pytest.mark.parametrize(
    ("voice", "expected"),
    [
        ("af_heart", "a"),
        ("am_michael", "a"),
        ("bf_emma", "b"),
        ("bm_george", "b"),
        ("hf_alpha", "h"),
        ("hm_omega", "h"),
        ("ef_dora", "e"),
        ("jf_alpha", "j"),
        ("zf_xiaobei", "z"),
    ],
)
def test_lang_code_comes_from_the_voice_id(voice, expected):
    assert lang_code_for_voice(voice) == expected


@pytest.mark.parametrize("voice", ["", "heart", "xx_nope", None, 42])
def test_invalid_voice_falls_back_to_default_language(voice):
    assert lang_code_for_voice(voice) == "a"


async def _drain(tts, text, voice):
    return [chunk async for chunk in tts.synthesize(text, voice=voice)]


async def test_british_voice_builds_a_british_pipeline(fake_kokoro):
    tts = KokoroTts()
    await tts.open()
    assert fake_kokoro["built"] == ["a"]  # the default, from open()

    chunks = await _drain(tts, "Good evening.", "bf_emma")
    assert chunks  # audio flowed
    assert fake_kokoro["built"] == ["a", "b"]


async def test_same_language_voices_reuse_the_pipeline(fake_kokoro):
    tts = KokoroTts()
    await tts.open()
    await _drain(tts, "Hello.", "af_heart")
    await _drain(tts, "Hello again.", "am_michael")
    assert fake_kokoro["built"] == ["a"]  # built once, at open()


async def test_unloadable_voice_pack_fails_honestly(fake_kokoro):
    fake_kokoro["fail_langs"].add("h")
    tts = KokoroTts()
    await tts.open()
    with pytest.raises(VoiceUnavailableError, match="lang_code 'h'"):
        await _drain(tts, "Namaste.", "hf_alpha")
    # No fallback: the American pipeline was never asked to speak it.
    assert fake_kokoro["built"] == ["a"]


async def test_synthesize_before_open_still_refused(fake_kokoro):
    tts = KokoroTts()
    with pytest.raises(VoiceUnavailableError, match="before open"):
        await _drain(tts, "Hello.", "af_heart")
