// Mic capture for push-to-talk dictation (linkage-audit F1 fix).
//
// The break this closes: the composer mic opened a MediaStream
// and held it, but nothing ever read the stream — no
// DICTATION_CHUNK_REQUEST was ever sent, so the engine's STT
// could never receive audio (FIX-PLAN, "Post-Wave-6 linkage
// audit", flag F1).
//
// Wire contract, pinned from the engine (do not re-derive):
//   - audio is BASE64 RAW PCM16 MONO samples — the engine wraps
//     the bytes in a WAV container itself before transcribing
//     (aether_engine/voice/stt.py: pcm16_to_wav + the SttEngine
//     protocol, "Transcribe raw PCM16 mono audio").
//   - DICTATION_CHUNK_REQUEST payload: { audio, sample_rate,
//     is_final } (aether_engine/routes/websocket.py,
//     _handle_dictation_chunk_request); sample_rate defaults to
//     16000 engine-side (_coerce_sample_rate).
//   - DICTATION_STOP_REQUEST payload: { audio, sample_rate };
//     a missing/empty audio is honest silence, answered ok with
//     empty text (aether_engine/routes/ws_voice.py,
//     send_dictation_stop_result).
//   - The engine chains chunk + stop work in arrival order
//     (enqueue_dictation, RUX-B-09), so sends from here may be
//     fire-and-forget: the stop response can never overtake a
//     chunk response.
//
// Chunking semantics — CUMULATIVE, not segmented: every chunk
// carries the whole utterance buffer captured so far, and the
// stop carries the complete buffer. That is the only reading
// consistent with the consumers: CHUNK_RESPONSE and
// STOP_RESPONSE each REPLACE dictationState.text
// (src/hooks/useEngineMessages.js), which DraggableChatWindow
// composes over the input snapshot from press time — a chunk
// response is a provisional full-so-far transcript, the stop
// response the committed one. It is also the STT module's own
// model: "the pipeline re-transcribes the growing utterance
// buffer on a cadence and emits each improved text as a
// provisional partial" (aether_engine/voice/stt.py docstring).
//
// Cadence: no interval is pinned anywhere in the code. 2 s
// keeps partials feeling live while the engine's serialised
// transcription chain (each link can take seconds on CPU)
// stays short; a chunk is only sent when new audio arrived
// since the previous one.

export const DICTATION_SAMPLE_RATE = 16000;
export const DICTATION_CHUNK_INTERVAL_MS = 2000;

const SCRIPT_BUFFER_SIZE = 4096;

/** Boxcar-resample mono Float32 audio between sample rates. */
export function resampleFloat32(samples, inputRate, outputRate = DICTATION_SAMPLE_RATE) {
  if (!samples || samples.length === 0) return new Float32Array(0);
  if (!inputRate || !outputRate || inputRate === outputRate) {
    return Float32Array.from(samples);
  }
  const ratio = inputRate / outputRate;
  const outLength = Math.max(1, Math.round(samples.length / ratio));
  const out = new Float32Array(outLength);
  for (let i = 0; i < outLength; i += 1) {
    const start = Math.floor(i * ratio);
    const end = Math.min(samples.length, Math.max(start + 1, Math.floor((i + 1) * ratio)));
    let sum = 0;
    for (let j = start; j < end; j += 1) sum += samples[j];
    out[i] = sum / (end - start);
  }
  return out;
}

/** Float32 [-1, 1] samples -> little-endian PCM16. */
export function float32ToPcm16(samples) {
  const out = new Int16Array(samples.length);
  for (let i = 0; i < samples.length; i += 1) {
    const s = Math.max(-1, Math.min(1, samples[i] || 0));
    out[i] = s < 0 ? Math.round(s * 0x8000) : Math.round(s * 0x7FFF);
  }
  return out;
}

/** PCM16 -> base64 (chunked so long utterances stay off the call-stack limit). */
export function pcm16ToBase64(pcm) {
  const bytes = new Uint8Array(pcm.buffer, pcm.byteOffset, pcm.byteLength);
  let binary = '';
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
  }
  return btoa(binary);
}

/** Resample + convert + base64 one Float32 buffer at `inputRate`. */
export function encodeDictationAudio(floatSamples, inputRate) {
  const resampled = resampleFloat32(floatSamples, inputRate, DICTATION_SAMPLE_RATE);
  return pcm16ToBase64(float32ToPcm16(resampled));
}

