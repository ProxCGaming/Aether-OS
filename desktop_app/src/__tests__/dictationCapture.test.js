// F1 (linkage audit): the dictation capture module — encoding
// helpers and the capture session, unit-tested with a mocked
// Web Audio graph (jsdom has no media APIs). The wire contract
// under test is the engine's: base64 raw PCM16 mono at
// 16000 Hz, cumulative chunks, final audio on stop
// (see src/dictationCapture.js for the pinned evidence).

import {
  startDictationCapture,
  resampleFloat32,
  float32ToPcm16,
  pcm16ToBase64,
  encodeDictationAudio,
  DICTATION_SAMPLE_RATE,
} from '../dictationCapture';

function makeMockContext({ sampleRate = 48000 } = {}) {
  const processor = { connect: jest.fn(), disconnect: jest.fn(), onaudioprocess: null };
  const source = { connect: jest.fn(), disconnect: jest.fn() };
  const gain = { gain: { value: 1 }, connect: jest.fn(), disconnect: jest.fn() };
  const ctx = {
    sampleRate,
    state: 'running',
    destination: {},
    createMediaStreamSource: jest.fn(() => source),
    createScriptProcessor: jest.fn(() => processor),
    createGain: jest.fn(() => gain),
    resume: jest.fn(() => Promise.resolve()),
    close: jest.fn(() => Promise.resolve()),
    __processor: processor,
    __source: source,
    __gain: gain,
  };
  return ctx;
}

function makeStream() {
  const track = { stop: jest.fn(), kind: 'audio' };
  return { getTracks: jest.fn(() => [track]), __track: track };
}

function pushAudio(ctx, samples) {
  ctx.__processor.onaudioprocess({
    inputBuffer: { getChannelData: () => Float32Array.from(samples) },
  });
}

function decodePcm(b64) {
  const buf = Buffer.from(b64, 'base64');
  const out = [];
  for (let i = 0; i + 1 < buf.length; i += 2) out.push(buf.readInt16LE(i));
  return out;
}

describe('dictationCapture encoding helpers', () => {
  it('float32ToPcm16 converts and clamps to int16 range', () => {
    expect(Array.from(float32ToPcm16(new Float32Array([0, 1, -1, 0.5, -0.5, 2, -2]))))
      .toEqual([0, 32767, -32768, 16384, -16384, 32767, -32768]);
  });

  it('resampleFloat32 boxcar-averages 48k -> 16k and passes 16k through', () => {
    const out = resampleFloat32(new Float32Array([0, 0.3, 0.6, 0.9, 0.2, 0.5]), 48000, 16000);
    expect(out.length).toBe(2);
    expect(out[0]).toBeCloseTo(0.3);
    expect(out[1]).toBeCloseTo(0.5333, 3);
    const same = resampleFloat32(new Float32Array([0.1, -0.2]), 16000, 16000);
    expect(same.length).toBe(2);
    expect(same[0]).toBeCloseTo(0.1);
    expect(same[1]).toBeCloseTo(-0.2);
  });

  it('pcm16ToBase64 round-trips little-endian samples', () => {
    const b64 = pcm16ToBase64(new Int16Array([0, 32767, -32768, 12345]));
    expect(decodePcm(b64)).toEqual([0, 32767, -32768, 12345]);
  });

  it('encodeDictationAudio yields PCM16 at the dictation rate', () => {
    // 4800 samples @48k = 0.1 s -> 1600 samples @16k.
    const b64 = encodeDictationAudio(new Float32Array(4800).fill(0.5), 48000);
    const pcm = decodePcm(b64);
    expect(pcm.length).toBe(1600);
    expect(pcm[0]).toBe(16384);
    expect(DICTATION_SAMPLE_RATE).toBe(16000);
  });
});

describe('startDictationCapture session', () => {
  let ctx;

  beforeEach(() => {
    jest.useFakeTimers();
    ctx = makeMockContext();
    window.AudioContext = jest.fn(() => ctx);
  });

  afterEach(() => {
    jest.useRealTimers();
    delete window.AudioContext;
    delete window.webkitAudioContext;
  });

  it('streams cumulative PCM16 chunks on the cadence, only when audio grew', () => {
    const onChunk = jest.fn();
    const session = startDictationCapture({ stream: makeStream(), onChunk });
    expect(ctx.createMediaStreamSource).toHaveBeenCalled();
    expect(ctx.__source.connect).toHaveBeenCalledWith(ctx.__processor);
    // The mic is routed through a zero gain — never audible.
    expect(ctx.__gain.gain.value).toBe(0);

    pushAudio(ctx, new Array(4800).fill(0.5)); // 0.1 s @48k
    jest.advanceTimersByTime(2000);
    expect(onChunk).toHaveBeenCalledTimes(1);
    const first = onChunk.mock.calls[0][0];
    expect(first.sample_rate).toBe(16000);
    expect(decodePcm(first.audio).length).toBe(1600);

    // No new audio -> no re-send of the same buffer.
    jest.advanceTimersByTime(2000);
    expect(onChunk).toHaveBeenCalledTimes(1);

    pushAudio(ctx, new Array(4800).fill(0.5));
    jest.advanceTimersByTime(2000);
    expect(onChunk).toHaveBeenCalledTimes(2);
    const second = onChunk.mock.calls[1][0];
    // Cumulative: the second chunk carries the whole utterance.
    expect(decodePcm(second.audio).length).toBe(3200);
    session.stop();
  });

  it('stop returns the complete utterance and tears the graph down', () => {
    const onChunk = jest.fn();
    const session = startDictationCapture({ stream: makeStream(), onChunk });
    pushAudio(ctx, new Array(9600).fill(0.25)); // 0.2 s @48k

    const final = session.stop();
    expect(final.sample_rate).toBe(16000);
    expect(decodePcm(final.audio).length).toBe(3200);
    expect(ctx.__processor.disconnect).toHaveBeenCalled();
    expect(ctx.__source.disconnect).toHaveBeenCalled();
    expect(ctx.close).toHaveBeenCalled();

    // Late audio + timer ticks after stop are inert; a second
    // stop reports silence instead of re-sending the utterance.
    pushAudio(ctx, new Array(4800).fill(0.9));
    jest.advanceTimersByTime(10000);
    expect(onChunk).not.toHaveBeenCalled();
    expect(session.stop()).toEqual({ audio: '', sample_rate: 16000 });
  });

  it('stop with no captured audio returns empty audio (engine-honest silence)', () => {
    const session = startDictationCapture({ stream: makeStream(), onChunk: jest.fn() });
    expect(session.stop()).toEqual({ audio: '', sample_rate: 16000 });
    expect(ctx.close).toHaveBeenCalled();
  });

  it('throws when the Web Audio graph is unavailable', () => {
    delete window.AudioContext;
    expect(() => startDictationCapture({ stream: makeStream(), onChunk: jest.fn() }))
      .toThrow(/not available/);

    const bareCtx = { sampleRate: 48000, close: jest.fn() };
    window.AudioContext = jest.fn(() => bareCtx);
    expect(() => startDictationCapture({ stream: makeStream(), onChunk: jest.fn() }))
      .toThrow(/not available/);
    expect(bareCtx.close).toHaveBeenCalled();
  });
});
