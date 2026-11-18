import React, { useEffect, useMemo, useState } from 'react';
import { X, Copy, Check, Download, ExternalLink, FileCode, FileText, GitCommit, Image, Database, AlertCircle, Eye, Loader2 } from 'lucide-react';
import CodeRenderer from './renderers/CodeRenderer';
import MarkdownRenderer from './renderers/MarkdownRenderer';
import DiffRenderer from './renderers/DiffRenderer';
import DataRenderer from './renderers/DataRenderer';
import ImageViewer from './renderers/ImageViewer';
import ProjectPreview from './renderers/ProjectPreview';
import { artifactSavePayload } from './artifactSave';

// Epic 8, E8-2.2: web artifacts (HTML pages) get a Preview tab beside
// the Code tab; everything else stays code-only.
const isWebArtifact = (art) => {
  if (!art) return false;
  if (art.kind === 'report') return false;
  if (art.kind === 'html') return true;
  const name = (art.file_path || art.title || '').toLowerCase();
  return /\.(html|htm)$/.test(name);
};

// Epic 8, E8-2.4: render summary documents with Markdown viewer
export function SummaryDocumentRenderer({ content = '', _title = '' }) {
  return (
    <div
      data-testid="summary-document-renderer"
      style={{ height: '100%', display: 'flex', flexDirection: 'column' }}
    >
      <MarkdownRenderer content={content} />
    </div>
  );
}

const projectDirOf = (filePath) => {
  if (!filePath) return null;
  const dir = filePath.replace(/[/\\][^/\\]*$/, '');
  return dir || null;
};

// D3-17: viewer routing and list icons share ONE extension source.
// The extension comes from the title when the title carries one,
// else from the file path — an artifact titled "notes" whose path
// ends in .md is a Markdown artifact. Before this, routing looked
// at the title only (path-routed artifacts fell through to the
// code viewer) and the icon's image set disagreed with the
// viewer's (bmp/ico routed to the image viewer but iconed as code).
const extOf = (name) => {
  const n = (name || '').toLowerCase();
  const dot = n.lastIndexOf('.');
  return dot >= 0 ? n.slice(dot) : '';
};

const effectiveExt = (art) => extOf(art?.title) || extOf(art?.file_path);

const IMAGE_EXTS = new Set(['.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.bmp', '.ico']);

