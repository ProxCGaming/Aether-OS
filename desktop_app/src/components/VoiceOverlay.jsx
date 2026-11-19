import React from 'react';
import { useVoiceSession } from './VoiceSession';
import { kokoroVoiceDisplayName } from '../speechCatalog';

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
// N09: playback position is reported back to the engine through
// onPlaybackProgress — the highest engine chunk_seq this player
// has FINISHED playing (a source's natural onended under the cut
// epoch it was scheduled in; a source stopped by a cut never
// counts). One -1 report when the session goes live arms the
// engine's playback-based heard accounting; without any report
// the engine cannot know what was actually heard. Reports are
// fire-and-forget: nothing on the live path waits for them.
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
  notice = null,
  error = null,
  inputDeviceId = '',
  ttsVoice = null,
  sttModel = null,
  sttConfiguredModel = null,
  webrtcConfig = null,
  audioChunk = null,
  playbackCut = 0,
  onStop = () => {},
  onDismiss = () => {},
  onRtcOffer = () => {},
  rtcAnswer = null,
  onRtcIceCandidate = () => {},
  rtcRemoteCandidate = null,
  onPlaybackProgress = () => {},
}) {
  const { micLive, micError, rtcState, rtcError, teardownCapture } = useVoiceSession({
    open,
    active,
    inputDeviceId,
    webrtcConfig,
    audioChunk,
    playbackCut,
    rtcAnswer,
    rtcRemoteCandidate,
    onRtcOffer,
    onRtcIceCandidate,
    onPlaybackProgress,
  });

  if (!open) return null;

  const handleStop = () => {
    teardownCapture();
    onStop();
  };

  return (
    <div
      data-testid="voice-overlay"
      className="aether-glass"
      style={{
        position: 'absolute',
        left: '50%',
        bottom: '110px',
        transform: 'translateX(-50%)',
        zIndex: 1200,
        minWidth: '320px',
        maxWidth: '520px',
        // Pill radius stays inline — it overrides the glass
        // class's --radius-lg.
        borderRadius: '999px',
        padding: '16px 18px',
        color: 'var(--text-primary)',
      }}
    >
      {/* S3-M2: the waveform/mic-dot keyframes live in src/index.css,
          not a runtime style element — one of those would force
          style-src 'unsafe-inline' into the app CSP. */}
      {status === 'error' || rtcError ? (
        <div data-testid="voice-error" style={{ fontSize: '13px', lineHeight: 1.5 }}>
          <div style={{ fontWeight: 600, marginBottom: '6px' }}>Voice unavailable</div>
          <div style={{ color: 'var(--danger)', marginBottom: '12px' }}>{status === 'error' ? error : rtcError}</div>
          <button
            type="button"
            data-testid="voice-dismiss-btn"
            onClick={onDismiss}
            style={{
              background: 'var(--bg-hover)',
              border: '1px solid var(--border-strong)',
              color: 'var(--text-primary)',
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

          {/* The EFFECTIVE speech profile: the voice speaking
              and the STT model actually in effect — after a
              degradation step-down that is the smaller model,
              named, with the configured model it stepped down
              from shown as a chip. Never the merely-configured
              values dressed up as the live ones. */}
          {(ttsVoice || sttModel) && (
            <div
              data-testid="voice-effective-profile"
              style={{ display: 'flex', alignItems: 'center', gap: '8px', color: 'var(--text-secondary)', fontSize: '12px', marginBottom: '8px' }}
            >
              <span>
                {ttsVoice && `Voice: ${kokoroVoiceDisplayName(ttsVoice)}`}
                {ttsVoice && sttModel && ' · '}
                {sttModel && `Listening: ${sttModel}`}
              </span>
              {sttModel && sttConfiguredModel && sttModel !== sttConfiguredModel && (
                <span
                  data-testid="voice-stepped-down"
                  style={{ fontSize: '10px', color: 'var(--accent)', border: '1px solid rgba(var(--accent-rgb), 0.45)', borderRadius: '999px', padding: '1px 7px', whiteSpace: 'nowrap' }}
                >
                  Stepped down from {sttConfiguredModel}
                </span>
              )}
            </div>
          )}
          {rtcState && RTC_STATE_LABELS[rtcState] && (
            <div
              data-testid="voice-rtc-state"
              style={{
                color: rtcState === 'disconnected' ? 'var(--danger)' : 'var(--text-secondary)',
                fontSize: '12px',
                marginBottom: '8px',
              }}
            >
              {RTC_STATE_LABELS[rtcState]}
            </div>
          )}
          {micError && (
            <div data-testid="voice-mic-error" style={{ color: 'var(--danger)', fontSize: '12px', marginBottom: '8px' }}>
              {micError}
            </div>
          )}
          {notice && (
            <div data-testid="voice-notice" style={{ color: 'var(--text-secondary)', fontSize: '12px', marginBottom: '8px' }}>
              {notice}
            </div>
          )}
          {partialText && (
            <div data-testid="voice-partial" style={{ fontSize: '13px', color: 'var(--text-secondary)', marginBottom: '6px' }}>
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
              background: 'rgba(201, 42, 42, 0.15)',
              border: '1px solid rgba(201, 42, 42, 0.35)',
              color: 'var(--danger)',
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
