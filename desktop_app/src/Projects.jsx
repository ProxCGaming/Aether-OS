import React, { useState, useEffect } from 'react';
import { FolderPlus, Folder, Trash2, Search } from 'lucide-react';
import './chat.css';

export default function Projects({ wsRef }) {
  const [workspaces, setWorkspaces] = useState([]);
  // Per-workspace sessions: the reply-level count of sessions filed
  // under no workspace, and the row currently armed for removal
  // (the Q7 confirmation shows before any REMOVE is sent).
  const [unboundSessionCount, setUnboundSessionCount] = useState(null);
  const [confirmRemovePath, setConfirmRemovePath] = useState(null);

  // Fetch workspace list from engine on mount
  useEffect(() => {
    if (!wsRef?.current) return;

    const handleMessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'WORKSPACE_LIST_RESPONSE') {
          setWorkspaces(data.payload.workspaces || []);
          if (typeof data.payload.unbound_session_count === 'number') {
            setUnboundSessionCount(data.payload.unbound_session_count);
          }
        } else if (data.type === 'WORKSPACE_ADD_RESPONSE') {
          if (data.payload.success && data.payload.workspace) {
            setWorkspaces(prev => [...prev, data.payload.workspace]);
          }
        } else if (data.type === 'WORKSPACE_SET_ACTIVE_RESPONSE') {
          if (data.payload.success && wsRef.current) {
            wsRef.current.send(JSON.stringify({
              type: 'WORKSPACE_LIST_REQUEST',
              schema_version: 1,
              request_id: Date.now().toString(),
              payload: {}
            }));
          }
        } else if (data.type === 'WORKSPACE_REMOVE_RESPONSE') {
          if (data.payload.success) {
            setConfirmRemovePath(null);
            // Q7: the sessions the removed root owned are unbound
            // now — the engine reports how many in the reply, so
            // the No workspace line stays true without a refetch.
            if (typeof data.payload.unbound_sessions === 'number') {
              setUnboundSessionCount(prev => (prev ?? 0) + data.payload.unbound_sessions);
            }
            setWorkspaces(prev => {
              const remaining = prev.filter(w => w.path !== data.payload.path);
              // E6-F11: the first root is the active workspace (the
              // engine's LIST rule). Removing the active row promotes
              // its successor — recompute the badge from the reply
              // instead of leaving every row unmarked until a refetch.
              const removedWasActive = prev.some(w => w.path === data.payload.path && w.is_active);
              if (removedWasActive && remaining.length > 0) {
                return remaining.map((w, idx) => (idx === 0 ? { ...w, is_active: true } : w));
              }
              return remaining;
            });
          }
        }
      } catch (e) {
        // ignore parse errors
      }
    };

    wsRef.current.addEventListener('message', handleMessage);

    // Request initial list
    wsRef.current.send(JSON.stringify({
      type: 'WORKSPACE_LIST_REQUEST',
      schema_version: 1,
      request_id: Date.now().toString(),
      payload: {}
    }));

    return () => {
      if (wsRef.current) wsRef.current.removeEventListener('message', handleMessage);
    };
  }, [wsRef]);

  const handleAddWorkspace = async () => {
    // Real folder picker via Electron IPC
    if (!window.electronAPI?.showOpenDialog) return;
    try {
      const selectedPath = await window.electronAPI.showOpenDialog({
        properties: ['openDirectory'],
        title: 'Select Workspace Folder'
      });
      if (selectedPath && wsRef?.current) {
        wsRef.current.send(JSON.stringify({
          type: 'WORKSPACE_ADD_REQUEST',
          schema_version: 1,
          request_id: Date.now().toString(),
          payload: { path: selectedPath }
        }));
      }
    } catch (e) {
      console.error('Failed to open folder picker:', e);
    }
  };

  const handleSetActive = (path) => {
    if (wsRef?.current) {
      wsRef.current.send(JSON.stringify({
        type: 'WORKSPACE_SET_ACTIVE_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: { path }
      }));
    }
  };

  const handleRemoveWorkspace = (path) => {
    if (wsRef?.current) {
      wsRef.current.send(JSON.stringify({
        type: 'WORKSPACE_REMOVE_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: { path }
      }));
    }
  };

  return (
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', color: '#f0f0f5', padding: '16px', overflowY: 'auto' }} className="chat-scroll">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: '32px' }}>
        <div>
          <h1 style={{ fontSize: '28px', margin: '0 0 8px 0', fontWeight: 600 }}>Workspaces</h1>
          <p style={{ color: '#9090a0', margin: 0, fontSize: '14px' }}>Add local folders for Aether to index and provide project-specific context.</p>
        </div>
        <button 
          onClick={handleAddWorkspace}
          style={{ background: 'var(--accent)', color: '#fff', border: 'none', padding: '10px 20px', borderRadius: '12px', display: 'flex', alignItems: 'center', gap: '8px', cursor: 'pointer', fontWeight: 500 }}
        >
          <FolderPlus size={18} /> Add Folder
        </button>
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
        {workspaces.map(ws => (
          <div key={ws.path} style={{ background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.08)', borderRadius: '16px', padding: '20px', display: 'flex', justifyContent: 'space-between', alignItems: 'center', opacity: ws.exists === false ? 0.55 : 1 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '16px' }}>
              <div style={{ background: 'rgba(59, 130, 246, 0.2)', padding: '12px', borderRadius: '12px', color: '#60a5fa' }}>
                <Folder size={24} />
              </div>
              <div>
                <h3 style={{ fontSize: '16px', margin: '0 0 4px 0' }}>{ws.name}</h3>
                <p style={{ color: '#9090a0', margin: 0, fontSize: '13px', fontFamily: 'monospace' }}>{ws.path}</p>
                {typeof ws.session_count === 'number' && (
                  <p style={{ color: '#a78bfa', margin: '4px 0 0 0', fontSize: '12px' }}>{ws.session_count} session{ws.session_count === 1 ? '' : 's'}</p>
                )}
              </div>
            </div>
            <div style={{ display: 'flex', gap: '12px', alignItems: 'center' }}>
              {confirmRemovePath === ws.path ? (
                <>
                  <span style={{ color: '#fca5a5', fontSize: '12px', maxWidth: '300px' }}>
                    {ws.session_count ?? 0} session{(ws.session_count ?? 0) === 1 ? '' : 's'} will move to No workspace — nothing is deleted.
                  </span>
                  <button
                    onClick={() => handleRemoveWorkspace(ws.path)}
                    style={{ background: 'rgba(239, 68, 68, 0.85)', color: '#fff', border: 'none', padding: '6px 12px', borderRadius: '8px', cursor: 'pointer', fontSize: '12px', fontWeight: 500 }}
                  >
                    Remove
                  </button>
                  <button
                    onClick={() => setConfirmRemovePath(null)}
                    style={{ background: 'rgba(255,255,255,0.06)', color: '#e2e8f0', border: '1px solid rgba(255,255,255,0.15)', padding: '6px 12px', borderRadius: '8px', cursor: 'pointer', fontSize: '12px' }}
                  >
                    Cancel
                  </button>
                </>
              ) : (
              <>
              {ws.is_active && (
                <div style={{ color: '#a78bfa', fontSize: '12px', border: '1px solid rgba(167,139,250,0.5)', borderRadius: '8px', padding: '3px 10px' }}>Active</div>
              )}
              {ws.exists === false && (
                <div style={{ color: '#f59e0b', fontSize: '12px' }}>Folder missing</div>
              )}
              {!ws.is_active && ws.exists !== false && (
                <button
                  onClick={() => handleSetActive(ws.path)}
                  style={{ background: 'rgba(255,255,255,0.06)', color: '#e2e8f0', border: '1px solid rgba(255,255,255,0.15)', padding: '6px 12px', borderRadius: '8px', cursor: 'pointer', fontSize: '12px' }}
                >
                  Set active
                </button>
              )}
              {/* Only show Indexed badge if the engine reports is_indexed === true */}
              {ws.is_indexed && (
                <div style={{ color: '#10b981', fontSize: '12px', display: 'flex', alignItems: 'center', gap: '4px' }}>
                  <Search size={14} /> Indexed
                </div>
              )}
              <button 
                onClick={() => setConfirmRemovePath(ws.path)}
                title="Remove workspace"
                aria-label={`Remove workspace ${ws.path}`}
                style={{ background: 'rgba(239, 68, 68, 0.1)', color: '#ef4444', border: 'none', padding: '8px', borderRadius: '8px', cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'center' }}
              >
                <Trash2 size={16} />
              </button>
              </>
              )}
            </div>
          </div>
        ))}
        {unboundSessionCount !== null && (
          <div style={{ background: 'rgba(255,255,255,0.02)', border: '1px dashed rgba(255,255,255,0.12)', borderRadius: '16px', padding: '16px 20px', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <div>
              <h3 style={{ fontSize: '14px', margin: '0 0 4px 0', color: '#e2e8f0' }}>No workspace</h3>
              <p style={{ color: '#9090a0', margin: 0, fontSize: '13px' }}>Sessions not filed under any workspace.</p>
            </div>
            <div style={{ color: '#a78bfa', fontSize: '13px' }}>{unboundSessionCount} unbound session{unboundSessionCount === 1 ? '' : 's'}</div>
          </div>
        )}
        {workspaces.length === 0 && (
          <div style={{ textAlign: 'center', color: '#9090a0', padding: '40px', background: 'rgba(255,255,255,0.02)', borderRadius: '16px' }}>
            No workspaces added. Add a local folder to get started.
          </div>
        )}
      </div>
    </div>
  );
}