export default function ArtifactsCanvas({
  isOpen = false,
  onClose = () => {},
  artifacts = [],
  selectedArtifactId = null,
  onSelectArtifact = () => {},
  activeArtifactContent = null,
  isLoading = false,
  devServerInfo = null,
  onDetectDevServer = null
}) {
  const [copied, setCopied] = useState(false);
  // D3-18: a rejected clipboard write is user-visible feedback,
  // not a silent no-op.
  const [copyFailed, setCopyFailed] = useState(false);
  const [viewerTab, setViewerTab] = useState('code');
  const [openError, setOpenError] = useState(null);

  // Group artifacts by task_id and sort tasks newest first
  const taskGroups = useMemo(() => {
    const groups = new Map();

    for (const art of artifacts) {
      const tid = art.task_id || 'unknown_task';
      if (!groups.has(tid)) {
        groups.set(tid, {
          taskId: tid,
          latestTime: art.created_at || 0,
          artifacts: []
        });
      }
      const grp = groups.get(tid);
      grp.artifacts.push(art);
      if ((art.created_at || 0) > grp.latestTime) {
        grp.latestTime = art.created_at || 0;
      }
    }

    // Sort groups newest first
    return Array.from(groups.values()).sort((a, b) => b.latestTime - a.latestTime);
  }, [artifacts]);

  const currentArtifact = activeArtifactContent?.artifact || artifacts.find(a => a.id === selectedArtifactId);
  const currentArtifactId = currentArtifact?.id || null;

  // E8-2.2: switching artifacts always lands back on the Code tab —
  // the preview is an on-demand view, never the default.
  useEffect(() => {
    setViewerTab('code');
    setOpenError(null);
    setCopied(false);
    setCopyFailed(false);
  }, [currentArtifactId]);

  // E8-2.2: opening the Preview tab asks the engine to probe for a
  // dev server in the artifact's project directory. Deps are kept
  // narrow on purpose: detection fires once per tab-open/artifact,
  // not on every artifact-list refresh.
  useEffect(() => {
    if (viewerTab !== 'preview' || !onDetectDevServer || !isWebArtifact(currentArtifact)) return;
    onDetectDevServer({ workspace_root: null, project_path: projectDirOf(currentArtifact.file_path) });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [viewerTab, currentArtifactId]);

  if (!isOpen) return null;

  const handleCopy = () => {
    const textToCopy = activeArtifactContent?.content || activeArtifactContent?.diff || '';
    if (textToCopy && navigator.clipboard) {
      navigator.clipboard.writeText(textToCopy).then(() => {
        setCopyFailed(false);
        setCopied(true);
        setTimeout(() => setCopied(false), 1800);
      }).catch(() => {
        // D3-18: surface the failure on the button itself for the
        // same dwell as the success state.
        setCopied(false);
        setCopyFailed(true);
        setTimeout(() => setCopyFailed(false), 1800);
      });
    }
  };

  const handleSaveCopy = () => {
    // N06: save the artifact's real bytes under its real MIME type.
    // D3-06: artifactSavePayload's contract is bytes-or-null; a
    // null for content that EXISTS (e.g. undecodable base64) is a
    // failure the user must see — the old code returned silently,
    // so the click produced no save and no error.
    let payload = null;
    try {
      payload = artifactSavePayload(currentArtifact, activeArtifactContent);
    } catch {
      payload = null;
    }
    if (!payload) {
      const rawContent = activeArtifactContent?.content || activeArtifactContent?.diff || '';
      if (rawContent) {
        setOpenError("Could not save a copy — this artifact's content could not be decoded.");
      }
      return;
    }
    const blob = new Blob([payload.bytes], { type: payload.mimeType });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = payload.filename;
    a.click();
    URL.revokeObjectURL(url);
  };

  const handleOpenExternally = async () => {
    // N06: hand the file to the OS default application through the
    // shell seam (shell.openPath in the main process). A shell error
    // string is surfaced, never swallowed or rerouted to a picker.
    const filePath = currentArtifact?.file_path;
    if (!filePath || !window.electronAPI?.openPath) return;
    try {
      const err = await window.electronAPI.openPath(filePath);
      setOpenError(err ? String(err) : null);
    } catch (e) {
      setOpenError((e && e.message) || 'Could not open the file externally.');
    }
  };

  const renderViewer = () => {
    if (!currentArtifact) {
      return (
        <div
          data-testid="canvas-empty-state"
          style={{
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
            height: '100%',
            color: 'var(--text-muted)',
            fontSize: '13px',
            gap: '8px'
          }}
        >
          <FileCode size={32} />
          <span>Select an artifact to view</span>
        </div>
      );
    }

    if (activeArtifactContent?.exists === false) {
      return (
        <div
          data-testid="artifact-file-missing"
          style={{
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
            height: '100%',
            padding: '24px',
            textAlign: 'center',
            background: 'rgba(201, 42, 42, 0.08)',
            border: '1px solid rgba(201, 42, 42, 0.25)',
            borderRadius: 'var(--radius-md)',
            color: 'var(--danger)',
            gap: '12px'
          }}
        >
          <AlertCircle size={32} color="var(--danger)" />
          <div style={{ fontWeight: 600, fontSize: '14px' }}>File No Longer Exists</div>
          <div style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>
            {activeArtifactContent?.error || `File ${currentArtifact.file_path} no longer exists on disk.`}
          </div>
          <div style={{ fontSize: '11px', color: 'var(--text-muted)' }}>
            Metadata is preserved, but file contents cannot be loaded.
          </div>
        </div>
      );
    }

    const kind = currentArtifact.kind;
    const content = activeArtifactContent?.content || '';
    const diff = activeArtifactContent?.diff || '';

    // D3-19: the loading prop is honoured — while content is in
    // flight and there is nothing to show yet, the pane says so
    // instead of rendering an empty viewer. App.jsx wires the
    // prop (Wave 5, D1 slice: artifact-load lifecycle in App).
    if (isLoading && !content && !diff) {
      return (
        <div
          data-testid="artifact-content-loading"
          style={{
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
            height: '100%',
            color: 'var(--text-muted)',
            fontSize: '13px',
            gap: '8px'
          }}
        >
          <Loader2 size={28} className="aether-spinner" />
          <span>Loading artifact…</span>
        </div>
      );
    }

    if (kind === 'diff') {
      return <DiffRenderer diff={diff || content} filePath={currentArtifact.file_path} />;
    }

    if (kind === 'report') {
      return <SummaryDocumentRenderer content={content} title={currentArtifact.title} />;
    }

    const ext = effectiveExt(currentArtifact);

    if (ext === '.md' || ext === '.markdown') {
      return <MarkdownRenderer content={content} />;
    }

    if (ext === '.json') {
      return <DataRenderer content={content} format="json" />;
    }

    if (ext === '.csv') {
      return <DataRenderer content={content} format="csv" />;
    }

    if (IMAGE_EXTS.has(ext)) {
      return <ImageViewer src={content} alt={currentArtifact.title} />;
    }

    return <CodeRenderer content={content} fileName={currentArtifact.title} />;
  };

  // E8-2.2: only web artifacts get viewer tabs, and never for a file
  // that has vanished (the missing placeholder owns that state).
  const showViewerTabs = Boolean(
    currentArtifact && isWebArtifact(currentArtifact) && activeArtifactContent?.exists !== false
  );

  const requestDetection = () => {
    if (onDetectDevServer && currentArtifact) {
      onDetectDevServer({ workspace_root: null, project_path: projectDirOf(currentArtifact.file_path) });
    }
  };

  const getArtifactIcon = (art) => {
    if (art.kind === 'diff') return <GitCommit size={14} color="var(--accent)" />;
    if (art.kind === 'report') return <FileText size={14} color="var(--text-secondary)" />;
    // D3-17: same effective extension and same image set as the
    // viewer routing above.
    const ext = effectiveExt(art);
    if (ext === '.md' || ext === '.markdown') return <FileText size={14} color="var(--text-secondary)" />;
    if (ext === '.json' || ext === '.csv') return <Database size={14} color="var(--accent)" />;
    if (IMAGE_EXTS.has(ext)) return <Image size={14} color="var(--text-secondary)" />;
    return <FileCode size={14} color="var(--success)" />;
  };

  // N06 / Grill Q9+Q11: the canvas is a DOCKED right panel in the
  // App layout — an in-flow flex item of the stage whose 480px the
  // chat gives up (the chat's right edge reserves 530px while the
  // canvas is open) — not an absolute overlay floating above it.
  return (
    <div
      data-testid="artifacts-canvas"
      className="aether-content-panel"
      style={{
        position: 'relative',
        width: '480px',
        flexShrink: 0,
        marginLeft: 'auto',
        marginTop: '40px',
        marginRight: '40px',
        marginBottom: '40px',
        zIndex: 40,
        display: 'flex',
        flexDirection: 'column',
        borderRadius: 'var(--radius-lg)',
        overflow: 'hidden'
      }}
    >
      {/* Canvas Top Bar */}
      <div
        style={{
          padding: '12px 16px',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          borderBottom: '1px solid var(--border-default)',
          background: 'var(--bg-raised)'
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <span style={{ fontSize: '14px', fontWeight: 600, color: 'var(--text-primary)' }}>Artifacts</span>
          <span
            data-testid="canvas-total-badge"
            style={{
              fontSize: '11px',
              fontWeight: 600,
              background: 'rgba(var(--accent-rgb), 0.18)',
              color: 'var(--accent)',
              padding: '2px 8px',
              borderRadius: '999px',
              border: '1px solid rgba(var(--accent-rgb), 0.35)'
            }}
          >
            {artifacts.length}
          </span>
        </div>
        <button
          type="button"
          data-testid="canvas-close-btn"
          onClick={onClose}
          title="Close Artifacts Canvas"
          style={{
            background: 'transparent',
            border: 'none',
            color: 'var(--text-muted)',
            cursor: 'pointer',
            padding: '4px',
            borderRadius: '50%',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            transition: 'color 0.2s'
          }}
        >
          <X size={16} />
        </button>
      </div>

      {/* Main split: Task List (top collapsible / scrollable) and Active Viewer (bottom) */}
      <div style={{ display: 'flex', flexDirection: 'column', flex: 1, overflow: 'hidden' }}>
        {/* Task Groups List */}
        <div
          style={{
            maxHeight: '180px',
            overflowY: 'auto',
            borderBottom: '1px solid var(--border-default)',
            padding: '8px 12px',
            display: 'flex',
            flexDirection: 'column',
            gap: '8px',
            flexShrink: 0,
            background: 'var(--bg-base)'
          }}
        >
          {taskGroups.length === 0 ? (
            <div style={{ fontSize: '12px', color: 'var(--text-muted)', padding: '8px 0', textAlign: 'center' }}>
              No artifacts recorded yet.
            </div>
          ) : (
            taskGroups.map((grp) => (
              <div key={grp.taskId} style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                <div
                  data-testid="task-group-header"
                  style={{
                    fontSize: '11px',
                    fontWeight: 600,
                    color: 'var(--text-muted)',
                    textTransform: 'uppercase',
                    letterSpacing: '0.05em',
                    padding: '2px 4px'
                  }}
                >
                  {grp.taskId}
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', gap: '2px' }}>
                  {grp.artifacts.map((art) => {
                    const isSelected = art.id === selectedArtifactId;
                    return (
                      <div
                        key={art.id}
                        data-testid={`artifact-item-${art.id}`}
                        onClick={() => onSelectArtifact(art.id)}
                        style={{
                          display: 'flex',
                          alignItems: 'center',
                          justifyContent: 'space-between',
                          padding: '6px 10px',
                          borderRadius: '6px',
                          cursor: 'pointer',
                          background: isSelected ? 'var(--bg-overlay)' : 'transparent',
                          border: isSelected ? '1px solid var(--accent)' : '1px solid transparent',
                          transition: 'background 150ms'
                        }}
                      >
                        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', minWidth: 0 }}>
                          {getArtifactIcon(art)}
                          <span
                            style={{
                              fontSize: '13px',
                              color: isSelected ? 'var(--text-primary)' : 'var(--text-secondary)',
                              fontWeight: isSelected ? 600 : 400,
                              whiteSpace: 'nowrap',
                              overflow: 'hidden',
                              textOverflow: 'ellipsis'
                            }}
                          >
                            {art.title}
                          </span>
                        </div>
                        <span
                          style={{
                            fontSize: '10px',
                            fontWeight: 600,
                            padding: '1px 6px',
                            borderRadius: '999px',
                            background: art.kind === 'diff'
                              ? 'rgba(var(--accent-rgb), 0.18)'
                              : art.kind === 'report'
                              ? 'var(--bg-hover)'
                              : 'var(--bg-hover)',
                            color: art.kind === 'diff'
                              ? 'var(--accent)'
                              : art.kind === 'report'
                              ? 'var(--text-secondary)'
                              : 'var(--text-muted)',
                            textTransform: 'uppercase'
                          }}
                        >
                          {art.kind}
                        </span>
                      </div>
                    );
                  })}
                </div>
              </div>
            ))
          )}
        </div>

        {/* Viewer Area */}
        <div
          data-testid="artifact-viewer-container"
          style={{
            flex: 1,
            display: 'flex',
            flexDirection: 'column',
            overflow: 'hidden',
            padding: '14px',
            gap: '10px'
          }}
        >
          {currentArtifact && (
            <div
              style={{
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'space-between',
                paddingBottom: '8px',
                borderBottom: '1px solid var(--border-default)'
              }}
            >
              <div style={{ display: 'flex', flexDirection: 'column', minWidth: 0, gap: '2px' }}>
                <div data-testid="artifact-title" style={{ fontSize: '14px', fontWeight: 600, color: 'var(--text-primary)' }}>
                  {currentArtifact.title}
                </div>
                {currentArtifact.file_path && (
                  <div
                    data-testid="artifact-path"
                    style={{
                      fontSize: '11px',
                      color: 'var(--text-muted)',
                      fontFamily: 'var(--font-mono)',
                      whiteSpace: 'nowrap',
                      overflow: 'hidden',
                      textOverflow: 'ellipsis'
                    }}
                  >
                    {currentArtifact.file_path}
                  </div>
                )}
              </div>

              {/* View-Only Actions */}
              <div style={{ display: 'flex', alignItems: 'center', gap: '6px', flexShrink: 0 }}>
                <button
                  type="button"
                  data-testid="artifact-copy-btn"
                  onClick={handleCopy}
                  title="Copy artifact contents"
                  style={{
                    background: 'var(--bg-hover)',
                    border: '1px solid var(--border-default)',
                    borderRadius: 'var(--radius-sm)',
                    color: 'var(--text-primary)',
                    padding: '4px 8px',
                    cursor: 'pointer',
                    display: 'flex',
                    alignItems: 'center',
                    gap: '4px',
                    fontSize: '11px'
                  }}
                >
                  {copied ? <Check size={12} color="var(--success)" /> : <Copy size={12} />}
                  <span>{copied ? 'Copied' : copyFailed ? 'Copy failed' : 'Copy'}</span>
                </button>
                <button
                  type="button"
                  data-testid="artifact-save-btn"
                  onClick={handleSaveCopy}
                  title="Save a copy"
                  style={{
                    background: 'var(--bg-hover)',
                    border: '1px solid var(--border-default)',
                    borderRadius: 'var(--radius-sm)',
                    color: 'var(--text-primary)',
                    padding: '4px 8px',
                    cursor: 'pointer',
                    display: 'flex',
                    alignItems: 'center',
                    gap: '4px',
                    fontSize: '11px'
                  }}
                >
                  <Download size={12} />
                  <span>Save</span>
                </button>
                <button
                  type="button"
                  data-testid="artifact-open-btn"
                  onClick={handleOpenExternally}
                  title="Open externally"
                  style={{
                    background: 'var(--bg-hover)',
                    border: '1px solid var(--border-default)',
                    borderRadius: 'var(--radius-sm)',
                    color: 'var(--text-primary)',
                    padding: '4px 8px',
                    cursor: 'pointer',
                    display: 'flex',
                    alignItems: 'center',
                    gap: '4px',
                    fontSize: '11px'
                  }}
                >
                  <ExternalLink size={12} />
                  <span>Open</span>
                </button>
              </div>
            </div>
          )}

          {openError && (
            <div
              data-testid="artifact-open-error"
              role="alert"
              style={{
                fontSize: '12px',
                color: 'var(--danger)',
                background: 'rgba(201, 42, 42, 0.08)',
                border: '1px solid rgba(201, 42, 42, 0.25)',
                borderRadius: 'var(--radius-sm)',
                padding: '6px 10px',
                flexShrink: 0
              }}
            >
              {openError}
            </div>
          )}

          {/* E8-2.2: Code / Preview tabs — web artifacts only */}
          {showViewerTabs && (
            <div
              data-testid="artifact-viewer-tabs"
              style={{ display: 'flex', alignItems: 'center', gap: '4px', flexShrink: 0 }}
            >
              {[
                { id: 'code', label: 'Code', icon: <FileCode size={12} /> },
                { id: 'preview', label: 'Preview', icon: <Eye size={12} /> },
              ].map((tab) => {
                const isActive = viewerTab === tab.id;
                return (
                  <button
                    key={tab.id}
                    type="button"
                    data-testid={`artifact-tab-${tab.id}`}
                    onClick={() => setViewerTab(tab.id)}
                    style={{
                      background: isActive ? 'var(--bg-overlay)' : 'var(--bg-raised)',
                      border: isActive ? '1px solid var(--accent)' : '1px solid var(--border-default)',
                      borderRadius: 'var(--radius-sm)',
                      color: isActive ? 'var(--accent)' : 'var(--text-muted)',
                      padding: '4px 10px',
                      cursor: 'pointer',
                      display: 'flex',
                      alignItems: 'center',
                      gap: '5px',
                      fontSize: '11px',
                      fontWeight: isActive ? 600 : 400,
                    }}
                  >
                    {tab.icon}
                    <span>{tab.label}</span>
                  </button>
                );
              })}
            </div>
          )}

          {/* Active Renderer */}
          <div style={{ flex: 1, overflow: 'hidden' }}>
            {viewerTab === 'preview' && showViewerTabs ? (
              <ProjectPreview
                key={currentArtifact.id}
                artifact={currentArtifact}
                content={currentArtifact.kind === 'diff' ? null : (activeArtifactContent?.content || null)}
                devServer={devServerInfo}
                onDetect={requestDetection}
              />
            ) : (
              renderViewer()
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
