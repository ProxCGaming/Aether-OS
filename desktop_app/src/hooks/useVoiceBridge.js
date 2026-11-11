import { useState, useEffect, useRef, useCallback } from 'react';

// Wave 5, Annex C row 5 (voice bridge): the voice state-machine
// glue, extracted from App.jsx. The bridge owns the voice session
// state the overlay renders (status, partial/final text, RTC
// signalling props), the seq refs the once-registered message
// handler stamps its chunks with, and the dictation / read-aloud
// controls. App hands it the socket and the active-session
// identity; chat commits go through App's setChatMessages, because
// a finished voice reply lands as a normal chat message.

export function useVoiceBridge(deps) {
  const { wsRef, activeSessionRef, activeSession, setChatMessages } = deps;

  // Epic 8, E8-5.1a: voice mode. voiceState mirrors the engine
  // session — {status: 'starting'|'active'|'error', sessionId,
  // partialText, assistantText, announcementText, error,
  // webrtcConfig} — or null when
  // voice mode is off. Partial transcripts are provisional display
  // only; finals commit into chatMessages (the engine persists the
  // same turns to the session store). The ref mirror exists
  // because the engine-message handler is registered once and must
  // not close over render-scoped state.
  const [voiceState, setVoiceState] = useState(null);
  const [voiceAudioChunk, setVoiceAudioChunk] = useState(null);
  // E8-5.1b: bumped on every VOICE_BARGE_IN; the overlay cuts its
  // scheduled playback when this changes.
  const [voicePlaybackCut, setVoicePlaybackCut] = useState(0);
  // F-12b: RTC signalling for the live voice session. The engine's
  // answer and its ICE candidates are seq-numbered props for the
  // overlay (the voiceAudioChunk pattern); the overlay's offer and
  // local candidates go up through the callbacks below.
  const [voiceRtcAnswer, setVoiceRtcAnswer] = useState(null);
  const [voiceRtcRemoteCandidate, setVoiceRtcRemoteCandidate] = useState(null);
  const voiceStateRef = useRef(null);
  const voiceChunkSeqRef = useRef(0);
  const voiceRtcSeqRef = useRef(0);

  const applyVoiceState = useCallback((next) => {
    voiceStateRef.current = next;
    setVoiceState(next);
  }, []);

  const sendVoiceRequest = useCallback((type, payload) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type,
        schema_version: 1,
        request_id: `voice_${Date.now()}`,
        payload,
      }));
      return true;
    }
    return false;
  }, [wsRef]);

  const commitVoiceAssistant = useCallback((state) => {
    const text = (state && state.assistantText) || '';
    if (text.trim()) {
      setChatMessages(prev => [...prev, {
        id: `voice-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        role: 'agent', content: text, thoughts: [], isFinal: true,
      }]);
    }
  }, [setChatMessages]);

  const handleVoiceStart = useCallback(() => {
    const current = voiceStateRef.current;
    if (current && current.status !== 'error') return;
    setVoiceRtcAnswer(null);
    setVoiceRtcRemoteCandidate(null);
    const sessionId = activeSessionRef.current;
    if (!sendVoiceRequest('VOICE_SESSION_START_REQUEST', { session_id: sessionId })) {
      applyVoiceState({
        status: 'error', sessionId, partialText: '', assistantText: '',
        announcementText: '',
        error: 'voice unavailable: the engine is not connected', webrtcConfig: null,
      });
      return;
    }
    applyVoiceState({
      status: 'starting', sessionId, partialText: '',
      assistantText: '', announcementText: '', error: null, webrtcConfig: null,
    });
  }, [applyVoiceState, sendVoiceRequest, activeSessionRef]);

  const handleVoiceStop = useCallback(() => {
    const current = voiceStateRef.current;
    if (!current) return;
    // Tear the UI down immediately (the overlay unmount stops the
    // mic); the engine stops its pipeline on the request — or on
    // disconnect if the request never arrives. The reply streamed
    // so far commits as a normal message.
    commitVoiceAssistant(current);
    sendVoiceRequest('VOICE_SESSION_STOP_REQUEST', {
      session_id: current.sessionId || activeSessionRef.current,
    });
    setVoiceRtcAnswer(null);
    setVoiceRtcRemoteCandidate(null);
    applyVoiceState(null);
  }, [applyVoiceState, commitVoiceAssistant, sendVoiceRequest, activeSessionRef]);

  const handleVoiceDismissError = useCallback(() => {
    setVoiceRtcAnswer(null);
    setVoiceRtcRemoteCandidate(null);
    applyVoiceState(null);
  }, [applyVoiceState]);

  // F-12b: the overlay offers (it holds the microphone); the
  // engine answers. A refused negotiation flips the session to the
  // error state — the overlay must never sit live with no media.
  const handleVoiceRtcOffer = useCallback((offer) => {
    const current = voiceStateRef.current;
    if (!current) return;
    sendVoiceRequest('VOICE_RTC_OFFER_REQUEST', {
      session_id: current.sessionId || activeSessionRef.current,
      sdp: offer,
    });
  }, [sendVoiceRequest, activeSessionRef]);

  const handleVoiceRtcIceCandidate = useCallback((candidate) => {
    const current = voiceStateRef.current;
    if (!current) return;
    sendVoiceRequest('VOICE_RTC_ICE_REQUEST', {
      session_id: current.sessionId || activeSessionRef.current,
      candidate,
    });
  }, [sendVoiceRequest, activeSessionRef]);

  // N09: the overlay reports the highest engine chunk_seq it has
  // FINISHED playing; the engine's heard-word accounting reads
  // these reports, never its own emission count. Fire-and-forget —
  // no response rides back.
  const handleVoicePlaybackProgress = useCallback((playedSeq) => {
    const current = voiceStateRef.current;
    if (!current) return;
    sendVoiceRequest('VOICE_PLAYBACK_PROGRESS', {
      session_id: current.sessionId || activeSessionRef.current,
      played_seq: playedSeq,
    });
  }, [sendVoiceRequest, activeSessionRef]);

  // Epic 8, E8-5.3: Push-to-talk dictation + assistant message read-aloud
  const [dictationState, setDictationState] = useState(null);
  const [readAloudState, setReadAloudState] = useState(null);
  const readAloudAudioCtxRef = useRef(null);
  const readAloudSourcesRef = useRef(new Set());
  const readAloudPlayHeadRef = useRef(0);

  const handleDictationStart = useCallback(() => {
    const sessionId = activeSessionRef.current;
    if (!sendVoiceRequest('DICTATION_START_REQUEST', { session_id: sessionId })) {
      setDictationState({
        status: 'error',
        error: 'voice unavailable: the engine is not connected',
        text: '',
      });
      return;
    }
    setDictationState({ status: 'starting', error: null, text: '' });
  }, [sendVoiceRequest, activeSessionRef]);

  // F1 (linkage audit): the chunk sender the capture session
  // streams through. Payload shape is the engine contract
  // (websocket.py _handle_dictation_chunk_request): base64 raw
  // PCM16 mono in `audio`, `sample_rate`, `is_final`. Chunks are
  // cumulative (see src/dictationCapture.js); the engine chains
  // them in arrival order (RUX-B-09), so no response is awaited.
  const handleDictationChunk = useCallback((chunk) => {
    if (!chunk || !chunk.audio) return;
    sendVoiceRequest('DICTATION_CHUNK_REQUEST', {
      session_id: activeSessionRef.current,
      audio: chunk.audio,
      sample_rate: chunk.sample_rate || 16000,
      is_final: false,
    });
  }, [sendVoiceRequest, activeSessionRef]);

  const handleDictationStop = useCallback((finalAudio) => {
    const sessionId = activeSessionRef.current;
    // The final transcription rides the STOP request: the
    // complete utterance buffer, base64 PCM16 (an empty string
    // is honest silence to the engine — answered ok, no text).
    sendVoiceRequest('DICTATION_STOP_REQUEST', {
      session_id: sessionId,
      audio: (finalAudio && finalAudio.audio) || '',
      sample_rate: (finalAudio && finalAudio.sample_rate) || 16000,
    });
    setDictationState(prev => (prev ? { ...prev, status: 'idle' } : null));
  }, [sendVoiceRequest, activeSessionRef]);

  const stopAppReadAloud = useCallback(() => {
    readAloudSourcesRef.current.forEach((src) => {
      try { src.stop(); } catch {}
    });
    readAloudSourcesRef.current.clear();
    if (readAloudAudioCtxRef.current) {
      readAloudPlayHeadRef.current = readAloudAudioCtxRef.current.currentTime;
    } else {
      readAloudPlayHeadRef.current = 0;
    }
  }, []);

  const handleReadAloudStart = useCallback((messageId, text) => {
    stopAppReadAloud();
    if (!sendVoiceRequest('READ_ALOUD_START_REQUEST', { message_id: messageId, text })) {
      setReadAloudState({
        messageId,
        status: 'error',
        error: 'voice unavailable: the engine is not connected',
      });
      return;
    }
    setReadAloudState({ messageId, status: 'starting', error: null });
  }, [sendVoiceRequest, stopAppReadAloud]);

  const handleReadAloudStop = useCallback((messageId) => {
    stopAppReadAloud();
    sendVoiceRequest('READ_ALOUD_STOP_REQUEST', { message_id: messageId });
    setReadAloudState(null);
  }, [sendVoiceRequest, stopAppReadAloud]);

  // Leaving the voice session's chat ends voice mode without
  // committing the provisional bubbles here — the engine already
  // persisted the finals to that session's store.
  useEffect(() => {
    const current = voiceStateRef.current;
    if (current && current.sessionId && current.sessionId !== activeSession) {
      sendVoiceRequest('VOICE_SESSION_STOP_REQUEST', { session_id: current.sessionId });
      applyVoiceState(null);
    }
  }, [activeSession, applyVoiceState, sendVoiceRequest]);

  return {
    voiceState, voiceAudioChunk, voicePlaybackCut,
    voiceRtcAnswer, voiceRtcRemoteCandidate,
    dictationState, readAloudState,
    voiceStateRef, voiceChunkSeqRef, voiceRtcSeqRef,
    readAloudAudioCtxRef, readAloudSourcesRef, readAloudPlayHeadRef,
    applyVoiceState, setVoiceAudioChunk, setVoicePlaybackCut,
    setVoiceRtcAnswer, setVoiceRtcRemoteCandidate,
    setDictationState, setReadAloudState,
    commitVoiceAssistant, stopAppReadAloud,
    handleVoiceStart, handleVoiceStop, handleVoiceDismissError,
    handleVoiceRtcOffer, handleVoiceRtcIceCandidate,
    handleVoicePlaybackProgress,
    handleDictationStart, handleDictationChunk, handleDictationStop,
    handleReadAloudStart, handleReadAloudStop,
  };
}
