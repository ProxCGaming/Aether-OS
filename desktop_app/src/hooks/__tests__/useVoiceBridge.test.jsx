import React from 'react';
import { render, act } from '@testing-library/react';
import { useVoiceBridge } from '../useVoiceBridge';

// Wave 5, Annex C row 5 (voice bridge): contract tests for the
// extracted voice state-machine glue. Driven through a fake socket
// and the active-session identity App hands it; assertions cover
// the wire requests, the offline error states (the overlay must
// never sit live with no engine), the stop-commit, and the
// session-change teardown.

global.WebSocket = { OPEN: 1, CLOSED: 3, CONNECTING: 0, CLOSING: 2 };

function makeDeps({ online = true } = {}) {
  const ws = { readyState: online ? 1 : 3, send: jest.fn() };
  return {
    wsRef: { current: ws },
    activeSessionRef: { current: 's1' },
    activeSession: 's1',
    setChatMessages: jest.fn(),
    _ws: ws,
  };
}

let bridge;
function Probe({ deps }) {
  bridge = useVoiceBridge(deps);
  return null;
}

function mount(opts) {
  const deps = makeDeps(opts);
  const utils = render(<Probe deps={deps} />);
  const sent = () => deps._ws.send.mock.calls.map(([raw]) => JSON.parse(raw));
  return { deps, sent, ...utils };
}

const ACTIVE = {
  status: 'active', sessionId: 's1', partialText: '',
  assistantText: 'spoken reply', announcementText: '', error: null, webrtcConfig: null,
};

describe('useVoiceBridge', () => {
  afterEach(() => {
    jest.restoreAllMocks();
  });

  it('start online: sends the start request and enters the starting state', () => {
    const { sent } = mount();
    act(() => { bridge.handleVoiceStart(); });
    expect(sent().at(-1)).toEqual(expect.objectContaining({
      type: 'VOICE_SESSION_START_REQUEST',
      payload: { session_id: 's1' },
    }));
    expect(bridge.voiceState.status).toBe('starting');
    expect(bridge.voiceStateRef.current).toBe(bridge.voiceState);
  });

  it('start offline: no send, and the state is the honest error', () => {
    const { deps } = mount({ online: false });
    act(() => { bridge.handleVoiceStart(); });
    expect(deps._ws.send).not.toHaveBeenCalled();
    expect(bridge.voiceState.status).toBe('error');
    expect(bridge.voiceState.error).toMatch(/not connected/);
  });

  it('start is refused while a live session exists', () => {
    const { deps } = mount();
    act(() => { bridge.applyVoiceState(ACTIVE); });
    deps._ws.send.mockClear();
    act(() => { bridge.handleVoiceStart(); });
    expect(deps._ws.send).not.toHaveBeenCalled();
    expect(bridge.voiceState.status).toBe('active');
  });

  it('stop commits the streamed reply as a chat message and tears down', () => {
    const { deps, sent } = mount();
    act(() => { bridge.applyVoiceState(ACTIVE); });
    act(() => { bridge.handleVoiceStop(); });
    expect(sent().at(-1)).toEqual(expect.objectContaining({
      type: 'VOICE_SESSION_STOP_REQUEST',
      payload: { session_id: 's1' },
    }));
    expect(bridge.voiceState).toBeNull();
    expect(bridge.voiceStateRef.current).toBeNull();
    const updater = deps.setChatMessages.mock.calls.at(-1)[0];
    const next = updater([]);
    expect(next).toHaveLength(1);
    expect(next[0]).toEqual(expect.objectContaining({
      role: 'agent', content: 'spoken reply', isFinal: true,
    }));
  });

  it('stop with no session is a no-op', () => {
    const { deps } = mount();
    act(() => { bridge.handleVoiceStop(); });
    expect(deps._ws.send).not.toHaveBeenCalled();
  });

  it('RTC offer / ICE / playback progress ride the live session', () => {
    const { sent } = mount();
    act(() => { bridge.applyVoiceState(ACTIVE); });
    act(() => { bridge.handleVoiceRtcOffer('sdp-offer'); });
    expect(sent().at(-1)).toEqual(expect.objectContaining({
      type: 'VOICE_RTC_OFFER_REQUEST',
      payload: { session_id: 's1', sdp: 'sdp-offer' },
    }));
    act(() => { bridge.handleVoiceRtcIceCandidate({ candidate: 'c1' }); });
    expect(sent().at(-1)).toEqual(expect.objectContaining({
      type: 'VOICE_RTC_ICE_REQUEST',
      payload: { session_id: 's1', candidate: { candidate: 'c1' } },
    }));
    act(() => { bridge.handleVoicePlaybackProgress(7); });
    expect(sent().at(-1)).toEqual(expect.objectContaining({
      type: 'VOICE_PLAYBACK_PROGRESS',
      payload: { session_id: 's1', played_seq: 7 },
    }));
  });

  it('leaving the voice session\'s chat stops the engine session', () => {
    const { deps, sent, rerender } = mount();
    act(() => { bridge.applyVoiceState(ACTIVE); });
    deps.activeSession = 's2';
    rerender(<Probe deps={deps} />);
    expect(sent().at(-1)).toEqual(expect.objectContaining({
      type: 'VOICE_SESSION_STOP_REQUEST',
      payload: { session_id: 's1' },
    }));
    expect(bridge.voiceState).toBeNull();
  });

  it('dictation: start/stop requests and the offline error state', () => {
    const { sent } = mount();
    act(() => { bridge.handleDictationStart(); });
    expect(sent().at(-1)).toEqual(expect.objectContaining({
      type: 'DICTATION_START_REQUEST',
      payload: { session_id: 's1' },
    }));
    expect(bridge.dictationState.status).toBe('starting');
    act(() => { bridge.handleDictationStop(); });
    expect(sent().at(-1)).toEqual(expect.objectContaining({ type: 'DICTATION_STOP_REQUEST' }));
    expect(bridge.dictationState.status).toBe('idle');

    const off = mount({ online: false });
    act(() => { bridge.handleDictationStart(); });
    expect(bridge.dictationState.status).toBe('error');
    off.unmount();
  });

  it('read-aloud: start/stop requests, state lifecycle, offline error', () => {
    const { sent } = mount();
    act(() => { bridge.handleReadAloudStart('m1', 'read me'); });
    expect(sent().at(-1)).toEqual(expect.objectContaining({
      type: 'READ_ALOUD_START_REQUEST',
      payload: { message_id: 'm1', text: 'read me' },
    }));
    expect(bridge.readAloudState).toEqual({ messageId: 'm1', status: 'starting', error: null });
    act(() => { bridge.handleReadAloudStop('m1'); });
    expect(sent().at(-1)).toEqual(expect.objectContaining({
      type: 'READ_ALOUD_STOP_REQUEST',
      payload: { message_id: 'm1' },
    }));
    expect(bridge.readAloudState).toBeNull();

    const off = mount({ online: false });
    act(() => { bridge.handleReadAloudStart('m2', 'nope'); });
    expect(bridge.readAloudState.status).toBe('error');
    off.unmount();
  });
});
