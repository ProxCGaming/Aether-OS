import React from 'react';
import { Sliders } from 'lucide-react';
import LoadStateRow from './LoadStateRow';

// Wave 5 (Annex C row 7): the Voice Tuning tab, extracted from
// SettingsPanel.jsx — the shell keeps the slider values, the
// commit-on-release handlers (D2-08) and the config plumbing;
// this panel is the controlled rendering.
export default function VoicePanel({ configAnswered, configLoadError, isConnected, onRetryConfigLoad, vadThreshold, setVadThreshold, commitVadThreshold, pendingVadValue, silenceDurationMs, setSilenceDurationMs, commitSilenceDuration, pendingSilenceValue, smartTurnSensitivity, setSmartTurnSensitivity, commitSmartTurnSensitivity, pendingSmartTurnValue }) {
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
