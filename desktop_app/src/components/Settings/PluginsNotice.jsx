import React from 'react';

export default function PluginsNotice() {
  return (
    <div style={{
      background: 'rgba(var(--accent-rgb), 0.1)',
      border: '1px solid rgba(var(--accent-rgb), 0.2)',
      borderRadius: '8px',
      padding: '12px 16px',
      marginBottom: '20px',
      color: 'var(--accent)',
      fontSize: '13px'
    }}>
      <strong>Notice:</strong> Plugin tools are displayed here to reflect what is declared in the plugin manifest. However, they are not yet callable by the agent.
    </div>
  );
}
