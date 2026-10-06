import React from 'react';

export default function MCPNotice() {
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
      <strong>Notice:</strong> MCP Tools are currently in display-only mode. They are registered and visible to the model, but execution is not yet implemented.
    </div>
  );
}
