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
  // RUX-C-11: until the PROJECT_CONFIG_GET reply lands, the panel
  // showed an empty editor and "No active workspace." — a pre-answer
  // state dressed as a fact about the user's setup. While not
  // ready the editor and save are disabled and the root line says
  // the layer is still loading. Standalone callers that never pass
  // it keep the old always-ready behaviour.
  configReady = true,
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
        background: 'var(--bg-raised)',
        border: '1px solid var(--border-default)',
        borderRadius: '12px',
        padding: '24px',
      }}
    >
      <h3 style={{ fontSize: '16px', fontWeight: 500, marginBottom: '8px', color: 'var(--text-primary)', display: 'flex', alignItems: 'center', gap: '8px' }}>
        <Layers size={18} style={{ color: 'var(--accent)' }} /> Project config layer
      </h3>
      <p style={{ color: 'var(--text-secondary)', fontSize: '13px', margin: '0 0 12px 0' }}>
        The <code>.aether/config.json</code> file in the active workspace is merged over your
        global settings for tasks in that workspace (objects merge key by key; any other
        value replaces). This editor shows the layer itself — not the merged result.
      </p>
      <div data-testid="project-config-root" style={{ fontSize: '13px', color: 'var(--text-muted)', marginBottom: '12px', fontFamily: 'var(--font-mono)' }}>
        {!configReady
          ? 'Loading the project layer…'
          : workspaceRoot ? `Workspace: ${workspaceRoot}` : 'No active workspace.'}
      </div>
      {warning && (
        <div
          data-testid="project-config-warning"
          style={{ background: 'rgba(var(--accent-rgb),0.08)', border: '1px solid rgba(var(--accent-rgb),0.3)', borderRadius: '10px', padding: '10px 14px', fontSize: '13px', color: 'var(--accent)', marginBottom: '12px' }}
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
        disabled={!workspaceRoot || !configReady}
        aria-label="Project config layer JSON"
        style={{
          width: '100%',
          background: 'var(--bg-base)',
          border: '1px solid var(--border-default)',
          borderRadius: '6px',
          padding: '10px 12px',
          color: 'var(--text-primary)',
          fontSize: '13px',
          fontFamily: 'var(--font-mono)',
          resize: 'vertical',
          boxSizing: 'border-box',
          marginBottom: '12px',
        }}
      />
      {forbiddenKeys && forbiddenKeys.length > 0 && (
        <div data-testid="project-config-forbidden" style={{ fontSize: '12px', color: 'var(--text-secondary)', marginBottom: '12px' }}>
          Global-only keys are ignored in this file (a project must not loosen safety,
          routing or spend): {forbiddenKeys.join(', ')}.
        </div>
      )}
      <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
        <button
          type="button"
          data-testid="project-config-save"
          onClick={() => onSave && onSave(content)}
          disabled={!workspaceRoot || !configReady}
          style={{
            background: 'rgba(var(--accent-rgb),0.15)',
            color: workspaceRoot && configReady ? 'var(--accent)' : 'var(--text-muted)',
            border: '1px solid rgba(var(--accent-rgb),0.4)',
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
          <span data-testid="project-config-status" style={{ fontSize: '13px', color: 'var(--success)' }}>
            {status}
          </span>
        )}
      </div>
    </div>
  );
}
