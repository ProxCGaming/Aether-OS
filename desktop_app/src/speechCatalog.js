// The speech picker's shared catalog: the Kokoro voices Aether
// offers (with the one-line character description each row
// shows), the built-in faster-whisper sizes with their download
// size and speed/accuracy hint, the speech-language list and
// the speech-speed presets.
//
// One home for this data because two surfaces render it: the
// Settings → Voice Speech card (the full picker) and the voice
// overlay (which names the voice + model in effect). The values
// mirror the engine's vocabularies — voice ids are validated
// engine-side against the Kokoro id pattern, languages against
// aether_engine/voice/stt.py STT_LANGUAGES, speeds against
// TTS_SPEED_PRESETS — so a value offered here is a value the
// engine will accept.

export const KOKORO_VOICE_GROUPS = [
  {
    group: 'American English',
    chip: 'American',
    items: [
      { value: 'af_heart', name: 'Heart', description: "Warm, friendly American female — Aether's default voice." },
      { value: 'af_bella', name: 'Bella', description: 'Soft, gentle American female.' },
      { value: 'af_sarah', name: 'Sarah', description: 'Clear, steady American female.' },
      { value: 'af_nicole', name: 'Nicole', description: 'Calm, relaxed American female.' },
      { value: 'am_adam', name: 'Adam', description: 'Deep, confident American male.' },
      { value: 'am_michael', name: 'Michael', description: 'Warm, easy-going American male.' },
    ],
  },
  {
    group: 'British English',
    chip: 'British',
    items: [
      { value: 'bf_emma', name: 'Emma', description: 'Bright, friendly British female.' },
      { value: 'bf_isabella', name: 'Isabella', description: 'Polished, composed British female.' },
      { value: 'bm_george', name: 'George', description: 'Warm, measured British male.' },
      { value: 'bm_lewis', name: 'Lewis', description: 'Clear, calm British male.' },
    ],
  },
  {
    group: 'Hindi',
    chip: 'Hindi',
    items: [
      { value: 'hf_alpha', name: 'Alpha', description: 'Clear Hindi female.' },
      { value: 'hf_beta', name: 'Beta', description: 'Warm Hindi female.' },
      { value: 'hm_omega', name: 'Omega', description: 'Deep Hindi male.' },
      { value: 'hm_psi', name: 'Psi', description: 'Steady Hindi male.' },
    ],
  },
];

const VOICE_BY_ID = new Map(
  KOKORO_VOICE_GROUPS.flatMap((g) => g.items.map((item) => [item.value, item])),
);

// The display name for a voice id: the catalog name when the
// voice is a built-in, else a name derived from the id itself
// (Kokoro ids are <prefix>_<name>, so the name is in the id).
export function kokoroVoiceDisplayName(voiceId) {
  if (!voiceId) return '';
  const known = VOICE_BY_ID.get(voiceId);
  if (known) return known.name;
  const raw = String(voiceId).split('_').slice(1).join('_') || String(voiceId);
  return raw.charAt(0).toUpperCase() + raw.slice(1);
}

export function kokoroVoiceDescription(voiceId) {
  const known = VOICE_BY_ID.get(voiceId);
  return known ? known.description : null;
}

// The sensible faster-whisper sizes for a settings select, with
// the approximate download size and a plain speed/accuracy hint.
// The engine validator accepts more (the .en and distil
// variants, any faster-whisper-* HF repo id); those arrive as
// downloaded hub repos or as the preserved current value.
export const BUILTIN_STT_MODELS = ['tiny', 'base', 'small', 'medium', 'large-v3', 'turbo'];

export const STT_MODEL_INFO = {
  tiny: { size: '~75 MB', hint: 'fastest, least accurate' },
  base: { size: '~140 MB', hint: 'fast, basic accuracy' },
  small: { size: '~460 MB', hint: 'balanced speed and accuracy' },
  medium: { size: '~1.5 GB', hint: 'slower, more accurate' },
  'large-v3': { size: '~2.9 GB', hint: 'slowest, most accurate' },
  turbo: { size: '~800 MB', hint: 'fast, near-large accuracy' },
};

// The dropdown label for a built-in size: name, size, hint.
export function sttModelLabel(model) {
  const info = STT_MODEL_INFO[model];
  if (!info) return model;
  return `${model} — ${info.size} · ${info.hint}`;
}

// Speech languages the picker offers ('auto' = the model
// detects). Mirrors the engine's STT_LANGUAGES vocabulary.
export const STT_LANGUAGE_OPTIONS = [
  { value: 'auto', label: 'Auto (detect language)' },
  { value: 'en', label: 'English' },
  { value: 'hi', label: 'Hindi' },
  { value: 'bn', label: 'Bengali' },
  { value: 'es', label: 'Spanish' },
  { value: 'fr', label: 'French' },
  { value: 'de', label: 'German' },
  { value: 'it', label: 'Italian' },
  { value: 'pt', label: 'Portuguese' },
  { value: 'ja', label: 'Japanese' },
  { value: 'ko', label: 'Korean' },
  { value: 'zh', label: 'Chinese' },
  { value: 'ru', label: 'Russian' },
  { value: 'ar', label: 'Arabic' },
];

// Speech-speed presets (the config stores the preset name; the
// engine maps it to Kokoro's speed factor).
export const TTS_SPEED_OPTIONS = [
  { value: 'slow', label: 'Slow' },
  { value: 'normal', label: 'Normal' },
  { value: 'fast', label: 'Fast' },
];

export function ttsSpeedLabel(value) {
  const found = TTS_SPEED_OPTIONS.find((o) => o.value === value);
  return found ? found.label : value;
}

// Measured speech-to-text turn latency, per model — the ONLY
// measurements that exist today, taken on the build VM (Whisper
// 'small' on CPU: ~5,556 ms per turn, the figure behind the Hub
// risk register's STT latency entry). The owner's own PC has
// NOT been measured yet, so the panel labels this number with
// its machine whenever it shows it — it is a warning sign, not
// a verdict on the user's hardware.
export const MEASURED_STT_TURN_MS = {
  small: 5556,
};
export const MEASURED_STT_SOURCE = 'the build VM';
