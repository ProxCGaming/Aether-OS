import React, { useState, useEffect } from 'react';
import { FolderPlus, Folder, Trash2, Search } from 'lucide-react';
import './chat.css';

export default function Projects({ wsRef }) {
  const [workspaces, setWorkspaces] = useState([]);

  // Fetch workspace list from engine on mount
  useEffect(() => {
    if (!wsRef?.current) return;

    const handleMessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'WORKSPACE_LIST_RESPONSE') {
          setWorkspaces(data.payload.workspaces || []);
        } else if (data.type === 'WORKSPACE_ADD_RESPONSE') {
          if (data.payload.success && data.payload.workspace) {
            setWorkspaces(prev => [...prev, data.payload.workspace]);
          }
        } else if (data.type === 'WORKSPACE_REMOVE_RESPONSE') {
          if (data.payload.success) {
            setWorkspaces(prev => prev.filter(w => w.path !== data.payload.path));
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
          <div key={ws.path} style={{ background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.08)', borderRadius: '16px', padding: '20px', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: '16px' }}>
              <div style={{ background: 'rgba(59, 130, 246, 0.2)', padding: '12px', borderRadius: '12px', color: '#60a5fa' }}>
                <Folder size={24} />
              </div>
              <div>
                <h3 style={{ fontSize: '16px', margin: '0 0 4px 0' }}>{ws.name}</h3>
                <p style={{ color: '#9090a0', margin: 0, fontSize: '13px', fontFamily: 'monospace' }}>{ws.path}</p>
              </div>
            </div>
            <div style={{ display: 'flex', gap: '12px', alignItems: 'center' }}>
              {/* Only show Indexed badge if the engine reports is_indexed === true */}
              {ws.is_indexed && (
                <div style={{ color: '#10b981', fontSize: '12px', display: 'flex', alignItems: 'center', gap: '4px' }}>
                  <Search size={14} /> Indexed
                </div>
              )}
              <button 
                onClick={() => handleRemoveWorkspace(ws.path)}
                style={{ background: 'rgba(239, 68, 68, 0.1)', color: '#ef4444', border: 'none', padding: '8px', borderRadius: '8px', cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'center' }}
              >
                <Trash2 size={16} />
              </button>
            </div>
          </div>
        ))}
        {workspaces.length === 0 && (
          <div style={{ textAlign: 'center', color: '#9090a0', padding: '40px', background: 'rgba(255,255,255,0.02)', borderRadius: '16px' }}>
            No workspaces added. Add a local folder to get started.
          </div>
        )}
      </div>
    </div>
  );
}
