import React from 'react';

export default function MCPNotice() {
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
      <strong>Notice:</strong> MCP tools run only when their server is connected. A disconnected server&apos;s tools are not visible to the model.
    </div>
  );
}
