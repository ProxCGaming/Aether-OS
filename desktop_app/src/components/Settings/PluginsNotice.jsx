import React from 'react';

export default function PluginsNotice() {
  return (
    <div style={{
      background: 'rgba(245, 158, 11, 0.1)',
      border: '1px solid rgba(245, 158, 11, 0.2)',
      borderRadius: '8px',
      padding: '12px 16px',
      marginBottom: '20px',
      color: '#fbbf24',
      fontSize: '13px'
    }}>
      <strong>Notice:</strong> Plugin tools are displayed here to reflect what is declared in the plugin manifest. However, they are not yet callable by the agent.
    </div>
  );
}
