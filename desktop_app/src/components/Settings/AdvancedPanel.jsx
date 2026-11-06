import React from 'react';
import { Network, Scissors } from 'lucide-react';
import { defaultSnipHotkey } from '../../snipHotkey';
import LoadStateRow from './LoadStateRow';
import ProjectConfigPanel from './ProjectConfigPanel';

// Wave 5 (Annex C row 7): the Advanced tab, extracted from
// SettingsPanel.jsx — reset, backup/restore, routing &
// fallbacks, the project config layer and the snip hotkey. The
// shell keeps the state, the snip-hotkey apply flow and the
// sends; this panel is the controlled rendering.
export default function AdvancedPanel({ configAnswered, isConnected, resetArmed, resetDone, resetError, onResetClick, includeSecrets, onIncludeSecretsChange, backupStatus, onCreateBackup, restorePath, onRestorePathChange, restoreStatus, onRestoreBackup, versionCheck, onCheckUpdates, onExportDiagnostics, configUpdateError, disableFallbacks, onDisableFallbacksChange, fallbackChain, onFallbackChainChange, onFallbackChainCommit, projectConfigReady, projectLayer, onProjectLayerSave, snipHotkey, onSnipHotkeyChange, onSnipHotkeyBlur, onSnipHotkeyReset, snipHotkeyError, pendingSnipValue }) {
  return (
    <div style={{ color: '#f0f0f5', display: 'flex', flexDirection: 'column', gap: '24px' }}>
      <div>
        <h2 style={{ fontSize: '24px', marginBottom: '8px', fontWeight: 500 }}>Advanced Settings</h2>
        <p style={{ color: '#9090a0', fontSize: '14px' }}>System configurations and developer options.</p>
      </div>

      {/* RUX-C-01: the routing/fallback controls and the
          project layer below are config-driven — until the
          first config answer lands, the section says its
          saved values are still loading. */}
      {!configAnswered && (
        <LoadStateRow
          loadState="loading"
          isConnected={isConnected}
          testId="advanced-config-load-state"
          variant="plain"
          loadingText="Loading your saved settings…"
          offlineText="Waiting for the engine — your saved settings will appear here once it connects."
        />
      )}

      <div style={{ background: 'rgba(239,68,68,0.05)', border: '1px solid rgba(239,68,68,0.25)', borderRadius: '12px', padding: '24px' }}>
        <h3 style={{ fontSize: '16px', fontWeight: 500, marginBottom: '8px', color: '#fff' }}>Reset all settings</h3>
        <p style={{ color: '#9090a0', fontSize: '13px', margin: '0 0 16px 0' }}>
          Restores every engine setting to its default and saves immediately. Your API keys (held in the
          encrypted secret store), your files, and your memory are NOT touched.
        </p>
        {resetError && (
          <div style={{ color: '#f87171', fontSize: '13px', marginBottom: '12px' }}>Reset failed: {resetError}</div>
        )}
        {resetDone ? (
          <div style={{ color: '#10b981', fontSize: '13px' }}>Settings were reset to defaults.</div>
        ) : (
          <button
            onClick={onResetClick}
            style={{ background: resetArmed ? '#ef4444' : 'rgba(239,68,68,0.15)', color: resetArmed ? '#fff' : '#f87171', border: '1px solid rgba(239,68,68,0.4)', padding: '9px 18px', borderRadius: '10px', cursor: 'pointer', fontWeight: 500 }}
          >
            {resetArmed ? 'Click again to confirm reset' : 'Reset all settings'}
          </button>
        )}
      </div>
            
      <div style={{ background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.05)', borderRadius: '12px', padding: '24px' }}>
        <h3 style={{ fontSize: '16px', fontWeight: 500, marginBottom: '8px', color: '#fff' }}>Backup & restore</h3>
        <p style={{ color: '#9090a0', fontSize: '13px', margin: '0 0 16px 0' }}>
          A backup snapshots your settings, memory, sessions and skills into one zip in your workspace's
          AetherBackups folder. API keys stay out unless you include the (encrypted) secret store.
          Restoring replaces those files on disk — restart Aether afterwards for it to take effect.
        </p>
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap', marginBottom: '12px' }}>
          <button
            onClick={onCreateBackup}
            style={{ background: 'rgba(245,158,11,0.15)', color: '#f59e0b', border: '1px solid rgba(245,158,11,0.4)', padding: '9px 18px', borderRadius: '10px', cursor: 'pointer', fontWeight: 500 }}
          >
            Create backup
          </button>
          <label style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '13px', color: '#c0c0cc', cursor: 'pointer' }}>
            <input type="checkbox" checked={includeSecrets} onChange={(e) => onIncludeSecretsChange(e.target.checked)} />
            Include encrypted secret store
          </label>
        </div>
        {backupStatus && <div style={{ fontSize: '13px', color: '#a0a0b0', marginBottom: '16px', wordBreak: 'break-all' }}>{backupStatus}</div>}
        <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
          <input
            value={restorePath}
            onChange={(e) => onRestorePathChange(e.target.value)}
            placeholder="Path to a backup zip…"
            aria-label="Backup zip path"
            style={{ flex: 1, minWidth: '220px', background: 'rgba(255,255,255,0.04)', border: '1px solid rgba(255,255,255,0.1)', borderRadius: '8px', padding: '9px 12px', color: '#f0f0f5', fontSize: '13px' }}
          />
          <button
            onClick={onRestoreBackup}
            style={{ background: 'rgba(255,255,255,0.06)', color: '#f0f0f5', border: '1px solid rgba(255,255,255,0.15)', padding: '9px 18px', borderRadius: '10px', cursor: 'pointer', fontWeight: 500 }}
          >
            Restore
          </button>
        </div>
        {restoreStatus && <div style={{ fontSize: '13px', color: '#a0a0b0', marginTop: '12px' }}>{restoreStatus}</div>}
        <div style={{ borderTop: '1px solid rgba(255,255,255,0.06)', marginTop: '20px', paddingTop: '16px', display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap' }}>
          <button
            onClick={onCheckUpdates}
            style={{ background: 'rgba(255,255,255,0.06)', color: '#f0f0f5', border: '1px solid rgba(255,255,255,0.15)', padding: '9px 18px', borderRadius: '10px', cursor: 'pointer', fontWeight: 500 }}
          >
            Check for updates
          </button>
          <button
            onClick={onExportDiagnostics}
            style={{ background: 'rgba(255,255,255,0.06)', color: '#f0f0f5', border: '1px solid rgba(255,255,255,0.15)', padding: '9px 18px', borderRadius: '10px', cursor: 'pointer', fontWeight: 500 }}
          >
            Export diagnostics
          </button>
          <button
            onClick={() => { if (window.electronAPI && window.electronAPI.openLogsFolder) window.electronAPI.openLogsFolder(); }}
            style={{ background: 'rgba(255,255,255,0.06)', color: '#f0f0f5', border: '1px solid rgba(255,255,255,0.15)', padding: '9px 18px', borderRadius: '10px', cursor: 'pointer', fontWeight: 500 }}
          >
            Open logs folder
          </button>
          {versionCheck && <span style={{ fontSize: '13px', color: '#a0a0b0' }}>{versionCheck}</span>}
        </div>
      </div>

      <div style={{ background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.05)', borderRadius: '12px', padding: '24px' }}>
        <h3 style={{ fontSize: '16px', fontWeight: 500, marginBottom: '16px', color: '#fff', display: 'flex', alignItems: 'center', gap: '8px' }}>
          <Network size={18} style={{ color: '#a78bfa' }} /> Model Routing & Fallbacks
        </h3>

        {/* D2-06: this tab's own config writes report their
            refusal HERE — the error used to be passed only to
            panels on other tabs. */}
        {configUpdateError && (
          <div style={{ color: '#f87171', fontSize: '13px', marginBottom: '16px' }}>{configUpdateError}</div>
        )}

        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '24px' }}>
          <div>
            <div style={{ fontWeight: 500, fontSize: '14px', marginBottom: '4px' }}>Disable Fallback System</div>
            <div style={{ fontSize: '12px', color: '#9090a0' }}>Prevent the system from automatically trying alternative models when the current one fails.</div>
          </div>
          <label style={{ display: 'flex', alignItems: 'center', cursor: 'pointer' }}>
            <input
              type="checkbox"
              checked={disableFallbacks}
              onChange={(e) => onDisableFallbacksChange(e.target.checked)}
              style={{ accentColor: '#7c3aed', width: '18px', height: '18px' }}
            />
          </label>
        </div>

        {!disableFallbacks && (
          <div>
            <div style={{ fontWeight: 500, fontSize: '14px', marginBottom: '8px' }}>Fallback Provider Chain (Optional)</div>
            <div style={{ fontSize: '12px', color: '#9090a0', marginBottom: '12px' }}>
              Comma-separated list of providers to try in order (e.g., <code>openrouter, groq, google_gemini</code>). Leave empty for automatic selection.
            </div>
            <input
              type="text"
              value={fallbackChain}
              onChange={(e) => onFallbackChainChange(e.target.value)}
              onBlur={onFallbackChainCommit}
              placeholder="E.g. openrouter, groq, anthropic"
              style={{
                width: '100%',
                background: 'rgba(0,0,0,0.2)',
                border: '1px solid rgba(255,255,255,0.1)',
                borderRadius: '6px',
                padding: '10px 12px',
                color: '#fff',
                fontSize: '13px'
              }}
            />
          </div>
        )}
      </div>

      <ProjectConfigPanel
        configReady={projectConfigReady}
        workspaceRoot={projectLayer.workspaceRoot}
        content={projectLayer.content}
        exists={projectLayer.exists}
        forbiddenKeys={projectLayer.forbiddenKeys}
        warning={projectLayer.warning}
        status={projectLayer.status}
        onSave={onProjectLayerSave}
      />

      <div style={{
        background: 'rgba(255,255,255,0.02)',
        border: '1px solid rgba(255,255,255,0.06)',
        borderRadius: '8px',
        padding: '16px',
        marginTop: '16px'
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '8px' }}>
          <Scissors size={16} color="#7c3aed" />
          <div style={{ fontWeight: 500, fontSize: '14px' }}>Screen Snip Hotkey</div>
        </div>
        <div style={{ fontSize: '12px', color: '#9090a0', marginBottom: '12px' }}>
          Accelerator to trigger screen region snipping (e.g., <code>Ctrl+Alt+S</code>). Applies as soon as you leave the field — no restart needed.
        </div>
        <div data-testid="snip-hotkey-note" style={{ fontSize: '12px', color: '#9090a0', marginBottom: '12px' }}>
          This shortcut is global: it works system-wide while Aether is running, so it can override the same keys in other apps.
        </div>
        <input
          type="text"
          value={snipHotkey}
          data-testid="snip-hotkey-input"
          onChange={(e) => onSnipHotkeyChange(e.target.value)}
          onBlur={onSnipHotkeyBlur}
          placeholder={defaultSnipHotkey()}
          style={{
            width: '100%',
            background: 'rgba(0,0,0,0.2)',
            border: '1px solid rgba(255,255,255,0.1)',
            borderRadius: '6px',
            padding: '10px 12px',
            color: '#fff',
            fontSize: '13px'
          }}
        />
        {snipHotkeyError && (
          <div data-testid="snip-hotkey-error" style={{ fontSize: '12px', color: '#f87171', marginTop: '8px' }}>
            {snipHotkeyError}
          </div>
        )}
        {pendingSnipValue !== undefined && (
          <div data-testid="snip-hotkey-queued" style={{ fontSize: '12px', color: '#fbbf24', marginTop: '8px' }}>
            Snip hotkey change to {pendingSnipValue} queued — will be saved when the engine reconnects.
          </div>
        )}
        <button
          type="button"
          data-testid="snip-hotkey-reset"
          onClick={onSnipHotkeyReset}
          style={{
            marginTop: '12px',
            background: 'rgba(255,255,255,0.06)',
            border: '1px solid rgba(255,255,255,0.1)',
            borderRadius: '6px',
            padding: '8px 12px',
            color: '#fff',
            fontSize: '12px',
            cursor: 'pointer'
          }}
        >
          Reset to default ({defaultSnipHotkey()})
        </button>
      </div>
    </div>
  );
}
