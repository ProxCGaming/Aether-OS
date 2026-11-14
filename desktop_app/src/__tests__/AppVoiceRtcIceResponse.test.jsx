import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import App from '../App';

// Master build S2: VOICE_RTC_ICE_RESPONSE (the answer to the
// renderer's VOICE_RTC_ICE_REQUEST candidates going up) previously
// had NO handler in the renderer — an engine refusal (e.g. no active
// voice session) landed nowhere and the overlay could sit
// live-looking behind a failed negotiation. The new branch mirrors
// the VOICE_RTC_OFFER_RESPONSE refusal: ok:false for the live
// session flips voice to its error state; ok:true is a pure
// acknowledgement and changes nothing. Harness follows
// AppVoiceRtc.test.jsx (F-12b).

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

describe('App S2 VOICE_RTC_ICE_RESPONSE handling', () => {
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

  it('a refused ICE candidate becomes the voice error, not a live overlay', async () => {
    await startVoice();
    emitEngineMessage({
      type: 'VOICE_RTC_ICE_RESPONSE',
      payload: {
        ok: false, session_id: 's1',
        error: 'voice unavailable: no active voice session',
      },
    });
    await flushPromises();
    expect(screen.getByTestId('voice-error')).toHaveTextContent('no active voice session');
    expect(screen.queryByTestId('voice-mic-indicator')).toBeNull();
  });

  it('an accepted ICE candidate (ok:true) is an ack — the session stays live, no error', async () => {
    await startVoice();
    emitEngineMessage({
      type: 'VOICE_RTC_ICE_RESPONSE',
      payload: { ok: true, session_id: 's1', error: null },
    });
    await flushPromises();
    expect(screen.queryByTestId('voice-error')).toBeNull();
    expect(screen.getByTestId('voice-mic-indicator')).toBeInTheDocument();
  });

  it('an ICE refusal for a different session does not touch the live one', async () => {
    await startVoice();
    emitEngineMessage({
      type: 'VOICE_RTC_ICE_RESPONSE',
      payload: {
        ok: false, session_id: 'someone-elses-session',
        error: 'voice unavailable: no active voice session',
      },
    });
    await flushPromises();
    expect(screen.queryByTestId('voice-error')).toBeNull();
    expect(screen.getByTestId('voice-mic-indicator')).toBeInTheDocument();
  });
});
