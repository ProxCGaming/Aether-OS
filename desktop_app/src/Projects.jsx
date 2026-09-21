import React, { useState } from 'react';
import { FolderPlus, Folder, Trash2, Search } from 'lucide-react';
import './chat.css';

export default function Projects() {
  const [workspaces, setWorkspaces] = useState([
    { id: 1, name: 'Aether-OS Backend', path: 'e:/JA' }
  ]);

  const handleAddWorkspace = () => {
    // In a real app, this would use window.electronAPI.showOpenDialog()
    // and save to backend.
    setWorkspaces([...workspaces, { id: Date.now(), name: 'New Project', path: '/path/to/project' }]);
  };

  const handleRemoveWorkspace = (id) => {
    setWorkspaces(workspaces.filter(w => w.id !== id));
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
          <div key={ws.id} style={{ background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.08)', borderRadius: '16px', padding: '20px', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
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
              <div style={{ color: '#10b981', fontSize: '12px', display: 'flex', alignItems: 'center', gap: '4px' }}>
                <Search size={14} /> Indexed
              </div>
              <button 
                onClick={() => handleRemoveWorkspace(ws.id)}
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
