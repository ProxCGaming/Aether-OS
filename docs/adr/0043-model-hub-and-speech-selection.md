# ADR 0043: Model Hub and Speech Selection

## Status
Accepted

## Date
2026-11-02

## Context
Speech models were not configuration at all. The STT model name
`"small"` was the constructor default at all three speech-engine
construction sites — dictation/read-aloud
(`aether_engine/routes/websocket.py`), the voice-mode pipeline
(`aether_engine/voice/pipeline.py`), and the media analyser's
transcripts (`aether_engine/media/analyzer.py`) — and no call site
passed anything else. Speech-model downloads were implicit
first-use fetches by the underlying libraries: no consent, no
progress, no way to see or remove what had arrived. The Local
Models tab served only Ollama LLM brains, from a static catalogue
shipped with the app.

The owner's ask started as a speech-model picker and grew, through
the grill of 2 Nov 2026
(`speech-model-picker-grill-20261102.md`), into a single model
hub: one place that browses both model sources, says honestly how
each model runs, downloads the chosen variant, and feeds the
app's existing model settings. The build landed as six slices on
`build/full-build`: catalogue layer (`607242f`), download routing
(`05cef26`), speech configuration (`23bebff`), the hub UI in the
Local Models tab (`d2786c4`), speech selection surfaces
(`b47ee74`), and this record (final slice).

## Decision
1. **Sources are bounded: Hugging Face + the Ollama library
   only.** Browsing/search binds the live public Hugging Face API
   and the Ollama library; the local runtime is Ollama only. A
   broader multi-engine scope was considered during the grill and
   explicitly declined by the owner — no other inference engines
   are integrated.
2. **Every model carries a runnability label, computed, never
   guessed.** Each hub entry is labelled **in-app** (runs inside
   the engine, the speech families), **via Ollama** (GGUF
   brains), **not locally runnable**, or **unknown**, derived
   from the model's family and file formats. Safetensors-only
   brains are labelled not locally runnable (no serving stack in
   the app runs them), and speech-family GGUF files are never
   labelled Ollama-runnable. Where the evidence does not decide,
   the label is `unknown`, not a guess.
3. **Licences come from source metadata, or are absent.**
   Licence is surfaced where the source provides it (Hugging
   Face metadata); where a source carries none — the shipped
   Ollama catalogue has no licence data — the licence is null
   rather than inferred.
4. **Downloads route by the label.** A GGUF variant downloads
   through Ollama as an `hf.co/<repo>:<quant>` pull; a speech
   repo downloads file-by-file into the Hugging Face cache the
   speech libraries already read. Each store runs one download
   at a time and refuses a second with a busy answer. Hub
   downloads are user-initiated tasks of their own: they never
   cancel, pre-empt, or share fate with agent tasks.
5. **Speech configuration is three strict fields.**
   `speech_stt_model` is shared by dictation and voice mode;
   `transcription_stt_model` is a separate field for the media
   analyser (the owner's decision — transcription does not
   follow the voice picker); `speech_tts_voice` selects the TTS
   voice. All three are validated offline at write time against
   what the corresponding library can actually load, and refusal
   is atomic: an invalid value rejects the write instead of
   being stored to fail later.
6. **The dictation cache is keyed by the configured model.**
   Dictation/read-aloud engines are cached per connection; the
   cache key now includes the configured STT model, so changing
   the setting cannot silently leave dictation running the
   previously loaded model until reconnect.
7. **Apply semantics differ for ears and mouth.** An STT model
   change takes effect at the next voice session — the live
   session finishes on the model it started with, and the UI
   says so. A TTS voice change applies per sentence, so it is
   live immediately.
8. **Voice selection is capability-driven.** What control a
   speech model offers depends on the model: premade voice lists
   get a dropdown (built, exercised by Kokoro), reference-audio
   models get a reference-file shape, and fixed-voice models
   show no control. The hub metadata carries the capability from
   the start; only the premade dropdown is built in this
   programme — the other shapes become real when such a model is
   added, without redesign.
9. **Selection lives in Settings → Models & Tasks; there is no
   HUD surface.** Download management lives in the hub (the
   Local Models tab); which model serves which job is chosen in
   the model settings beside the auxiliary-models card. The
   owner's earlier Main HUD idea was withdrawn by the owner
   during the grill and was not built.
10. **The Local Models tab's destination-folder picker is
    removed.** Hub downloads have no destination concept — Ollama
    pulls land in Ollama's store and speech files in the HF
    cache — so the picker had nothing left to set.

## Consequences
- Three divergent Ollama catalogues exist in the codebase. The
  hub reads the shipped JSON catalogue, mirror-pinned in code;
  consolidating the three into one source is future work, not
  part of this programme.
- Cancelling an HF speech download lands between files: the
  download stops at a file boundary, and a partially fetched
  repo can remain in the HF cache until removed.
- A speech value hand-edited into the config file bypasses the
  write-time validators; at load it fails validation and the
  engine falls back to the default rather than passing the
  invalid value to faster-whisper.
