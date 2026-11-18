import React from 'react';
import { render, screen, fireEvent, act, within } from '@testing-library/react';
import '@testing-library/jest-dom';
import SpeechSettingsPanel from '../components/Settings/SpeechSettingsPanel';

// H5: the Speech card (since Wave 4 it lives in Settings →
// Voice, the one voice home). It is a CONTROLLED panel in the
// E6-F05 sense — SettingsPanel owns the socket, applies config
// payloads, and hands this component the applied config plus
// the shared sendConfigUpdate callback; the card itself only
// fetches the hub's downloaded inventory (the
// MODEL_HUB_DOWNLOADED pair, correlated by request_id exactly
// as LocalModelsTab does) so downloaded speech repos can be
// offered, and runs the voice-preview pair (VOICE_PREVIEW).
//
// The mock socket below keeps a SET of message handlers, like a
// real WebSocket, so the card's own listener and any other
// listener coexist.

function makeWsRef() {
  const handlers = new Set();
  const wsRef = {
    current: {
      readyState: 1,
      send: jest.fn(),
      addEventListener: jest.fn((event, handler) => { handlers.add(handler); }),
      removeEventListener: jest.fn((event, handler) => { handlers.delete(handler); }),
    },
  };
  const sent = () => wsRef.current.send.mock.calls.map(([raw]) => JSON.parse(raw));
  const deliver = (data) => {
    act(() => { handlers.forEach((h) => h({ data: JSON.stringify(data) })); });
  };
  return { wsRef, sent, deliver };
}

const BASE_CONFIG = {
  speech_stt_model: 'small',
  transcription_stt_model: 'small',
  speech_tts_voice: 'af_heart',
};

const DL_STT = {
  source: 'hf', id: 'Systran/faster-whisper-medium', name: 'faster-whisper-medium',
  family: 'speech-to-text', downloads: null, licence: null,
  formats: ['ctranslate2'], runnability: 'in-app',
  size_bytes: 1500000000, downloaded: true,
  path: '/cache/models--Systran--faster-whisper-medium',
};

const DL_BRAIN = {
  source: 'ollama', id: 'qwen3:8b', name: 'qwen3:8b', family: 'brain',
  downloads: null, licence: null, formats: ['gguf'], runnability: 'ollama',
  size_bytes: 5261334937, downloaded: true, path: '/models/qwen3',
};

function renderCard(props = {}) {
  const { wsRef, sent, deliver } = makeWsRef();
  const onConfigUpdate = jest.fn();
  render(
    <SpeechSettingsPanel
      config={BASE_CONFIG}
      configError=""
      onConfigUpdate={onConfigUpdate}
      wsRef={wsRef}
      {...props}
    />,
  );
  return { wsRef, sent, deliver, onConfigUpdate };
}

function openDropdown(testId) {
  const wrapper = screen.getByTestId(testId);
  // The trigger button is the wrapper's first button; clicking it
  // opens the option list.
  fireEvent.click(wrapper.querySelector('button'));
  return wrapper;
}

