import React, { useState, useEffect } from 'react';
import { Layers } from 'lucide-react';

// E6-F05 (Epic 1 layered-config handoff): an editor for the project
// layer of the config stack — `.aether/config.json` in the active
// workspace, which config_layers merges over the global settings
// for tasks in that workspace. The editor shows the layer itself
// (its raw file text), never the merged result: what you see is
// exactly the overlay the engine consumes. Keys the project layer
// is forbidden from setting (safety, routing, spend — the engine
// strips them before merging) are listed so a file that tries is
// not a silent no-op.
//
// The panel is controlled: SettingsPanel owns the socket, fetches
// the layer when Advanced opens, applies PROJECT_CONFIG responses,
// and sends saves via onSave.

export default function ProjectConfigPanel({
  workspaceRoot,
  content: appliedContent,
  exists,
  forbiddenKeys,
  warning,
  status,
  onSave,
}) {
  const [content, setContent] = useState(appliedContent || '');

  // Re-sync when SettingsPanel applies a fresh layer (the initial
  // fetch, or the canonical form of this panel's own save).
  useEffect(() => {
    setContent(appliedContent || '');
  }, [appliedContent]);

  return (
    <div
      style={{
        background: 'rgba(255,255,255,0.02)',
        border: '1px solid rgba(255,255,255,0.05)',
        borderRadius: '12px',
        padding: '24px',
      }}
    >
      <h3 style={{ fontSize: '16px', fontWeight: 500, marginBottom: '8px', color: '#fff', display: 'flex', alignItems: 'center', gap: '8px' }}>
        <Layers size={18} style={{ color: '#a78bfa' }} /> Project config layer
      </h3>
      <p style={{ color: '#9090a0', fontSize: '13px', margin: '0 0 12px 0' }}>
        The <code>.aether/config.json</code> file in the active workspace is merged over your
        global settings for tasks in that workspace (objects merge key by key; any other
        value replaces). This editor shows the layer itself — not the merged result.
      </p>
      <div data-testid="project-config-root" style={{ fontSize: '13px', color: '#c0c0cc', marginBottom: '12px', fontFamily: 'monospace' }}>
        {workspaceRoot ? `Workspace: ${workspaceRoot}` : 'No active workspace.'}
      </div>
      {warning && (
        <div
          data-testid="project-config-warning"
          style={{ background: 'rgba(245,158,11,0.08)', border: '1px solid rgba(245,158,11,0.3)', borderRadius: '10px', padding: '10px 14px', fontSize: '13px', color: '#fcd34d', marginBottom: '12px' }}
        >
          {warning}
        </div>
      )}
      <textarea
        data-testid="project-config-content"
        value={content}
        onChange={(e) => setContent(e.target.value)}
        placeholder={exists ? '' : '{\n  "reasoning_effort": "high"\n}'}
        rows={10}
        spellCheck={false}
        disabled={!workspaceRoot}
        aria-label="Project config layer JSON"
        style={{
          width: '100%',
          background: 'rgba(0,0,0,0.2)',
          border: '1px solid rgba(255,255,255,0.1)',
          borderRadius: '6px',
          padding: '10px 12px',
          color: '#fff',
          fontSize: '13px',
          fontFamily: 'monospace',
          resize: 'vertical',
          boxSizing: 'border-box',
          marginBottom: '12px',
        }}
      />
      {forbiddenKeys && forbiddenKeys.length > 0 && (
        <div data-testid="project-config-forbidden" style={{ fontSize: '12px', color: '#9090a0', marginBottom: '12px' }}>
          Global-only keys are ignored in this file (a project must not loosen safety,
          routing or spend): {forbiddenKeys.join(', ')}.
        </div>
      )}
      <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
        <button
          type="button"
          data-testid="project-config-save"
          onClick={() => onSave && onSave(content)}
          disabled={!workspaceRoot}
          style={{
            background: 'rgba(124,58,237,0.15)',
            color: workspaceRoot ? '#c4b5fd' : '#6b6b7a',
            border: '1px solid rgba(124,58,237,0.4)',
            padding: '8px 16px',
            borderRadius: '8px',
            cursor: workspaceRoot ? 'pointer' : 'default',
            fontSize: '13px',
            fontWeight: 500,
          }}
        >
          Save project layer
        </button>
        {status && (
          <span data-testid="project-config-status" style={{ fontSize: '13px', color: '#10b981' }}>
            {status}
          </span>
        )}
      </div>
    </div>
  );
}
