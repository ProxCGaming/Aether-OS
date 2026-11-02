import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import VoiceOverlay from '../VoiceOverlay';

// F-12b (Astra M18): the overlay actually negotiates. Before this
// slice it created a peer connection and tracks and then did
// nothing with them — no offer, no ICE, no answer — so no media
// could ever flow and a dead connection looked identical to a live
// one. These tests pin the real signalling behaviour against a
// full RTCPeerConnection double:
//
//   * on capture it creates an offer, sets it locally, and reports
//     it via onRtcOffer; local ICE candidates (and the null
//     end-of-candidates marker) are forwarded via onRtcIceCandidate
//     in the engine's wire shape;
//   * the engine's answer (rtcAnswer prop) is applied with
//     setRemoteDescription; engine candidates (rtcRemoteCandidate)
//     are queued until the answer is in, then applied in order;
//   * connection state is surfaced honestly: connecting/connected/
//     disconnected render as a visible connection line, and
//     failed/closed replace the overlay with an error and a
//     dismiss — never a silent dead overlay;
//   * a live session whose environment has no RTCPeerConnection at
//     all says so instead of opening a microphone to nowhere.

describe('VoiceOverlay RTC negotiation (F-12b)', () => {
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
      setLocalDescription: jest.fn(() => {
        instance.signalingState = 'have-local-offer';
        return Promise.resolve();
      }),
      setRemoteDescription: jest.fn(() => {
        instance.signalingState = 'stable';
        return Promise.resolve();
      }),
      addIceCandidate: jest.fn(() => Promise.resolve()),
      onicecandidate: null,
      onconnectionstatechange: null,
    };
    return instance;
  };

  const fireConnectionState = (state) => {
    pc.connectionState = state;
    if (pc.onconnectionstatechange) pc.onconnectionstatechange();
  };

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
    delete window.navigator.mediaDevices;
    delete window.RTCPeerConnection;
    delete window.AudioContext;
  });

  const baseProps = {
    open: true,
    active: true,
    status: 'listening',
    partialText: '',
    assistantText: '',
    error: null,
    webrtcConfig: { iceServers: [] },
    audioChunk: null,
    playbackCut: 0,
    onStop: jest.fn(),
    onDismiss: jest.fn(),
    onRtcOffer: jest.fn(),
    rtcAnswer: null,
    onRtcIceCandidate: jest.fn(),
    rtcRemoteCandidate: null,
  };

  it('creates an offer on capture and reports the local description', async () => {
    const onRtcOffer = jest.fn();
    render(<VoiceOverlay {...baseProps} onRtcOffer={onRtcOffer} />);
    await flush();
    expect(pc.createOffer).toHaveBeenCalled();
    expect(pc.setLocalDescription).toHaveBeenCalled();
    expect(onRtcOffer).toHaveBeenCalledWith(
      { type: 'offer', sdp: 'local-offer-sdp' });
    expect(screen.getByTestId('voice-rtc-state')).toHaveTextContent(/connecting/i);
  });

  it('forwards local ICE candidates in wire shape, including the end marker', async () => {
    const onRtcIceCandidate = jest.fn();
    render(<VoiceOverlay {...baseProps} onRtcIceCandidate={onRtcIceCandidate} />);
    await flush();
    expect(typeof pc.onicecandidate).toBe('function');
    pc.onicecandidate({
      candidate: {
        candidate: 'candidate:1 1 udp 1 127.0.0.1 9 typ host',
        sdpMid: '0',
        sdpMLineIndex: 0,
      },
    });
    expect(onRtcIceCandidate).toHaveBeenCalledWith({
      candidate: 'candidate:1 1 udp 1 127.0.0.1 9 typ host',
      sdpMid: '0',
      sdpMLineIndex: 0,
    });
    pc.onicecandidate({ candidate: null });
    expect(onRtcIceCandidate).toHaveBeenCalledWith(null);
  });

  it('applies the engine answer and only then remote candidates, in order', async () => {
    const { rerender } = render(<VoiceOverlay {...baseProps} />);
    await flush();
    // A candidate that arrives before the answer must be queued,
    // not applied (the peer has no remote description yet).
    const early = { candidate: 'candidate:early', sdpMid: '0', sdpMLineIndex: 0 };
    rerender(<VoiceOverlay {...baseProps} rtcRemoteCandidate={{ seq: 1, candidate: early }} />);
    await flush();
    expect(pc.addIceCandidate).not.toHaveBeenCalled();

    const answer = { type: 'answer', sdp: 'engine-answer-sdp' };
    rerender(<VoiceOverlay
      {...baseProps}
      rtcRemoteCandidate={{ seq: 1, candidate: early }}
      rtcAnswer={{ seq: 1, answer }}
    />);
    await flush();
    expect(pc.setRemoteDescription).toHaveBeenCalledWith(answer);
    expect(pc.addIceCandidate).toHaveBeenCalledTimes(1);
    expect(pc.addIceCandidate).toHaveBeenCalledWith(early);

    const late = { candidate: 'candidate:late', sdpMid: '0', sdpMLineIndex: 0 };
    rerender(<VoiceOverlay
      {...baseProps}
      rtcAnswer={{ seq: 1, answer }}
      rtcRemoteCandidate={{ seq: 2, candidate: late }}
    />);
    await flush();
    expect(pc.addIceCandidate).toHaveBeenCalledTimes(2);
    expect(pc.addIceCandidate).toHaveBeenLastCalledWith(late);
  });

  it('shows connected once the peer reports it', async () => {
    render(<VoiceOverlay {...baseProps} />);
    await flush();
    await act(async () => { fireConnectionState('connected'); });
    expect(screen.getByTestId('voice-rtc-state')).toHaveTextContent(/connected/i);
    expect(screen.getByTestId('voice-mic-indicator')).toBeInTheDocument();
  });

  it('surfaces a failed connection as an error with a dismiss, not a dead overlay', async () => {
    const onDismiss = jest.fn();
    render(<VoiceOverlay {...baseProps} onDismiss={onDismiss} />);
    await flush();
    await act(async () => { fireConnectionState('failed'); });
    expect(screen.getByTestId('voice-error')).toHaveTextContent(/connection failed/i);
    fireEvent.click(screen.getByTestId('voice-dismiss-btn'));
    expect(onDismiss).toHaveBeenCalled();
  });

  it('surfaces a closed connection as an error too', async () => {
    render(<VoiceOverlay {...baseProps} />);
    await flush();
    await act(async () => { fireConnectionState('connected'); });
    await act(async () => { fireConnectionState('closed'); });
    expect(screen.getByTestId('voice-error')).toHaveTextContent(/connection closed/i);
  });

  it('notes a disconnected peer without killing the session UI', async () => {
    render(<VoiceOverlay {...baseProps} />);
    await flush();
    await act(async () => { fireConnectionState('connected'); });
    await act(async () => { fireConnectionState('disconnected'); });
    expect(screen.getByTestId('voice-rtc-state')).toHaveTextContent(/interrupted/i);
    expect(screen.getByTestId('voice-stop-btn')).toBeInTheDocument();
    await act(async () => { fireConnectionState('connected'); });
    expect(screen.getByTestId('voice-rtc-state')).toHaveTextContent(/connected/i);
  });

  it('says so honestly when the environment has no RTCPeerConnection', async () => {
    delete window.RTCPeerConnection;
    render(<VoiceOverlay {...baseProps} />);
    await flush();
    expect(screen.getByTestId('voice-error')).toHaveTextContent(/webrtc is not available/i);
  });

  it('a refused session still never negotiates', async () => {
    const onRtcOffer = jest.fn();
    render(<VoiceOverlay
      {...baseProps}
      active={false}
      status="error"
      error="voice unavailable: faster-whisper is not installed (local STT)"
      onRtcOffer={onRtcOffer}
    />);
    await flush();
    expect(pc.createOffer).not.toHaveBeenCalled();
    expect(onRtcOffer).not.toHaveBeenCalled();
    expect(screen.getByTestId('voice-error')).toHaveTextContent('faster-whisper is not installed');
  });
});
