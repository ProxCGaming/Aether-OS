import React, { useEffect, useMemo, useState } from 'react';
import { X, Copy, Check, Download, ExternalLink, FileCode, FileText, GitCommit, Image, Database, AlertCircle, Eye } from 'lucide-react';
import CodeRenderer from './renderers/CodeRenderer';
import MarkdownRenderer from './renderers/MarkdownRenderer';
import DiffRenderer from './renderers/DiffRenderer';
import DataRenderer from './renderers/DataRenderer';
import ImageViewer from './renderers/ImageViewer';
import ProjectPreview from './renderers/ProjectPreview';

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

export default function ArtifactsCanvas({
  isOpen = false,
  onClose = () => {},
  artifacts = [],
  selectedArtifactId = null,
  onSelectArtifact = () => {},
  activeArtifactContent = null,
  _isLoading = false,
  devServerInfo = null,
  onDetectDevServer = null
}) {
  const [copied, setCopied] = useState(false);
  const [viewerTab, setViewerTab] = useState('code');

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
        setCopied(true);
        setTimeout(() => setCopied(false), 1800);
      }).catch(() => {});
    }
  };

  const handleSaveCopy = () => {
    const content = activeArtifactContent?.content || activeArtifactContent?.diff || '';
    let filename = currentArtifact?.title || 'artifact.txt';
    if (currentArtifact?.kind === 'report' && !filename.includes('.')) {
      filename = `${filename}.md`;
    }
    const blob = new Blob([content], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  };

  const handleOpenExternally = () => {
    if (currentArtifact?.file_path && window.electronAPI?.showOpenDialog) {
      // Best-effort external open through electron
      window.electronAPI.showOpenDialog({ defaultPath: currentArtifact.file_path }).catch(() => {});
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
            color: 'rgba(255, 255, 255, 0.4)',
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
            background: 'rgba(239, 68, 68, 0.08)',
            border: '1px solid rgba(239, 68, 68, 0.25)',
            borderRadius: '8px',
            color: '#fca5a5',
            gap: '12px'
          }}
        >
          <AlertCircle size={32} color="#ef4444" />
          <div style={{ fontWeight: 600, fontSize: '14px' }}>File No Longer Exists</div>
          <div style={{ fontSize: '12px', color: '#cbd5e1' }}>
            {activeArtifactContent?.error || `File ${currentArtifact.file_path} no longer exists on disk.`}
          </div>
          <div style={{ fontSize: '11px', color: 'rgba(255, 255, 255, 0.4)' }}>
            Metadata is preserved, but file contents cannot be loaded.
          </div>
        </div>
      );
    }

    const kind = currentArtifact.kind;
    const title = (currentArtifact.title || '').toLowerCase();
    const content = activeArtifactContent?.content || '';
    const diff = activeArtifactContent?.diff || '';

    if (kind === 'diff') {
      return <DiffRenderer diff={diff || content} filePath={currentArtifact.file_path} />;
    }

    if (kind === 'report') {
      return <SummaryDocumentRenderer content={content} title={currentArtifact.title} />;
    }

    if (title.endsWith('.md') || title.endsWith('.markdown')) {
      return <MarkdownRenderer content={content} />;
    }

    if (title.endsWith('.json')) {
      return <DataRenderer content={content} format="json" />;
    }

    if (title.endsWith('.csv')) {
      return <DataRenderer content={content} format="csv" />;
    }

    if (/\.(png|jpg|jpeg|gif|webp|svg|bmp|ico)$/.test(title)) {
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
    if (art.kind === 'diff') return <GitCommit size={14} color="#c084fc" />;
    if (art.kind === 'report') return <FileText size={14} color="#60a5fa" />;
    const t = (art.title || '').toLowerCase();
    if (t.endsWith('.md')) return <FileText size={14} color="#60a5fa" />;
    if (t.endsWith('.json') || t.endsWith('.csv')) return <Database size={14} color="#f59e0b" />;
    if (/\.(png|jpg|jpeg|gif|webp|svg)$/.test(t)) return <Image size={14} color="#ec4899" />;
    return <FileCode size={14} color="#34d399" />;
  };

  return (
    <div
      data-testid="artifacts-canvas"
      className="ios-glass"
      style={{
        position: 'absolute',
        top: '40px',
        right: '40px',
        bottom: '40px',
        width: '480px',
        zIndex: 1050,
        display: 'flex',
        flexDirection: 'column',
        boxShadow: '0 16px 40px rgba(0, 0, 0, 0.4), inset 0 1px 1px rgba(255, 255, 255, 0.15)',
        borderRadius: '16px',
        overflow: 'hidden',
        border: '1px solid rgba(255, 255, 255, 0.12)',
        background: 'rgba(23, 23, 30, 0.95)',
        backdropFilter: 'blur(20px)'
      }}
    >
      {/* Canvas Top Bar */}
      <div
        style={{
          padding: '12px 16px',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          borderBottom: '1px solid rgba(255, 255, 255, 0.08)',
          background: 'rgba(255, 255, 255, 0.03)'
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          <span style={{ fontSize: '14px', fontWeight: 600, color: '#fff' }}>Artifacts</span>
          <span
            data-testid="canvas-total-badge"
            style={{
              fontSize: '11px',
              fontWeight: 600,
              background: 'rgba(167, 139, 250, 0.2)',
              color: '#c084fc',
              padding: '2px 8px',
              borderRadius: '999px',
              border: '1px solid rgba(167, 139, 250, 0.35)'
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
            color: 'rgba(255, 255, 255, 0.6)',
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
            borderBottom: '1px solid rgba(255, 255, 255, 0.08)',
            padding: '8px 12px',
            display: 'flex',
            flexDirection: 'column',
            gap: '8px',
            flexShrink: 0,
            background: 'rgba(0, 0, 0, 0.2)'
          }}
        >
          {taskGroups.length === 0 ? (
            <div style={{ fontSize: '12px', color: 'rgba(255, 255, 255, 0.4)', padding: '8px 0', textAlign: 'center' }}>
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
                    color: 'rgba(255, 255, 255, 0.4)',
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
                          background: isSelected ? 'rgba(167, 139, 250, 0.18)' : 'transparent',
                          border: isSelected ? '1px solid rgba(167, 139, 250, 0.35)' : '1px solid transparent',
                          transition: 'background 150ms'
                        }}
                      >
                        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', minWidth: 0 }}>
                          {getArtifactIcon(art)}
                          <span
                            style={{
                              fontSize: '13px',
                              color: isSelected ? '#fff' : '#cbd5e1',
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
                              ? 'rgba(192, 132, 252, 0.2)'
                              : art.kind === 'report'
                              ? 'rgba(56, 189, 248, 0.2)'
                              : 'rgba(255, 255, 255, 0.08)',
                            color: art.kind === 'diff'
                              ? '#c084fc'
                              : art.kind === 'report'
                              ? '#38bdf8'
                              : '#94a3b8',
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
                borderBottom: '1px solid rgba(255, 255, 255, 0.06)'
              }}
            >
              <div style={{ display: 'flex', flexDirection: 'column', minWidth: 0, gap: '2px' }}>
                <div data-testid="artifact-title" style={{ fontSize: '14px', fontWeight: 600, color: '#fff' }}>
                  {currentArtifact.title}
                </div>
                {currentArtifact.file_path && (
                  <div
                    data-testid="artifact-path"
                    style={{
                      fontSize: '11px',
                      color: 'rgba(255, 255, 255, 0.4)',
                      fontFamily: 'monospace',
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
                    background: 'rgba(255, 255, 255, 0.08)',
                    border: '1px solid rgba(255, 255, 255, 0.1)',
                    borderRadius: '6px',
                    color: '#fff',
                    padding: '4px 8px',
                    cursor: 'pointer',
                    display: 'flex',
                    alignItems: 'center',
                    gap: '4px',
                    fontSize: '11px'
                  }}
                >
                  {copied ? <Check size={12} color="#10b981" /> : <Copy size={12} />}
                  <span>{copied ? 'Copied' : 'Copy'}</span>
                </button>
                <button
                  type="button"
                  data-testid="artifact-save-btn"
                  onClick={handleSaveCopy}
                  title="Save a copy"
                  style={{
                    background: 'rgba(255, 255, 255, 0.08)',
                    border: '1px solid rgba(255, 255, 255, 0.1)',
                    borderRadius: '6px',
                    color: '#fff',
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
                    background: 'rgba(255, 255, 255, 0.08)',
                    border: '1px solid rgba(255, 255, 255, 0.1)',
                    borderRadius: '6px',
                    color: '#fff',
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
                      background: isActive ? 'rgba(167, 139, 250, 0.18)' : 'rgba(255, 255, 255, 0.05)',
                      border: isActive ? '1px solid rgba(167, 139, 250, 0.35)' : '1px solid rgba(255, 255, 255, 0.08)',
                      borderRadius: '6px',
                      color: isActive ? '#fff' : '#94a3b8',
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
