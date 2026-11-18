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
      <strong>Notice:</strong> Plugins add skills and MCP servers — a plugin can&rsquo;t contribute tools directly. Tools arrive only through an MCP server, so a plugin that needs tools bundles one; a manifest that declares tools is refused at install.
    </div>
  );
}
