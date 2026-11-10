import React, { useState, useEffect } from 'react';
import { FolderPlus, Folder, Trash2, Search } from 'lucide-react';
import './chat.css';
import LoadStateRow from './components/Settings/LoadStateRow';

export default function Projects({ wsRef, isConnected = null, reconnectCount = 0 }) {
  const [workspaces, setWorkspaces] = useState([]);
  // RUX-C-08: the workspace list is a load state until the first
  // WORKSPACE_LIST_RESPONSE lands — "No workspaces added" before
  // that was the panel's initial state impersonating the engine's
  // answer, indistinguishable from a genuinely empty account.
  const [listLoad, setListLoad] = useState('loading');
  // RUX-C-08: add / set-active / remove replies used to be applied
  // only on success and silently dropped on failure, leaving the
  // user unable to tell whether anything happened. A refused action
  // is now a visible, named error until the next attempt or success.
  const [actionError, setActionError] = useState(null);
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
          setListLoad('ready');
          setWorkspaces(data.payload.workspaces || []);
          if (typeof data.payload.unbound_session_count === 'number') {
            setUnboundSessionCount(data.payload.unbound_session_count);
          }
        } else if (data.type === 'WORKSPACE_ADD_RESPONSE') {
          if (data.payload.success && data.payload.workspace) {
            setActionError(null);
            setWorkspaces(prev => [...prev, data.payload.workspace]);
          } else if (data.payload.success === false) {
            setActionError(data.payload.error || 'The workspace could not be added.');
          }
        } else if (data.type === 'WORKSPACE_SET_ACTIVE_RESPONSE') {
          if (data.payload.success === false) {
            setActionError(data.payload.error || 'That workspace could not be made active.');
          } else if (data.payload.success && wsRef.current) {
            wsRef.current.send(JSON.stringify({
              type: 'WORKSPACE_LIST_REQUEST',
              schema_version: 1,
              request_id: Date.now().toString(),
              payload: {}
            }));
          }
        } else if (data.type === 'WORKSPACE_REMOVE_RESPONSE') {
          if (data.payload.success === false) {
            setActionError(data.payload.error || 'The workspace could not be removed.');
          } else if (data.payload.success) {
            setActionError(null);
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
    // D2-09: reconnectCount (the socket layer's recovery counter)
    // is a dep — a list response lost across a disconnect is
    // re-fetched on reconnect instead of leaving the surface stale
    // for the whole session (the Speech/Usage RUX-A-17 pattern).
  }, [wsRef, reconnectCount]);

  const handleAddWorkspace = async () => {
    // Real folder picker via Electron IPC
    // D2-23: in a mount without the preload API this used to
    // return silently — the surface's primary action was a dead
    // click. Name the failure on the panel's action-error surface
    // instead (a rejected picker promise reports the same way).
    if (!window.electronAPI?.showOpenDialog) {
      setActionError('The folder picker is not available in this window — adding a workspace folder needs the desktop app.');
      return;
    }
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
      setActionError('The folder picker could not be opened.');
    }
  };

  const handleSetActive = (path) => {
    setActionError(null);
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
    setActionError(null);
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
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', color: 'var(--text-primary)', padding: '16px', overflowY: 'auto' }} className="chat-scroll">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: '32px' }}>
        <div>
          <h1 style={{ fontSize: '28px', margin: '0 0 8px 0', fontWeight: 600 }}>Workspaces</h1>
          <p style={{ color: 'var(--text-muted)', margin: 0, fontSize: '14px' }}>Add local folders for Aether to index and provide project-specific context.</p>
        </div>
        <button
          onClick={handleAddWorkspace}
          style={{ background: 'var(--accent)', color: 'var(--accent-on)', border: 'none', padding: '10px 20px', borderRadius: 'var(--radius-lg)', display: 'flex', alignItems: 'center', gap: '8px', cursor: 'pointer', fontWeight: 500 }}
        >
          <FolderPlus size={18} /> Add Folder
        </button>
      </div>

      {actionError && (
        <div
          data-testid="projects-action-error"
          style={{ background: 'rgba(201, 42, 42, 0.08)', border: '1px solid rgba(201, 42, 42, 0.3)', borderRadius: 'var(--radius-lg)', padding: '12px 16px', fontSize: '13px', color: 'var(--danger)', marginBottom: '16px' }}
        >
          {actionError}
        </div>
      )}

      <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
        {listLoad !== 'ready' && workspaces.length === 0 && (
          <LoadStateRow
            loadState={listLoad}
            isConnected={isConnected}
            testId="projects-list-load-state"
            loadingText="Loading workspaces…"
            offlineText="Waiting for the engine — your workspaces will appear here once it connects."
            errorText="Workspaces could not be loaded."
          />
        )}
        {workspaces.map(ws => (
          <div key={ws.path} style={{ background: 'var(--bg-raised)', border: '1px solid var(--border-default)', borderRadius: 'var(--radius-lg)', padding: '20px', display: 'flex', justifyContent: 'space-between', alignItems: 'center', opacity: ws.exists === false ? 0.55 : 1 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '16px' }}>
              <div style={{ background: 'var(--bg-hover)', padding: '12px', borderRadius: 'var(--radius-lg)', color: 'var(--accent)' }}>
                <Folder size={24} />
              </div>
              <div>
                <h3 style={{ fontSize: '16px', margin: '0 0 4px 0', color: 'var(--text-primary)' }}>{ws.name}</h3>
                <p style={{ color: 'var(--text-muted)', margin: 0, fontSize: '13px', fontFamily: 'var(--font-mono)' }}>{ws.path}</p>
                {typeof ws.session_count === 'number' && (
                  <p style={{ color: 'var(--text-secondary)', margin: '4px 0 0 0', fontSize: '12px' }}>{ws.session_count} session{ws.session_count === 1 ? '' : 's'}</p>
                )}
              </div>
            </div>
            <div style={{ display: 'flex', gap: '12px', alignItems: 'center' }}>
              {confirmRemovePath === ws.path ? (
                <>
                  <span style={{ color: 'var(--danger)', fontSize: '12px', maxWidth: '300px' }}>
                    {/* D2-22: the count is stated only when the
                        engine actually reported one (the list row
                        below guards the same way) — an unreported
                        count must not be confirmed as "0 sessions". */}
                    {typeof ws.session_count === 'number'
                      ? `${ws.session_count} session${ws.session_count === 1 ? '' : 's'} will move to No workspace — nothing is deleted.`
                      : 'Sessions filed here will move to No workspace — nothing is deleted.'}
                  </span>
                  <button
                    onClick={() => handleRemoveWorkspace(ws.path)}
                    style={{ background: 'var(--danger)', color: 'var(--text-primary)', border: 'none', padding: '6px 12px', borderRadius: 'var(--radius-md)', cursor: 'pointer', fontSize: '12px', fontWeight: 500 }}
                  >
                    Remove
                  </button>
                  <button
                    onClick={() => setConfirmRemovePath(null)}
                    style={{ background: 'var(--bg-hover)', color: 'var(--text-primary)', border: '1px solid var(--border-strong)', padding: '6px 12px', borderRadius: 'var(--radius-md)', cursor: 'pointer', fontSize: '12px' }}
                  >
                    Cancel
                  </button>
                </>
              ) : (
              <>
              {ws.is_active && (
                <div style={{ color: 'var(--accent)', fontSize: '12px', border: '1px solid rgba(245, 185, 66, 0.5)', borderRadius: 'var(--radius-md)', padding: '3px 10px' }}>Active</div>
              )}
              {ws.exists === false && (
                <div style={{ color: 'var(--accent)', fontSize: '12px' }}>Folder missing</div>
              )}
              {!ws.is_active && ws.exists !== false && (
                <button
                  onClick={() => handleSetActive(ws.path)}
                  style={{ background: 'var(--bg-hover)', color: 'var(--text-primary)', border: '1px solid var(--border-strong)', padding: '6px 12px', borderRadius: 'var(--radius-md)', cursor: 'pointer', fontSize: '12px' }}
                >
                  Set active
                </button>
              )}
              {/* Only show Indexed badge if the engine reports is_indexed === true */}
              {ws.is_indexed && (
                <div style={{ color: 'var(--success)', fontSize: '12px', display: 'flex', alignItems: 'center', gap: '4px' }}>
                  <Search size={14} /> Indexed
                </div>
              )}
              <button 
                onClick={() => setConfirmRemovePath(ws.path)}
                title="Remove workspace"
                aria-label={`Remove workspace ${ws.path}`}
                style={{ background: 'rgba(201, 42, 42, 0.12)', color: 'var(--danger)', border: 'none', padding: '8px', borderRadius: 'var(--radius-md)', cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'center' }}
              >
                <Trash2 size={16} />
              </button>
              </>
              )}
            </div>
          </div>
        ))}
        {unboundSessionCount !== null && (
          <div style={{ background: 'var(--bg-raised)', border: '1px dashed var(--border-strong)', borderRadius: 'var(--radius-lg)', padding: '16px 20px', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <div>
              <h3 style={{ fontSize: '14px', margin: '0 0 4px 0', color: 'var(--text-primary)' }}>No workspace</h3>
              <p style={{ color: 'var(--text-muted)', margin: 0, fontSize: '13px' }}>Sessions not filed under any workspace.</p>
            </div>
            <div style={{ color: 'var(--text-secondary)', fontSize: '13px' }}>{unboundSessionCount} unbound session{unboundSessionCount === 1 ? '' : 's'}</div>
          </div>
        )}
        {listLoad === 'ready' && workspaces.length === 0 && (
          <div style={{ textAlign: 'center', color: 'var(--text-muted)', padding: '40px', background: 'var(--bg-raised)', borderRadius: 'var(--radius-lg)' }}>
            No workspaces added. Add a local folder to get started.
          </div>
        )}
      </div>
    </div>
  );
}
