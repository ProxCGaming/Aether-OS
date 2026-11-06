import { renderHook, act } from '@testing-library/react';
import { useVoiceSession } from '../VoiceSession';

// Wave 5 (Annex C row 11): the session engine split out of
// VoiceOverlay. The overlay suites pin the same behaviour through
// the component; these tests pin the hook's own contract — device
// discipline, the N09 arming report, negotiation, teardown, and
// the D3-05 per-session sequence reset — at the seam.

describe('useVoiceSession — session engine contract', () => {
  let tracks;
  let stream;
  let pc;

  const flush = async () => { await act(async () => {}); };

  const makePc = () => {
    const instance = {
      addTrack: jest.fn(),
      close: jest.fn(),
      connectionState: 'new',
      signalingState: 'stable',
      localDescription: { type: 'offer', sdp: 'local-offer-sdp' },
      createOffer: jest.fn(() => Promise.resolve({ type: 'offer', sdp: 'raw-offer' })),
      setLocalDescription: jest.fn(() => Promise.resolve()),
      setRemoteDescription: jest.fn(() => Promise.resolve()),
      addIceCandidate: jest.fn(() => Promise.resolve()),
      onicecandidate: null,
      onconnectionstatechange: null,
    };
    return instance;
  };

  const baseProps = (overrides = {}) => ({
    open: true,
    active: true,
    webrtcConfig: { iceServers: [] },
    onRtcOffer: jest.fn(),
    onRtcIceCandidate: jest.fn(),
    onPlaybackProgress: jest.fn(),
    ...overrides,
  });

  beforeEach(() => {
    tracks = [{ stop: jest.fn(), kind: 'audio' }];
    stream = { getTracks: jest.fn(() => tracks) };
    Object.defineProperty(window.navigator, 'mediaDevices', {
      configurable: true,
      value: { getUserMedia: jest.fn(() => Promise.resolve(stream)) },
    });
    pc = makePc();
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
    delete window.RTCPeerConnection;
    delete window.AudioContext;
  });

  it('never touches a device while the session is not live', async () => {
    renderHook(() => useVoiceSession(baseProps({ active: false })));
    await flush();
    expect(window.navigator.mediaDevices.getUserMedia).not.toHaveBeenCalled();
    renderHook(() => useVoiceSession(baseProps({ open: false })));
    await flush();
    expect(window.navigator.mediaDevices.getUserMedia).not.toHaveBeenCalled();
  });

  it('arms playback reporting with -1 when the session goes live (N09)', async () => {
    const props = baseProps();
    renderHook(() => useVoiceSession(props));
    await flush();
    expect(props.onPlaybackProgress).toHaveBeenCalledWith(-1);
  });

  it('captures, negotiates an offer, and reports it through onRtcOffer (F-12b)', async () => {
    const props = baseProps();
    const { result } = renderHook(() => useVoiceSession(props));
    await flush();
    expect(window.navigator.mediaDevices.getUserMedia).toHaveBeenCalledWith({ audio: true });
    expect(result.current.micLive).toBe(true);
    expect(result.current.rtcState).toBe('connecting');
    expect(props.onRtcOffer).toHaveBeenCalledWith({ type: 'offer', sdp: 'local-offer-sdp' });
  });

  it('surfaces a microphone refusal as micError and never goes live', async () => {
    window.navigator.mediaDevices.getUserMedia = jest.fn(
      () => Promise.reject(new Error('denied by OS'))
    );
    const { result } = renderHook(() => useVoiceSession(baseProps()));
    await flush();
    expect(result.current.micLive).toBe(false);
    expect(result.current.micError).toContain('Microphone unavailable: denied by OS');
  });

  it('teardownCapture stops every track, closes the peer, and resets state', async () => {
    const { result } = renderHook(() => useVoiceSession(baseProps()));
    await flush();
    expect(result.current.micLive).toBe(true);
    act(() => { result.current.teardownCapture(); });
    expect(tracks[0].stop).toHaveBeenCalled();
    expect(pc.close).toHaveBeenCalled();
    expect(result.current.micLive).toBe(false);
    expect(result.current.rtcState).toBe(null);
    expect(result.current.rtcError).toBe(null);
  });

  it('resets the answer sequence on teardown, so a second session accepts the same seq (D3-05)', async () => {
    const answer = { seq: 7, answer: { type: 'answer', sdp: 'remote-sdp' } };
    const { rerender } = renderHook(
      (props) => useVoiceSession(props),
      { initialProps: baseProps() }
    );
    await flush();
    rerender(baseProps({ rtcAnswer: answer }));
    await flush();
    expect(pc.setRemoteDescription).toHaveBeenCalledTimes(1);

    // Session ends (overlay closes) and a new one starts: the
    // engine restarts its seq numbering, so seq 7 must apply again
    // rather than being dropped as a duplicate.
    rerender(baseProps({ open: false, rtcAnswer: null }));
    await flush();
    pc = makePc();
    window.RTCPeerConnection = jest.fn(() => pc);
    rerender(baseProps({ rtcAnswer: null }));
    await flush();
    rerender(baseProps({ rtcAnswer: answer }));
    await flush();
    expect(pc.setRemoteDescription).toHaveBeenCalledTimes(1);
    expect(pc.setRemoteDescription).toHaveBeenCalledWith({ type: 'answer', sdp: 'remote-sdp' });
  });
});
