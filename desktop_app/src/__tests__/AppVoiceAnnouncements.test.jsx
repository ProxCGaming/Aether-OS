import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import App from '../App';

// Epic 8, N10: background-task announcements arrive as
// VOICE_OUTPUT_CHUNKs tagged with a stable announcement_id. The
// desktop must play and display them (the overlay shows what is
// being announced) but NEVER commit them as chat messages — the
// announced task's own message already stands in chat, so a
// commit would render the same result twice. The same holds for
// a barge-in landing mid-announcement: its committed_text is the
// heard prefix of the announcement, not a new message.

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

const ANN_ID = 'voice-ann:vtask-1:completed';

describe('App voice announcements (N10)', () => {
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

  const streamAnnouncement = async () => {
    emitEngineMessage({
      type: 'VOICE_OUTPUT_CHUNK',
      payload: {
        session_id: 's1', speaker: 'assistant',
        text_delta: 'Report finished: ',
        audio_chunk: btoa('RIFFfake-wav-bytes'), timestamp: 1,
        announcement_id: ANN_ID, announcement_kind: 'completed',
        message_id: 'msg-42',
      },
    });
    await flushPromises();
    emitEngineMessage({
      type: 'VOICE_OUTPUT_CHUNK',
      payload: {
        session_id: 's1', speaker: 'assistant',
        text_delta: '3 files changed.',
        audio_chunk: null, timestamp: 2,
        announcement_id: ANN_ID, announcement_kind: 'completed',
        message_id: 'msg-42',
      },
    });
    await flushPromises();
  };

  it('plays and displays an announcement but never commits it as a message', async () => {
    await connectAndStartVoice();
    await streamAnnouncement();

    // Displayed in the overlay and played — but NOT shown as a
    // streaming chat bubble (it is not a reply turn).
    expect(screen.getByTestId('voice-assistant-stream'))
      .toHaveTextContent('Report finished: 3 files changed.');
    expect(screen.queryByTestId('voice-assistant-bubble')).toBeNull();
    expect(sources).toHaveLength(1);
    expect(sources[0].start).toHaveBeenCalled();

    // The user's next final transcript commits the pending reply —
    // the announcement must not ride along as a committed message.
    emitEngineMessage({
      type: 'VOICE_PARTIAL_TRANSCRIPT',
      payload: { session_id: 's1', speaker: 'user', text: 'thanks', is_final: true },
    });
    await flushPromises();

    expect(screen.getByText('thanks')).toBeInTheDocument();
    expect(screen.queryByText(/Report finished/)).toBeNull();
  });

  it('commits nothing when a barge-in lands mid-announcement', async () => {
    await connectAndStartVoice();
    await streamAnnouncement();

    emitEngineMessage({
      type: 'VOICE_BARGE_IN',
      payload: {
        session_id: 's1', stopped_at_char: 17,
        committed_text: 'Report finished:',
        announcement_id: ANN_ID,
      },
    });
    await flushPromises();

    // Playback still cuts at once…
    expect(sources[0].stop).toHaveBeenCalled();
    // …but the heard prefix of an announcement is not a message:
    // the task's own message already stands in chat.
    expect(screen.queryByText(/Report finished/)).toBeNull();
  });
});
