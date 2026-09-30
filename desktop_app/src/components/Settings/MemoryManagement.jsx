import React, { useState, useEffect } from 'react';
import { Trash2 } from 'lucide-react';
import { useAetherSocket } from '../../contexts/SocketContext';

export default function MemoryManagement() {
  const { wsRef, sendMessage } = useAetherSocket();
  const [memoryTab, setMemoryTab] = useState('episodic');
  const [episodes, setEpisodes] = useState([]);
  const [facts, setFacts] = useState([]);
  const [selectedEpisode, setSelectedEpisode] = useState(null);

  useEffect(() => {
    // Initial fetch when component mounts or memory tab changes
    if (memoryTab === 'episodic') {
      sendMessage('MEMORY_EPISODES_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: {} });
    } else if (memoryTab === 'kg') {
      sendMessage('MEMORY_GRAPH_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: {} });
    }

    const handleMessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'MEMORY_EPISODES_RESPONSE' || data.type === 'MEMORY_EPISODE_DELETE_RESPONSE') {
          setEpisodes(data.payload.episodes || []);
        } else if (data.type === 'MEMORY_EPISODES_CLEAR_RESPONSE') {
          setEpisodes([]);
          setSelectedEpisode(null);
        } else if (data.type === 'MEMORY_GRAPH_RESPONSE' || data.type === 'MEMORY_FACT_DELETE_RESPONSE' || data.type === 'MEMORY_ENTITY_DELETE_RESPONSE') {
          setFacts(data.payload.facts || []);
        } else if (data.type === 'MEMORY_GRAPH_CLEAR_RESPONSE') {
          setFacts([]);
        }
      } catch (e) {
        // ignore
      }
    };
    
    if (wsRef.current) {
      wsRef.current.addEventListener('message', handleMessage);
    }
    return () => {
      if (wsRef.current) {
        wsRef.current.removeEventListener('message', handleMessage);
      }
    };
  }, [wsRef, sendMessage, memoryTab]);

  const handleDeleteEpisode = (taskId) => {
    if (!taskId) return;
    sendMessage('MEMORY_EPISODE_DELETE_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: { task_id: taskId } });
    setEpisodes(prev => prev.filter(ep => ep.task_id !== taskId));
    if (selectedEpisode?.task_id === taskId) setSelectedEpisode(null);
  };

  const handleClearAllEpisodes = () => {
    if (window.confirm("Are you sure you want to clear all stored episodic memories?")) {
      sendMessage('MEMORY_EPISODES_CLEAR_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: {} });
      setEpisodes([]);
      setSelectedEpisode(null);
    }
  };

  const handleDeleteEntity = (entityName) => {
    if (!entityName) return;
    if (window.confirm(`Delete all facts associated with "${entityName}"?`)) {
      sendMessage('MEMORY_ENTITY_DELETE_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: { entity_name: entityName } });
      setFacts(prev => prev.filter(f => f.entity_name !== entityName));
    }
  };

  const handleClearAllFacts = () => {
    if (window.confirm("Are you sure you want to clear all facts from the Knowledge Graph?")) {
      sendMessage('MEMORY_GRAPH_CLEAR_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: {} });
      setFacts([]);
    }
  };

  return (
    <div style={{ color: '#f0f0f5' }}>
      <h2 style={{ fontSize: '24px', marginBottom: '8px', fontWeight: 500 }}>Memory & Storage</h2>
      <p style={{ color: '#9090a0', fontSize: '14px', marginBottom: '24px' }}>Manage the internal knowledge graph and episodic memory.</p>
      
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '24px' }}>
        <div style={{ display: 'flex', background: 'rgba(255,255,255,0.05)', padding: '4px', borderRadius: '12px', width: 'fit-content' }}>
            <button 
              onClick={() => setMemoryTab('episodic')}
              style={{ background: memoryTab === 'episodic' ? 'rgba(124,58,237,0.4)' : 'transparent', color: '#fff', border: 'none', padding: '8px 24px', borderRadius: '8px', cursor: 'pointer', transition: 'all 0.2s' }}>
              Episodic Memory
            </button>
            <button 
              onClick={() => setMemoryTab('kg')}
              style={{ background: memoryTab === 'kg' ? 'rgba(124,58,237,0.4)' : 'transparent', color: '#fff', border: 'none', padding: '8px 24px', borderRadius: '8px', cursor: 'pointer', transition: 'all 0.2s' }}>
              Knowledge Graph
            </button>
        </div>
        {memoryTab === 'episodic' && episodes.length > 0 && (
          <button
            onClick={handleClearAllEpisodes}
            style={{
              display: 'flex', alignItems: 'center', gap: '6px', background: 'rgba(239, 68, 68, 0.12)', border: '1px solid rgba(239, 68, 68, 0.3)', color: '#f87171', padding: '8px 16px', borderRadius: '10px', fontSize: '13px', fontWeight: 500, cursor: 'pointer', transition: 'all 0.2s'
            }}
            onMouseEnter={(e) => e.currentTarget.style.background = 'rgba(239, 68, 68, 0.22)'}
            onMouseLeave={(e) => e.currentTarget.style.background = 'rgba(239, 68, 68, 0.12)'}
          >
            <Trash2 size={15} />
            Clear All Memory
          </button>
        )}
        {memoryTab === 'kg' && facts.length > 0 && (
          <button
            onClick={handleClearAllFacts}
            style={{
              display: 'flex', alignItems: 'center', gap: '6px', background: 'rgba(239, 68, 68, 0.12)', border: '1px solid rgba(239, 68, 68, 0.3)', color: '#f87171', padding: '8px 16px', borderRadius: '10px', fontSize: '13px', fontWeight: 500, cursor: 'pointer', transition: 'all 0.2s'
            }}
            onMouseEnter={(e) => e.currentTarget.style.background = 'rgba(239, 68, 68, 0.22)'}
            onMouseLeave={(e) => e.currentTarget.style.background = 'rgba(239, 68, 68, 0.12)'}
          >
            <Trash2 size={15} />
            Clear All Knowledge
          </button>
        )}
      </div>

      {memoryTab === 'episodic' && (
        <div style={{ display: 'flex', gap: '24px', height: '600px' }}>
          <div style={{ width: '320px', display: 'flex', flexDirection: 'column', gap: '8px', overflowY: 'auto', paddingRight: '8px' }} className="chat-scroll">
            {episodes.length === 0 ? (
              <div style={{ color: '#9090a0', fontSize: '14px', padding: '24px', textAlign: 'center' }}>No episodes found.</div>
            ) : (
              episodes.map(ep => (
                <div 
                  key={ep.task_id || ep.id} 
                  onClick={() => setSelectedEpisode(ep)}
                  style={{ 
                    background: selectedEpisode?.task_id === ep.task_id ? 'rgba(124,58,237,0.2)' : 'rgba(255,255,255,0.02)', 
                    border: `1px solid ${selectedEpisode?.task_id === ep.task_id ? 'rgba(124,58,237,0.5)' : 'rgba(255,255,255,0.05)'}`, 
                    borderRadius: '12px', padding: '14px 16px', cursor: 'pointer', transition: 'all 0.2s', display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '12px'
                  }}
                >
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <h4 style={{ fontSize: '14px', fontWeight: 500, margin: 0, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                      {ep.user_prompt || 'System event'}
                    </h4>
                    <p style={{ fontSize: '12px', color: '#9090a0', marginTop: '4px', margin: 0 }}>
                      {new Date(ep.timestamp * 1000).toLocaleString()}
                    </p>
                  </div>
                  <button
                    title="Delete this episode"
                    onClick={(e) => {
                      e.stopPropagation();
                      handleDeleteEpisode(ep.task_id);
                    }}
                    style={{ background: 'transparent', border: 'none', color: '#9090a0', padding: '6px', borderRadius: '8px', cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'center', transition: 'all 0.2s', flexShrink: 0 }}
                    onMouseEnter={(e) => { e.currentTarget.style.color = '#ef4444'; e.currentTarget.style.background = 'rgba(239, 68, 68, 0.15)'; }}
                    onMouseLeave={(e) => { e.currentTarget.style.color = '#9090a0'; e.currentTarget.style.background = 'transparent'; }}
                  >
                    <Trash2 size={16} />
                  </button>
                </div>
              ))
            )}
          </div>
          <div style={{ flex: 1, background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.05)', borderRadius: '16px', padding: '24px', overflowY: 'auto' }} className="chat-scroll">
            {selectedEpisode ? (
              <div>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
                  <h3 style={{ fontSize: '18px', fontWeight: 500, margin: 0 }}>Episode Details</h3>
                  <button
                    onClick={() => handleDeleteEpisode(selectedEpisode.task_id)}
                    style={{ display: 'flex', alignItems: 'center', gap: '6px', background: 'rgba(239, 68, 68, 0.12)', border: '1px solid rgba(239, 68, 68, 0.25)', color: '#f87171', padding: '6px 14px', borderRadius: '8px', fontSize: '12px', fontWeight: 500, cursor: 'pointer', transition: 'all 0.2s' }}
                    onMouseEnter={(e) => e.currentTarget.style.background = 'rgba(239, 68, 68, 0.22)'}
                    onMouseLeave={(e) => e.currentTarget.style.background = 'rgba(239, 68, 68, 0.12)'}
                  >
                    <Trash2 size={14} />
                    Delete Episode
                  </button>
                </div>
                <div style={{ marginBottom: '16px' }}>
                  <div style={{ fontSize: '12px', color: '#a78bfa', marginBottom: '4px', textTransform: 'uppercase', letterSpacing: '0.05em' }}>User Prompt</div>
                  <div style={{ background: 'rgba(0,0,0,0.2)', padding: '12px', borderRadius: '8px', fontSize: '14px', whiteSpace: 'pre-wrap' }}>{selectedEpisode.user_prompt}</div>
                </div>
                <div style={{ marginBottom: '16px' }}>
                  <div style={{ fontSize: '12px', color: '#10b981', marginBottom: '4px', textTransform: 'uppercase', letterSpacing: '0.05em' }}>Response</div>
                  <div style={{ background: 'rgba(0,0,0,0.2)', padding: '12px', borderRadius: '8px', fontSize: '14px', whiteSpace: 'pre-wrap' }}>{selectedEpisode.outcome}</div>
                </div>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '16px' }}>
                  <div>
                    <div style={{ fontSize: '12px', color: '#9090a0', marginBottom: '4px' }}>Task ID</div>
                    <div style={{ fontSize: '13px', fontFamily: 'monospace' }}>{selectedEpisode.task_id}</div>
                  </div>
                  <div>
                    <div style={{ fontSize: '12px', color: '#9090a0', marginBottom: '4px' }}>Timestamp</div>
                    <div style={{ fontSize: '13px' }}>{new Date(selectedEpisode.timestamp * 1000).toLocaleString()}</div>
                  </div>
                  <div>
                    <div style={{ fontSize: '12px', color: '#9090a0', marginBottom: '4px' }}>Vector ID</div>
                    <div style={{ fontSize: '13px', fontFamily: 'monospace' }}>{selectedEpisode.vector_id || 'N/A'}</div>
                  </div>
                </div>
              </div>
            ) : (
              <div style={{ display: 'flex', height: '100%', alignItems: 'center', justifyContent: 'center', color: '#9090a0', fontSize: '14px' }}>
                Select an episode to view details.
              </div>
            )}
          </div>
        </div>
      )}

      {memoryTab === 'kg' && (
        <div>
          {facts.length === 0 ? (
            <div style={{ color: '#9090a0', fontSize: '14px', padding: '24px', textAlign: 'center', background: 'rgba(0,0,0,0.2)', borderRadius: '12px' }}>
              No facts in Knowledge Graph.
            </div>
          ) : (
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(300px, 1fr))', gap: '16px' }}>
              {Object.entries(facts.reduce((acc, f) => {
                acc[f.entity_name] = acc[f.entity_name] || [];
                acc[f.entity_name].push(f);
                return acc;
              }, {})).map(([entityName, entityFacts]) => (
                <div key={entityName} style={{ background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.08)', borderRadius: '16px', padding: '20px' }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                      <h3 style={{ fontSize: '16px', fontWeight: 500, margin: 0, color: '#a78bfa' }}>{entityName}</h3>
                      <span style={{ fontSize: '11px', background: 'rgba(255,255,255,0.1)', padding: '2px 8px', borderRadius: '12px', color: '#9090a0' }}>
                        {entityFacts[0]?.entity_type || 'Entity'}
                      </span>
                    </div>
                    <button
                      title={`Delete all facts for ${entityName}`}
                      onClick={() => handleDeleteEntity(entityName)}
                      style={{ background: 'transparent', border: 'none', color: '#9090a0', padding: '4px 6px', borderRadius: '6px', cursor: 'pointer', display: 'flex', alignItems: 'center', transition: 'all 0.2s' }}
                      onMouseEnter={(e) => { e.currentTarget.style.color = '#ef4444'; e.currentTarget.style.background = 'rgba(239, 68, 68, 0.15)'; }}
                      onMouseLeave={(e) => { e.currentTarget.style.color = '#9090a0'; e.currentTarget.style.background = 'transparent'; }}
                    >
                      <Trash2 size={16} />
                    </button>
                  </div>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                    {entityFacts.map(fact => (
                      <div key={fact.fact_id} style={{ display: 'flex', gap: '12px', alignItems: 'flex-start', background: 'rgba(0,0,0,0.2)', padding: '12px', borderRadius: '8px' }}>
                        <div style={{ flex: 1, fontSize: '13px', lineHeight: '1.5' }}>{fact.content}</div>
                      </div>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
