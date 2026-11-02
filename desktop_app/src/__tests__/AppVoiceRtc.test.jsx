import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import App from '../App';

// F-12b (Astra M18): App-level RTC signalling wiring. When the
// voice session goes live, the overlay's offer must reach the
// engine as VOICE_RTC_OFFER_REQUEST, the engine's answer must come
// back down to the overlay's peer connection, engine candidates
// (VOICE_RTC_ICE pushes) must be applied to it, local candidates
// must go up as VOICE_RTC_ICE_REQUEST, and a refused negotiation
// must surface as the voice error — never as a live-looking
// overlay with no media behind it.

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

describe('App voice RTC signalling (F-12b)', () => {
  let engineListeners;
  let electronAPI;
  let pc;

  const flushPromises = async () => { await act(async () => {}); };

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

    pc = {
      addTrack: jest.fn(),
      close: jest.fn(),
      connectionState: 'new',
      signalingState: 'stable',
      localDescription: { type: 'offer', sdp: 'local-offer-sdp' },
      createOffer: jest.fn(() => Promise.resolve({ type: 'offer', sdp: 'raw-offer' })),
      setLocalDescription: jest.fn(() => {
        pc.signalingState = 'have-local-offer';
        return Promise.resolve();
      }),
      setRemoteDescription: jest.fn(() => {
        pc.signalingState = 'stable';
        return Promise.resolve();
      }),
      addIceCandidate: jest.fn(() => Promise.resolve()),
      onicecandidate: null,
      onconnectionstatechange: null,
    };
    window.RTCPeerConnection = jest.fn(() => pc);
    window.AudioContext = jest.fn(() => ({
      currentTime: 0,
      destination: {},
      decodeAudioData: jest.fn(() => Promise.resolve({ duration: 0.5 })),
      createBufferSource: jest.fn(() => ({ connect: jest.fn(), start: jest.fn(), buffer: null })),
      close: jest.fn(),
    }));
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

  const sentPayloads = (type) => electronAPI.sendEngineMessage.mock.calls
    .map(([raw]) => { try { return JSON.parse(raw); } catch { return null; } })
    .filter((m) => m && m.type === type)
    .map((m) => m.payload);

  const startVoice = async () => {
    render(<App />);
    await flushPromises();
    emitEngineMessage({ type: '_ws_status', status: 'connected' });
    await flushPromises();
    emitEngineMessage({ type: 'SESSION_CREATE_RESPONSE', payload: { session_id: 's1' } });
    await flushPromises();
    fireEvent.click(screen.getByTestId('voice-header-btn'));
    emitEngineMessage({
      type: 'VOICE_SESSION_START_RESPONSE',
      payload: { ok: true, session_id: 's1', webrtc_config: { iceServers: [] }, error: null },
    });
    await flushPromises();
  };

  it('sends the overlay offer to the engine and applies the returning answer', async () => {
    await startVoice();
    expect(sentPayloads('VOICE_RTC_OFFER_REQUEST')).toEqual([
      { session_id: 's1', sdp: { type: 'offer', sdp: 'local-offer-sdp' } },
    ]);

    emitEngineMessage({
      type: 'VOICE_RTC_OFFER_RESPONSE',
      payload: {
        ok: true, session_id: 's1',
        answer: { type: 'answer', sdp: 'engine-answer-sdp' }, error: null,
      },
    });
    await flushPromises();
    expect(pc.setRemoteDescription).toHaveBeenCalledWith(
      { type: 'answer', sdp: 'engine-answer-sdp' });
  });

  it('applies engine ICE pushes to the peer and sends local candidates up', async () => {
    await startVoice();
    emitEngineMessage({
      type: 'VOICE_RTC_OFFER_RESPONSE',
      payload: {
        ok: true, session_id: 's1',
        answer: { type: 'answer', sdp: 'engine-answer-sdp' }, error: null,
      },
    });
    await flushPromises();

    const wire = { candidate: 'candidate:engine', sdpMid: '0', sdpMLineIndex: 0 };
    emitEngineMessage({
      type: 'VOICE_RTC_ICE',
      payload: { session_id: 's1', candidate: wire },
    });
    await flushPromises();
    expect(pc.addIceCandidate).toHaveBeenCalledWith(wire);

    pc.onicecandidate({
      candidate: { candidate: 'candidate:local', sdpMid: '0', sdpMLineIndex: 0 },
    });
    expect(sentPayloads('VOICE_RTC_ICE_REQUEST')).toEqual([
      { session_id: 's1', candidate: { candidate: 'candidate:local', sdpMid: '0', sdpMLineIndex: 0 } },
    ]);
  });

  it('a refused negotiation becomes the voice error, not a live overlay', async () => {
    await startVoice();
    emitEngineMessage({
      type: 'VOICE_RTC_OFFER_RESPONSE',
      payload: {
        ok: false, session_id: 's1', answer: null,
        error: 'voice unavailable: no active voice session',
      },
    });
    await flushPromises();
    expect(screen.getByTestId('voice-error')).toHaveTextContent('no active voice session');
    expect(screen.queryByTestId('voice-mic-indicator')).toBeNull();
  });
});
