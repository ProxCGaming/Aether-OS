import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import App from '../App';

// Lane C grouped S4: starting voice while offline was already
// honest ("voice unavailable: the engine is not connected"), but a
// socket drop DURING a voice session, dictation or read-aloud had
// no dedicated state — each surface kept looking live over a dead
// engine session. The disconnect transition now flips each live
// voice state to its error presentation (the engine ends its voice
// pipelines on disconnect; nothing auto-resumes on reconnect).

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

describe('Voice surfaces on a mid-session disconnect (grouped S4)', () => {
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

    const mockSource = { connect: jest.fn(), start: jest.fn(), stop: jest.fn(), onended: null };
    const mockAudioContext = {
      currentTime: 0,
      destination: {},
      createBufferSource: jest.fn(() => mockSource),
      decodeAudioData: jest.fn(() => Promise.resolve({ duration: 2 })),
      close: jest.fn(),
    };
    window.AudioContext = jest.fn(() => mockAudioContext);
    window.webkitAudioContext = window.AudioContext;
  });

  afterEach(() => {
    delete window.electronAPI;
    delete window.navigator.mediaDevices;
    delete window.RTCPeerConnection;
    delete window.AudioContext;
    delete window.webkitAudioContext;
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

  const disconnect = async () => {
    emitEngineMessage({ type: '_ws_status', status: 'disconnected' });
    await flushPromises();
  };

  it('a live voice session flips to the disconnected error instead of looking live', async () => {
    await connectAndRender();
    fireEvent.click(screen.getByTestId('voice-header-btn'));
    emitEngineMessage({
      type: 'VOICE_SESSION_START_RESPONSE',
      payload: { ok: true, session_id: 's1', webrtc_config: { iceServers: [] }, error: null },
    });
    await flushPromises();

    // Live pre-state: overlay listening, mic open, no error.
    expect(screen.getByTestId('voice-mic-indicator')).toBeInTheDocument();
    expect(screen.getByTestId('voice-status')).toHaveTextContent('Listening');
    expect(screen.queryByTestId('voice-error')).toBeNull();

    await disconnect();

    expect(screen.getByTestId('voice-error')).toHaveTextContent('engine connection was lost');
    expect(screen.queryByTestId('voice-mic-indicator')).toBeNull();
    expect(screen.queryByTestId('voice-status')).toBeNull();
  });

  it('active dictation stops with a disconnected note instead of recording on', async () => {
    await connectAndRender();
    const micBtn = screen.getByTestId('composer-mic-btn');
    fireEvent.mouseDown(micBtn);
    await flushPromises();
    emitEngineMessage({ type: 'DICTATION_START_RESPONSE', payload: { ok: true } });
    await flushPromises();

    expect(micBtn).toHaveAttribute('aria-pressed', 'true');
    expect(screen.queryByTestId('dictation-error')).toBeNull();

    await disconnect();

    expect(screen.getByTestId('dictation-error')).toHaveTextContent(
      'Dictation stopped — the engine connection was lost.'
    );
    expect(micBtn).toHaveAttribute('aria-pressed', 'false');
  });

  it('read-aloud playback stops with a disconnected note instead of playing on', async () => {
    await connectAndRender();

    // Commit an assistant message via a voice turn (the AppVoice
    // flow) so the chat has something to read aloud.
    fireEvent.click(screen.getByTestId('voice-header-btn'));
    emitEngineMessage({
      type: 'VOICE_SESSION_START_RESPONSE',
      payload: { ok: true, session_id: 's1', webrtc_config: { iceServers: [] }, error: null },
    });
    await flushPromises();
    emitEngineMessage({
      type: 'VOICE_OUTPUT_CHUNK',
      payload: { session_id: 's1', speaker: 'assistant', text_delta: 'The bicycle is red.', audio_chunk: null, timestamp: 1 },
    });
    fireEvent.click(screen.getByTestId('voice-stop-btn'));
    await flushPromises();
    expect(screen.getByText('The bicycle is red.')).toBeInTheDocument();

    const readBtn = document.querySelector('[data-testid^="read-aloud-btn-"]');
    expect(readBtn).not.toBeNull();
    const messageId = readBtn.getAttribute('data-testid').replace('read-aloud-btn-', '');
    fireEvent.click(readBtn);
    await flushPromises();
    emitEngineMessage({
      type: 'READ_ALOUD_START_RESPONSE',
      payload: { ok: true, message_id: messageId },
    });
    await flushPromises();

    // Playing pre-state.
    expect(readBtn).toHaveAttribute('title', 'Stop read aloud');
    expect(screen.queryByTestId('read-aloud-error')).toBeNull();

    await disconnect();

    expect(screen.getByTestId('read-aloud-error')).toHaveTextContent(
      'Read aloud stopped — the engine connection was lost.'
    );
    expect(readBtn).toHaveAttribute('title', 'Read aloud');
  });
});
