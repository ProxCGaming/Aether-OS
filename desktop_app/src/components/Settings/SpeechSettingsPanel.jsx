import React, { useState, useEffect, useRef } from 'react';
import { Mic, Play, Square, Check } from 'lucide-react';
import Dropdown from '../../Dropdown';
import {
  KOKORO_VOICE_GROUPS,
  BUILTIN_STT_MODELS,
  STT_LANGUAGE_OPTIONS,
  TTS_SPEED_OPTIONS,
  MEASURED_STT_TURN_MS,
  MEASURED_STT_SOURCE,
  kokoroVoiceDisplayName,
  sttModelLabel,
  ttsSpeedLabel,
} from '../../speechCatalog';

// The Speech card in Settings → Voice (the voice home: Speech
// and Tuning & Input live in the one Voice section). It is the
// selection surface for the speech fields — `speech_stt_model`
// (dictation + voice mode), `speech_tts_voice` (the Kokoro
// voice), `speech_tts_speed`, `speech_stt_language` and
// `transcription_stt_model` (video transcription).
//
// The panel is controlled, in the E6-F05 sense: SettingsPanel owns
// the socket, applies every CONFIG_GET / CONFIG_UPDATE /
// CONFIG_RESET payload through its single applyConfigValues point,
// and hands this component the applied config, the most recent
// strict-write refusal (configError, surfaced verbatim), and the
// one sendConfigUpdate callback — so offline queueing (E6-F15)
// keeps working and a refused write simply never lands in the
// config, leaving the controls showing the value actually in
// force. Its own socket traffic is request_id-correlated
// one-shots: the hub inventory pair (MODEL_HUB_DOWNLOADED) so
// speech repos downloaded in the Local Models hub can be offered
// here, and the voice preview pair (VOICE_PREVIEW) below.
//
// The controls mirror what the engine can actually do:
// dictation rebuilds its engine per model, so an STT change
// applies to dictation right away, but a live voice session keeps
// the model it started with — the change takes voice mode from
// the next session, and the card says so rather than pretending
// otherwise. The TTS voice is a per-sentence parameter, so it
// genuinely can change mid-conversation. Kokoro ships premade
// voices, so the voice control is a list of voice rows grouped
// by language — each row carries a one-line character
// description (the raw voice id is only a tooltip) and a Preview
// button that plays the engine's one fixed preview sentence in
// that voice. If the speech engine is not installed, the probe
// on open finds out and the card says so instead of offering
// dead buttons.

// Options for either STT select: the built-in sizes (labelled
// with size + speed/accuracy hint), then any downloaded in-app
// speech repos from the hub, then the current value itself if
// it is in neither list — a repo id that is not downloaded is
// labelled "(not downloaded)" so the select never silently
// drops a configured model, and never claims a repo is on disk
// when it is not. (A bare size name outside the short list,
// e.g. a distil variant, needs no such label: faster-whisper
// resolves bare names itself.)
function sttOptions(currentValue, downloadedStt) {
  const options = [
    {
      group: 'Built-in sizes',
      items: BUILTIN_STT_MODELS.map((m) => ({ value: m, label: sttModelLabel(m) })),
    },
  ];
  if (downloadedStt.length > 0) {
    options.push({
      group: 'Downloaded models',
      items: downloadedStt.map((row) => ({ value: row.id, label: row.name || row.id })),
    });
  }
  const known = new Set([
    ...BUILTIN_STT_MODELS,
    ...downloadedStt.map((row) => row.id),
  ]);
  if (currentValue && !known.has(currentValue)) {
    options.push({
      value: currentValue,
      label: currentValue.includes('/')
        ? `${currentValue} (not downloaded)`
        : currentValue,
    });
  }
  return options;
}

// The voice rows: the catalog groups, plus the current value as
// its own group when it is not one of the premade voices — a
// configured voice the list doesn't know stays selectable and
// previewable rather than vanishing.
function voiceGroups(currentValue) {
  const groups = KOKORO_VOICE_GROUPS.map((g) => ({
    group: g.group,
    chip: g.chip,
    items: g.items.map((item) => ({ ...item })),
  }));
  const known = new Set(
    groups.flatMap((g) => g.items.map((item) => item.value)),
  );
  if (currentValue && !known.has(currentValue)) {
    groups.push({
      group: 'Current selection',
      chip: null,
      items: [{
        value: currentValue,
        name: kokoroVoiceDisplayName(currentValue),
        description: 'A voice outside the built-in list — it stays here while it is your saved voice.',
      }],
    });
  }
  return groups;
}

