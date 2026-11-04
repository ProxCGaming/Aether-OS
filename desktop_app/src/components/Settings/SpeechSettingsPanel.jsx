import React, { useState, useEffect, useRef } from 'react';
import { Mic } from 'lucide-react';
import Dropdown from '../../Dropdown';

// Model Hub H5: the Speech card in Settings → Models & Tasks,
// beside the Auxiliary models card. It is the selection surface
// for the speech fields the engine gained in H3 —
// `speech_stt_model` (dictation + voice mode), `speech_tts_voice`
// (the Kokoro voice) and `transcription_stt_model` (video
// transcription) — which until now were only reachable by
// hand-editing the config file.
//
// The panel is controlled, in the E6-F05 sense: SettingsPanel owns
// the socket, applies every CONFIG_GET / CONFIG_UPDATE /
// CONFIG_RESET payload through its single applyConfigValues point,
// and hands this component the applied config, the most recent
// strict-write refusal (configError, surfaced verbatim), and the
// one sendConfigUpdate callback — so offline queueing (E6-F15)
// keeps working and a refused write simply never lands in the
// config, leaving the selects showing the value actually in
// force. The only socket traffic of its own is the hub inventory
// pair (MODEL_HUB_DOWNLOADED, request_id-correlated exactly as
// LocalModelsTab does it) so speech repos downloaded in the Local
// Models hub can be offered here.
//
// The controls mirror what the engine can actually do (H3):
// dictation rebuilds its engine per model, so an STT change
// applies to dictation right away, but a live voice session keeps
// the model it started with — the change takes voice mode from
// the next session, and the card says so rather than pretending
// otherwise. The TTS voice is a per-sentence parameter, so it
// genuinely can change mid-conversation. Kokoro ships premade
// voices, so the voice control is a dropdown grouped by language;
// a model with a different voice scheme would get a different
// control, per the owner's capability-driven rule.

// The sensible faster-whisper sizes for a settings select. The
// engine validator accepts more (the .en and distil variants, any
// faster-whisper-* HF repo id); those arrive here as downloaded
// hub repos or as the preserved current value below.
const BUILTIN_STT_MODELS = ['tiny', 'base', 'small', 'medium', 'large-v3', 'turbo'];

const KOKORO_VOICE_GROUPS = [
  {
    group: 'American English',
    items: [
      { value: 'af_heart', label: 'Heart (af_heart)' },
      { value: 'af_bella', label: 'Bella (af_bella)' },
      { value: 'af_sarah', label: 'Sarah (af_sarah)' },
      { value: 'af_nicole', label: 'Nicole (af_nicole)' },
      { value: 'am_adam', label: 'Adam (am_adam)' },
      { value: 'am_michael', label: 'Michael (am_michael)' },
    ],
  },
  {
    group: 'British English',
    items: [
      { value: 'bf_emma', label: 'Emma (bf_emma)' },
      { value: 'bf_isabella', label: 'Isabella (bf_isabella)' },
      { value: 'bm_george', label: 'George (bm_george)' },
      { value: 'bm_lewis', label: 'Lewis (bm_lewis)' },
    ],
  },
  {
    group: 'Hindi',
    items: [
      { value: 'hf_alpha', label: 'Alpha (hf_alpha)' },
      { value: 'hf_beta', label: 'Beta (hf_beta)' },
      { value: 'hm_omega', label: 'Omega (hm_omega)' },
      { value: 'hm_psi', label: 'Psi (hm_psi)' },
    ],
  },
];

