import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import VoiceOverlay from '../VoiceOverlay';

// Epic 8, E8-5.1a: the active voice interface. Device discipline is
// the point: the microphone is opened only while the overlay is
// open AND the engine session is live (active), and every track is
// stopped when the session ends, the user stops it, or the overlay
// unmounts. A refused session (error state) never touches a device.

describe('VoiceOverlay (E8-5.1a)', () => {
  let tracks;
  let stream;
  let pcInstance;

  const flush = async () => { await act(async () => {}); };

  beforeEach(() => {
    tracks = [{ stop: jest.fn(), kind: 'audio' }];
    stream = { getTracks: jest.fn(() => tracks) };
    Object.defineProperty(window.navigator, 'mediaDevices', {
      configurable: true,
      value: { getUserMedia: jest.fn(() => Promise.resolve(stream)) },
    });
    pcInstance = { addTrack: jest.fn(), close: jest.fn() };
    window.RTCPeerConnection = jest.fn(() => pcInstance);
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
    webrtcConfig: { iceServers: [{ urls: 'stun:example' }] },
    audioChunk: null,
    onStop: jest.fn(),
    onDismiss: jest.fn(),
  };

  it('renders nothing when closed', () => {
    const { container } = render(<VoiceOverlay {...baseProps} open={false} />);
    expect(container.firstChild).toBeNull();
    expect(window.navigator.mediaDevices.getUserMedia).not.toHaveBeenCalled();
  });

  it('opens the microphone only once the session is live, and shows the live indicator', async () => {
    render(<VoiceOverlay {...baseProps} active={false} status="starting" />);
    await flush();
    expect(window.navigator.mediaDevices.getUserMedia).not.toHaveBeenCalled();
    expect(screen.queryByTestId('voice-mic-indicator')).toBeNull();
  });

  it('captures the mic and wires the peer connection when active', async () => {
    render(<VoiceOverlay {...baseProps} />);
    await flush();
    expect(window.navigator.mediaDevices.getUserMedia).toHaveBeenCalledWith({ audio: true });
    expect(window.RTCPeerConnection).toHaveBeenCalledWith(
      expect.objectContaining({ iceServers: [{ urls: 'stun:example' }] }));
    expect(pcInstance.addTrack).toHaveBeenCalled();
    expect(screen.getByTestId('voice-mic-indicator')).toBeInTheDocument();
    expect(screen.getByTestId('voice-waveform')).toHaveClass('voice-waveform-live');
  });

  it('shows the error state without ever touching a device', async () => {
    render(<VoiceOverlay
      {...baseProps}
      active={false}
      status="error"
      error="voice unavailable: faster-whisper is not installed (local STT)"
    />);
    await flush();
    expect(screen.getByTestId('voice-error')).toHaveTextContent('faster-whisper is not installed');
    expect(window.navigator.mediaDevices.getUserMedia).not.toHaveBeenCalled();
    expect(screen.queryByTestId('voice-mic-indicator')).toBeNull();
  });

  it('dismiss button fires onDismiss from the error state', async () => {
    const onDismiss = jest.fn();
    render(<VoiceOverlay {...baseProps} active={false} status="error" error="nope" onDismiss={onDismiss} />);
    fireEvent.click(screen.getByTestId('voice-dismiss-btn'));
    expect(onDismiss).toHaveBeenCalled();
  });

  it('renders the live partial transcript and the streaming reply', async () => {
    render(<VoiceOverlay {...baseProps} partialText="the red" assistantText="The bicycle" status="speaking" />);
    await flush();
    expect(screen.getByTestId('voice-partial')).toHaveTextContent('the red');
    expect(screen.getByTestId('voice-assistant-stream')).toHaveTextContent('The bicycle');
    expect(screen.getByTestId('voice-status')).toHaveTextContent(/speaking/i);
  });

  it('stop button ends the session and stops the mic tracks immediately', async () => {
    const onStop = jest.fn();
    render(<VoiceOverlay {...baseProps} onStop={onStop} />);
    await flush();
    fireEvent.click(screen.getByTestId('voice-stop-btn'));
    expect(onStop).toHaveBeenCalled();
    expect(tracks[0].stop).toHaveBeenCalled();
  });

  it('unmount stops tracks and closes the peer connection', async () => {
    const { unmount } = render(<VoiceOverlay {...baseProps} />);
    await flush();
    unmount();
    expect(tracks[0].stop).toHaveBeenCalled();
    expect(pcInstance.close).toHaveBeenCalled();
  });

  it('plays TTS audio chunks through the audio context', async () => {
    const { rerender } = render(<VoiceOverlay {...baseProps} />);
    await flush();
    const wavB64 = btoa('RIFFfake-wav-bytes');
    rerender(<VoiceOverlay {...baseProps} audioChunk={{ seq: 1, data: wavB64 }} />);
    await flush();
    const ctx = window.AudioContext.mock.results[0].value;
    expect(ctx.decodeAudioData).toHaveBeenCalled();
    expect(ctx.createBufferSource).toHaveBeenCalled();
  });

  it('shows a mic-unavailable note instead of crashing without mediaDevices', async () => {
    delete window.navigator.mediaDevices;
    render(<VoiceOverlay {...baseProps} />);
    await flush();
    expect(screen.getByTestId('voice-mic-error')).toBeInTheDocument();
    expect(screen.queryByTestId('voice-mic-indicator')).toBeNull();
  });

  // N09: playback progress reporting — the engine's heard-word
  // accounting reads these reports, so a chunk counts as heard
  // only when its source ends naturally, never when a cut stops
  // it, and reporting arms (-1) as soon as the session is live.
  describe('playback progress reporting (N09)', () => {
    const installAudioCapture = () => {
      const state = { sources: [] };
      window.AudioContext = jest.fn(() => ({
        currentTime: 0,
        destination: {},
        decodeAudioData: jest.fn(() => Promise.resolve({ duration: 0.5 })),
        createBufferSource: jest.fn(() => {
          const source = {
            connect: jest.fn(), start: jest.fn(), stop: jest.fn(),
            buffer: null, onended: null,
          };
          state.sources.push(source);
          return source;
        }),
        close: jest.fn(),
      }));
      return state;
    };

    it('arms playback reporting (-1) when the session goes live', async () => {
      const onPlaybackProgress = jest.fn();
      render(<VoiceOverlay {...baseProps} onPlaybackProgress={onPlaybackProgress} />);
      await flush();
      expect(onPlaybackProgress).toHaveBeenCalledWith(-1);
    });

    it('reports a chunk as played when its source ends naturally', async () => {
      const audio = installAudioCapture();
      const onPlaybackProgress = jest.fn();
      const { rerender } = render(
        <VoiceOverlay {...baseProps} onPlaybackProgress={onPlaybackProgress} />);
      await flush();
      const wavB64 = btoa('RIFFfake-wav-bytes');
      rerender(<VoiceOverlay
        {...baseProps}
        onPlaybackProgress={onPlaybackProgress}
        audioChunk={{ seq: 1, engineSeq: 7, data: wavB64 }}
      />);
      await flush();
      expect(audio.sources).toHaveLength(1);
      expect(onPlaybackProgress).not.toHaveBeenCalledWith(7);
      act(() => { audio.sources[0].onended(); });
      expect(onPlaybackProgress).toHaveBeenCalledWith(7);
    });

    it('never reports a chunk stopped by a playback cut', async () => {
      const audio = installAudioCapture();
      const onPlaybackProgress = jest.fn();
      const { rerender } = render(
        <VoiceOverlay {...baseProps} onPlaybackProgress={onPlaybackProgress} />);
      await flush();
      const wavB64 = btoa('RIFFfake-wav-bytes');
      rerender(<VoiceOverlay
        {...baseProps}
        onPlaybackProgress={onPlaybackProgress}
        audioChunk={{ seq: 1, engineSeq: 7, data: wavB64 }}
      />);
      await flush();
      expect(audio.sources).toHaveLength(1);
      // A barge-in cut stops the scheduled source; Web Audio still
      // fires its onended — that end must NOT be reported as heard.
      rerender(<VoiceOverlay
        {...baseProps}
        onPlaybackProgress={onPlaybackProgress}
        playbackCut={1}
      />);
      await flush();
      expect(audio.sources[0].stop).toHaveBeenCalled();
      act(() => { audio.sources[0].onended(); });
      expect(onPlaybackProgress).not.toHaveBeenCalledWith(7);
    });

    it('does not report chunks that carry no engine sequence', async () => {
      const audio = installAudioCapture();
      const onPlaybackProgress = jest.fn();
      const { rerender } = render(
        <VoiceOverlay {...baseProps} onPlaybackProgress={onPlaybackProgress} />);
      await flush();
      const wavB64 = btoa('RIFFfake-wav-bytes');
      rerender(<VoiceOverlay
        {...baseProps}
        onPlaybackProgress={onPlaybackProgress}
        audioChunk={{ seq: 1, data: wavB64 }}
      />);
      await flush();
      expect(audio.sources).toHaveLength(1);
      act(() => { audio.sources[0].onended(); });
      expect(onPlaybackProgress).toHaveBeenCalledTimes(1);
      expect(onPlaybackProgress).toHaveBeenCalledWith(-1);
    });
  });
});
