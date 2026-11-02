import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import App from '../App';

// Epic 8, E8-5.1a: voice mode wiring. Pressing the voice button asks
// the engine to start a session; only an ok response opens the
// overlay (and the microphone). Partial transcripts render as a
// provisional bubble, the final transcript commits as a user
// message, and the talker's reply streams word by word into a
// provisional assistant bubble that commits when the session stops.
// A refused start shows the engine's honest reason and never opens
// a device.

jest.mock('react-force-graph-3d', () => ({
  __esModule: true,
  default: () => <div data-testid="force-graph-stub" />,
}));

jest.mock('react-markdown', () => ({
  __esModule: true,
  default: ({ children }) => <div data-testid="markdown-stub">{children}</div>,
}));

Element.prototype.scrollIntoView = jest.fn();
global.WebSocket = { OPEN: 1, CLOSED: 3, CONNECTING: 0, CLOSING: 2 };

describe('App voice mode (E8-5.1a)', () => {
  let engineListeners;
  let electronAPI;
  let getUserMedia;

  beforeEach(() => {
    engineListeners = [];
    electronAPI = {
      sendEngineMessage: jest.fn(),
      onEngineMessage: jest.fn((cb) => {
        engineListeners.push(cb);
        return () => {
          const idx = engineListeners.indexOf(cb);
          if (idx !== -1) engineListeners.splice(idx, 1);
        };
      }),
      getWsStatus: jest.fn(() => Promise.resolve(false)),
      getMemoryUsage: jest.fn(() => Promise.resolve(128)),
      onChatDetached: jest.fn(() => jest.fn()),
      onChatAttached: jest.fn(() => jest.fn()),
    };
    window.electronAPI = electronAPI;

    const tracks = [{ stop: jest.fn(), kind: 'audio' }];
    const stream = { getTracks: jest.fn(() => tracks) };
    getUserMedia = jest.fn(() => Promise.resolve(stream));
    Object.defineProperty(window.navigator, 'mediaDevices', {
      configurable: true,
      value: { getUserMedia },
    });
    window.RTCPeerConnection = jest.fn(() => ({ addTrack: jest.fn(), close: jest.fn() }));
  });

  afterEach(() => {
    delete window.electronAPI;
    delete window.navigator.mediaDevices;
    delete window.RTCPeerConnection;
  });

  const emitEngineMessage = (obj) => {
    const raw = typeof obj === 'string' ? obj : JSON.stringify(obj);
    act(() => {
      [...engineListeners].forEach((cb) => cb(raw));
    });
  };

  const flushPromises = async () => {
    await act(async () => {});
  };

  const connectAndRender = async () => {
    render(<App />);
    await flushPromises();
    emitEngineMessage({ type: '_ws_status', status: 'connected' });
    await flushPromises();
    emitEngineMessage({ type: 'SESSION_CREATE_RESPONSE', payload: { session_id: 's1' } });
    await flushPromises();
  };

  const sentPayloads = (type) => electronAPI.sendEngineMessage.mock.calls
    .map(([raw]) => { try { return JSON.parse(raw); } catch { return null; } })
    .filter((m) => m && m.type === type)
    .map((m) => m.payload);

  const startResponse = (overrides = {}) => ({
    type: 'VOICE_SESSION_START_RESPONSE',
    payload: {
      ok: true, session_id: 's1',
      webrtc_config: { iceServers: [] }, error: null,
      ...overrides,
    },
  });

  it('a refused start shows the honest reason and never opens the mic', async () => {
    await connectAndRender();
    fireEvent.click(screen.getByTestId('voice-header-btn'));
    expect(sentPayloads('VOICE_SESSION_START_REQUEST')).toEqual([{ session_id: 's1' }]);

    emitEngineMessage(startResponse({
      ok: false, webrtc_config: null,
      error: 'voice unavailable: faster-whisper is not installed (local STT)',
    }));
    await flushPromises();

    expect(screen.getByTestId('voice-error')).toHaveTextContent('faster-whisper is not installed');
    expect(getUserMedia).not.toHaveBeenCalled();
    expect(screen.queryByTestId('voice-mic-indicator')).toBeNull();
  });

  it('streams a full voice turn into the transcript and commits on stop', async () => {
    await connectAndRender();
    fireEvent.click(screen.getByTestId('voice-header-btn'));
    emitEngineMessage(startResponse());
    await flushPromises();

    expect(screen.getByTestId('voice-overlay')).toBeInTheDocument();
    expect(getUserMedia).toHaveBeenCalledWith({ audio: true });
    expect(screen.getByTestId('voice-mic-indicator')).toBeInTheDocument();
    expect(screen.getByTestId('voice-live-dot')).toBeInTheDocument();

    // Provisional partial: a ghost bubble, not yet a message.
    emitEngineMessage({
      type: 'VOICE_PARTIAL_TRANSCRIPT',
      payload: { session_id: 's1', speaker: 'user', text: 'the red', is_final: false },
    });
    expect(screen.getByTestId('voice-partial-bubble')).toHaveTextContent('the red');

    // Final transcript: the ghost becomes a committed user message.
    emitEngineMessage({
      type: 'VOICE_PARTIAL_TRANSCRIPT',
      payload: { session_id: 's1', speaker: 'user', text: 'the red bicycle', is_final: true },
    });
    expect(screen.queryByTestId('voice-partial-bubble')).toBeNull();
    expect(screen.getByText('the red bicycle')).toBeInTheDocument();

    // The reply streams word by word.
    emitEngineMessage({
      type: 'VOICE_OUTPUT_CHUNK',
      payload: { session_id: 's1', speaker: 'assistant', text_delta: 'The bicycle ', audio_chunk: null, timestamp: 1 },
    });
    expect(screen.getByTestId('voice-assistant-bubble')).toHaveTextContent('The bicycle');
    emitEngineMessage({
      type: 'VOICE_OUTPUT_CHUNK',
      payload: { session_id: 's1', speaker: 'assistant', text_delta: 'is red.', audio_chunk: null, timestamp: 2 },
    });
    expect(screen.getByTestId('voice-assistant-bubble')).toHaveTextContent('The bicycle is red.');

    // Stop: the request goes out, the overlay closes, and the
    // streamed reply stays in the transcript as a committed message.
    fireEvent.click(screen.getByTestId('voice-stop-btn'));
    expect(sentPayloads('VOICE_SESSION_STOP_REQUEST')).toEqual([{ session_id: 's1' }]);
    expect(screen.queryByTestId('voice-overlay')).toBeNull();
    expect(screen.queryByTestId('voice-assistant-bubble')).toBeNull();
    expect(screen.getByText('The bicycle is red.')).toBeInTheDocument();
    expect(screen.queryByTestId('voice-live-dot')).toBeNull();
  });

  it('ignores voice pushes for a different session', async () => {
    await connectAndRender();
    fireEvent.click(screen.getByTestId('voice-header-btn'));
    emitEngineMessage(startResponse());
    await flushPromises();

    emitEngineMessage({
      type: 'VOICE_PARTIAL_TRANSCRIPT',
      payload: { session_id: 'someone-else', speaker: 'user', text: 'stray', is_final: false },
    });
    expect(screen.queryByTestId('voice-partial-bubble')).toBeNull();
  });
});
