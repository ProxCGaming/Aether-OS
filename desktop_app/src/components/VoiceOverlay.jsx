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
//
// E8-5.1b: a barge-in (VOICE_BARGE_IN) bumps `playbackCut` — every
// scheduled-but-unplayed source stops at once, the play head
// resets so the next speech starts immediately, and a chunk whose
// decode finishes after the cut is dropped (it belongs to the
// reply the user just talked over).
//
// F-12b: the peer connection actually negotiates. The overlay is
// the offerer (it holds the microphone): on capture it creates an
// offer and reports it through onRtcOffer; local ICE candidates
// are forwarded through onRtcIceCandidate in the engine's wire
// shape; the engine's answer arrives as the rtcAnswer prop and is
// applied with setRemoteDescription; engine candidates arrive as
// rtcRemoteCandidate and are queued until the answer is in, then
// applied in order. Connection state is surfaced honestly — a
// connecting/connected/interrupted line while live, and a failed
// or closed peer replaces the overlay with an error instead of
// sitting there looking alive with no media behind it.

const STATUS_LABELS = {
  starting: 'Starting…',
  listening: 'Listening',
  thinking: 'Thinking',
  speaking: 'Speaking',
};

const RTC_STATE_LABELS = {
  new: 'Connecting…',
  connecting: 'Connecting…',
  checking: 'Connecting…',
  connected: 'Connected',
  disconnected: 'Connection interrupted — trying to reconnect…',
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
  playbackCut = 0,
  onStop = () => {},
  onDismiss = () => {},
  onRtcOffer = () => {},
  rtcAnswer = null,
  onRtcIceCandidate = () => {},
  rtcRemoteCandidate = null,
}) {
  const [micLive, setMicLive] = useState(false);
  const [micError, setMicError] = useState(null);
  const [rtcState, setRtcState] = useState(null);
  const [rtcError, setRtcError] = useState(null);
  const streamRef = useRef(null);
  const pcRef = useRef(null);
  const audioCtxRef = useRef(null);
  const playHeadRef = useRef(0);
  const sourcesRef = useRef(new Set());
  const cutEpochRef = useRef(0);
  const lastCutRef = useRef(playbackCut);
  const webrtcConfigRef = useRef(webrtcConfig);
  useEffect(() => {
    webrtcConfigRef.current = webrtcConfig;
  }, [webrtcConfig]);
  // Signalling callbacks live in refs so a re-render with fresh
  // prop identities never restarts capture or negotiation.
  const onRtcOfferRef = useRef(onRtcOffer);
  useEffect(() => {
    onRtcOfferRef.current = onRtcOffer;
  }, [onRtcOffer]);
  const onRtcIceCandidateRef = useRef(onRtcIceCandidate);
  useEffect(() => {
    onRtcIceCandidateRef.current = onRtcIceCandidate;
  }, [onRtcIceCandidate]);
  // Engine candidates that arrive before the answer is applied
  // queue here; the answer effect flushes them in order.
  const pendingRemoteRef = useRef([]);
  const remoteAppliedRef = useRef(false);
  const lastAnswerSeqRef = useRef(null);
  const lastCandidateSeqRef = useRef(null);

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
    pendingRemoteRef.current = [];
    remoteAppliedRef.current = false;
    setMicLive(false);
    setRtcState(null);
    setRtcError(null);
  }, []);

  const flushRemoteCandidates = useCallback(() => {
    const pc = pcRef.current;
    if (!pc || !remoteAppliedRef.current
        || typeof pc.addIceCandidate !== 'function') return;
    const queued = pendingRemoteRef.current;
    pendingRemoteRef.current = [];
    queued.forEach((candidate) => {
      try {
        const applied = pc.addIceCandidate(candidate);
        if (applied && typeof applied.catch === 'function') {
          applied.catch(() => { /* one stale candidate is not fatal */ });
        }
      } catch { /* as above: a single bad candidate never kills the call */ }
    });
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
        if (config && typeof RTC !== 'function') {
          // The engine expects a peer connection and this
          // environment cannot make one: say so, instead of
          // streaming the microphone to nowhere.
          setRtcError('WebRTC is not available in this environment — voice cannot connect.');
          return;
        }
        if (config && typeof RTC === 'function') {
          const pc = new RTC({ iceServers: config.iceServers || [] });
          stream.getTracks().forEach((track) => pc.addTrack(track, stream));
          pcRef.current = pc;
          setRtcState('connecting');
          pc.onicecandidate = (event) => {
            const forward = onRtcIceCandidateRef.current;
            if (!forward) return;
            const candidate = event ? event.candidate : null;
            forward(candidate ? {
              candidate: candidate.candidate,
              sdpMid: candidate.sdpMid ?? null,
              sdpMLineIndex: candidate.sdpMLineIndex ?? null,
            } : null);
          };
          pc.onconnectionstatechange = () => {
            const state = pc.connectionState;
            setRtcState(state);
            if (state === 'failed') {
              setRtcError('Voice connection failed — the link to the engine dropped. Try again.');
            } else if (state === 'closed') {
              setRtcError('Voice connection closed.');
            }
          };
          // Negotiate: we hold the microphone, so we offer. The
          // engine's answer returns through the rtcAnswer prop.
          if (typeof pc.createOffer === 'function'
              && typeof pc.setLocalDescription === 'function') {
            try {
              const offer = await pc.createOffer();
              if (cancelled) return;
              await pc.setLocalDescription(offer);
              if (cancelled) return;
              const local = pc.localDescription || offer;
              if (onRtcOfferRef.current) {
                onRtcOfferRef.current({ type: local.type, sdp: local.sdp });
              }
            } catch (e) {
              if (!cancelled) {
                setRtcError(`Voice connection failed: ${e && e.message ? e.message : e}`);
              }
            }
          }
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

  // The engine's answer: apply it, then flush any engine
  // candidates that arrived while it was in flight.
  useEffect(() => {
    if (!rtcAnswer || !rtcAnswer.answer) return undefined;
    if (lastAnswerSeqRef.current === rtcAnswer.seq) return undefined;
    lastAnswerSeqRef.current = rtcAnswer.seq;
    const pc = pcRef.current;
    if (!pc || typeof pc.setRemoteDescription !== 'function') return undefined;
    let cancelled = false;
    try {
      Promise.resolve(pc.setRemoteDescription(rtcAnswer.answer)).then(() => {
        if (cancelled) return;
        remoteAppliedRef.current = true;
        flushRemoteCandidates();
      }).catch((e) => {
        if (!cancelled) {
          setRtcError(`Voice connection failed: ${e && e.message ? e.message : e}`);
        }
      });
    } catch (e) {
      setRtcError(`Voice connection failed: ${e && e.message ? e.message : e}`);
    }
    return () => { cancelled = true; };
  }, [rtcAnswer, flushRemoteCandidates]);

  // Engine candidates: queue until the answer is in (the peer has
  // no remote description before that), then apply in order.
  useEffect(() => {
    if (!rtcRemoteCandidate) return;
    if (lastCandidateSeqRef.current === rtcRemoteCandidate.seq) return;
    lastCandidateSeqRef.current = rtcRemoteCandidate.seq;
    if (rtcRemoteCandidate.candidate) {
      pendingRemoteRef.current.push(rtcRemoteCandidate.candidate);
    }
    flushRemoteCandidates();
  }, [rtcRemoteCandidate, flushRemoteCandidates]);

  // Playback cut (E8-5.1b): stop every scheduled source, reset
  // the play head to now, and invalidate decodes still in flight.
  const stopPlayback = useCallback(() => {
    cutEpochRef.current += 1;
    sourcesRef.current.forEach((source) => {
      try { source.stop(); } catch { /* already ended */ }
    });
    sourcesRef.current.clear();
    if (audioCtxRef.current) {
      playHeadRef.current = audioCtxRef.current.currentTime;
    } else {
      playHeadRef.current = 0;
    }
  }, []);

  useEffect(() => {
    if (playbackCut !== lastCutRef.current) {
      lastCutRef.current = playbackCut;
      stopPlayback();
    }
  }, [playbackCut, stopPlayback]);

  // Playback: decode each TTS chunk and queue it after the last.
  const playChunk = useCallback((b64) => {
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx || !b64) return;
      if (!audioCtxRef.current) audioCtxRef.current = new Ctx();
      const ctx = audioCtxRef.current;
      const epoch = cutEpochRef.current;
      const binary = window.atob(b64);
      const bytes = new Uint8Array(binary.length);
      for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
      const buffer = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
      const decoded = ctx.decodeAudioData(buffer);
      if (decoded && typeof decoded.then === 'function') {
        decoded.then((audioBuffer) => {
          // A cut landed while this chunk decoded: it belongs to
          // the interrupted reply — drop it, never schedule it.
          if (epoch !== cutEpochRef.current) return;
          const source = ctx.createBufferSource();
          source.buffer = audioBuffer;
          source.connect(ctx.destination);
          source.onended = () => { sourcesRef.current.delete(source); };
          sourcesRef.current.add(source);
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
      {/* S3-M2: the waveform/mic-dot keyframes live in src/index.css,
          not a runtime style element — one of those would force
          style-src 'unsafe-inline' into the app CSP. */}
      {status === 'error' || rtcError ? (
        <div data-testid="voice-error" style={{ fontSize: '13px', lineHeight: 1.5 }}>
          <div style={{ fontWeight: 600, marginBottom: '6px' }}>Voice unavailable</div>
          <div style={{ color: '#fca5a5', marginBottom: '12px' }}>{status === 'error' ? error : rtcError}</div>
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

          {rtcState && RTC_STATE_LABELS[rtcState] && (
            <div
              data-testid="voice-rtc-state"
              style={{
                color: rtcState === 'disconnected' ? '#fca5a5' : '#a5b4fc',
                fontSize: '12px',
                marginBottom: '8px',
              }}
            >
              {RTC_STATE_LABELS[rtcState]}
            </div>
          )}
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
