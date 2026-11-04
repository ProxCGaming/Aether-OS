import React, { useState, useEffect, useCallback, useMemo } from 'react';
import {
  Layers,
  Pause,
  Play,
  RefreshCw,
  Search,
  AlertTriangle,
  FileText,
  FileX,
  CheckCircle2,
  Clock,
  Sliders,
  Folder,
} from 'lucide-react';
import { useAetherSocket } from '../../contexts/SocketContext';
import LoadStateRow from './LoadStateRow';

const REASON_METADATA = {
  gitignore: { label: 'Gitignored', color: '#818cf8', bg: 'rgba(99, 102, 241, 0.15)' },
  too_large: { label: 'Too large (>512 KB)', color: '#f87171', bg: 'rgba(239, 68, 68, 0.15)' },
  binary: { label: 'Binary', color: '#fb923c', bg: 'rgba(249, 115, 22, 0.15)' },
  lockfile: { label: 'Lockfile', color: '#fbbf24', bg: 'rgba(245, 158, 11, 0.15)' },
  not_prose: { label: 'Not prose', color: '#94a3b8', bg: 'rgba(148, 163, 184, 0.15)' },
};

function formatBytes(bytes) {
  if (!bytes || bytes <= 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  const i = Math.floor(Math.log(bytes) / Math.log(1024));
  return `${(bytes / Math.pow(1024, i)).toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

export default function WorkspaceIndexingPanel({ wsRef: propWsRef, initialWorkspace = '' }) {
  const socket = useAetherSocket();
  const wsRef = propWsRef || socket?.wsRef;
  const contextSendMessage = socket?.sendMessage;
  const isConnected = socket?.isConnected;
  // RUX-C-07: the status record below starts as a fabricated
  // placeholder (an embedder the user may not have, zero files) —
  // until the first real status reply lands, the panel renders a
  // load state instead of those invented numbers. A status reply
  // carrying an error is the error state; progress events only
  // exist once the engine knows the status, so they answer it too.
  const [statusLoad, setStatusLoad] = useState('loading');

  const [activeWorkspace, setActiveWorkspace] = useState(initialWorkspace);
  const [workspaces, setWorkspaces] = useState([]);
  const [status, setStatus] = useState({
    status: 'idle',
    total_files: 0,
    indexed_files: 0,
    total_chunks: 0,
    embedder: 'qwen3-embedding:0.6b',
    paused: false,
    indexed_docs: [],
    excluded_files: [],
  });
  const [progress, setProgress] = useState(null);
  const [activeTab, setActiveTab] = useState('indexed'); // 'indexed' | 'excluded'
  const [filterQuery, setFilterQuery] = useState('');
  const [warningModal, setWarningModal] = useState({ isOpen: false, pendingEmbedder: '' });

  const sendEvent = useCallback(
    (type, payload = {}) => {
      const msg = {
        type,
        schema_version: 1,
        request_id: Date.now().toString(),
        payload,
      };
      if (contextSendMessage) {
        contextSendMessage(type, {
          schema_version: 1,
          request_id: msg.request_id,
          payload,
        });
      } else if (wsRef?.current?.readyState === 1 || wsRef?.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify(msg));
      }
    },
    [contextSendMessage, wsRef]
  );

  // Request initial workspace list if active workspace not specified
  useEffect(() => {
    if (!activeWorkspace) {
      sendEvent('WORKSPACE_LIST_REQUEST', {});
    }
  }, [activeWorkspace, sendEvent]);

  // Request status when activeWorkspace is ready
  useEffect(() => {
    if (activeWorkspace) {
      // A (re)fetch for this workspace is unanswered until its
      // status reply lands — including workspace switches.
      setStatusLoad('loading');
      sendEvent('WORKSPACE_INDEX_STATUS_REQUEST', { workspace_root: activeWorkspace });
    }
  }, [activeWorkspace, sendEvent]);

  // Listen for socket events
  useEffect(() => {
    if (!wsRef?.current) return;

    const handleMessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'WORKSPACE_LIST_RESPONSE') {
          const list = data.payload?.workspaces || [];
          setWorkspaces(list);
          if (!activeWorkspace && list.length > 0) {
            const active = list.find((w) => w.is_active) || list[0];
            setActiveWorkspace(active.path);
          } else if (list.length === 0) {
            // No workspace exists, so no status fetch will ever be
            // sent — the placeholder gate must not spin forever.
            setStatusLoad('ready');
          }
        } else if (data.type === 'WORKSPACE_INDEX_STATUS_RESPONSE') {
          if (data.payload?.error) {
            setStatusLoad('error');
          } else if (data.payload) {
            setStatusLoad('ready');
            setStatus((prev) => ({
              ...prev,
              ...data.payload,
              paused: data.payload.paused ?? prev.paused,
              indexed_docs: data.payload.indexed_docs || prev.indexed_docs || [],
              excluded_files: data.payload.excluded_files || prev.excluded_files || [],
            }));
            if (data.payload.status !== 'indexing') {
              setProgress(null);
            }
          }
        } else if (data.type === 'WORKSPACE_INDEX_PROGRESS') {
          // Progress is only emitted for a workspace whose status
          // the engine is tracking — it answers the gate too.
          setStatusLoad('ready');
          setProgress(data.payload);
          if (data.payload.status) {
            setStatus((prev) => ({ ...prev, status: data.payload.status }));
          }
        } else if (data.type === 'WORKSPACE_INDEX_PAUSE_RESPONSE') {
          const isPaused = Boolean(data.payload?.paused);
          setStatus((prev) => ({
            ...prev,
            paused: isPaused,
            status: isPaused ? 'paused' : 'idle',
          }));
        } else if (data.type === 'WORKSPACE_INDEX_CONFIG_RESPONSE') {
          if (data.payload?.ok) {
            setStatusLoad('loading');
            sendEvent('WORKSPACE_INDEX_STATUS_REQUEST', { workspace_root: activeWorkspace });
          }
        } else if (data.type === 'WORKSPACE_INDEX_START_RESPONSE') {
          setStatusLoad('loading');
          sendEvent('WORKSPACE_INDEX_STATUS_REQUEST', { workspace_root: activeWorkspace });
        }
      } catch {
        // ignore parse error
      }
    };

    const target = wsRef.current;
    target.addEventListener('message', handleMessage);
    return () => {
      if (target?.removeEventListener) {
        target.removeEventListener('message', handleMessage);
      }
    };
  }, [wsRef, activeWorkspace, sendEvent]);

  const handleTogglePause = () => {
    const nextPause = !status.paused;
    sendEvent('WORKSPACE_INDEX_PAUSE_REQUEST', {
      workspace_root: activeWorkspace,
      pause: nextPause,
    });
    // optimistic update
    setStatus((prev) => ({
      ...prev,
      paused: nextPause,
      status: nextPause ? 'paused' : 'idle',
    }));
  };

  const handleReindex = () => {
    sendEvent('WORKSPACE_INDEX_START_REQUEST', {
      workspace_root: activeWorkspace,
      confirm: true,
      embedder: status.embedder,
    });
    setStatus((prev) => ({ ...prev, status: 'indexing' }));
  };

  const handleEmbedderSelectChange = (e) => {
    const selected = e.target.value;
    if (selected === status.embedder) return;
    // Show warning modal BEFORE executing switch
    setWarningModal({
      isOpen: true,
      pendingEmbedder: selected,
    });
  };

  const handleCancelEmbedderSwitch = () => {
    setWarningModal({ isOpen: false, pendingEmbedder: '' });
  };

  const handleConfirmEmbedderSwitch = () => {
    const newEmbedder = warningModal.pendingEmbedder;
    setWarningModal({ isOpen: false, pendingEmbedder: '' });

    // Send config update
    sendEvent('WORKSPACE_INDEX_CONFIG_REQUEST', {
      workspace_root: activeWorkspace,
      embedder: newEmbedder,
    });

    // Also trigger re-index
    sendEvent('WORKSPACE_INDEX_START_REQUEST', {
      workspace_root: activeWorkspace,
      confirm: true,
      embedder: newEmbedder,
    });

    setStatus((prev) => ({
      ...prev,
      embedder: newEmbedder,
      status: 'indexing',
      indexed_files: 0,
      total_chunks: 0,
    }));
  };

  // Filtered lists
  const filteredIndexedDocs = useMemo(() => {
    const docs = Array.isArray(status.indexed_docs) ? status.indexed_docs : [];
    if (!filterQuery) return docs;
    const q = filterQuery.toLowerCase();
    return docs.filter((d) => (d.file_path || '').toLowerCase().includes(q));
  }, [status.indexed_docs, filterQuery]);

  const filteredExcludedFiles = useMemo(() => {
    const files = Array.isArray(status.excluded_files) ? status.excluded_files : [];
    if (!filterQuery) return files;
    const q = filterQuery.toLowerCase();
    return files.filter(
      (f) =>
        (f.file_path || '').toLowerCase().includes(q) ||
        (f.reason || '').toLowerCase().includes(q)
    );
  }, [status.excluded_files, filterQuery]);

  return (
    <div style={{ color: '#f0f0f5', display: 'flex', flexDirection: 'column', gap: '20px' }}>
      {/* Header */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
            <Layers size={22} style={{ color: '#818cf8' }} />
            <h2 style={{ fontSize: '20px', fontWeight: 600, margin: 0 }}>Workspace Indexing</h2>
          </div>
          <p style={{ color: '#9090a0', fontSize: '13px', margin: '4px 0 0 0' }}>
            Local prose embedding index for workspace RAG and deep file search citations.
          </p>
        </div>

        {/* Workspace Root Selector */}
        {workspaces.length > 1 && (
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <Folder size={14} style={{ color: '#9090a0' }} />
            <select
              value={activeWorkspace}
              onChange={(e) => setActiveWorkspace(e.target.value)}
              aria-label="Workspace Root"
              style={{
                background: 'rgba(255,255,255,0.06)',
                border: '1px solid rgba(255,255,255,0.12)',
                borderRadius: '8px',
                color: '#fff',
                padding: '6px 12px',
                fontSize: '12px',
                outline: 'none',
              }}
            >
              {workspaces.map((w) => (
                <option key={w.path} value={w.path} style={{ background: '#181824' }}>
                  {w.name || w.path}
                </option>
              ))}
            </select>
          </div>
        )}
      </div>

      {/* RUX-C-07: until the first real status reply lands, a load
          state stands where the status card would be — the
          placeholder record (idle, 0 files, a default embedder) is
          never rendered as if the engine had reported it. */}
      {statusLoad !== 'ready' ? (
        <LoadStateRow
          loadState={statusLoad}
          isConnected={isConnected}
          testId="indexing-status-load-state"
          loadingText="Loading indexing status…"
          offlineText="Waiting for the engine — indexing status will appear here once it connects."
          errorText="Indexing status could not be loaded."
        />
      ) : (
      <div
        style={{
          background: 'rgba(255, 255, 255, 0.03)',
          border: '1px solid rgba(255, 255, 255, 0.08)',
          borderRadius: '14px',
          padding: '16px 20px',
          display: 'flex',
          flexDirection: 'column',
          gap: '16px',
        }}
      >
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: '12px' }}>
          {/* Status Badge */}
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
            <div
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: '6px',
                padding: '4px 10px',
                borderRadius: '20px',
                fontSize: '12px',
                fontWeight: 500,
                background:
                  status.status === 'indexing'
                    ? 'rgba(59, 130, 246, 0.2)'
                    : status.status === 'paused' || status.paused
                    ? 'rgba(245, 158, 11, 0.2)'
                    : 'rgba(34, 197, 94, 0.2)',
                color:
                  status.status === 'indexing'
                    ? '#60a5fa'
                    : status.status === 'paused' || status.paused
                    ? '#fbbf24'
                    : '#4ade80',
              }}
            >
              <span
                style={{
                  width: '7px',
                  height: '7px',
                  borderRadius: '50%',
                  background: 'currentColor',
                }}
              />
              {status.status === 'indexing'
                ? 'Indexing'
                : status.status === 'paused' || status.paused
                ? 'Paused'
                : 'Idle / Up-to-date'}
            </div>

            {/* Path indicator */}
            {activeWorkspace && (
              <span style={{ fontSize: '12px', color: '#9090a0', fontFamily: 'monospace' }}>
                {activeWorkspace}
              </span>
            )}
          </div>

          {/* Action Buttons */}
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <button
              onClick={handleTogglePause}
              aria-label={status.paused || status.status === 'paused' ? 'Resume Indexing' : 'Pause Indexing'}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '6px',
                padding: '7px 14px',
                borderRadius: '8px',
                border: '1px solid rgba(255,255,255,0.12)',
                background: status.paused ? 'rgba(34, 197, 94, 0.15)' : 'rgba(255,255,255,0.06)',
                color: status.paused ? '#4ade80' : '#fff',
                fontSize: '12px',
                fontWeight: 500,
                cursor: 'pointer',
                transition: 'all 0.15s',
              }}
            >
              {status.paused || status.status === 'paused' ? (
                <>
                  <Play size={13} />
                  Resume Indexing
                </>
              ) : (
                <>
                  <Pause size={13} />
                  Pause Indexing
                </>
              )}
            </button>

            <button
              onClick={handleReindex}
              aria-label="Re-index Workspace"
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '6px',
                padding: '7px 14px',
                borderRadius: '8px',
                border: '1px solid rgba(124, 58, 237, 0.4)',
                background: 'rgba(124, 58, 237, 0.2)',
                color: '#c4b5fd',
                fontSize: '12px',
                fontWeight: 500,
                cursor: 'pointer',
                transition: 'all 0.15s',
              }}
            >
              <RefreshCw size={13} />
              Re-index
            </button>
          </div>
        </div>

        {/* Progress Bar (Visible while indexing) */}
        {progress && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '12px', color: '#a5b4fc' }}>
              <span>
                {progress.current_file
                  ? `Processing ${progress.current_file}`
                  : 'Updating workspace index...'}
              </span>
              <span>
                {progress.indexed} / {progress.total} files
              </span>
            </div>
            <div
              style={{
                width: '100%',
                height: '6px',
                borderRadius: '3px',
                background: 'rgba(255,255,255,0.1)',
                overflow: 'hidden',
              }}
            >
              <div
                style={{
                  height: '100%',
                  width: `${progress.total > 0 ? (progress.indexed / progress.total) * 100 : 0}%`,
                  background: 'linear-gradient(90deg, #6366f1, #8b5cf6)',
                  transition: 'width 0.3s ease',
                }}
              />
            </div>
          </div>
        )}

        {/* Metrics Grid */}
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fit, minmax(130px, 1fr))',
            gap: '12px',
            borderTop: '1px solid rgba(255,255,255,0.06)',
            paddingTop: '12px',
          }}
        >
          <div>
            <div style={{ fontSize: '11px', color: '#9090a0', textTransform: 'uppercase' }}>Indexed Files</div>
            <div style={{ fontSize: '18px', fontWeight: 600, color: '#fff', marginTop: '2px' }}>
              {status.indexed_files || 0}
            </div>
          </div>

          <div>
            <div style={{ fontSize: '11px', color: '#9090a0', textTransform: 'uppercase' }}>Excluded Files</div>
            <div style={{ fontSize: '18px', fontWeight: 600, color: '#f87171', marginTop: '2px' }}>
              {Array.isArray(status.excluded_files) ? status.excluded_files.length : 0}
            </div>
          </div>

          <div>
            <div style={{ fontSize: '11px', color: '#9090a0', textTransform: 'uppercase' }}>Total Chunks</div>
            <div style={{ fontSize: '18px', fontWeight: 600, color: '#fff', marginTop: '2px' }}>
              {status.total_chunks || 0}
            </div>
          </div>

          <div>
            <div style={{ fontSize: '11px', color: '#9090a0', textTransform: 'uppercase' }}>Embedder Model</div>
            <div style={{ fontSize: '13px', fontWeight: 500, color: '#818cf8', marginTop: '4px' }}>
              {status.embedder || 'qwen3-embedding:0.6b'}
            </div>
          </div>
        </div>
      </div>
      )}

      {/* Embedder Configuration Box */}
      <div
        style={{
          background: 'rgba(255, 255, 255, 0.03)',
          border: '1px solid rgba(255, 255, 255, 0.08)',
          borderRadius: '14px',
          padding: '16px 20px',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          gap: '16px',
        }}
      >
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <Sliders size={15} style={{ color: '#818cf8' }} />
            <span style={{ fontSize: '14px', fontWeight: 500 }}>Embedding Model Selection</span>
          </div>
          <p style={{ color: '#9090a0', fontSize: '12px', margin: '3px 0 0 0' }}>
            Unified local model for workspace RAG and episodic memory. Changing models requires a full re-index.
          </p>
        </div>

        <div style={{ minWidth: '220px' }}>
          <select
            id="embedder-select"
            aria-label="Embedding Model"
            value={status.embedder || 'qwen3-embedding:0.6b'}
            disabled={statusLoad !== 'ready'}
            onChange={handleEmbedderSelectChange}
            style={{
              width: '100%',
              background: 'rgba(255,255,255,0.06)',
              border: '1px solid rgba(255,255,255,0.12)',
              borderRadius: '8px',
              color: '#fff',
              padding: '8px 12px',
              fontSize: '13px',
              outline: 'none',
              cursor: 'pointer',
            }}
          >
            <option value="qwen3-embedding:0.6b" style={{ background: '#181824' }}>
              qwen3-embedding:0.6b (Standard, 1024 dims)
            </option>
            <option value="qwen3-embedding:4b" style={{ background: '#181824' }}>
              qwen3-embedding:4b (Quality, 1024 dims)
            </option>
          </select>
        </div>
      </div>

      {/* Files Browser Tabs & Filter */}
      <div
        style={{
          background: 'rgba(255, 255, 255, 0.03)',
          border: '1px solid rgba(255, 255, 255, 0.08)',
          borderRadius: '14px',
          overflow: 'hidden',
          display: 'flex',
          flexDirection: 'column',
        }}
      >
        {/* Navigation & Search Bar */}
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            padding: '12px 16px',
            borderBottom: '1px solid rgba(255,255,255,0.06)',
            flexWrap: 'wrap',
            gap: '10px',
          }}
        >
          {/* Tab buttons */}
          <div style={{ display: 'flex', gap: '8px' }}>
            <button
              onClick={() => setActiveTab('indexed')}
              role="button"
              aria-label="Indexed Files"
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '6px',
                padding: '6px 14px',
                borderRadius: '8px',
                border: 'none',
                background: activeTab === 'indexed' ? 'rgba(124, 58, 237, 0.3)' : 'transparent',
                color: activeTab === 'indexed' ? '#fff' : '#9090a0',
                fontSize: '13px',
                fontWeight: 500,
                cursor: 'pointer',
              }}
            >
              <FileText size={14} />
              Indexed Files ({Array.isArray(status.indexed_docs) ? status.indexed_docs.length : status.indexed_files || 0})
            </button>

            <button
              onClick={() => setActiveTab('excluded')}
              role="button"
              aria-label="Excluded Files"
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '6px',
                padding: '6px 14px',
                borderRadius: '8px',
                border: 'none',
                background: activeTab === 'excluded' ? 'rgba(239, 68, 68, 0.2)' : 'transparent',
                color: activeTab === 'excluded' ? '#fca5a5' : '#9090a0',
                fontSize: '13px',
                fontWeight: 500,
                cursor: 'pointer',
              }}
            >
              <FileX size={14} />
              Excluded Files ({Array.isArray(status.excluded_files) ? status.excluded_files.length : 0})
            </button>
          </div>

          {/* Search Filter */}
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: '6px',
              background: 'rgba(255,255,255,0.05)',
              border: '1px solid rgba(255,255,255,0.08)',
              borderRadius: '8px',
              padding: '6px 12px',
              width: '240px',
            }}
          >
            <Search size={13} style={{ color: '#9090a0' }} />
            <input
              type="text"
              placeholder="Filter files or reasons…"
              value={filterQuery}
              onChange={(e) => setFilterQuery(e.target.value)}
              style={{
                background: 'transparent',
                border: 'none',
                color: '#fff',
                fontSize: '12px',
                outline: 'none',
                width: '100%',
              }}
            />
          </div>
        </div>

        {/* Files Content List */}
        <div style={{ maxHeight: '380px', overflowY: 'auto' }}>
          {activeTab === 'indexed' && (
            <div data-testid="indexed-files-list">
              {filteredIndexedDocs.length === 0 ? (
                statusLoad !== 'ready' ? (
                  <div style={{ padding: '24px 16px' }}>
                    <LoadStateRow
                      loadState={statusLoad}
                      isConnected={isConnected}
                      testId="indexing-files-load-state"
                      variant="plain"
                      style={{ textAlign: 'center' }}
                      loadingText="Loading indexed files…"
                      offlineText="Waiting for the engine — the indexed file list will appear here once it connects."
                      errorText="The indexed file list could not be loaded."
                    />
                  </div>
                ) : (
                  <div style={{ padding: '36px 16px', textAlign: 'center', color: '#9090a0', fontSize: '13px' }}>
                    No indexed prose files found.
                  </div>
                )
              ) : (
                <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '13px' }}>
                  <thead>
                    <tr style={{ borderBottom: '1px solid rgba(255,255,255,0.06)', color: '#9090a0', textAlign: 'left' }}>
                      <th style={{ padding: '10px 16px', fontWeight: 500 }}>File Path</th>
                      <th style={{ padding: '10px 16px', fontWeight: 500, width: '110px' }}>Size</th>
                      <th style={{ padding: '10px 16px', fontWeight: 500, width: '110px' }}>Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredIndexedDocs.map((doc, idx) => (
                      <tr
                        key={doc.file_path || idx}
                        style={{
                          borderBottom: '1px solid rgba(255,255,255,0.03)',
                          background: idx % 2 === 0 ? 'transparent' : 'rgba(255,255,255,0.015)',
                        }}
                      >
                        <td style={{ padding: '10px 16px', fontFamily: 'monospace', color: '#e2e8f0' }}>
                          {doc.file_path}
                        </td>
                        <td style={{ padding: '10px 16px', color: '#9090a0' }}>
                          {formatBytes(doc.file_size)}
                        </td>
                        <td style={{ padding: '10px 16px' }}>
                          <span
                            style={{
                              display: 'inline-flex',
                              alignItems: 'center',
                              gap: '4px',
                              padding: '2px 8px',
                              borderRadius: '12px',
                              fontSize: '11px',
                              background:
                                doc.status === 'pending'
                                  ? 'rgba(245, 158, 11, 0.15)'
                                  : 'rgba(34, 197, 94, 0.15)',
                              color: doc.status === 'pending' ? '#fbbf24' : '#4ade80',
                            }}
                          >
                            {doc.status === 'pending' ? (
                              <>
                                <Clock size={11} />
                                pending
                              </>
                            ) : (
                              <>
                                <CheckCircle2 size={11} />
                                indexed
                              </>
                            )}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          )}

          {activeTab === 'excluded' && (
            <div data-testid="excluded-files-list">
              {filteredExcludedFiles.length === 0 ? (
                statusLoad !== 'ready' ? (
                  <div style={{ padding: '24px 16px' }}>
                    <LoadStateRow
                      loadState={statusLoad}
                      isConnected={isConnected}
                      variant="plain"
                      style={{ textAlign: 'center' }}
                      loadingText="Loading excluded files…"
                      offlineText="Waiting for the engine — the excluded file list will appear here once it connects."
                      errorText="The excluded file list could not be loaded."
                    />
                  </div>
                ) : (
                  <div style={{ padding: '36px 16px', textAlign: 'center', color: '#9090a0', fontSize: '13px' }}>
                    No excluded files found.
                  </div>
                )
              ) : (
                <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '13px' }}>
                  <thead>
                    <tr style={{ borderBottom: '1px solid rgba(255,255,255,0.06)', color: '#9090a0', textAlign: 'left' }}>
                      <th style={{ padding: '10px 16px', fontWeight: 500 }}>File Path</th>
                      <th style={{ padding: '10px 16px', fontWeight: 500, width: '180px' }}>Exclusion Reason</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredExcludedFiles.map((file, idx) => {
                      const reasonMeta = REASON_METADATA[file.reason] || {
                        label: file.reason || 'Excluded',
                        color: '#94a3b8',
                        bg: 'rgba(148, 163, 184, 0.15)',
                      };
                      return (
                        <tr
                          key={file.file_path || idx}
                          style={{
                            borderBottom: '1px solid rgba(255,255,255,0.03)',
                            background: idx % 2 === 0 ? 'transparent' : 'rgba(255,255,255,0.015)',
                          }}
                        >
                          <td style={{ padding: '10px 16px', fontFamily: 'monospace', color: '#cbd5e1' }}>
                            {file.file_path}
                          </td>
                          <td style={{ padding: '10px 16px' }}>
                            <span
                              style={{
                                display: 'inline-block',
                                padding: '2px 8px',
                                borderRadius: '12px',
                                fontSize: '11px',
                                fontWeight: 500,
                                background: reasonMeta.bg,
                                color: reasonMeta.color,
                              }}
                            >
                              {reasonMeta.label}
                            </span>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              )}
            </div>
          )}
        </div>
      </div>

      {/* Full Re-index Warning Modal */}
      {warningModal.isOpen && (
        <div
          data-testid="reindex-warning-modal"
          style={{
            position: 'fixed',
            inset: 0,
            background: 'rgba(0,0,0,0.7)',
            backdropFilter: 'blur(4px)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            zIndex: 1000,
          }}
        >
          <div
            style={{
              background: '#1a1a24',
              border: '1px solid rgba(239, 68, 68, 0.3)',
              borderRadius: '16px',
              padding: '24px',
              maxWidth: '460px',
              width: '90%',
              boxShadow: '0 20px 40px rgba(0,0,0,0.6)',
              display: 'flex',
              flexDirection: 'column',
              gap: '16px',
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
              <div
                style={{
                  background: 'rgba(239, 68, 68, 0.15)',
                  padding: '10px',
                  borderRadius: '10px',
                  color: '#f87171',
                }}
              >
                <AlertTriangle size={22} />
              </div>
              <h3 style={{ margin: 0, fontSize: '17px', fontWeight: 600, color: '#fff' }}>
                Full Re-index Required
              </h3>
            </div>

            <p style={{ margin: 0, fontSize: '13px', color: '#cbd5e1', lineHeight: '1.5' }}>
              Switching the embedder model from <strong>{status.embedder}</strong> to{' '}
              <strong>{warningModal.pendingEmbedder}</strong> will drop all existing vector embeddings. Vectors are
              incomparable across different model architectures, requiring a full re-index of the workspace.
            </p>

            <div
              style={{
                display: 'flex',
                justifyContent: 'flex-end',
                gap: '10px',
                marginTop: '8px',
              }}
            >
              <button
                onClick={handleCancelEmbedderSwitch}
                style={{
                  padding: '8px 16px',
                  borderRadius: '8px',
                  border: '1px solid rgba(255,255,255,0.12)',
                  background: 'transparent',
                  color: '#9090a0',
                  fontSize: '13px',
                  cursor: 'pointer',
                }}
              >
                Cancel
              </button>

              <button
                onClick={handleConfirmEmbedderSwitch}
                style={{
                  padding: '8px 16px',
                  borderRadius: '8px',
                  border: 'none',
                  background: 'linear-gradient(135deg, #ef4444, #dc2626)',
                  color: '#fff',
                  fontSize: '13px',
                  fontWeight: 500,
                  cursor: 'pointer',
                }}
              >
                Confirm & Re-index
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
