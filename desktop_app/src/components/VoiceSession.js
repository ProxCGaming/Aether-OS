import { useCallback, useEffect, useRef, useState } from 'react';
import { audioConstraintForDevice } from '../micDevices';

// Wave 5 (Annex C row 11): the voice session seam, split out of
// VoiceOverlay.jsx. This hook is the session engine — microphone
// capture, the WebRTC negotiation, TTS playback with barge-in
// cuts, and the N09 heard-chunk reporting — while VoiceOverlay
// keeps only the presentation. The full device-discipline
// contract is documented at the top of VoiceOverlay.jsx and
// applies here unchanged: the microphone opens ONLY while the
// overlay is open AND the engine session is live, and stopping,
// dismissing, or unmounting stops every mic track and closes the
// peer connection.

export function useVoiceSession({
  open = false,
  active = false,
  inputDeviceId = '',
  webrtcConfig = null,
  audioChunk = null,
  playbackCut = 0,
  rtcAnswer = null,
  rtcRemoteCandidate = null,
  onRtcOffer = () => {},
  onRtcIceCandidate = () => {},
  onPlaybackProgress = () => {},
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
  // The selected microphone, read through a ref like the WebRTC
  // config: capture uses the device selected when the session
  // went live — a mid-session Settings change never re-opens
  // the microphone under a talking session; it applies to the
  // next session, the same rule as the speech model.
  const inputDeviceIdRef = useRef(inputDeviceId);
  useEffect(() => {
    inputDeviceIdRef.current = inputDeviceId;
  }, [inputDeviceId]);
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
  const onPlaybackProgressRef = useRef(onPlaybackProgress);
  useEffect(() => {
    onPlaybackProgressRef.current = onPlaybackProgress;
  }, [onPlaybackProgress]);
  // Highest engine chunk_seq already reported as finished; the
  // engine takes the max, and this keeps reports monotonic even
  // if an onended lands late.
  const reportedSeqRef = useRef(-1);

  // N09: going live arms playback reporting (-1: nothing finished
  // yet). Re-firing on a later activation is harmless — reports
  // never rewind the engine's position.
  useEffect(() => {
    if (open && active) onPlaybackProgressRef.current(-1);
  }, [open, active]);
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
    // D3-05: the per-session sequence state resets too. The engine
    // restarts its seq numbering each session; without this, a
    // second session's answer/candidate whose seq equals the last
    // session's is dropped as a "duplicate", and heard-chunk
    // reports stay suppressed for engineSeq <= the old session's
    // high-water mark — playback accounting silently stalls.
    reportedSeqRef.current = -1;
    lastAnswerSeqRef.current = null;
    lastCandidateSeqRef.current = null;
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
        const stream = await navigator.mediaDevices.getUserMedia({
          audio: audioConstraintForDevice(inputDeviceIdRef.current),
        });
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
  // engineSeq is the chunk's engine-side identity (N09): when the
  // scheduled source ends NATURALLY — same cut epoch it was
  // scheduled in, so a cut-stopped source never counts — the
  // chunk is reported as finished playing.
  const playChunk = useCallback((b64, engineSeq = null) => {
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
          source.onended = () => {
            sourcesRef.current.delete(source);
            // N09: a natural end under the scheduling epoch means
            // the user actually heard this chunk. (stop() also
            // fires onended, but a cut bumps the epoch first, so
            // cut-stopped sources are never reported. A chunk
            // whose natural end races a cut is simply not
            // reported — the accounting stays conservative.)
            if (typeof engineSeq === 'number'
                && epoch === cutEpochRef.current
                && engineSeq > reportedSeqRef.current) {
              reportedSeqRef.current = engineSeq;
              onPlaybackProgressRef.current(engineSeq);
            }
          };
          sourcesRef.current.add(source);
          const startAt = Math.max(ctx.currentTime, playHeadRef.current || 0);
          source.start(startAt);
          playHeadRef.current = startAt + (audioBuffer.duration || 0);
        }).catch(() => { /* an undecodable chunk is skipped, never fatal */ });
      }
    } catch { /* playback is best-effort */ }
  }, []);

  useEffect(() => {
    if (audioChunk && audioChunk.data) {
      playChunk(audioChunk.data, audioChunk.engineSeq ?? null);
    }
  }, [audioChunk, playChunk]);

  useEffect(() => () => {
    if (audioCtxRef.current) {
      try { audioCtxRef.current.close(); } catch { /* already closed */ }
      audioCtxRef.current = null;
    }
  }, []);

  return { micLive, micError, rtcState, rtcError, teardownCapture };
}
