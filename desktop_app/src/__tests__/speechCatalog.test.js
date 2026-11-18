import {
  KOKORO_VOICE_GROUPS,
  STT_LANGUAGE_OPTIONS,
  TTS_SPEED_OPTIONS,
  kokoroVoiceDescription,
  kokoroVoiceDisplayName,
  sttModelLabel,
} from '../speechCatalog';

// The speech catalog is the renderer's mirror of the engine's
// vocabularies: every value offered here must be a value the
// engine's strict config validation accepts (voice ids by the
// Kokoro pattern, languages by STT_LANGUAGES, speeds by
// TTS_SPEED_PRESETS in aether_engine).

describe('speechCatalog', () => {
  it('every offered voice has a name and a one-line description', () => {
    const voices = KOKORO_VOICE_GROUPS.flatMap((g) => g.items);
    expect(voices.length).toBeGreaterThan(0);
    for (const voice of voices) {
      expect(voice.value).toMatch(/^[a-z]{2}_[a-z]+$/);
      expect(voice.name.length).toBeGreaterThan(0);
      expect(voice.description.length).toBeGreaterThan(0);
      expect(kokoroVoiceDisplayName(voice.value)).toBe(voice.name);
      expect(kokoroVoiceDescription(voice.value)).toBe(voice.description);
    }
  });

  it('derives a display name for a voice outside the catalog', () => {
    expect(kokoroVoiceDisplayName('af_custom')).toBe('Custom');
    expect(kokoroVoiceDisplayName('zm_yunjian')).toBe('Yunjian');
    expect(kokoroVoiceDescription('af_custom')).toBeNull();
  });

  it('offers exactly the engine language vocabulary', () => {
    expect(STT_LANGUAGE_OPTIONS.map((o) => o.value)).toEqual([
      'auto', 'en', 'hi', 'bn', 'es', 'fr', 'de', 'it', 'pt',
      'ja', 'ko', 'zh', 'ru', 'ar',
    ]);
  });

  it('offers exactly the engine speed presets', () => {
    expect(TTS_SPEED_OPTIONS.map((o) => o.value)).toEqual(['slow', 'normal', 'fast']);
  });

  it('labels built-in STT models with size and hint, and passes other values through', () => {
    expect(sttModelLabel('small')).toBe('small — ~460 MB · balanced speed and accuracy');
    expect(sttModelLabel('turbo')).toContain('~800 MB');
    expect(sttModelLabel('distil-large-v3')).toBe('distil-large-v3');
  });
});
