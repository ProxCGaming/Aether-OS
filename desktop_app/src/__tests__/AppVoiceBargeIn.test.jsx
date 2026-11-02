import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import App from '../App';

// Epic 8, E8-5.1b: when the engine reports a barge-in, the client
// honours the committed-words rule end to end: the streaming bubble
// is replaced by exactly the text that was actually heard (a new
// committed message; an empty heard text commits nothing), and the
// overlay's scheduled audio is cut at once via the playbackCut
// signal App threads through DraggableChatWindow.

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

describe('App voice barge-in (E8-5.1b)', () => {
  let engineListeners;
  let electronAPI;
  let sources;

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
    Object.defineProperty(window.navigator, 'mediaDevices', {
      configurable: true,
      value: { getUserMedia: jest.fn(() => Promise.resolve(stream)) },
    });
    window.RTCPeerConnection = jest.fn(() => ({ addTrack: jest.fn(), close: jest.fn() }));

    sources = [];
    const ctx = {
      currentTime: 0,
      destination: {},
      decodeAudioData: jest.fn(() => Promise.resolve({ duration: 0.5 })),
      createBufferSource: jest.fn(() => {
        const source = {
          buffer: null, connect: jest.fn(), start: jest.fn(),
          stop: jest.fn(), onended: null,
        };
        sources.push(source);
        return source;
      }),
      close: jest.fn(),
    };
    window.AudioContext = jest.fn(() => ctx);
  });

  afterEach(() => {
    delete window.electronAPI;
    delete window.navigator.mediaDevices;
    delete window.RTCPeerConnection;
    delete window.AudioContext;
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

  const connectAndStartVoice = async () => {
    render(<App />);
    await flushPromises();
    emitEngineMessage({ type: '_ws_status', status: 'connected' });
    await flushPromises();
    emitEngineMessage({ type: 'SESSION_CREATE_RESPONSE', payload: { session_id: 's1' } });
    await flushPromises();
    fireEvent.click(screen.getByTestId('voice-header-btn'));
    emitEngineMessage({
      type: 'VOICE_SESSION_START_RESPONSE',
      payload: {
        ok: true, session_id: 's1',
        webrtc_config: { iceServers: [] }, error: null,
      },
    });
    await flushPromises();
    expect(screen.getByTestId('voice-overlay')).toBeInTheDocument();
  };

  const streamReplyWithAudio = async () => {
    emitEngineMessage({
      type: 'VOICE_OUTPUT_CHUNK',
      payload: {
        session_id: 's1', speaker: 'assistant', text_delta: 'The bicycle ',
        audio_chunk: btoa('RIFFfake-wav-bytes'), timestamp: 1,
      },
    });
    await flushPromises();
    emitEngineMessage({
      type: 'VOICE_OUTPUT_CHUNK',
      payload: {
        session_id: 's1', speaker: 'assistant', text_delta: 'is red.',
        audio_chunk: null, timestamp: 2,
      },
    });
    await flushPromises();
    expect(screen.getByTestId('voice-assistant-bubble')).toHaveTextContent('The bicycle is red.');
    expect(sources).toHaveLength(1);
    expect(sources[0].start).toHaveBeenCalled();
  };

  it('commits only the heard words and cuts playback on barge-in', async () => {
    await connectAndStartVoice();
    await streamReplyWithAudio();

    emitEngineMessage({
      type: 'VOICE_BARGE_IN',
      payload: { session_id: 's1', stopped_at_char: 11, committed_text: 'The bicycle' },
    });
    await flushPromises();

    // The streaming bubble is gone; exactly the heard words remain
    // as a committed message — the unheard tail does not survive.
    expect(screen.queryByTestId('voice-assistant-bubble')).toBeNull();
    expect(screen.getByText('The bicycle')).toBeInTheDocument();
    expect(screen.queryByText('The bicycle is red.')).toBeNull();
    // And the scheduled audio was cut, not left playing.
    expect(sources[0].stop).toHaveBeenCalled();
  });

  it('commits nothing when nothing was heard yet', async () => {
    await connectAndStartVoice();
    await streamReplyWithAudio();

    emitEngineMessage({
      type: 'VOICE_BARGE_IN',
      payload: { session_id: 's1', stopped_at_char: 0, committed_text: '' },
    });
    await flushPromises();

    expect(screen.queryByTestId('voice-assistant-bubble')).toBeNull();
    expect(screen.queryByText('The bicycle')).toBeNull();
    expect(sources[0].stop).toHaveBeenCalled();
  });
});
