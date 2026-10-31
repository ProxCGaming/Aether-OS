import React, { useState, useEffect, useMemo, useCallback } from 'react';
import { 
  Search, 
  Table as TableIcon, 
  LayoutGrid, 
  ExternalLink, 
  FileCode, 
  FileText, 
  GitCommit, 
  Database, 
  Image as ImageIcon, 
  Layers, 
  X,
  Clock,
  FolderGit2
} from 'lucide-react';
import '../chat.css';

export default function GlobalArtifactsView({
  artifacts: controlledArtifacts,
  initialArtifacts,
  sessions = [],
  wsRef,
  onNavigateToSession = () => {}
}) {
  const [internalArtifacts, setInternalArtifacts] = useState(
    () => initialArtifacts || []
  );
  const [searchQuery, setSearchQuery] = useState('');
  const [selectedKind, setSelectedKind] = useState('all');
  const [viewMode, setViewMode] = useState('table'); // 'table' | 'grid'

  // Determine effective artifacts: controlled if passed, otherwise internal state
  const effectiveArtifacts = controlledArtifacts ?? internalArtifacts;

  // Request global artifacts and listen for real-time updates / deletions
  useEffect(() => {
    const ws = wsRef?.current;
    if (!ws) return;

    const handleMessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'ARTIFACT_LIST_RESPONSE') {
          // If request was global or has artifacts array
          if (data.request_id?.startsWith('global_artifacts_') || data.payload?.global) {
            setInternalArtifacts(data.payload?.artifacts || []);
          }
        } else if (data.type === 'SESSION_DELETE_RESPONSE') {
          const deletedId = data.payload?.session_id;
          if (deletedId) {
            setInternalArtifacts(prev => prev.filter(art => art.session_id !== deletedId));
          }
        } else if (data.type === 'ARTIFACT_RECORDED') {
          if (data.payload?.artifact_id) {
            const newArt = {
              id: data.payload.artifact_id,
              session_id: data.payload.session_id,
              task_id: data.payload.task_id,
              kind: data.payload.kind,
              title: data.payload.title,
              file_path: data.payload.path,
              created_at: data.payload.created_at || Date.now()
            };
            setInternalArtifacts(prev => [newArt, ...prev.filter(a => a.id !== newArt.id)]);
          }
        }
      } catch {
        // ignore parse error
      }
    };

    ws.addEventListener('message', handleMessage);

    // Initial query if we don't have controlled artifacts
    if (!controlledArtifacts && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({
        type: 'ARTIFACT_LIST_REQUEST',
        schema_version: 1,
        request_id: `global_artifacts_${Date.now()}`,
        payload: { global: true }
      }));
    }

    return () => {
      ws.removeEventListener('message', handleMessage);
    };
  }, [wsRef, controlledArtifacts]);

  // Resolve session title helper
  const getSessionTitle = useCallback((sessionId) => {
    const s = sessions.find(item => item.id === sessionId);
    return s?.title || sessionId || 'Unknown Session';
  }, [sessions]);

  // Filtered artifacts
  const filteredArtifacts = useMemo(() => {
    return effectiveArtifacts.filter(art => {
      // Kind filter
      if (selectedKind !== 'all' && (art.kind || 'file') !== selectedKind) {
        return false;
      }
      // Search filter
      if (searchQuery.trim()) {
        const q = searchQuery.toLowerCase().trim();
        const title = (art.title || '').toLowerCase();
        const path = (art.file_path || '').toLowerCase();
        const taskId = (art.task_id || '').toLowerCase();
        const sessId = (art.session_id || '').toLowerCase();
        const sessTitle = getSessionTitle(art.session_id).toLowerCase();
        return title.includes(q) || path.includes(q) || taskId.includes(q) || sessId.includes(q) || sessTitle.includes(q);
      }
      return true;
    });
  }, [effectiveArtifacts, selectedKind, searchQuery, getSessionTitle]);

  // Kind counts
  const kindCounts = useMemo(() => {
    const counts = { all: effectiveArtifacts.length, file: 0, diff: 0, report: 0 };
    for (const art of effectiveArtifacts) {
      const k = art.kind || 'file';
      if (counts[k] !== undefined) {
        counts[k]++;
      } else {
        counts[k] = 1;
      }
    }
    return counts;
  }, [effectiveArtifacts]);

  const getArtifactIcon = (art) => {
    if (art.kind === 'diff') return <GitCommit size={15} color="#c084fc" />;
    if (art.kind === 'report') return <FileText size={15} color="#38bdf8" />;
    const t = (art.title || '').toLowerCase();
    if (t.endsWith('.md')) return <FileText size={15} color="#60a5fa" />;
    if (t.endsWith('.json') || t.endsWith('.csv')) return <Database size={15} color="#f59e0b" />;
    if (/\.(png|jpg|jpeg|gif|webp|svg)$/.test(t)) return <ImageIcon size={15} color="#ec4899" />;
    return <FileCode size={15} color="#34d399" />;
  };

  const formatTimestamp = (ts) => {
    if (!ts) return '—';
    try {
      const date = new Date(ts);
      return date.toLocaleDateString(undefined, {
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit'
      });
    } catch {
      return '—';
    }
  };

  return (
    <div
      data-testid="global-artifacts-view"
      style={{
        flex: 1,
        display: 'flex',
        flexDirection: 'column',
        height: '100%',
        overflow: 'hidden',
        color: '#f0f0f5',
        position: 'relative'
      }}
    >
      {/* Header */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '20px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
          <div
            style={{
              width: '36px',
              height: '36px',
              borderRadius: '10px',
              background: 'linear-gradient(135deg, rgba(139, 92, 246, 0.3), rgba(99, 102, 241, 0.15))',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              border: '1px solid rgba(139, 92, 246, 0.3)'
            }}
          >
            <Layers size={18} color="#c4b5fd" />
          </div>
          <div>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
              <h1 style={{ color: '#fff', fontSize: '24px', fontWeight: 600, margin: 0 }}>Artifacts</h1>
              <span
                data-testid="artifacts-count-badge"
                style={{
                  background: 'rgba(139, 92, 246, 0.25)',
                  border: '1px solid rgba(139, 92, 246, 0.4)',
                  color: '#c4b5fd',
                  borderRadius: '999px',
                  padding: '2px 8px',
                  fontSize: '11px',
                  fontWeight: 600
                }}
              >
                {effectiveArtifacts.length}
              </span>
            </div>
            <p style={{ margin: 0, fontSize: '12px', color: '#94a3b8' }}>
              Cross-session deliverables, code files, diffs, and generated reports
            </p>
          </div>
        </div>

        {/* View mode toggle (Table / Grid) */}
        <div
          style={{
            display: 'flex',
            background: 'rgba(255, 255, 255, 0.05)',
            borderRadius: '8px',
            padding: '3px',
            border: '1px solid rgba(255, 255, 255, 0.1)'
          }}
        >
          <button
            type="button"
            data-testid="switch-view-table"
            onClick={() => setViewMode('table')}
            title="Table View"
            style={{
              background: viewMode === 'table' ? 'rgba(255, 255, 255, 0.12)' : 'transparent',
              border: 'none',
              color: viewMode === 'table' ? '#fff' : '#94a3b8',
              borderRadius: '6px',
              padding: '6px 10px',
              cursor: 'pointer',
              display: 'flex',
              alignItems: 'center',
              gap: '6px',
              fontSize: '12px',
              fontWeight: viewMode === 'table' ? 600 : 400,
              transition: 'all 0.2s'
            }}
          >
            <TableIcon size={14} />
            <span>Table</span>
          </button>
          <button
            type="button"
            data-testid="switch-view-grid"
            onClick={() => setViewMode('grid')}
            title="Grid View"
            style={{
              background: viewMode === 'grid' ? 'rgba(255, 255, 255, 0.12)' : 'transparent',
              border: 'none',
              color: viewMode === 'grid' ? '#fff' : '#94a3b8',
              borderRadius: '6px',
              padding: '6px 10px',
              cursor: 'pointer',
              display: 'flex',
              alignItems: 'center',
              gap: '6px',
              fontSize: '12px',
              fontWeight: viewMode === 'grid' ? 600 : 400,
              transition: 'all 0.2s'
            }}
          >
            <LayoutGrid size={14} />
            <span>Grid</span>
          </button>
        </div>
      </div>

      {/* Toolbar: Search input & Kind filter chips */}
      <div
        style={{
          display: 'flex',
          flexWrap: 'wrap',
          gap: '12px',
          alignItems: 'center',
          justifyContent: 'space-between',
          marginBottom: '16px'
        }}
      >
        {/* Search */}
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            background: 'rgba(0, 0, 0, 0.25)',
            border: '1px solid rgba(255, 255, 255, 0.1)',
            borderRadius: '8px',
            padding: '6px 12px',
            gap: '8px',
            width: '320px',
            maxWidth: '100%'
          }}
        >
          <Search size={15} color="#94a3b8" />
          <input
            type="text"
            data-testid="artifacts-search-input"
            placeholder="Search by title, path, session, task..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            style={{
              background: 'transparent',
              border: 'none',
              outline: 'none',
              color: '#fff',
              fontSize: '12px',
              width: '100%'
            }}
          />
          {searchQuery && (
            <button
              type="button"
              onClick={() => setSearchQuery('')}
              style={{
                background: 'transparent',
                border: 'none',
                color: '#94a3b8',
                cursor: 'pointer',
                padding: '2px',
                display: 'flex',
                alignItems: 'center'
              }}
            >
              <X size={13} />
            </button>
          )}
        </div>

        {/* Kind Filters */}
        <div style={{ display: 'flex', gap: '6px', alignItems: 'center' }}>
          {[
            { id: 'all', label: 'All', count: kindCounts.all },
            { id: 'file', label: 'Files', count: kindCounts.file || 0 },
            { id: 'diff', label: 'Diffs', count: kindCounts.diff || 0 },
            { id: 'report', label: 'Reports', count: kindCounts.report || 0 },
          ].map(k => {
            const isActive = selectedKind === k.id;
            return (
              <button
                key={k.id}
                type="button"
                data-testid={`filter-kind-${k.id}`}
                onClick={() => setSelectedKind(k.id)}
                style={{
                  background: isActive ? 'rgba(139, 92, 246, 0.22)' : 'rgba(255, 255, 255, 0.05)',
                  border: isActive ? '1px solid rgba(139, 92, 246, 0.45)' : '1px solid rgba(255, 255, 255, 0.08)',
                  color: isActive ? '#fff' : '#94a3b8',
                  borderRadius: '20px',
                  padding: '4px 10px',
                  cursor: 'pointer',
                  fontSize: '11px',
                  fontWeight: isActive ? 600 : 400,
                  display: 'flex',
                  alignItems: 'center',
                  gap: '6px',
                  transition: 'all 0.15s'
                }}
              >
                <span>{k.label}</span>
                <span
                  style={{
                    background: isActive ? 'rgba(139, 92, 246, 0.4)' : 'rgba(255, 255, 255, 0.1)',
                    padding: '1px 5px',
                    borderRadius: '10px',
                    fontSize: '10px'
                  }}
                >
                  {k.count}
                </span>
              </button>
            );
          })}
        </div>
      </div>

      {/* Main Content Area */}
      <div style={{ flex: 1, overflowY: 'auto' }} className="chat-scroll">
        {effectiveArtifacts.length === 0 ? (
          <div
            data-testid="empty-artifacts-state"
            style={{
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
              justifyContent: 'center',
              height: '320px',
              color: '#94a3b8',
              gap: '12px'
            }}
          >
            <div
              style={{
                width: '56px',
                height: '56px',
                borderRadius: '50%',
                background: 'rgba(255, 255, 255, 0.04)',
                border: '1px solid rgba(255, 255, 255, 0.08)',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center'
              }}
            >
              <Layers size={26} color="#64748b" />
            </div>
            <div style={{ fontSize: '15px', fontWeight: 500, color: '#e2e8f0' }}>No artifacts found</div>
            <div style={{ fontSize: '12px', color: '#64748b', maxWidth: '360px', textAlign: 'center' }}>
              Files, code changes, and deliverables produced by agents during conversation tasks will be tracked here.
            </div>
          </div>
        ) : filteredArtifacts.length === 0 ? (
          <div
            data-testid="empty-search-state"
            style={{
              display: 'flex',
              flexDirection: 'column',
              alignItems: 'center',
              justifyContent: 'center',
              height: '240px',
              color: '#94a3b8',
              gap: '10px'
            }}
          >
            <Search size={28} color="#64748b" />
            <div style={{ fontSize: '14px', fontWeight: 500, color: '#e2e8f0' }}>No artifacts found</div>
            <div style={{ fontSize: '12px', color: '#64748b' }}>
              No items match &quot;{searchQuery}&quot; with filter &quot;{selectedKind}&quot;.
            </div>
          </div>
        ) : viewMode === 'table' ? (
          /* Table View */
          <div
            data-testid="view-mode-table"
            style={{
              background: 'rgba(15, 17, 23, 0.5)',
              borderRadius: '10px',
              border: '1px solid rgba(255, 255, 255, 0.08)',
              overflow: 'hidden'
            }}
          >
            <table style={{ width: '100%', borderCollapse: 'collapse', textAlign: 'left', fontSize: '12px' }}>
              <thead>
                <tr style={{ borderBottom: '1px solid rgba(255, 255, 255, 0.08)', background: 'rgba(255, 255, 255, 0.02)' }}>
                  <th style={{ padding: '12px 16px', color: '#94a3b8', fontWeight: 600 }}>Artifact</th>
                  <th style={{ padding: '12px 16px', color: '#94a3b8', fontWeight: 600 }}>Kind</th>
                  <th style={{ padding: '12px 16px', color: '#94a3b8', fontWeight: 600 }}>Session</th>
                  <th style={{ padding: '12px 16px', color: '#94a3b8', fontWeight: 600 }}>Task</th>
                  <th style={{ padding: '12px 16px', color: '#94a3b8', fontWeight: 600 }}>Created</th>
                  <th style={{ padding: '12px 16px', color: '#94a3b8', fontWeight: 600, textAlign: 'right' }}>Action</th>
                </tr>
              </thead>
              <tbody>
                {filteredArtifacts.map(art => {
                  const sessTitle = getSessionTitle(art.session_id);
                  return (
                    <tr
                      key={art.id}
                      data-testid={`artifact-item-${art.id}`}
                      onClick={() => onNavigateToSession(art)}
                      style={{
                        borderBottom: '1px solid rgba(255, 255, 255, 0.04)',
                        cursor: 'pointer',
                        transition: 'background 0.15s'
                      }}
                      onMouseEnter={(e) => { e.currentTarget.style.background = 'rgba(255, 255, 255, 0.04)'; }}
                      onMouseLeave={(e) => { e.currentTarget.style.background = 'transparent'; }}
                    >
                      {/* Title & File path */}
                      <td style={{ padding: '12px 16px', maxWidth: '300px' }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                          {getArtifactIcon(art)}
                          <div style={{ overflow: 'hidden' }}>
                            <div
                              data-testid={`artifact-title-${art.id}`}
                              style={{
                                color: '#f8fafc',
                                fontWeight: 500,
                                whiteSpace: 'nowrap',
                                overflow: 'hidden',
                                textOverflow: 'ellipsis'
                              }}
                            >
                              {art.title}
                            </div>
                            {art.file_path && (
                              <div
                                style={{
                                  fontSize: '11px',
                                  color: '#64748b',
                                  whiteSpace: 'nowrap',
                                  overflow: 'hidden',
                                  textOverflow: 'ellipsis'
                                }}
                              >
                                {art.file_path}
                              </div>
                            )}
                          </div>
                        </div>
                      </td>

                      {/* Kind Badge */}
                      <td style={{ padding: '12px 16px' }}>
                        <span
                          style={{
                            fontSize: '10px',
                            fontWeight: 600,
                            padding: '2px 7px',
                            borderRadius: '999px',
                            background:
                              art.kind === 'diff'
                                ? 'rgba(192, 132, 252, 0.18)'
                                : art.kind === 'report'
                                ? 'rgba(56, 189, 248, 0.18)'
                                : 'rgba(255, 255, 255, 0.08)',
                            color:
                              art.kind === 'diff'
                                ? '#c084fc'
                                : art.kind === 'report'
                                ? '#38bdf8'
                                : '#94a3b8',
                            textTransform: 'uppercase'
                          }}
                        >
                          {art.kind || 'file'}
                        </span>
                      </td>

                      {/* Session Attribution */}
                      <td style={{ padding: '12px 16px' }}>
                        <div
                          data-testid={`artifact-session-${art.id}`}
                          style={{
                            display: 'inline-flex',
                            alignItems: 'center',
                            gap: '6px',
                            padding: '2px 8px',
                            borderRadius: '6px',
                            background: 'rgba(255, 255, 255, 0.05)',
                            color: '#cbd5e1',
                            fontSize: '11px',
                            maxWidth: '180px',
                            whiteSpace: 'nowrap',
                            overflow: 'hidden',
                            textOverflow: 'ellipsis'
                          }}
                        >
                          <FolderGit2 size={12} color="#a78bfa" />
                          <span>{sessTitle}</span>
                        </div>
                      </td>

                      {/* Task Attribution */}
                      <td style={{ padding: '12px 16px' }}>
                        <div
                          data-testid={`artifact-task-${art.id}`}
                          style={{
                            color: '#94a3b8',
                            fontSize: '11px',
                            fontFamily: 'monospace'
                          }}
                        >
                          {art.task_id}
                        </div>
                      </td>

                      {/* Created Time */}
                      <td style={{ padding: '12px 16px', color: '#64748b', fontSize: '11px', whiteSpace: 'nowrap' }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '4px' }}>
                          <Clock size={11} />
                          <span>{formatTimestamp(art.created_at)}</span>
                        </div>
                      </td>

                      {/* Jump to Session Button */}
                      <td style={{ padding: '12px 16px', textAlign: 'right' }}>
                        <button
                          type="button"
                          data-testid={`jump-to-session-${art.id}`}
                          onClick={(e) => {
                            e.stopPropagation();
                            onNavigateToSession(art);
                          }}
                          title="Open artifact in originating session canvas"
                          style={{
                            background: 'rgba(139, 92, 246, 0.15)',
                            border: '1px solid rgba(139, 92, 246, 0.3)',
                            borderRadius: '6px',
                            color: '#c4b5fd',
                            padding: '4px 10px',
                            fontSize: '11px',
                            fontWeight: 500,
                            cursor: 'pointer',
                            display: 'inline-flex',
                            alignItems: 'center',
                            gap: '5px',
                            transition: 'all 0.15s'
                          }}
                          onMouseEnter={(e) => { e.currentTarget.style.background = 'rgba(139, 92, 246, 0.3)'; }}
                          onMouseLeave={(e) => { e.currentTarget.style.background = 'rgba(139, 92, 246, 0.15)'; }}
                        >
                          <span>Open in Canvas</span>
                          <ExternalLink size={12} />
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          /* Grid View */
          <div
            data-testid="view-mode-grid"
            style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))',
              gap: '14px'
            }}
          >
            {filteredArtifacts.map(art => {
              const sessTitle = getSessionTitle(art.session_id);
              return (
                <div
                  key={art.id}
                  data-testid={`artifact-item-${art.id}`}
                  onClick={() => onNavigateToSession(art)}
                  style={{
                    background: 'rgba(20, 22, 30, 0.6)',
                    border: '1px solid rgba(255, 255, 255, 0.08)',
                    borderRadius: '10px',
                    padding: '14px',
                    display: 'flex',
                    flexDirection: 'column',
                    justifyContent: 'space-between',
                    cursor: 'pointer',
                    transition: 'all 0.2s',
                    position: 'relative'
                  }}
                  onMouseEnter={(e) => {
                    e.currentTarget.style.borderColor = 'rgba(139, 92, 246, 0.4)';
                    e.currentTarget.style.background = 'rgba(25, 28, 38, 0.8)';
                  }}
                  onMouseLeave={(e) => {
                    e.currentTarget.style.borderColor = 'rgba(255, 255, 255, 0.08)';
                    e.currentTarget.style.background = 'rgba(20, 22, 30, 0.6)';
                  }}
                >
                  <div>
                    {/* Header: Icon + Title + Kind */}
                    <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: '8px', marginBottom: '8px' }}>
                      <div style={{ display: 'flex', alignItems: 'center', gap: '8px', minWidth: 0 }}>
                        {getArtifactIcon(art)}
                        <span
                          data-testid={`artifact-title-${art.id}`}
                          style={{
                            color: '#fff',
                            fontSize: '13px',
                            fontWeight: 600,
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
                          fontSize: '9px',
                          fontWeight: 600,
                          padding: '2px 6px',
                          borderRadius: '999px',
                          background:
                            art.kind === 'diff'
                              ? 'rgba(192, 132, 252, 0.18)'
                              : art.kind === 'report'
                              ? 'rgba(56, 189, 248, 0.18)'
                              : 'rgba(255, 255, 255, 0.08)',
                          color:
                            art.kind === 'diff'
                              ? '#c084fc'
                              : art.kind === 'report'
                              ? '#38bdf8'
                              : '#94a3b8',
                          textTransform: 'uppercase',
                          flexShrink: 0
                        }}
                      >
                        {art.kind || 'file'}
                      </span>
                    </div>

                    {/* File Path */}
                    {art.file_path && (
                      <div
                        style={{
                          fontSize: '11px',
                          color: '#64748b',
                          whiteSpace: 'nowrap',
                          overflow: 'hidden',
                          textOverflow: 'ellipsis',
                          marginBottom: '12px'
                        }}
                      >
                        {art.file_path}
                      </div>
                    )}

                    {/* Attribution pills */}
                    <div style={{ display: 'flex', flexDirection: 'column', gap: '4px', marginBottom: '12px' }}>
                      <div
                        data-testid={`artifact-session-${art.id}`}
                        style={{
                          display: 'inline-flex',
                          alignItems: 'center',
                          gap: '6px',
                          fontSize: '11px',
                          color: '#cbd5e1'
                        }}
                      >
                        <FolderGit2 size={12} color="#a78bfa" />
                        <span style={{ whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                          {sessTitle}
                        </span>
                      </div>
                      <div
                        data-testid={`artifact-task-${art.id}`}
                        style={{
                          fontSize: '10px',
                          color: '#94a3b8',
                          fontFamily: 'monospace'
                        }}
                      >
                        Task: {art.task_id}
                      </div>
                    </div>
                  </div>

                  {/* Card Footer: Timestamp + Jump Button */}
                  <div
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'space-between',
                      borderTop: '1px solid rgba(255, 255, 255, 0.06)',
                      paddingTop: '8px',
                      marginTop: '4px'
                    }}
                  >
                    <span style={{ fontSize: '10px', color: '#64748b' }}>
                      {formatTimestamp(art.created_at)}
                    </span>
                    <button
                      type="button"
                      data-testid={`jump-to-session-${art.id}`}
                      onClick={(e) => {
                        e.stopPropagation();
                        onNavigateToSession(art);
                      }}
                      title="Open artifact in originating session canvas"
                      style={{
                        background: 'transparent',
                        border: 'none',
                        color: '#a78bfa',
                        cursor: 'pointer',
                        padding: '4px',
                        display: 'flex',
                        alignItems: 'center',
                        gap: '4px',
                        fontSize: '11px',
                        fontWeight: 500
                      }}
                    >
                      <span>Canvas</span>
                      <ExternalLink size={12} />
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
