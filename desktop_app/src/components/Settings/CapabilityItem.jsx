import React from 'react';

// D4-1: the ONE Capability Item shell — the card every capability
// row lives in (Batch 1 pattern: name, source badge, status, with
// the panel's own detail and controls as children/actions).
// Adopted by the Tools, Skills, Plugins and MCP panels so a row
// reads the same way on every surface: header (name + badges on
// the left, the panel's actions on the right), an optional
// description line under the name, then the panel's detail.
//
// `tone="error"` is the invalid / load-error row state: the row is
// shown with its reason, never dropped (FF4-07's rule).
export default function CapabilityItem({
  name,
  badges = null,
  description = null,
  tone = 'default',
  actions = null,
  children = null,
  testId,
}) {
  const isError = tone === 'error';
  return (
    <div
      data-testid={testId}
      data-capability-tone={tone}
      style={{
        background: 'var(--bg-raised)',
        border: isError ? '1px solid rgba(201, 42, 42, 0.3)' : '1px solid var(--border-default)',
        borderRadius: '12px',
        padding: '16px',
      }}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: '16px' }}>
        <div style={{ minWidth: 0 }}>
          <h4 style={{ fontSize: '15px', fontWeight: 500, display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap', margin: 0 }}>
            {name}
            {badges}
          </h4>
          {description && (
            <div style={{ fontSize: '13px', color: 'var(--text-muted)', marginTop: '4px' }}>
              {description}
            </div>
          )}
        </div>
        {actions && (
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexShrink: 0 }}>
            {actions}
          </div>
        )}
      </div>
      {children}
    </div>
  );
}