describe('SpeechSettingsPanel (H5)', () => {
  it('renders the applied config values in the selects and the voice rows', () => {
    renderCard({
      config: {
        speech_stt_model: 'medium',
        transcription_stt_model: 'base',
        speech_tts_voice: 'bm_george',
      },
    });
    expect(within(screen.getByTestId('speech-stt-select')).getByText(/medium —/)).toBeInTheDocument();
    expect(within(screen.getByTestId('speech-transcription-select')).getByText(/base —/)).toBeInTheDocument();
    const george = screen.getByTestId('speech-voice-row-bm_george');
    expect(george).toHaveAttribute('aria-checked', 'true');
    expect(within(george).getByText('George')).toBeInTheDocument();
  });

  it('falls back to the engine defaults before any config arrives', () => {
    renderCard({ config: null });
    expect(within(screen.getByTestId('speech-stt-select')).getByText(/small —/)).toBeInTheDocument();
    expect(within(screen.getByTestId('speech-transcription-select')).getByText(/small —/)).toBeInTheDocument();
    expect(screen.getByTestId('speech-voice-row-af_heart')).toHaveAttribute('aria-checked', 'true');
  });

  it('offers downloaded hub STT repos (and only STT repos) in the model selects', () => {
    const { sent, deliver } = renderCard();
    const request = sent().find((m) => m.type === 'MODEL_HUB_DOWNLOADED_REQUEST');
    expect(request).toBeTruthy();
    deliver({
      type: 'MODEL_HUB_DOWNLOADED_RESPONSE',
      request_id: request.request_id,
      payload: { models: [DL_STT, DL_BRAIN], error: null },
    });
    const stt = openDropdown('speech-stt-select');
    expect(within(stt).getByText('faster-whisper-medium')).toBeInTheDocument();
    expect(within(stt).queryByText('qwen3:8b')).not.toBeInTheDocument();
    const transcription = openDropdown('speech-transcription-select');
    expect(within(transcription).getByText('faster-whisper-medium')).toBeInTheDocument();
  });

  it('keeps a current repo value that is not downloaded, labelled as such', () => {
    const { onConfigUpdate } = renderCard({
      config: { ...BASE_CONFIG, speech_stt_model: 'Systran/faster-whisper-large-v3' },
    });
    expect(
      within(screen.getByTestId('speech-stt-select'))
        .getByText('Systran/faster-whisper-large-v3 (not downloaded)'),
    ).toBeInTheDocument();
    const stt = openDropdown('speech-stt-select');
    // The label shows twice once open: the trigger's selected label
    // and the option itself — and the option is live, not just shown:
    // picking it writes the preserved value back.
    const matches = within(stt).getAllByText('Systran/faster-whisper-large-v3 (not downloaded)');
    expect(matches).toHaveLength(2);
    fireEvent.click(matches[1]);
    expect(onConfigUpdate).toHaveBeenCalledWith({ speech_stt_model: 'Systran/faster-whisper-large-v3' });
  });

  it('keeps a current voice that is not in the premade list as its own row', () => {
    renderCard({ config: { ...BASE_CONFIG, speech_tts_voice: 'af_custom' } });
    const row = screen.getByTestId('speech-voice-row-af_custom');
    expect(row).toBeInTheDocument();
    expect(row).toHaveAttribute('aria-checked', 'true');
    // The raw id survives as the row's tooltip, not as its label.
    expect(row).toHaveAttribute('title', 'af_custom');
  });

  it('writes exactly speech_stt_model from the speech-to-text select', () => {
    const { onConfigUpdate } = renderCard();
    const stt = openDropdown('speech-stt-select');
    fireEvent.click(within(stt).getByText(/large-v3 —/));
    expect(onConfigUpdate).toHaveBeenCalledTimes(1);
    expect(onConfigUpdate).toHaveBeenCalledWith({ speech_stt_model: 'large-v3' });
  });

  it('writes exactly transcription_stt_model from the transcription select — never speech_stt_model', () => {
    const { onConfigUpdate } = renderCard();
    const transcription = openDropdown('speech-transcription-select');
    fireEvent.click(within(transcription).getByText(/turbo —/));
    expect(onConfigUpdate).toHaveBeenCalledTimes(1);
    expect(onConfigUpdate).toHaveBeenCalledWith({ transcription_stt_model: 'turbo' });
    expect(onConfigUpdate).not.toHaveBeenCalledWith(
      expect.objectContaining({ speech_stt_model: expect.anything() }));
  });

  it('writes exactly speech_tts_voice from a voice row', () => {
    const { onConfigUpdate } = renderCard();
    fireEvent.click(screen.getByTestId('speech-voice-row-bf_emma'));
    expect(onConfigUpdate).toHaveBeenCalledTimes(1);
    expect(onConfigUpdate).toHaveBeenCalledWith({ speech_tts_voice: 'bf_emma' });
  });

  it('groups the Kokoro voices by language, including the Hindi voices', () => {
    renderCard();
    const list = screen.getByTestId('speech-voice-list');
    expect(within(list).getByText('American English')).toBeInTheDocument();
    expect(within(list).getByText('British English')).toBeInTheDocument();
    // 'Hindi' is both the group header and each row's language chip.
    expect(within(list).getAllByText('Hindi').length).toBeGreaterThan(0);
    expect(within(screen.getByTestId('speech-voice-row-hf_alpha')).getByText('Alpha')).toBeInTheDocument();
    expect(within(screen.getByTestId('speech-voice-row-hm_psi')).getByText('Psi')).toBeInTheDocument();
  });

  it('surfaces a refused write inline, verbatim', () => {
    renderCard({ configError: "speech_stt_model: unsupported speech model 'bogus'" });
    expect(screen.getByTestId('speech-error')).toHaveTextContent(
      "speech_stt_model: unsupported speech model 'bogus'");
  });

  it('states the honest apply semantics: dictation now, voice mode next session, voice mid-conversation', () => {
    renderCard();
    expect(screen.getByText(/dictation right away/)).toBeInTheDocument();
    expect(screen.getByText(/next voice session/)).toBeInTheDocument();
    expect(screen.getByText(/mid-conversation/)).toBeInTheDocument();
    expect(screen.getByText(/video transcription/)).toBeInTheDocument();
  });
});