const rowStyle = {
  background: 'var(--bg-raised)',
  border: '1px solid var(--border-default)',
  borderRadius: '12px',
  padding: '12px 16px',
  display: 'flex',
  justifyContent: 'space-between',
  alignItems: 'center',
  gap: '16px',
};

export default function SpeechSettingsPanel({ config, configError, onConfigUpdate, pendingMessages = [], reconnectCount = 0, wsRef, configReady }) {
  // RUX-C-11: until the first config answer lands, the controls
  // below show engine defaults ('small', 'af_heart') — values the
  // user may never have chosen, presented as their configuration.
  // The controls are disabled (one fieldset) until the config is
  // actually known; the section's banner explains why. Callers
  // that don't pass configReady get the derived rule: a non-null
  // config IS an answer.
  const ready = configReady !== undefined ? configReady : config != null;
  const [downloadedStt, setDownloadedStt] = useState([]);
  // Whether the local speech engine can speak at all, from the
  // preview probe: 'unknown' until the probe answers, then
  // 'available' / 'unavailable' (+ the engine's own reason).
  const [ttsAvailability, setTtsAvailability] = useState('unknown');
  const [ttsUnavailableReason, setTtsUnavailableReason] = useState('');
  // The one in-flight / playing preview: {voice, status, error}.
  const [preview, setPreview] = useState({ voice: null, status: 'idle', error: null });
  // request_id -> one-shot response handler for this panel's
  // own request/response pairs (hub inventory + voice preview).
  const pendingRef = useRef(new Map());
  const audioCtxRef = useRef(null);
  const previewSourcesRef = useRef(new Set());

  const stopPreviewPlayback = () => {
    previewSourcesRef.current.forEach((source) => {
      try { source.stop(); } catch { /* already ended */ }
    });
    previewSourcesRef.current.clear();
  };

  // Play the preview response's WAV chunks back-to-back (the
  // read-aloud playback pattern: decode each chunk and schedule
  // it after the previous one). No AudioContext (tests, odd
  // browsers) simply ends the preview — the request/response
  // honesty above does not depend on audible output.
  const playPreviewChunks = async (chunks, voiceId) => {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx || !chunks || chunks.length === 0) {
      setPreview({ voice: voiceId, status: 'idle', error: null });
      return;
    }
    if (!audioCtxRef.current) audioCtxRef.current = new Ctx();
    const ctx = audioCtxRef.current;
    stopPreviewPlayback();
    let head = ctx.currentTime;
    let remaining = chunks.length;
    const finishOne = () => {
      remaining -= 1;
      if (remaining <= 0) {
        setPreview((prev) => (prev.voice === voiceId && prev.status === 'playing'
          ? { voice: voiceId, status: 'idle', error: null }
          : prev));
      }
    };
    for (const b64 of chunks) {
      try {
        const binary = window.atob(b64);
        const bytes = new Uint8Array(binary.length);
        for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
        const buffer = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
        // eslint-disable-next-line no-await-in-loop
        const audioBuffer = await ctx.decodeAudioData(buffer);
        const source = ctx.createBufferSource();
        source.buffer = audioBuffer;
        source.connect(ctx.destination);
        source.onended = () => {
          previewSourcesRef.current.delete(source);
          finishOne();
        };
        previewSourcesRef.current.add(source);
        const startAt = Math.max(ctx.currentTime, head);
        source.start(startAt);
        head = startAt + (audioBuffer.duration || 0);
      } catch {
        finishOne();
      }
    }
    if (remaining <= 0) {
      setPreview({ voice: voiceId, status: 'idle', error: null });
    }
  };

  // One request_id-correlated send on the shared socket.
  const sendOneShot = (type, payload, handler) => {
    const ws = wsRef && wsRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) return false;
    const requestId = `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
    pendingRef.current.set(requestId, handler);
    ws.send(JSON.stringify({
      type, schema_version: 1, request_id: requestId, payload,
    }));
    return true;
  };

  // Fetch the hub's downloaded inventory once, on mount: the
  // speech-to-text rows are the extra options in both STT selects.
  // Wave 2 slice 4 (RUX-A-17): the send below is OPEN-guarded, so a
  // mount while the engine was down fetched nothing and the selects
  // kept labelling downloaded models "(not downloaded)" forever;
  // reconnectCount re-runs the effect on every recovery. The
  // preview probe rides the same effect for the same reason: an
  // engine that was down at mount gets re-asked when it returns.
  useEffect(() => {
    const ws = wsRef && wsRef.current;
    if (!ws || typeof ws.addEventListener !== 'function') return undefined;
    const handleMessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.request_id && pendingRef.current.has(data.request_id)) {
          const handler = pendingRef.current.get(data.request_id);
          pendingRef.current.delete(data.request_id);
          handler(data.payload || {});
        }
      } catch (e) {
        // Not JSON / not ours — the panel's own handler owns it.
      }
    };
    ws.addEventListener('message', handleMessage);
    if (ws.readyState === WebSocket.OPEN) {
      sendOneShot('MODEL_HUB_DOWNLOADED_REQUEST', {}, (payload) => {
        if (payload && Array.isArray(payload.models)) {
          setDownloadedStt(payload.models.filter(
            (row) => row && row.family === 'speech-to-text',
          ));
        }
      });
      sendOneShot('VOICE_PREVIEW_REQUEST', { probe: true }, (payload) => {
        if (payload && payload.ok) {
          setTtsAvailability('available');
          setTtsUnavailableReason('');
        } else {
          setTtsAvailability('unavailable');
          setTtsUnavailableReason(
            String((payload && payload.error) || '').replace(/^voice unavailable: /, ''));
        }
      });
    }
    return () => ws.removeEventListener('message', handleMessage);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wsRef, reconnectCount]);

  // Leaving the card stops any preview still playing.
  useEffect(() => () => stopPreviewPlayback(), []);

  const handlePreviewClick = (voiceId) => {
    if (preview.voice === voiceId && preview.status === 'playing') {
      stopPreviewPlayback();
      setPreview({ voice: voiceId, status: 'idle', error: null });
      return;
    }
    stopPreviewPlayback();
    setPreview({ voice: voiceId, status: 'loading', error: null });
    const sent = sendOneShot('VOICE_PREVIEW_REQUEST', { voice: voiceId }, (payload) => {
      if (payload && payload.ok) {
        setPreview({ voice: voiceId, status: 'playing', error: null });
        playPreviewChunks(payload.audio_chunks || [], voiceId);
      } else {
        const message = (payload && payload.error) || 'The preview could not be played.';
        if (String(message).startsWith('voice unavailable:')) {
          setTtsAvailability('unavailable');
          setTtsUnavailableReason(message.replace(/^voice unavailable: /, ''));
        }
        setPreview({ voice: voiceId, status: 'error', error: message });
      }
    });
    if (!sent) {
      setPreview({
        voice: voiceId,
        status: 'error',
        error: 'Not connected to the engine — a preview needs the engine running.',
      });
    }
  };

  // RUX-A-22: the controls are controlled from the applied config,
  // and onConfigUpdate (SettingsPanel's sendConfigUpdate) QUEUES
  // while the engine is unreachable — so an offline pick used to
  // snap straight back and then ghost-apply on reconnect with no
  // hint at any point. SettingsPanel hands down the socket layer's
  // reactive queue (pendingMessages); a queued CONFIG_UPDATE whose
  // config touches a field marks exactly that field: its control goes
  // inert and names the pending value until the config reply lands
  // and the control follows it for real.
  const pendingConfigEntry = (key) => (pendingMessages || []).find(
    (m) => m && m.type === 'CONFIG_UPDATE_REQUEST'
      && m.payload && m.payload.config && key in m.payload.config,
  );
  const queuedHintStyle = { fontSize: '12px', color: 'var(--accent)', marginTop: '8px', width: '280px' };
  const inertWrap = (pending) => ({
    opacity: pending ? 0.55 : 1,
    pointerEvents: pending ? 'none' : 'auto',
  });

  const error = configError || '';
  // Engine defaults (H3) until the first config payload lands.
  const sttModel = (config && config.speech_stt_model) || 'small';
  const transcriptionModel = (config && config.transcription_stt_model) || 'small';
  const ttsVoice = (config && config.speech_tts_voice) || 'af_heart';
  const ttsSpeed = (config && config.speech_tts_speed) || 'normal';
  const sttLanguage = (config && config.speech_stt_language) || 'auto';
  const voicePending = pendingConfigEntry('speech_tts_voice');
  const speedPending = pendingConfigEntry('speech_tts_speed');
  const languagePending = pendingConfigEntry('speech_stt_language');
  const measuredSlowMs = MEASURED_STT_TURN_MS[sttModel];

  return (
    <div style={{ marginTop: '32px' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
        <h3 style={{ fontSize: '18px', fontWeight: 500, margin: 0, display: 'flex', alignItems: 'center', gap: '8px' }}>
          <Mic size={18} /> Speech
        </h3>
      </div>
      <p style={{ color: 'var(--text-muted)', fontSize: '13px', marginBottom: '16px' }}>
        The ears and mouth Aether uses for dictation, voice mode and transcription. Speech models run in the app — download more in the Local Models hub.
      </p>

      {error && (
        <div
          data-testid="speech-error"
          style={{ background: 'rgba(201,42,42,0.08)', border: '1px solid rgba(201,42,42,0.3)', borderRadius: '10px', padding: '12px 16px', fontSize: '13px', color: 'var(--danger)', marginBottom: '16px' }}
        >
          {error}
        </div>
      )}

      <fieldset
        data-testid="speech-config-gate"
        disabled={!ready}
        style={{
          border: 0, margin: 0, padding: 0, minWidth: 0,
          display: 'flex', flexDirection: 'column', gap: '8px',
          opacity: ready ? 1 : 0.55,
        }}
      >
        <div style={rowStyle}>
          <div>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <h4 style={{ fontSize: '14px', fontWeight: 600, margin: 0 }}>Speech-to-text model</h4>
              <span style={{ fontSize: '11px', color: 'var(--text-secondary)' }}>Dictation &amp; voice mode</span>
            </div>
            <p style={{ fontSize: '12px', color: 'var(--text-secondary)', marginTop: '4px', marginBottom: 0 }}>
              A new model applies to dictation right away, and to voice mode from your next voice session — a session already talking keeps the model it started with.
            </p>
            {measuredSlowMs && (
              <p data-testid="speech-slow-warning" style={{ fontSize: '12px', color: 'var(--accent)', marginTop: '8px', marginBottom: 0 }}>
                Heads up: measured on {MEASURED_STT_SOURCE}, this model took about {(measuredSlowMs / 1000).toFixed(1)} seconds per spoken turn — slow enough that voice mode warns you and steps down to a faster model on its own. Your own PC hasn&apos;t been measured yet, so treat this as a warning, not a verdict on your machine.
              </p>
            )}
          </div>
          <div data-testid="speech-stt-select">
            <div style={inertWrap(pendingConfigEntry('speech_stt_model'))}>
              <Dropdown
                value={sttModel}
                onChange={(val) => onConfigUpdate({ speech_stt_model: val })}
                options={sttOptions(sttModel, downloadedStt)}
                style={{ width: '280px' }}
                align="right"
              />
            </div>
            {pendingConfigEntry('speech_stt_model') && (
              <div data-testid="speech-stt-queued" style={queuedHintStyle}>
                Speech-to-text change to {pendingConfigEntry('speech_stt_model').payload.config.speech_stt_model} queued — will apply when the engine reconnects.
              </div>
            )}
          </div>
        </div>

        <div style={rowStyle}>
          <div>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <h4 style={{ fontSize: '14px', fontWeight: 600, margin: 0 }}>Speech language</h4>
              <span style={{ fontSize: '11px', color: 'var(--text-secondary)' }}>Dictation &amp; voice mode</span>
            </div>
            <p style={{ fontSize: '12px', color: 'var(--text-secondary)', marginTop: '4px', marginBottom: 0 }}>
              The language you speak in. Auto lets the speech model detect it per recording; naming one is faster and more accurate when you always speak the same language.
            </p>
          </div>
          <div data-testid="speech-language-select">
            <div style={inertWrap(languagePending)}>
              <Dropdown
                value={sttLanguage}
                onChange={(val) => onConfigUpdate({ speech_stt_language: val })}
                options={STT_LANGUAGE_OPTIONS}
                style={{ width: '280px' }}
                align="right"
              />
            </div>
            {languagePending && (
              <div data-testid="speech-language-queued" style={queuedHintStyle}>
                Speech language change queued — will apply when the engine reconnects.
              </div>
            )}
          </div>
        </div>

        <div style={{ ...rowStyle, flexDirection: 'column', alignItems: 'stretch', justifyContent: 'flex-start' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: '16px' }}>
            <div>
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                <h4 style={{ fontSize: '14px', fontWeight: 600, margin: 0 }}>Voice</h4>
                <span style={{ fontSize: '11px', color: 'var(--text-secondary)' }}>Kokoro text-to-speech</span>
              </div>
              <p style={{ fontSize: '12px', color: 'var(--text-secondary)', marginTop: '4px', marginBottom: 0 }}>
                The voice can change mid-conversation — a new voice speaks from the next sentence. Preview plays the same short sentence in every voice, so the comparison is fair.
              </p>
            </div>
          </div>

          {ttsAvailability === 'unavailable' && (
            <div data-testid="speech-preview-unavailable" style={{ fontSize: '12px', color: 'var(--accent)', marginTop: '12px' }}>
              Voice preview isn&apos;t available on this machine — {ttsUnavailableReason || 'the local speech engine is not installed'}. You can still pick a voice; it will speak once the speech engine is installed.
            </div>
          )}
          {preview.status === 'error' && preview.error && (
            <div data-testid="speech-preview-error" style={{ fontSize: '12px', color: 'var(--danger)', marginTop: '12px' }}>
              {preview.error}
            </div>
          )}

          <div data-testid="speech-voice-list" role="radiogroup" aria-label="Voice" style={{ marginTop: '12px', ...inertWrap(voicePending) }}>
            {voiceGroups(ttsVoice).map((group) => (
              <div key={group.group} style={{ marginBottom: '12px' }}>
                <div style={{ fontSize: '11px', textTransform: 'uppercase', letterSpacing: '0.05em', color: 'var(--text-secondary)', fontWeight: 600, marginBottom: '6px' }}>
                  {group.group}
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                  {group.items.map((item) => {
                    const selected = item.value === ttsVoice;
                    const isPreviewing = preview.voice === item.value
                      && (preview.status === 'loading' || preview.status === 'playing');
                    return (
                      <div
                        key={item.value}
                        data-testid={`speech-voice-row-${item.value}`}
                        role="radio"
                        aria-checked={selected}
                        tabIndex={0}
                        title={item.value}
                        onClick={() => { if (!selected) onConfigUpdate({ speech_tts_voice: item.value }); }}
                        onKeyDown={(e) => {
                          if ((e.key === 'Enter' || e.key === ' ') && !selected) {
                            e.preventDefault();
                            onConfigUpdate({ speech_tts_voice: item.value });
                          }
                        }}
                        style={{
                          display: 'flex', alignItems: 'center', gap: '12px',
                          padding: '10px 12px', borderRadius: '10px', cursor: selected ? 'default' : 'pointer',
                          background: selected ? 'rgba(var(--accent-rgb), 0.10)' : 'var(--bg-overlay)',
                          border: `1px solid ${selected ? 'var(--accent)' : 'var(--border-default)'}`,
                        }}
                      >
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                            <span style={{ fontSize: '13px', fontWeight: 600, color: 'var(--text-primary)' }}>{item.name}</span>
                            {group.chip && (
                              <span style={{ fontSize: '10px', color: 'var(--text-secondary)', border: '1px solid var(--border-default)', borderRadius: '999px', padding: '1px 7px' }}>
                                {group.chip}
                              </span>
                            )}
                            {selected && <Check size={13} color="var(--accent)" />}
                          </div>
                          <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginTop: '2px' }}>
                            {item.description}
                          </div>
                        </div>
                        <button
                          type="button"
                          data-testid={`speech-voice-preview-${item.value}`}
                          aria-label={`Preview voice ${item.name}`}
                          disabled={ttsAvailability === 'unavailable'}
                          onClick={(e) => { e.stopPropagation(); handlePreviewClick(item.value); }}
                          style={{
                            display: 'flex', alignItems: 'center', gap: '6px',
                            background: 'transparent', color: 'var(--accent)',
                            border: '1px solid rgba(var(--accent-rgb), 0.4)', borderRadius: '8px',
                            padding: '6px 10px', fontSize: '12px',
                            cursor: ttsAvailability === 'unavailable' ? 'not-allowed' : 'pointer',
                            opacity: ttsAvailability === 'unavailable' ? 0.5 : 1,
                          }}
                        >
                          {preview.voice === item.value && preview.status === 'playing'
                            ? <Square size={12} />
                            : <Play size={12} />}
                          {preview.voice === item.value && preview.status === 'loading'
                            ? 'Loading…'
                            : (isPreviewing ? 'Stop' : 'Preview')}
                        </button>
                      </div>
                    );
                  })}
                </div>
              </div>
            ))}
          </div>
          {voicePending && (
            <div data-testid="speech-voice-queued" style={{ fontSize: '12px', color: 'var(--accent)', marginTop: '4px' }}>
              Voice change to {kokoroVoiceDisplayName(voicePending.payload.config.speech_tts_voice)} queued — will apply when the engine reconnects.
            </div>
          )}
        </div>

        <div style={rowStyle}>
          <div>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <h4 style={{ fontSize: '14px', fontWeight: 600, margin: 0 }}>Speech speed</h4>
              <span style={{ fontSize: '11px', color: 'var(--text-secondary)' }}>How fast Aether talks</span>
            </div>
            <p style={{ fontSize: '12px', color: 'var(--text-secondary)', marginTop: '4px', marginBottom: 0 }}>
              Takes effect the next time Aether starts speaking — a conversation already talking keeps its pace.
            </p>
          </div>
          <div>
            <div
              data-testid="speech-speed-control"
              style={{ display: 'flex', background: 'var(--bg-overlay)', padding: '4px', borderRadius: '10px', width: '280px', ...inertWrap(speedPending) }}
            >
              {TTS_SPEED_OPTIONS.map((option) => (
                <button
                  key={option.value}
                  type="button"
                  data-testid={`speech-speed-${option.value}`}
                  onClick={() => onConfigUpdate({ speech_tts_speed: option.value })}
                  style={{
                    flex: 1,
                    background: ttsSpeed === option.value ? 'rgba(var(--accent-rgb), 0.18)' : 'transparent',
                    color: ttsSpeed === option.value ? 'var(--accent)' : 'var(--text-secondary)',
                    border: 'none', padding: '7px 0', borderRadius: '7px', cursor: 'pointer',
                    fontSize: '13px', transition: 'all 0.2s',
                  }}
                >
                  {option.label}
                </button>
              ))}
            </div>
            {speedPending && (
              <div data-testid="speech-speed-queued" style={queuedHintStyle}>
                Speech speed change to {ttsSpeedLabel(speedPending.payload.config.speech_tts_speed)} queued — will apply when the engine reconnects.
              </div>
            )}
          </div>
        </div>

        <div style={rowStyle}>
          <div>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <h4 style={{ fontSize: '14px', fontWeight: 600, margin: 0 }}>Transcription model</h4>
              <span style={{ fontSize: '11px', color: 'var(--text-secondary)' }}>Media library</span>
            </div>
            <p style={{ fontSize: '12px', color: 'var(--text-secondary)', marginTop: '4px', marginBottom: 0 }}>
              The model used for video transcription, kept separate from dictation so a heavy model never slows down your typing.
            </p>
          </div>
          <div data-testid="speech-transcription-select">
            <div style={inertWrap(pendingConfigEntry('transcription_stt_model'))}>
              <Dropdown
                value={transcriptionModel}
                onChange={(val) => onConfigUpdate({ transcription_stt_model: val })}
                options={sttOptions(transcriptionModel, downloadedStt)}
                style={{ width: '280px' }}
                align="right"
              />
            </div>
            {pendingConfigEntry('transcription_stt_model') && (
              <div data-testid="speech-transcription-queued" style={queuedHintStyle}>
                Transcription model change to {pendingConfigEntry('transcription_stt_model').payload.config.transcription_stt_model} queued — will apply when the engine reconnects.
              </div>
            )}
          </div>
        </div>
      </fieldset>
    </div>
  );
}
