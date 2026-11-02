import React from 'react';
import { render, screen, fireEvent, act, within } from '@testing-library/react';
import '@testing-library/jest-dom';
import SettingsPanel from '../SettingsPanel';

// H5 integration: the Speech card lives in Settings → Models &
// Tasks beside the Auxiliary models card, is fed by the panel's
// single CONFIG_GET / applyConfigValues path, and its edits go
// out through sendConfigUpdate like every other handed-off
// surface (E6-F05/F15). DiagnosticsPanel (the Agents host) is
// stubbed: its own suites cover it.
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

function openModelsTab(wsRef) {
  render(<SettingsPanel wsRef={wsRef} providers={[]} localModels={[]} />);
  fireEvent.click(screen.getByText('Models & Tasks'));
}

describe('SettingsPanel Speech card (H5)', () => {
  it('Models & Tasks hosts the Speech card beside Auxiliary models, fed by the config fetch', () => {
    const { wsRef, sent, deliver } = makeWsRef();
    openModelsTab(wsRef);
    expect(screen.getByText('⚙ Auxiliary models')).toBeInTheDocument();
    expect(screen.getByText('Speech')).toBeInTheDocument();
    // Opening Models fetches the config for the controlled panels.
    expect(sent().map((m) => m.type)).toContain('CONFIG_GET_REQUEST');
    // The card also asks the hub what speech models are downloaded.
    expect(sent().map((m) => m.type)).toContain('MODEL_HUB_DOWNLOADED_REQUEST');
    deliver({ type: 'CONFIG_GET_RESPONSE', payload: { config: SPEECH_CONFIG } });
    expect(within(screen.getByTestId('speech-stt-select')).getByText('medium')).toBeInTheDocument();
    expect(within(screen.getByTestId('speech-transcription-select')).getByText('base')).toBeInTheDocument();
    expect(within(screen.getByTestId('speech-voice-select')).getByText('George (bm_george)')).toBeInTheDocument();
    expect(screen.getByText(/next voice session/)).toBeInTheDocument();
  });

  it('a transcription change goes out as a CONFIG_UPDATE carrying only transcription_stt_model', () => {
    const { wsRef, sent, deliver } = makeWsRef();
    openModelsTab(wsRef);
    deliver({ type: 'CONFIG_GET_RESPONSE', payload: { config: SPEECH_CONFIG } });
    const wrapper = screen.getByTestId('speech-transcription-select');
    fireEvent.click(wrapper.querySelector('button'));
    fireEvent.click(within(wrapper).getByText('large-v3'));
    const updates = sent().filter((m) => m.type === 'CONFIG_UPDATE_REQUEST');
    expect(updates).toHaveLength(1);
    expect(updates[0].payload.config).toEqual({ transcription_stt_model: 'large-v3' });
  });

  it('a speech-to-text change goes out as a CONFIG_UPDATE carrying only speech_stt_model', () => {
    const { wsRef, sent, deliver } = makeWsRef();
    openModelsTab(wsRef);
    deliver({ type: 'CONFIG_GET_RESPONSE', payload: { config: SPEECH_CONFIG } });
    const wrapper = screen.getByTestId('speech-stt-select');
    fireEvent.click(wrapper.querySelector('button'));
    fireEvent.click(within(wrapper).getByText('turbo'));
    const updates = sent().filter((m) => m.type === 'CONFIG_UPDATE_REQUEST');
    expect(updates).toHaveLength(1);
    expect(updates[0].payload.config).toEqual({ speech_stt_model: 'turbo' });
  });

  it('a refused speech write is shown in the card, verbatim', () => {
    const { wsRef, deliver } = makeWsRef();
    openModelsTab(wsRef);
    deliver({ type: 'CONFIG_GET_RESPONSE', payload: { config: SPEECH_CONFIG } });
    deliver({
      type: 'CONFIG_UPDATE_RESPONSE',
      payload: { success: false, error: "speech_stt_model: unsupported speech model 'bogus'" },
    });
    expect(screen.getByTestId('speech-error')).toHaveTextContent(
      "speech_stt_model: unsupported speech model 'bogus'");
  });
});
