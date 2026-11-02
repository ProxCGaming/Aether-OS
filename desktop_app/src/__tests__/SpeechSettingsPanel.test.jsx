import React from 'react';
import { render, screen, fireEvent, act, within } from '@testing-library/react';
import '@testing-library/jest-dom';
import SpeechSettingsPanel from '../components/Settings/SpeechSettingsPanel';

// H5: the Speech card in Settings → Models & Tasks. It is a
// CONTROLLED panel in the E6-F05 sense — SettingsPanel owns the
// socket, applies config payloads, and hands this component the
// applied config plus the shared sendConfigUpdate callback; the
// card itself only fetches the hub's downloaded inventory (the
// MODEL_HUB_DOWNLOADED pair, correlated by request_id exactly as
// LocalModelsTab does) so downloaded speech repos can be offered.
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
  it('renders the applied config values in all three selects', () => {
    renderCard({
      config: {
        speech_stt_model: 'medium',
        transcription_stt_model: 'base',
        speech_tts_voice: 'bm_george',
      },
    });
    expect(within(screen.getByTestId('speech-stt-select')).getByText('medium')).toBeInTheDocument();
    expect(within(screen.getByTestId('speech-transcription-select')).getByText('base')).toBeInTheDocument();
    expect(within(screen.getByTestId('speech-voice-select')).getByText('George (bm_george)')).toBeInTheDocument();
  });

  it('falls back to the engine defaults before any config arrives', () => {
    renderCard({ config: null });
    expect(within(screen.getByTestId('speech-stt-select')).getByText('small')).toBeInTheDocument();
    expect(within(screen.getByTestId('speech-transcription-select')).getByText('small')).toBeInTheDocument();
    expect(within(screen.getByTestId('speech-voice-select')).getByText('Heart (af_heart)')).toBeInTheDocument();
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

  it('keeps a current voice that is not in the premade list as its own option', () => {
    renderCard({ config: { ...BASE_CONFIG, speech_tts_voice: 'af_custom' } });
    const voice = openDropdown('speech-voice-select');
    expect(within(voice).getAllByText('af_custom').length).toBeGreaterThan(0);
  });

  it('writes exactly speech_stt_model from the speech-to-text select', () => {
    const { onConfigUpdate } = renderCard();
    const stt = openDropdown('speech-stt-select');
    fireEvent.click(within(stt).getByText('large-v3'));
    expect(onConfigUpdate).toHaveBeenCalledTimes(1);
    expect(onConfigUpdate).toHaveBeenCalledWith({ speech_stt_model: 'large-v3' });
  });

  it('writes exactly transcription_stt_model from the transcription select — never speech_stt_model', () => {
    const { onConfigUpdate } = renderCard();
    const transcription = openDropdown('speech-transcription-select');
    fireEvent.click(within(transcription).getByText('turbo'));
    expect(onConfigUpdate).toHaveBeenCalledTimes(1);
    expect(onConfigUpdate).toHaveBeenCalledWith({ transcription_stt_model: 'turbo' });
    expect(onConfigUpdate).not.toHaveBeenCalledWith(
      expect.objectContaining({ speech_stt_model: expect.anything() }));
  });

  it('writes exactly speech_tts_voice from the voice select', () => {
    const { onConfigUpdate } = renderCard();
    const voice = openDropdown('speech-voice-select');
    fireEvent.click(within(voice).getByText('Emma (bf_emma)'));
    expect(onConfigUpdate).toHaveBeenCalledTimes(1);
    expect(onConfigUpdate).toHaveBeenCalledWith({ speech_tts_voice: 'bf_emma' });
  });

  it('groups the Kokoro voices by language, including the Hindi voices', () => {
    renderCard();
    const voice = openDropdown('speech-voice-select');
    expect(within(voice).getByText('American English')).toBeInTheDocument();
    expect(within(voice).getByText('British English')).toBeInTheDocument();
    expect(within(voice).getByText('Hindi')).toBeInTheDocument();
    expect(within(voice).getByText('Alpha (hf_alpha)')).toBeInTheDocument();
    expect(within(voice).getByText('Psi (hm_psi)')).toBeInTheDocument();
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