// Options for either STT select: the built-in sizes, then any
// downloaded in-app speech repos from the hub, then the current
// value itself if it is in neither list — a repo id that is not
// downloaded is labelled "(not downloaded)" so the select never
// silently drops a configured model, and never claims a repo is
// on disk when it is not. (A bare size name outside the short
// list, e.g. a distil variant, needs no such label: faster-whisper
// resolves bare names itself.)
function sttOptions(currentValue, downloadedStt) {
  const options = [
    {
      group: 'Built-in sizes',
      items: BUILTIN_STT_MODELS.map((m) => ({ value: m, label: m })),
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

function voiceOptions(currentValue) {
  const groups = KOKORO_VOICE_GROUPS.map((g) => ({
    group: g.group,
    items: g.items.map((item) => ({ ...item })),
  }));
  const known = new Set(
    groups.flatMap((g) => g.items.map((item) => item.value)),
  );
  if (currentValue && !known.has(currentValue)) {
    groups.push({
      group: 'Current selection',
      items: [{ value: currentValue, label: currentValue }],
    });
  }
  return groups;
}

const rowStyle = {
  background: 'rgba(255,255,255,0.02)',
  border: '1px solid rgba(255,255,255,0.05)',
  borderRadius: '12px',
  padding: '12px 16px',
  display: 'flex',
  justifyContent: 'space-between',
  alignItems: 'center',
  gap: '16px',
};

export default function SpeechSettingsPanel({ config, configError, onConfigUpdate, pendingMessages = [], reconnectCount = 0, wsRef, configReady }) {
  // RUX-C-11: until the first config answer lands, the selects
  // below show engine defaults ('small', 'af_heart') — values the
  // user may never have chosen, presented as their configuration.
  // The selects are disabled (one fieldset) until the config is
  // actually known; the Models tab's banner explains why. Callers
  // that don't pass configReady get the derived rule: a non-null
  // config IS an answer.
  const ready = configReady !== undefined ? configReady : config != null;
  const [downloadedStt, setDownloadedStt] = useState([]);
  // request_id -> one-shot response handler for the hub pair.
  const pendingRef = useRef(new Map());

  // Fetch the hub's downloaded inventory once, on mount: the
  // speech-to-text rows are the extra options in both STT selects.
  // Wave 2 slice 4 (RUX-A-17): the send below is OPEN-guarded, so a
  // mount while the engine was down fetched nothing and the selects
  // kept labelling downloaded models "(not downloaded)" forever;
  // reconnectCount re-runs the effect on every recovery.
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
      const requestId = `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
      pendingRef.current.set(requestId, (payload) => {
        if (payload && Array.isArray(payload.models)) {
          setDownloadedStt(payload.models.filter(
            (row) => row && row.family === 'speech-to-text',
          ));
        }
      });
      ws.send(JSON.stringify({
        type: 'MODEL_HUB_DOWNLOADED_REQUEST',
        schema_version: 1,
        request_id: requestId,
        payload: {},
      }));
    }
    return () => ws.removeEventListener('message', handleMessage);
  }, [wsRef, reconnectCount]);

  // RUX-A-22: the selects are controlled from the applied config,
  // and onConfigUpdate (SettingsPanel's sendConfigUpdate) QUEUES
  // while the engine is unreachable — so an offline pick used to
  // snap straight back and then ghost-apply on reconnect with no
  // hint at any point. SettingsPanel hands down the socket layer's
  // reactive queue (pendingMessages); a queued CONFIG_UPDATE whose
  // config touches a field marks exactly that field: its select goes
  // inert and names the pending value until the config reply lands
  // and the control follows it for real.
  const pendingConfigEntry = (key) => (pendingMessages || []).find(
    (m) => m && m.type === 'CONFIG_UPDATE_REQUEST'
      && m.payload && m.payload.config && key in m.payload.config,
  );
  const queuedHintStyle = { fontSize: '12px', color: '#fbbf24', marginTop: '8px', width: '280px' };
  const inertWrap = (pending) => ({
    opacity: pending ? 0.55 : 1,
    pointerEvents: pending ? 'none' : 'auto',
  });

  const error = configError || '';
  // Engine defaults (H3) until the first config payload lands.
  const sttModel = (config && config.speech_stt_model) || 'small';
  const transcriptionModel = (config && config.transcription_stt_model) || 'small';
  const ttsVoice = (config && config.speech_tts_voice) || 'af_heart';

  return (
    <div style={{ marginTop: '32px' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
        <h3 style={{ fontSize: '18px', fontWeight: 500, margin: 0, display: 'flex', alignItems: 'center', gap: '8px' }}>
          <Mic size={18} /> Speech
        </h3>
      </div>
      <p style={{ color: '#9090a0', fontSize: '13px', marginBottom: '16px' }}>
        The ears and mouth Aether uses for dictation, voice mode and transcription. Speech models run in the app — download more in the Local Models hub.
      </p>

      {error && (
        <div
          data-testid="speech-error"
          style={{ background: 'rgba(239,68,68,0.08)', border: '1px solid rgba(239,68,68,0.3)', borderRadius: '10px', padding: '12px 16px', fontSize: '13px', color: '#fca5a5', marginBottom: '16px' }}
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
              <span style={{ fontSize: '11px', color: '#9090a0' }}>Dictation &amp; voice mode</span>
            </div>
            <p style={{ fontSize: '12px', color: '#9090a0', marginTop: '4px', marginBottom: 0 }}>
              A new model applies to dictation right away, and to voice mode from your next voice session — a session already talking keeps the model it started with.
            </p>
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
              <h4 style={{ fontSize: '14px', fontWeight: 600, margin: 0 }}>Voice</h4>
              <span style={{ fontSize: '11px', color: '#9090a0' }}>Kokoro text-to-speech</span>
            </div>
            <p style={{ fontSize: '12px', color: '#9090a0', marginTop: '4px', marginBottom: 0 }}>
              The voice can change mid-conversation — a new voice speaks from the next sentence.
            </p>
          </div>
          <div data-testid="speech-voice-select">
            <div style={inertWrap(pendingConfigEntry('speech_tts_voice'))}>
              <Dropdown
                value={ttsVoice}
                onChange={(val) => onConfigUpdate({ speech_tts_voice: val })}
                options={voiceOptions(ttsVoice)}
                style={{ width: '280px' }}
                align="right"
              />
            </div>
            {pendingConfigEntry('speech_tts_voice') && (
              <div data-testid="speech-voice-queued" style={queuedHintStyle}>
                Voice change to {pendingConfigEntry('speech_tts_voice').payload.config.speech_tts_voice} queued — will apply when the engine reconnects.
              </div>
            )}
          </div>
        </div>

        <div style={rowStyle}>
          <div>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <h4 style={{ fontSize: '14px', fontWeight: 600, margin: 0 }}>Transcription model</h4>
              <span style={{ fontSize: '11px', color: '#9090a0' }}>Media library</span>
            </div>
            <p style={{ fontSize: '12px', color: '#9090a0', marginTop: '4px', marginBottom: 0 }}>
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