describe('SpeechSettingsPanel — Wave 4 voice home', () => {
  it('shows a one-line character description per voice, with the raw id only as a tooltip', () => {
    renderCard();
    const heart = screen.getByTestId('speech-voice-row-af_heart');
    expect(within(heart).getByText(/Warm, friendly American female/)).toBeInTheDocument();
    expect(heart).toHaveAttribute('title', 'af_heart');
    expect(screen.queryByText('(af_heart)')).not.toBeInTheDocument();
    expect(screen.queryByText('Heart (af_heart)')).not.toBeInTheDocument();
  });

  it('labels the built-in STT sizes with a download size and a speed/accuracy hint', () => {
    renderCard();
    const stt = openDropdown('speech-stt-select');
    // The selected label shows in the trigger AND the option list.
    expect(within(stt).getAllByText('small — ~460 MB · balanced speed and accuracy').length).toBeGreaterThan(0);
    expect(within(stt).getByText('large-v3 — ~2.9 GB · slowest, most accurate')).toBeInTheDocument();
    expect(within(stt).getByText('turbo — ~800 MB · fast, near-large accuracy')).toBeInTheDocument();
  });

  it('warns about the measured-slow model, labelled with the machine it was measured on', () => {
    renderCard(); // BASE_CONFIG selects 'small' — the measured model.
    const warning = screen.getByTestId('speech-slow-warning');
    expect(warning).toHaveTextContent('build VM');
    expect(warning).toHaveTextContent('5.6 seconds');
    expect(warning).toHaveTextContent(/hasn't been measured yet/);
  });

  it('shows no slow warning for an unmeasured model', () => {
    renderCard({ config: { ...BASE_CONFIG, speech_stt_model: 'medium' } });
    expect(screen.queryByTestId('speech-slow-warning')).not.toBeInTheDocument();
  });

  it('writes exactly speech_tts_speed from the speed control', () => {
    const { onConfigUpdate } = renderCard({
      config: { ...BASE_CONFIG, speech_tts_speed: 'normal' },
    });
    fireEvent.click(screen.getByTestId('speech-speed-fast'));
    expect(onConfigUpdate).toHaveBeenCalledTimes(1);
    expect(onConfigUpdate).toHaveBeenCalledWith({ speech_tts_speed: 'fast' });
  });

  it('writes exactly speech_stt_language from the language select', () => {
    const { onConfigUpdate } = renderCard({
      config: { ...BASE_CONFIG, speech_stt_language: 'auto' },
    });
    const language = openDropdown('speech-language-select');
    fireEvent.click(within(language).getByText('Hindi'));
    expect(onConfigUpdate).toHaveBeenCalledTimes(1);
    expect(onConfigUpdate).toHaveBeenCalledWith({ speech_stt_language: 'hi' });
  });

  it('marks the speed and language controls inert with a queued note while their writes are queued', () => {
    renderCard({
      pendingMessages: [
        { type: 'CONFIG_UPDATE_REQUEST', payload: { config: { speech_tts_speed: 'fast' } } },
        { type: 'CONFIG_UPDATE_REQUEST', payload: { config: { speech_stt_language: 'hi' } } },
      ],
    });
    expect(screen.getByTestId('speech-speed-queued')).toHaveTextContent('Fast');
    expect(screen.getByTestId('speech-language-queued')).toBeInTheDocument();
  });

  it('probes preview availability on open and sends a preview request naming only the voice', () => {
    const { sent, deliver } = renderCard();
    const probe = sent().find(
      (m) => m.type === 'VOICE_PREVIEW_REQUEST' && m.payload && m.payload.probe);
    expect(probe).toBeTruthy();
    deliver({
      type: 'VOICE_PREVIEW_RESPONSE',
      request_id: probe.request_id,
      payload: { ok: true, voice: null, audio_chunks: [], error: null },
    });
    fireEvent.click(screen.getByTestId('speech-voice-preview-bf_emma'));
    const requests = sent().filter((m) => m.type === 'VOICE_PREVIEW_REQUEST');
    const previewSend = requests[requests.length - 1];
    // The request names a voice and carries NO text — the
    // sentence is the engine's fixed one.
    expect(previewSend.payload).toEqual({ voice: 'bf_emma' });
    deliver({
      type: 'VOICE_PREVIEW_RESPONSE',
      request_id: previewSend.request_id,
      payload: { ok: true, voice: 'bf_emma', audio_chunks: [], error: null },
    });
    expect(screen.queryByTestId('speech-preview-error')).not.toBeInTheDocument();
  });

  it('says honestly when the speech engine is not installed, and disables the preview buttons', () => {
    const { sent, deliver } = renderCard();
    const probe = sent().find(
      (m) => m.type === 'VOICE_PREVIEW_REQUEST' && m.payload && m.payload.probe);
    deliver({
      type: 'VOICE_PREVIEW_RESPONSE',
      request_id: probe.request_id,
      payload: {
        ok: false, voice: null, audio_chunks: [],
        error: 'voice unavailable: kokoro is not installed (local TTS)',
      },
    });
    const note = screen.getByTestId('speech-preview-unavailable');
    expect(note).toHaveTextContent('kokoro is not installed');
    expect(screen.getByTestId('speech-voice-preview-af_heart')).toBeDisabled();
  });

  it('surfaces a refused preview verbatim and flips to the unavailable note', () => {
    const { sent, deliver } = renderCard();
    fireEvent.click(screen.getByTestId('speech-voice-preview-af_heart'));
    const requests = sent().filter(
      (m) => m.type === 'VOICE_PREVIEW_REQUEST' && m.payload && m.payload.voice);
    const previewSend = requests[requests.length - 1];
    deliver({
      type: 'VOICE_PREVIEW_RESPONSE',
      request_id: previewSend.request_id,
      payload: {
        ok: false, voice: 'af_heart', audio_chunks: [],
        error: 'voice unavailable: kokoro is not installed (local TTS)',
      },
    });
    expect(screen.getByTestId('speech-preview-error')).toHaveTextContent(
      'kokoro is not installed');
    expect(screen.getByTestId('speech-preview-unavailable')).toBeInTheDocument();
  });
});
