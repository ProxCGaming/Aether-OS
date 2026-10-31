import React, { useState } from 'react';
import { RefreshCw, Globe, AlertCircle } from 'lucide-react';

// Epic 8, E8-2.2: sandboxed project preview for web artifacts.
// The sandbox token set is pinned by the PRD — exactly
// "allow-scripts allow-same-origin", nothing more permissive: framed
// pages get no access to Electron APIs, the parent window, top
// navigation, forms, or popups.
const PREVIEW_SANDBOX = 'allow-scripts allow-same-origin';
const CANDIDATE_PORTS = [5173, 3000, 8000, 8080];

const normalizeUrl = (raw) => {
  const trimmed = (raw || '').trim();
  if (!trimmed) return null;
  if (/^https?:\/\//i.test(trimmed)) return trimmed;
  return `http://${trimmed}`;
};

export default function ProjectPreview({
  artifact = null,
  content = null,
  devServer = null,
  onDetect = null,
  candidatePorts = CANDIDATE_PORTS,
}) {
  const [selectedPort, setSelectedPort] = useState(null);
  const [customUrl, setCustomUrl] = useState(null);
  const [urlDraft, setUrlDraft] = useState(null);
  const [reloadKey, setReloadKey] = useState(0);

  const effectivePort = selectedPort ?? devServer?.port ?? null;
  const serverUrl = customUrl
    ?? (effectivePort ? `http://localhost:${effectivePort}` : null)
    ?? (devServer?.running ? devServer.url : null);

  // What the frame shows: a live server when one is known/chosen,
  // otherwise the artifact's own HTML as a static preview, otherwise
  // an actionable fallback (never a dead frame).
  const mode = serverUrl ? 'server' : (content ? 'static' : 'empty');
  const urlBarValue = urlDraft ?? serverUrl ?? artifact?.file_path ?? '';

  const handleGo = (event) => {
    event.preventDefault();
    const next = normalizeUrl(urlDraft ?? urlBarValue);
    if (next) {
      setCustomUrl(next);
      setUrlDraft(null);
    }
  };

  const handlePortChange = (event) => {
    const value = event.target.value;
    setSelectedPort(value === '' ? null : Number(value));
    setCustomUrl(null);
    setUrlDraft(null);
  };

  const handleReload = () => {
    setReloadKey((key) => key + 1);
    if (onDetect) onDetect();
  };

  const toolbarStyle = {
    display: 'flex',
    alignItems: 'center',
    gap: '6px',
    padding: '8px',
    background: 'rgba(255, 255, 255, 0.04)',
    border: '1px solid rgba(255, 255, 255, 0.08)',
    borderRadius: '8px',
  };
  const controlStyle = {
    background: 'rgba(255, 255, 255, 0.08)',
    border: '1px solid rgba(255, 255, 255, 0.1)',
    borderRadius: '6px',
    color: '#fff',
    padding: '4px 8px',
    fontSize: '11px',
  };

  return (
    <div
      data-testid="project-preview"
      style={{ display: 'flex', flexDirection: 'column', height: '100%', gap: '8px' }}
    >
      <div style={toolbarStyle}>
        <Globe size={13} color="#94a3b8" style={{ flexShrink: 0 }} />
        <form
          onSubmit={handleGo}
          style={{ display: 'flex', alignItems: 'center', gap: '6px', flex: 1, minWidth: 0 }}
        >
          <input
            data-testid="preview-url-input"
            type="text"
            value={urlBarValue}
            onChange={(event) => setUrlDraft(event.target.value)}
            placeholder="http://localhost:5173"
            spellCheck={false}
            style={{
              ...controlStyle,
              flex: 1,
              minWidth: 0,
              fontFamily: 'monospace',
              background: 'rgba(0, 0, 0, 0.25)',
            }}
          />
          <button type="submit" data-testid="preview-go-btn" style={{ ...controlStyle, cursor: 'pointer' }}>
            Go
          </button>
        </form>
        <select
          data-testid="preview-port-select"
          value={selectedPort === null ? '' : String(selectedPort)}
          onChange={handlePortChange}
          title="Dev server port"
          style={{ ...controlStyle, cursor: 'pointer' }}
        >
          <option value="">Auto port</option>
          {candidatePorts.map((port) => (
            <option key={port} value={String(port)}>
              :{port}
            </option>
          ))}
        </select>
        <button
          type="button"
          data-testid="preview-reload-btn"
          onClick={handleReload}
          title="Reload preview"
          style={{ ...controlStyle, cursor: 'pointer', display: 'flex', alignItems: 'center' }}
        >
          <RefreshCw size={12} />
        </button>
        {devServer?.framework && (
          <span
            data-testid="preview-framework"
            style={{
              fontSize: '10px',
              fontWeight: 600,
              padding: '2px 7px',
              borderRadius: '999px',
              background: 'rgba(52, 211, 153, 0.15)',
              border: '1px solid rgba(52, 211, 153, 0.35)',
              color: '#34d399',
              whiteSpace: 'nowrap',
            }}
          >
            {devServer.framework}
          </span>
        )}
      </div>

      <div style={{ flex: 1, overflow: 'hidden', borderRadius: '8px', background: '#fff' }}>
        {mode === 'server' && (
          <iframe
            key={reloadKey}
            data-testid="project-preview-iframe"
            title="Project preview"
            sandbox={PREVIEW_SANDBOX}
            src={serverUrl}
            style={{ width: '100%', height: '100%', border: 'none', display: 'block' }}
          />
        )}
        {mode === 'static' && (
          <iframe
            key={reloadKey}
            data-testid="project-preview-iframe"
            title="Project preview"
            sandbox={PREVIEW_SANDBOX}
            srcDoc={content}
            style={{ width: '100%', height: '100%', border: 'none', display: 'block' }}
          />
        )}
        {mode === 'empty' && (
          <div
            data-testid="preview-no-server"
            style={{
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
              justifyContent: 'center',
              height: '100%',
              padding: '24px',
              textAlign: 'center',
              background: 'rgba(245, 158, 11, 0.07)',
              color: '#fcd34d',
              gap: '10px',
            }}
          >
            <AlertCircle size={28} color="#f59e0b" />
            <div style={{ fontWeight: 600, fontSize: '13px' }}>No dev server detected</div>
            <div style={{ fontSize: '12px', color: '#cbd5e1', maxWidth: '300px' }}>
              Nothing answered on ports {candidatePorts.join(', ')}. Start the
              project&apos;s dev server and reload, or pick a port above to load it directly.
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
