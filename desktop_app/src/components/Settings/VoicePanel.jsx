import React, { useEffect, useRef, useState } from 'react';
import { Sliders, Mic } from 'lucide-react';
import Dropdown from '../../Dropdown';
import LoadStateRow from './LoadStateRow';
import { audioConstraintForDevice, useMicrophoneDevices } from '../../micDevices';

// Wave 5 (Annex C row 7): the Voice Tuning tab, extracted from
// SettingsPanel.jsx — the shell keeps the slider values, the
// commit-on-release handlers (D2-08) and the config plumbing;
// this panel is the controlled rendering.
//
// Wave 4: the Voice section is the ONE voice home — the Speech
// card renders alongside this panel, and this panel gains the
// Input card: the microphone every capture path uses (voice
// mode and dictation alike, through the engine config's
// speech_input_device_id) and the dictation button behaviour
// (dictation_mode: hold-to-talk or click-to-start/stop). Both
// are handed-off config fields with the same queued honesty as
// the sliders: the shell passes the applied value, the commit
// callback and the pending (queued) value.
export default function VoicePanel({ configAnswered, configLoadError, isConnected, onRetryConfigLoad, vadThreshold, setVadThreshold, commitVadThreshold, pendingVadValue, silenceDurationMs, setSilenceDurationMs, commitSilenceDuration, pendingSilenceValue, smartTurnSensitivity, setSmartTurnSensitivity, commitSmartTurnSensitivity, pendingSmartTurnValue, inputDeviceId = '', onInputDeviceChange, pendingInputDeviceValue, dictationMode = 'hold', onDictationModeChange, pendingDictationModeValue }) {
  const microphones = useMicrophoneDevices();
  const [micTest, setMicTest] = useState({ active: false, level: 0, error: '' });
  const micStreamRef = useRef(null);
  const micCtxRef = useRef(null);
  const micRafRef = useRef(0);

  const stopMicTest = () => {
    if (micRafRef.current) cancelAnimationFrame(micRafRef.current);
    micRafRef.current = 0;
    if (micStreamRef.current) {
      micStreamRef.current.getTracks().forEach((track) => track.stop());
      micStreamRef.current = null;
    }
    if (micCtxRef.current) {
      micCtxRef.current.close().catch(() => {});
      micCtxRef.current = null;
    }
    setMicTest({ active: false, level: 0, error: '' });
  };

  const startMicTest = async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: audioConstraintForDevice(inputDeviceId),
      });
      micStreamRef.current = stream;
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) {
        stream.getTracks().forEach((track) => track.stop());
        micStreamRef.current = null;
        setMicTest({ active: false, level: 0, error: 'Live level monitoring is not available in this browser.' });
        return;
      }
      const ctx = new Ctx();
      micCtxRef.current = ctx;
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 512;
      ctx.createMediaStreamSource(stream).connect(analyser);
      const data = new Uint8Array(analyser.fftSize);
      const tick = () => {
        analyser.getByteTimeDomainData(data);
        let sum = 0;
        for (let i = 0; i < data.length; i += 1) {
          const d = (data[i] - 128) / 128;
          sum += d * d;
        }
        const rms = Math.sqrt(sum / data.length);
        setMicTest({ active: true, level: Math.min(1, rms * 3), error: '' });
        micRafRef.current = requestAnimationFrame(tick);
      };
      tick();
    } catch {
      setMicTest({ active: false, level: 0, error: 'Microphone access was blocked — check this app’s microphone permission, then try again.' });
    }
  };

  // The test stream never outlives the panel or a device switch.
  useEffect(() => () => {
    if (micRafRef.current) cancelAnimationFrame(micRafRef.current);
    if (micStreamRef.current) {
      micStreamRef.current.getTracks().forEach((track) => track.stop());
    }
    if (micCtxRef.current) micCtxRef.current.close().catch(() => {});
  }, []);
  useEffect(() => {
    if (micTest.active) stopMicTest();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [inputDeviceId]);

  const micOptions = [
    { value: '', label: 'System default' },
    ...microphones.map((d) => ({ value: d.deviceId, label: d.label })),
  ];
  if (inputDeviceId && !microphones.some((d) => d.deviceId === inputDeviceId)) {
    // The saved microphone is not plugged in right now: keep it
    // selectable and say so, rather than silently showing the
    // default as if it were the saved choice.
    micOptions.push({ value: inputDeviceId, label: 'Saved microphone (not connected)' });
  }
  const inputPending = pendingInputDeviceValue !== undefined;
  const dictationPending = pendingDictationModeValue !== undefined;
  const inert = (pending) => ({
    opacity: pending || !configAnswered ? 0.55 : 1,
    pointerEvents: pending || !configAnswered ? 'none' : 'auto',
  });
  const dictationModes = [
    {
      value: 'hold',
      title: 'Hold to talk',
      blurb: 'Keep the mic button held while you speak; release to send.',
    },
    {
      value: 'toggle',
      title: 'Click to start & stop',
      blurb: 'Click the mic button once to start dictating, and again when you are done.',
    },
  ];
  return (
    <div style={{ color: 'var(--text-primary)' }}>
      <h2 style={{ fontSize: '24px', marginBottom: '8px', fontWeight: 500 }}>Voice Tuning</h2>
      <p style={{ color: 'var(--text-muted)', fontSize: '14px', marginBottom: '24px' }}>
        Fine-tune speech detection and endpointing thresholds. Changes take effect on the next voice session.
      </p>

      {/* RUX-C-01: the sliders below are config-driven — until
          the first config answer lands they hold local
          defaults, and the section says so rather than letting
          0.50 / 700 ms pose as the saved values. */}
      {!configAnswered && (
        <div style={{ marginBottom: '24px' }}>
          <LoadStateRow
            loadState={configLoadError ? 'error' : 'loading'}
            isConnected={isConnected}
            testId="voice-config-load-state"
            variant="plain"
            loadingText="Loading your saved voice settings…"
            offlineText="Waiting for the engine — your saved voice settings will appear here once it connects."
            errorText={configLoadError || 'Your saved voice settings could not be loaded.'}
          />
          {configLoadError && (
            <button type="button" onClick={onRetryConfigLoad} style={{ marginTop: '8px', background: 'transparent', color: 'var(--accent)', border: '1px solid rgba(var(--accent-rgb),0.4)', padding: '6px 14px', borderRadius: '8px', cursor: 'pointer', fontSize: '13px' }}>
              Retry
            </button>
          )}
        </div>
      )}

      <div style={{
        background: 'var(--bg-raised)',
        border: '1px solid var(--border-default)',
        borderRadius: '12px',
        padding: '24px',
        marginBottom: '20px'
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '16px' }}>
          <Mic size={18} style={{ color: 'var(--accent)' }} />
          <h3 style={{ fontSize: '16px', fontWeight: 500, color: 'var(--text-primary)', margin: 0 }}>Microphone &amp; Dictation</h3>
        </div>

        <div style={{ marginBottom: '24px' }}>
          <div style={{ fontWeight: 500, fontSize: '14px', color: 'var(--text-primary)', marginBottom: '6px' }}>
            Microphone
          </div>
          <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginBottom: '10px' }}>
            The microphone Aether hears — used by both voice mode and dictation. Names appear after the browser has microphone permission; until then devices show as numbered microphones.
          </div>
          <div data-testid="input-device-select" style={inert(inputPending)}>
            <Dropdown
              value={inputDeviceId}
              onChange={(val) => { if (onInputDeviceChange) onInputDeviceChange(val); }}
              options={micOptions}
              style={{ width: '320px' }}
            />
          </div>
          {inputPending && (
            <div data-testid="input-device-queued" style={{ fontSize: '12px', color: 'var(--accent)', marginTop: '8px' }}>
              Microphone change queued — will apply when the engine reconnects.
            </div>
          )}
          <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginTop: '12px' }}>
            <button
              type="button"
              data-testid="mic-test-toggle"
              onClick={() => { if (micTest.active) stopMicTest(); else startMicTest(); }}
              style={{ background: 'transparent', color: 'var(--accent)', border: '1px solid rgba(var(--accent-rgb),0.4)', padding: '6px 14px', borderRadius: '8px', cursor: 'pointer', fontSize: '13px' }}
            >
              {micTest.active ? 'Stop test' : 'Test microphone'}
            </button>
            {micTest.active && (
              <div style={{ flex: 1, maxWidth: '240px', height: '8px', borderRadius: '4px', background: 'var(--bg-overlay)', overflow: 'hidden' }}>
                <div
                  data-testid="mic-level-fill"
                  style={{ height: '100%', width: `${Math.round(micTest.level * 100)}%`, background: 'var(--accent)', transition: 'width 60ms linear' }}
                />
              </div>
            )}
          </div>
          {micTest.error && (
            <div data-testid="mic-test-error" style={{ fontSize: '12px', color: 'var(--danger)', marginTop: '8px' }}>
              {micTest.error}
            </div>
          )}
        </div>

        <div>
          <div style={{ fontWeight: 500, fontSize: '14px', color: 'var(--text-primary)', marginBottom: '6px' }}>
            Dictation button
          </div>
          <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginBottom: '10px' }}>
            How the microphone button in the chat composer starts and stops dictation.
          </div>
          <div style={{ display: 'flex', gap: '12px', ...inert(dictationPending) }}>
            {dictationModes.map((mode) => {
              const selected = dictationMode === mode.value;
              return (
                <button
                  key={mode.value}
                  type="button"
                  data-testid={`dictation-mode-${mode.value}`}
                  aria-pressed={selected}
                  onClick={() => { if (onDictationModeChange) onDictationModeChange(mode.value); }}
                  style={{
                    flex: 1, textAlign: 'left', padding: '12px 14px', borderRadius: '10px', cursor: 'pointer',
                    background: selected ? 'rgba(var(--accent-rgb), 0.10)' : 'var(--bg-overlay)',
                    border: `1px solid ${selected ? 'var(--accent)' : 'var(--border-default)'}`,
                    color: 'var(--text-primary)',
                  }}
                >
                  <div style={{ fontSize: '13px', fontWeight: 600, marginBottom: '4px' }}>{mode.title}</div>
                  <div style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>{mode.blurb}</div>
                </button>
              );
            })}
          </div>
          {dictationPending && (
            <div data-testid="dictation-mode-queued" style={{ fontSize: '12px', color: 'var(--accent)', marginTop: '8px' }}>
              Dictation button change to {pendingDictationModeValue === 'toggle' ? 'Click to start & stop' : 'Hold to talk'} queued — will apply when the engine reconnects.
            </div>
          )}
        </div>
      </div>

      <div style={{
        background: 'var(--bg-raised)',
        border: '1px solid var(--border-default)',
        borderRadius: '12px',
        padding: '24px',
        marginBottom: '20px'
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '16px' }}>
          <Sliders size={18} style={{ color: 'var(--accent)' }} />
          <h3 style={{ fontSize: '16px', fontWeight: 500, color: 'var(--text-primary)', margin: 0 }}>Endpointing Thresholds</h3>
        </div>

        <div style={{ marginBottom: '24px' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '6px' }}>
            <label htmlFor="vad-threshold-slider" style={{ fontWeight: 500, fontSize: '14px', color: 'var(--text-primary)' }}>
              VAD Speech Threshold
            </label>
            <span data-testid="vad-threshold-value" style={{ fontSize: '13px', color: 'var(--accent)', fontWeight: 600 }}>
              {vadThreshold.toFixed(2)}
            </span>
          </div>
          <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginBottom: '10px' }}>
            Confidence threshold for Silero VAD to register voice activity (default: 0.50). Higher values reduce false triggers from background noise; lower values catch softer speech.
          </div>
          <input
            id="vad-threshold-slider"
            data-testid="vad-threshold-slider"
            type="range"
            min="0.1"
            max="0.9"
            step="0.05"
            disabled={!configAnswered}
            value={vadThreshold}
            onChange={(e) => {
              // D2-08: a drag is dozens of ticks — local
              // state follows the pointer, but the config
              // write commits ONCE on release (see
              // commitVadThreshold), so the engine is not
              // written per tick and its per-tick echo cannot
              // move the slider under the user's pointer
              // mid-drag. The RUX-A-15 delivery discipline is
              // preserved at commit time: through
              // sendSettingsAction the edit QUEUES while
              // offline, and the queued note under the slider
              // names the pending value until the reply lands.
              setVadThreshold(parseFloat(e.target.value));
            }}
            onPointerUp={commitVadThreshold}
            onKeyUp={commitVadThreshold}
            onBlur={commitVadThreshold}
            style={{ width: '100%', accentColor: 'var(--accent)', cursor: 'pointer' }}
          />
          {pendingVadValue !== undefined && (
            <div data-testid="vad-threshold-queued" style={{ fontSize: '12px', color: 'var(--accent)', marginTop: '8px' }}>
              VAD threshold change to {Number(pendingVadValue).toFixed(2)} queued — will apply when the engine reconnects.
            </div>
          )}
        </div>

        <div>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '6px' }}>
            <label htmlFor="silence-duration-slider" style={{ fontWeight: 500, fontSize: '14px', color: 'var(--text-primary)' }}>
              Silence Duration
            </label>
            <span data-testid="silence-duration-value" style={{ fontSize: '13px', color: 'var(--accent)', fontWeight: 600 }}>
              {silenceDurationMs} ms
            </span>
          </div>
          <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginBottom: '10px' }}>
            Trailing silence window required before the turn is marked complete (default: 700 ms). Lower values produce faster turn-taking; higher values allow natural pauses while speaking.
          </div>
          <input
            id="silence-duration-slider"
            data-testid="silence-duration-slider"
            type="range"
            min="200"
            max="2000"
            step="50"
            disabled={!configAnswered}
            value={silenceDurationMs}
            onChange={(e) => {
              // D2-08: same commit-on-release conversion as
              // the VAD slider above — display follows the
              // drag; the write lands once, on release.
              setSilenceDurationMs(parseInt(e.target.value, 10));
            }}
            onPointerUp={commitSilenceDuration}
            onKeyUp={commitSilenceDuration}
            onBlur={commitSilenceDuration}
            style={{ width: '100%', accentColor: 'var(--accent)', cursor: 'pointer' }}
          />
          {pendingSilenceValue !== undefined && (
            <div data-testid="silence-duration-queued" style={{ fontSize: '12px', color: 'var(--accent)', marginTop: '8px' }}>
              Silence duration change to {pendingSilenceValue} ms queued — will apply when the engine reconnects.
            </div>
          )}
        </div>

        {/* Master build S21 (voice G8): Smart Turn sensitivity —
            the third endpointing control. The engine field
            (UserConfig.smart_turn_sensitivity) has been read by
            the Endpointer all along; it simply had no UI. Same
            D2-08 commit-on-release wiring as the sliders above,
            and 0.00 is a real value (Smart Turn off), not an
            unset state. */}
        <div style={{ marginTop: '24px' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '6px' }}>
            <label htmlFor="smart-turn-sensitivity-slider" style={{ fontWeight: 500, fontSize: '14px', color: 'var(--text-primary)' }}>
              Smart Turn Sensitivity
            </label>
            <span data-testid="smart-turn-sensitivity-value" style={{ fontSize: '13px', color: 'var(--accent)', fontWeight: 600 }}>
              {smartTurnSensitivity.toFixed(2)}
            </span>
          </div>
          <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginBottom: '10px' }}>
            How strongly the built-in Smart Turn heuristic bends the silence window (default: 0.50). It judges the partial transcript as you speak: a sentence that sounds finished ends the turn earlier, an unfinished one is held longer — bounded between 120 ms and 1,200 ms either way. At 0.00 Smart Turn is off and turns end on silence alone.
          </div>
          <input
            id="smart-turn-sensitivity-slider"
            data-testid="smart-turn-sensitivity-slider"
            type="range"
            min="0"
            max="1"
            step="0.05"
            disabled={!configAnswered}
            value={smartTurnSensitivity}
            onChange={(e) => {
              // D2-08: same commit-on-release conversion as the
              // sliders above — display follows the drag; the
              // write lands once, on release.
              setSmartTurnSensitivity(parseFloat(e.target.value));
            }}
            onPointerUp={commitSmartTurnSensitivity}
            onKeyUp={commitSmartTurnSensitivity}
            onBlur={commitSmartTurnSensitivity}
            style={{ width: '100%', accentColor: 'var(--accent)', cursor: 'pointer' }}
          />
          {pendingSmartTurnValue !== undefined && (
            <div data-testid="smart-turn-sensitivity-queued" style={{ fontSize: '12px', color: 'var(--accent)', marginTop: '8px' }}>
              Smart Turn sensitivity change to {Number(pendingSmartTurnValue).toFixed(2)} queued — will apply when the engine reconnects.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
