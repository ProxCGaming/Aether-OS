import React, { useCallback, useEffect, useRef, useState } from 'react';

// Epic 8, E8-5.1a: the active voice interface.
//
// Device discipline is the contract: the microphone is opened ONLY
// while this overlay is open AND the engine session is live
// (active) — a refused start (status 'error') renders the engine's
// honest reason and never touches a device. Stopping, dismissing,
// or unmounting stops every mic track and closes the peer
// connection. TTS audio chunks (base64 WAV, VOICE_OUTPUT_CHUNK)
// are decoded and played back-to-back through one AudioContext.

const STATUS_LABELS = {
  starting: 'Starting…',
  listening: 'Listening',
  thinking: 'Thinking',
  speaking: 'Speaking',
};

export default function VoiceOverlay({
  open = false,
  active = false,
  status = 'starting',
  partialText = '',
  assistantText = '',
  error = null,
  webrtcConfig = null,
  audioChunk = null,
  onStop = () => {},
  onDismiss = () => {},
}) {
  const [micLive, setMicLive] = useState(false);
  const [micError, setMicError] = useState(null);
  const streamRef = useRef(null);
  const pcRef = useRef(null);
  const audioCtxRef = useRef(null);
  const playHeadRef = useRef(0);
  const webrtcConfigRef = useRef(webrtcConfig);
  useEffect(() => {
    webrtcConfigRef.current = webrtcConfig;
  }, [webrtcConfig]);

  const teardownCapture = useCallback(() => {
    if (streamRef.current) {
      try {
        streamRef.current.getTracks().forEach((track) => track.stop());
      } catch { /* already stopped */ }
      streamRef.current = null;
    }
    if (pcRef.current) {
      try { pcRef.current.close(); } catch { /* already closed */ }
      pcRef.current = null;
    }
    setMicLive(false);
  }, []);

  // Capture: runs only for a live session. The engine confirmed the
  // session before `active` flips, so a refused or pending start
  // never reaches getUserMedia.
  useEffect(() => {
    if (!open || !active) return undefined;
    let cancelled = false;
    const startCapture = async () => {
      if (!navigator.mediaDevices || typeof navigator.mediaDevices.getUserMedia !== 'function') {
        setMicError('Microphone is not available in this environment.');
        return;
      }
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        if (cancelled) {
          stream.getTracks().forEach((track) => track.stop());
          return;
        }
        streamRef.current = stream;
        setMicLive(true);
        const RTC = window.RTCPeerConnection;
        const config = webrtcConfigRef.current;
        if (config && typeof RTC === 'function') {
          const pc = new RTC({ iceServers: config.iceServers || [] });
          stream.getTracks().forEach((track) => pc.addTrack(track, stream));
          pcRef.current = pc;
        }
      } catch (e) {
        if (!cancelled) {
          setMicError(`Microphone unavailable: ${e && e.message ? e.message : e}`);
        }
      }
    };
    startCapture();
    return () => {
      cancelled = true;
      teardownCapture();
    };
  }, [open, active, teardownCapture]);

  // Playback: decode each TTS chunk and queue it after the last.
  const playChunk = useCallback((b64) => {
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx || !b64) return;
      if (!audioCtxRef.current) audioCtxRef.current = new Ctx();
      const ctx = audioCtxRef.current;
      const binary = window.atob(b64);
      const bytes = new Uint8Array(binary.length);
      for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
      const buffer = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
      const decoded = ctx.decodeAudioData(buffer);
      if (decoded && typeof decoded.then === 'function') {
        decoded.then((audioBuffer) => {
          const source = ctx.createBufferSource();
          source.buffer = audioBuffer;
          source.connect(ctx.destination);
          const startAt = Math.max(ctx.currentTime, playHeadRef.current || 0);
          source.start(startAt);
          playHeadRef.current = startAt + (audioBuffer.duration || 0);
        }).catch(() => { /* an undecodable chunk is skipped, never fatal */ });
      }
    } catch { /* playback is best-effort */ }
  }, []);

  useEffect(() => {
    if (audioChunk && audioChunk.data) playChunk(audioChunk.data);
  }, [audioChunk, playChunk]);

  useEffect(() => () => {
    if (audioCtxRef.current) {
      try { audioCtxRef.current.close(); } catch { /* already closed */ }
      audioCtxRef.current = null;
    }
  }, []);

  if (!open) return null;

  const handleStop = () => {
    teardownCapture();
    onStop();
  };

  return (
    <div
      data-testid="voice-overlay"
      style={{
        position: 'absolute',
        left: '50%',
        bottom: '110px',
        transform: 'translateX(-50%)',
        zIndex: 1200,
        minWidth: '320px',
        maxWidth: '520px',
        background: 'rgba(18, 18, 26, 0.92)',
        backdropFilter: 'blur(18px)',
        border: '1px solid rgba(167, 139, 250, 0.35)',
        borderRadius: '16px',
        padding: '16px 18px',
        color: '#fff',
        boxShadow: '0 16px 48px rgba(0, 0, 0, 0.5)',
      }}
    >
      <style>{`
        @keyframes voiceWavePulse {
          0%, 100% { transform: scaleY(0.35); }
          50% { transform: scaleY(1); }
        }
        @keyframes voiceDotPulse {
          0%, 100% { opacity: 1; }
          50% { opacity: 0.35; }
        }
        .voice-wave-bar {
          display: inline-block;
          width: 4px;
          height: 22px;
          margin-right: 4px;
          border-radius: 2px;
          background: #a78bfa;
          transform-origin: center;
        }
        .voice-waveform-live .voice-wave-bar {
          animation: voiceWavePulse 0.9s ease-in-out infinite;
        }
        .voice-mic-dot {
          display: inline-block;
          width: 9px;
          height: 9px;
          border-radius: 50%;
          background: #f87171;
          animation: voiceDotPulse 1.4s ease-in-out infinite;
        }
      `}</style>

      {status === 'error' ? (
        <div data-testid="voice-error" style={{ fontSize: '13px', lineHeight: 1.5 }}>
          <div style={{ fontWeight: 600, marginBottom: '6px' }}>Voice unavailable</div>
          <div style={{ color: '#fca5a5', marginBottom: '12px' }}>{error}</div>
          <button
            type="button"
            data-testid="voice-dismiss-btn"
            onClick={onDismiss}
            style={{
              background: 'rgba(255, 255, 255, 0.1)',
              border: '1px solid rgba(255, 255, 255, 0.15)',
              color: '#fff',
              cursor: 'pointer',
              padding: '6px 14px',
              borderRadius: '999px',
              fontSize: '12px',
            }}
          >
            Dismiss
          </button>
        </div>
      ) : (
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px', marginBottom: '10px' }}>
            {micLive && <span data-testid="voice-mic-indicator" className="voice-mic-dot" />}
            <span data-testid="voice-status" style={{ fontSize: '13px', fontWeight: 600 }}>
              {STATUS_LABELS[status] || 'Listening'}
            </span>
            <span
              data-testid="voice-waveform"
              className={`voice-waveform${status === 'listening' && micLive ? ' voice-waveform-live' : ''}`}
              style={{ display: 'inline-flex', alignItems: 'center', marginLeft: 'auto' }}
            >
              {[0, 1, 2, 3, 4].map((i) => (
                <span key={i} className="voice-wave-bar" style={{ animationDelay: `${i * 0.12}s` }} />
              ))}
            </span>
          </div>

          {micError && (
            <div data-testid="voice-mic-error" style={{ color: '#fca5a5', fontSize: '12px', marginBottom: '8px' }}>
              {micError}
            </div>
          )}
          {partialText && (
            <div data-testid="voice-partial" style={{ fontSize: '13px', color: '#c4b5fd', marginBottom: '6px' }}>
              {partialText}
            </div>
          )}
          {assistantText && (
            <div data-testid="voice-assistant-stream" style={{ fontSize: '13px', lineHeight: 1.5, marginBottom: '6px' }}>
              {assistantText}
            </div>
          )}

          <button
            type="button"
            data-testid="voice-stop-btn"
            onClick={handleStop}
            style={{
              background: 'rgba(248, 113, 113, 0.18)',
              border: '1px solid rgba(248, 113, 113, 0.45)',
              color: '#fff',
              cursor: 'pointer',
              padding: '6px 14px',
              borderRadius: '999px',
              fontSize: '12px',
              fontWeight: 600,
            }}
          >
            Stop voice
          </button>
        </div>
      )}
    </div>
  );
}
