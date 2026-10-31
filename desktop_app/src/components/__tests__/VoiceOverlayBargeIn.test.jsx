import React from 'react';
import { render, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import VoiceOverlay from '../VoiceOverlay';

// Epic 8, E8-5.1b: barge-in playback cut. When the engine reports
// a barge-in (VOICE_BARGE_IN), App bumps `playbackCut`; the overlay
// must stop every scheduled-but-unplayed TTS source at once, reset
// its play head so the next chunk starts immediately, and drop a
// chunk whose decode finishes after the cut (it belongs to the
// reply the user just talked over).

describe('VoiceOverlay barge-in playback cut (E8-5.1b)', () => {
  let sources;
  let ctx;
  let stream;

  const flush = async () => { await act(async () => {}); };

  beforeEach(() => {
    sources = [];
    const tracks = [{ stop: jest.fn(), kind: 'audio' }];
    stream = { getTracks: jest.fn(() => tracks) };
    Object.defineProperty(window.navigator, 'mediaDevices', {
      configurable: true,
      value: { getUserMedia: jest.fn(() => Promise.resolve(stream)) },
    });
    window.RTCPeerConnection = jest.fn(() => ({ addTrack: jest.fn(), close: jest.fn() }));
    ctx = {
      currentTime: 0,
      destination: {},
      decodeAudioData: jest.fn(() => Promise.resolve({ duration: 0.5 })),
      createBufferSource: jest.fn(() => {
        const source = {
          buffer: null,
          connect: jest.fn(),
          start: jest.fn(),
          stop: jest.fn(),
          onended: null,
        };
        sources.push(source);
        return source;
      }),
      close: jest.fn(),
    };
    window.AudioContext = jest.fn(() => ctx);
  });

  afterEach(() => {
    delete window.navigator.mediaDevices;
    delete window.RTCPeerConnection;
    delete window.AudioContext;
  });

  const baseProps = {
    open: true,
    active: true,
    status: 'speaking',
    partialText: '',
    assistantText: 'The bicycle is red.',
    error: null,
    webrtcConfig: { iceServers: [] },
    audioChunk: null,
    playbackCut: 0,
    onStop: jest.fn(),
    onDismiss: jest.fn(),
  };

  const wav = () => btoa('RIFFfake-wav-bytes');

  it('stops every scheduled source when the cut lands', async () => {
    const { rerender } = render(<VoiceOverlay {...baseProps} />);
    await flush();
    rerender(<VoiceOverlay {...baseProps} audioChunk={{ seq: 1, data: wav() }} />);
    await flush();
    rerender(<VoiceOverlay {...baseProps} audioChunk={{ seq: 2, data: wav() }} />);
    await flush();
    expect(sources).toHaveLength(2);
    expect(sources[0].start).toHaveBeenCalled();
    expect(sources[1].start).toHaveBeenCalled();

    rerender(<VoiceOverlay {...baseProps} playbackCut={1} />);
    await flush();
    expect(sources[0].stop).toHaveBeenCalledTimes(1);
    expect(sources[1].stop).toHaveBeenCalledTimes(1);
  });

  it('resets the play head so post-cut audio starts immediately', async () => {
    const { rerender } = render(<VoiceOverlay {...baseProps} />);
    await flush();
    rerender(<VoiceOverlay {...baseProps} audioChunk={{ seq: 1, data: wav() }} />);
    await flush();
    // First chunk: starts at 0, play head advances to 0.5.
    expect(sources[0].start).toHaveBeenCalledWith(0);

    rerender(<VoiceOverlay {...baseProps} playbackCut={1} />);
    await flush();
    rerender(<VoiceOverlay {...baseProps} playbackCut={1} audioChunk={{ seq: 2, data: wav() }} />);
    await flush();
    // Without the reset this would be scheduled at 0.5 (the stale
    // play head) — after a barge-in the new speech plays NOW.
    expect(sources[1].start).toHaveBeenCalledWith(0);
  });

  it('drops a chunk whose decode finishes after the cut', async () => {
    let resolveDecode;
    ctx.decodeAudioData = jest.fn(() => new Promise((resolve) => { resolveDecode = resolve; }));
    const { rerender } = render(<VoiceOverlay {...baseProps} />);
    await flush();
    rerender(<VoiceOverlay {...baseProps} audioChunk={{ seq: 1, data: wav() }} />);
    await flush();
    // The cut lands while the chunk is still decoding.
    rerender(<VoiceOverlay {...baseProps} playbackCut={1} />);
    await flush();
    resolveDecode({ duration: 0.5 });
    await flush();
    expect(sources).toHaveLength(0);
    expect(ctx.createBufferSource).not.toHaveBeenCalled();
  });

  it('does not cut on mount or on unrelated re-renders', async () => {
    const { rerender } = render(<VoiceOverlay {...baseProps} playbackCut={3} />);
    await flush();
    rerender(<VoiceOverlay {...baseProps} playbackCut={3} audioChunk={{ seq: 1, data: wav() }} />);
    await flush();
    expect(sources).toHaveLength(1);
    expect(sources[0].stop).not.toHaveBeenCalled();
  });
});