/**
 * Start reading `stream` (an already-acquired mic MediaStream —
 * the caller owns getUserMedia and its error copy) and stream
 * cumulative dictation chunks to `onChunk({ audio, sample_rate })`.
 *
 * Returns a session: `stop()` disconnects the graph, closes the
 * AudioContext and returns the final `{ audio, sample_rate }`
 * for the STOP request (audio '' when nothing was captured —
 * the engine tolerates it as silence). Throws when the Web
 * Audio graph is unavailable, so the caller can surface its
 * microphone-unavailable error and release the stream.
 */
export function startDictationCapture({ stream, onChunk, chunkIntervalMs = DICTATION_CHUNK_INTERVAL_MS } = {}) {
  const Ctx = typeof window !== 'undefined'
    ? (window.AudioContext || window.webkitAudioContext)
    : null;
  if (!Ctx || !stream) {
    throw new Error('Audio capture is not available in this environment.');
  }
  const ctx = new Ctx();
  if (typeof ctx.createMediaStreamSource !== 'function'
    || typeof ctx.createScriptProcessor !== 'function') {
    try { ctx.close(); } catch { /* best effort */ }
    throw new Error('Audio capture is not available in this environment.');
  }
  const inputRate = (typeof ctx.sampleRate === 'number' && ctx.sampleRate > 0)
    ? ctx.sampleRate
    : DICTATION_SAMPLE_RATE;

  const source = ctx.createMediaStreamSource(stream);
  const processor = ctx.createScriptProcessor(SCRIPT_BUFFER_SIZE, 1, 1);

  // Captured Float32 frames at the context's own rate; the
  // whole buffer is re-encoded per chunk/stop (cumulative
  // semantics above), so no incremental encoder state exists.
  const frames = [];
  let totalSamples = 0;
  let lastChunkSamples = 0;
  let stopped = false;

  processor.onaudioprocess = (event) => {
    if (stopped) return;
    try {
      const data = event.inputBuffer.getChannelData(0);
      const copy = Float32Array.from(data);
      frames.push(copy);
      totalSamples += copy.length;
    } catch { /* a torn-down graph must not throw into the audio thread */ }
  };

  source.connect(processor);
  // A ScriptProcessor only runs while connected towards the
  // destination; route through a zero gain so the mic is never
  // audible in the speakers.
  if (typeof ctx.createGain === 'function' && ctx.destination) {
    const mute = ctx.createGain();
    mute.gain.value = 0;
    processor.connect(mute);
    mute.connect(ctx.destination);
  } else if (ctx.destination) {
    processor.connect(ctx.destination);
  }
  if (ctx.state === 'suspended' && typeof ctx.resume === 'function') {
    try {
      const p = ctx.resume();
      if (p && typeof p.catch === 'function') p.catch(() => {});
    } catch { /* resume is best-effort; a running context needs nothing */ }
  }

  const encodeBuffer = () => {
    if (totalSamples === 0) return '';
    const all = new Float32Array(totalSamples);
    let offset = 0;
    frames.forEach((frame) => { all.set(frame, offset); offset += frame.length; });
    return encodeDictationAudio(all, inputRate);
  };

  const timer = (typeof setInterval === 'function' && chunkIntervalMs > 0)
    ? setInterval(() => {
      if (stopped || totalSamples === lastChunkSamples) return;
      lastChunkSamples = totalSamples;
      if (typeof onChunk === 'function') {
        onChunk({ audio: encodeBuffer(), sample_rate: DICTATION_SAMPLE_RATE });
      }
    }, chunkIntervalMs)
    : null;

  return {
    sampleRate: DICTATION_SAMPLE_RATE,
    stop() {
      if (stopped) return { audio: '', sample_rate: DICTATION_SAMPLE_RATE };
      stopped = true;
      if (timer !== null) clearInterval(timer);
      try { processor.disconnect(); } catch { /* best effort */ }
      try { source.disconnect(); } catch { /* best effort */ }
      const audio = encodeBuffer();
      try {
        const p = ctx.close();
        if (p && typeof p.catch === 'function') p.catch(() => {});
      } catch { /* best effort */ }
      return { audio, sample_rate: DICTATION_SAMPLE_RATE };
    },
  };
}
