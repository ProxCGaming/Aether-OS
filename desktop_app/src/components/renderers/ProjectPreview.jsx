import React, { useState } from 'react';
import { RefreshCw, Globe, AlertCircle } from 'lucide-react';
import { toFileUrl } from './fileUrl';

// Epic 8, E8-2.2: sandboxed project preview for web artifacts.
// Security fix M01 (supersedes the PRD's original sandbox pin):
// previewed content is untrusted and must never reach this window's
// Electron bridge (window.electronAPI -> 'engine-send' -> engine).
//
// Server mode: an Electron <webview> with its own session
// partition instead of an iframe. The guest is a separate
// WebContents with no preload and no shared window tree, so it
// cannot reach the bridge even when its origin coincides with the
// app's own (in dev the shell loads http://localhost:5173, which
// is also candidate preview port #1). The guest keeps its own
// origin, so the previewed app's scripts, storage and cookies
// keep working. Requires webviewTag in main.cjs (see there).
//
// Static mode WITH a file on disk (FUP-2, completes the S3-M2
// consequence): the SAME <webview> guest route as server mode,
// pointed at a file:// URL built from the artifact's real
// file_path. This panel's layout is unchanged — only the frame
// underneath changed. Why: S3-M2 put a strict CSP on the app
// window (script-src 'self', no inline scripts) and a srcDoc
// iframe inherits it, so static artifacts' inline scripts stopped
// running there. The guest session is a separate WebContents NOT
// governed by the app page's CSP (see electron/securityPolicy.cjs),
// so the artifact renders exactly as authored: inline scripts
// run, relative sibling assets (CSS/JS/images next to the HTML)
// resolve against the real file location, and the rendered bytes
// are the file's bytes by construction — no content is piped
// through this component, transformed, sanitised or rewritten on
// this path. Isolation is the guest boundary itself (separate
// WebContents, dedicated partition, no preload), exactly as in
// server mode. The app CSP/sandbox are untouched. Verified with a
// live Electron 44 probe: a file:// page in this guest ran its
// inline script and loaded sibling JS/CSS/image assets while the
// host page carried the production CSP, and the guest had no
// electronAPI.
//
// Static mode WITHOUT a file_path (content-only artifact): there
// is no file to be faithful to, so the pre-FUP-2 route stays — a
// srcDoc iframe whose sandbox is "allow-scripts" WITHOUT
// "allow-same-origin". Scripts in the preview still run, but the
// document gets an opaque origin, so it is cross-origin to this
// app window: reading window.parent.electronAPI throws and top
// navigation is blocked. (With allow-same-origin, a srcDoc frame
// shares the embedder's origin — the original pin — and the
// bridge was reachable; a canary proved it.) Note this fallback
// still inherits the app CSP, so inline scripts in content-only
// artifacts do not run; that is accepted for this path because
// there is no on-disk page whose fidelity could be promised.
const STATIC_PREVIEW_SANDBOX = 'allow-scripts';
const PREVIEW_PARTITION = 'aether-preview';
const CANDIDATE_PORTS = [5173, 3000, 8000, 8080];

// The file:// URL for a file-backed static preview is built by
// ./fileUrl.js (toFileUrl) from the artifact's real file_path.

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
  // FUP-2: a static artifact with a real file on disk loads through
  // the webview guest from that file; only a content-only artifact
  // (no usable file_path) uses the srcDoc iframe.
  const staticFileUrl = mode === 'static' ? toFileUrl(artifact?.file_path) : null;
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
          <webview
            key={reloadKey}
            data-testid="project-preview-iframe"
            title="Project preview"
            src={serverUrl}
            partition={PREVIEW_PARTITION}
            style={{ width: '100%', height: '100%', border: 'none', display: 'block' }}
          />
        )}
        {mode === 'static' && staticFileUrl && (
          <webview
            key={reloadKey}
            data-testid="project-preview-iframe"
            title="Project preview"
            src={staticFileUrl}
            partition={PREVIEW_PARTITION}
            style={{ width: '100%', height: '100%', border: 'none', display: 'block' }}
          />
        )}
        {mode === 'static' && !staticFileUrl && (
          <iframe
            key={reloadKey}
            data-testid="project-preview-iframe"
            title="Project preview"
            sandbox={STATIC_PREVIEW_SANDBOX}
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
