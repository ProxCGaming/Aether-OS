import React from 'react';
import LogsPanel from './LogsPanel';
import MachineCapabilityCard from './MachineCapabilityCard';
import CapabilityCheckCard from './CapabilityCheckCard';
import FeatureStateCard from './FeatureStateCard';

// Wave 5 (Annex C row 7): the Capabilities tab, extracted from
// SettingsPanel.jsx — the diagnostics run's state, no-answer
// timer and sends live in the shell (its message handler
// resolves the run); this panel renders the status card and the
// Logs panel and fires the run/export through the handlers it
// is given.
export default function CapabilitiesPanel({ capabilityStatus, capabilityHistory, isDiagnosticsRunning, diagnosticsQueued, diagnosticsError, onRunDiagnostics, isConnected, sendSettingsAction, onExportBundle, engineConfig = null }) {
  return (
    <div style={{ color: 'var(--text-primary)' }}>
      <h2 style={{ fontSize: '24px', marginBottom: '8px', fontWeight: 500 }}>Engine Capabilities</h2>
      <p style={{ color: 'var(--text-muted)', fontSize: '14px', marginBottom: '24px' }}>Monitor the health and capabilities of the Aether execution environment.</p>
            
      <div style={{ background: 'var(--bg-raised)', border: '1px solid var(--border-default)', borderRadius: 'var(--radius-lg)', padding: '24px' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '24px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
            <div style={{ width: '12px', height: '12px', borderRadius: '50%', background: capabilityStatus && capabilityStatus.failed_count === 0 ? 'var(--success)' : (capabilityStatus && capabilityStatus.failed_count > 0 ? 'var(--danger)' : 'var(--text-muted)'), boxShadow: capabilityStatus && capabilityStatus.failed_count === 0 ? '0 0 10px rgba(76, 195, 138,0.5)' : 'none' }}></div>
            <h3 style={{ fontSize: '16px' }}>{capabilityStatus ? (capabilityStatus.failed_count === 0 ? 'System Healthy' : `${capabilityStatus.failed_count} Issues Detected`) : 'Status Unknown'}</h3>
          </div>
          <button
            onClick={onRunDiagnostics}
            disabled={isDiagnosticsRunning || diagnosticsQueued}
            style={{ 
              background: isDiagnosticsRunning ? 'rgba(var(--accent-rgb), 0.2)' : 'transparent', 
              color: isDiagnosticsRunning ? 'var(--accent)' : 'var(--accent)', 
              border: '1px solid rgba(var(--accent-rgb), 0.5)', 
              padding: '8px 16px', 
              borderRadius: '8px', 
              cursor: isDiagnosticsRunning ? 'wait' : 'pointer',
              transition: 'all 0.2s ease',
              display: 'flex',
              alignItems: 'center',
              gap: '8px'
            }}>
            {isDiagnosticsRunning ? (
              <>
                <svg style={{ width: '16px', height: '16px', animation: 'spin 1s linear infinite' }} xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
                  <circle style={{ opacity: 0.25 }} cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
                  <path style={{ opacity: 0.75 }} fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                </svg>
                Testing...
              </>
            ) : diagnosticsQueued ? (
              'Run Queued'
            ) : (
              'Run Diagnostics Now'
            )}
          </button>
        </div>

        {diagnosticsQueued && (
          <div data-testid="diagnostics-run-queued" style={{ fontSize: '13px', color: 'var(--accent)', marginBottom: '16px' }}>
            Diagnostics run queued — will run when the engine reconnects.
          </div>
        )}

        {diagnosticsError && (
          <div data-testid="diagnostics-run-error" style={{ fontSize: '13px', color: 'var(--danger)', marginBottom: '16px' }}>
            {diagnosticsError}
          </div>
        )}
              
        <div style={{ background: 'var(--bg-base)', border: '1px solid var(--border-default)', borderRadius: '12px', padding: '16px', fontFamily: 'var(--font-mono)', fontSize: '13px', color: 'var(--success)', height: '200px', overflowY: 'auto' }} className="chat-scroll">
          {capabilityHistory.length === 0 ? (
            <div style={{ color: 'var(--text-secondary)', marginBottom: '8px' }}>[System] Ready to run diagnostics. Click the button above.</div>
          ) : (
            capabilityHistory.map((log, idx) => (
              <div key={idx} style={{ color: log.status === 'OK' || log.status === 'RUNNING' ? 'var(--success)' : (log.status === 'WARN' ? 'var(--accent)' : 'var(--danger)') }}>
                [{new Date(log.timestamp * 1000).toLocaleTimeString()}] {log.target_name}: {log.status} {log.details ? `(${log.details})` : ''}
              </div>
            ))
          )}
        </div>
      </div>

      {/* Master build S10: the "This machine" card — the
          hardware probe + capability matrix. Self-contained
          (its own socket request); the diagnostics card above
          is about MODEL capability checks, a different thing,
          and the card says so. */}
      <MachineCapabilityCard />

      {/* Master build S21 (R17 wire): the capability-check
          scheduler card — scheduled and on-demand MODEL checks
          (the diagnostics card above is environment health) —
          and the per-feature state+reason card, which reads the
          same S10 report plus the applied config. */}
      <CapabilityCheckCard />
      <FeatureStateCard config={engineConfig} />

      {/* Unified logging (5 Nov 2026): the Logs & Diagnostics
          cards (status / viewer / controls) live below the
          diagnostics-run card, which is unchanged. */}
      <LogsPanel
        isConnected={isConnected}
        sendSettingsAction={sendSettingsAction}
        onExportBundle={onExportBundle}
      />
    </div>
  );
}
