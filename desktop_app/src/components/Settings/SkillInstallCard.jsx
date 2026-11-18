import React from 'react';

// Wave 5 (D4-6): the skill install card — the trust surface for a
// registry install, rendered in the Skills panel from the prepare
// reply. Prepare fetched and hashed the skill WITHOUT writing
// anything; this card shows the pin the install will be checked
// against (the install refuses if the source changed after this
// review), where the pin will be recorded, and the landing state:
// a fresh install is DISABLED, and enabling it is a separate act
// in the list below — never a side effect of installing.
export default function SkillInstallCard({ pin, onConfirm, onCancel }) {
  if (!pin) return null;
  const sourceLabel = pin.source_type === 'github' ? 'GitHub' : 'Local directory';
  return (
    <div
      data-testid="skill-install-card"
      style={{
        background: 'var(--bg-raised)',
        border: '1px solid rgba(var(--accent-rgb), 0.35)',
        borderRadius: '12px',
        padding: '16px',
        marginBottom: '24px',
      }}
    >
      <h3 style={{ fontSize: '15px', fontWeight: 600, margin: '0 0 8px 0' }}>
        Install skill “{pin.name}”?
      </h3>
      {pin.description && (
        <p style={{ fontSize: '13px', color: 'var(--text-secondary)', margin: '0 0 12px 0' }}>
          {pin.description}
        </p>
      )}
      <div style={{ display: 'flex', flexDirection: 'column', gap: '6px', fontSize: '13px', color: 'var(--text-secondary)', marginBottom: '12px' }}>
        <div>Source: {sourceLabel} — {pin.source}</div>
        {pin.version && <div>Version: {pin.version}</div>}
        <div>
          Pin (SHA-256):{' '}
          <span style={{ fontFamily: 'var(--font-mono)', fontSize: '12px', color: 'var(--text-primary)' }}>
            {pin.hash}
          </span>
        </div>
      </div>
      <p style={{ fontSize: '12px', color: 'var(--text-muted)', margin: '0 0 4px 0', lineHeight: 1.5 }}>
        The files are verified against this pin before anything is written, and the pin is
        recorded with the install — if the skill's files are edited later, the list will
        show it as modified against the pin.
      </p>
      <p style={{ fontSize: '12px', color: 'var(--text-muted)', margin: '0 0 16px 0', lineHeight: 1.5 }}>
        It will be installed disabled. Enabling it is a separate step, from the list below.
      </p>
      <div style={{ display: 'flex', gap: '12px', justifyContent: 'flex-end' }}>
        <button
          onClick={onCancel}
          className="aether-solid-btn"
          style={{ background: 'var(--bg-hover)', border: '1px solid var(--border-strong)', padding: '8px 16px', borderRadius: '8px', cursor: 'pointer', fontSize: '13px', fontWeight: 500, color: 'var(--text-primary)' }}
        >
          Cancel
        </button>
        <button
          onClick={onConfirm}
          className="aether-solid-btn"
          style={{ background: 'var(--accent)', border: '1px solid var(--accent)', color: 'var(--accent-on)', padding: '8px 16px', borderRadius: '8px', cursor: 'pointer', fontSize: '13px', fontWeight: 500 }}
        >
          Install
        </button>
      </div>
    </div>
  );
}
