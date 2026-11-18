import React from 'react';
import { render, screen, fireEvent, act, within } from '@testing-library/react';
import '@testing-library/jest-dom';
import SettingsPanel from '../SettingsPanel';

// The Speech card lives in Settings → Voice (the one voice
// home: Speech beside Tuning & Input), fed by the panel's
// single CONFIG_GET / applyConfigValues path, and its edits go
// out through sendConfigUpdate like every other handed-off
// surface (E6-F05/F15). Models & Tasks carries a cross-link
// where the card used to live. DiagnosticsPanel (the Agents
// host) is stubbed: its own suites cover it.
jest.mock('../components/Settings/DiagnosticsPanel', () => () => (
  <div data-testid="diagnostics-stub" />
));
jest.mock('../contexts/SocketContext', () => ({
  useAetherSocket: () => ({
    wsRef: { current: { addEventListener: jest.fn(), removeEventListener: jest.fn() } },
    sendMessage: jest.fn(),
  }),
}));

// A real WebSocket fans messages out to every registered
// listener; the mock keeps a set so SettingsPanel's handler and
// the Speech card's hub-inventory listener coexist.
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

const SPEECH_CONFIG = {
  speech_stt_model: 'medium',
  transcription_stt_model: 'base',
  speech_tts_voice: 'bm_george',
};

function openVoiceTab(wsRef) {
  render(<SettingsPanel wsRef={wsRef} providers={[]} localModels={[]} />);
  fireEvent.click(screen.getByText('Voice Tuning'));
}

function openModelsTab(wsRef) {
  render(<SettingsPanel wsRef={wsRef} providers={[]} localModels={[]} />);
  fireEvent.click(screen.getByText('Models & Tasks'));
}

describe('SettingsPanel Speech card (voice home)', () => {
  it('the Voice section hosts the Speech card beside Tuning & Input, fed by the config fetch', () => {
    const { wsRef, sent, deliver } = makeWsRef();
    openVoiceTab(wsRef);
    expect(screen.getByText('Speech')).toBeInTheDocument();
    // Opening Voice fetches the config for the controlled panels.
    expect(sent().map((m) => m.type)).toContain('CONFIG_GET_REQUEST');
    // The card also asks the hub what speech models are downloaded.
    expect(sent().map((m) => m.type)).toContain('MODEL_HUB_DOWNLOADED_REQUEST');
    deliver({ type: 'CONFIG_GET_RESPONSE', payload: { config: SPEECH_CONFIG } });
    expect(within(screen.getByTestId('speech-stt-select')).getByText(/medium —/)).toBeInTheDocument();
    expect(within(screen.getByTestId('speech-transcription-select')).getByText(/base —/)).toBeInTheDocument();
    const george = screen.getByTestId('speech-voice-row-bm_george');
    expect(george).toHaveAttribute('aria-checked', 'true');
    expect(within(george).getByText('George')).toBeInTheDocument();
    expect(screen.getAllByText(/next voice session/).length).toBeGreaterThan(0);
  });

  it('Models & Tasks cross-links to the Voice section, and the link lands on the Speech card', () => {
    const { wsRef } = makeWsRef();
    openModelsTab(wsRef);
    expect(screen.getByText('Speech lives in Voice settings')).toBeInTheDocument();
    expect(screen.queryByTestId('speech-stt-select')).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId('models-open-voice-settings'));
    expect(screen.getByTestId('speech-stt-select')).toBeInTheDocument();
    expect(screen.getByTestId('speech-voice-list')).toBeInTheDocument();
  });

  it('a transcription change goes out as a CONFIG_UPDATE carrying only transcription_stt_model', () => {
    const { wsRef, sent, deliver } = makeWsRef();
    openVoiceTab(wsRef);
    deliver({ type: 'CONFIG_GET_RESPONSE', payload: { config: SPEECH_CONFIG } });
    const wrapper = screen.getByTestId('speech-transcription-select');
    fireEvent.click(wrapper.querySelector('button'));
    fireEvent.click(within(wrapper).getByText(/large-v3 —/));
    const updates = sent().filter((m) => m.type === 'CONFIG_UPDATE_REQUEST');
    expect(updates).toHaveLength(1);
    expect(updates[0].payload.config).toEqual({ transcription_stt_model: 'large-v3' });
  });

  it('a speech-to-text change goes out as a CONFIG_UPDATE carrying only speech_stt_model', () => {
    const { wsRef, sent, deliver } = makeWsRef();
    openVoiceTab(wsRef);
    deliver({ type: 'CONFIG_GET_RESPONSE', payload: { config: SPEECH_CONFIG } });
    const wrapper = screen.getByTestId('speech-stt-select');
    fireEvent.click(wrapper.querySelector('button'));
    fireEvent.click(within(wrapper).getByText(/turbo —/));
    const updates = sent().filter((m) => m.type === 'CONFIG_UPDATE_REQUEST');
    expect(updates).toHaveLength(1);
    expect(updates[0].payload.config).toEqual({ speech_stt_model: 'turbo' });
  });

  it('a refused speech write is shown in the card, verbatim', () => {
    const { wsRef, deliver } = makeWsRef();
    openVoiceTab(wsRef);
    deliver({ type: 'CONFIG_GET_RESPONSE', payload: { config: SPEECH_CONFIG } });
    deliver({
      type: 'CONFIG_UPDATE_RESPONSE',
      payload: { success: false, error: "speech_stt_model: unsupported speech model 'bogus'" },
    });
    expect(screen.getByTestId('speech-error')).toHaveTextContent(
      "speech_stt_model: unsupported speech model 'bogus'");
  });
});
