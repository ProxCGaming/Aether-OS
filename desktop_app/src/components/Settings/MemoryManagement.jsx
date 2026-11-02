import React, { useState, useEffect } from 'react';
import { Trash2, Pencil, Download } from 'lucide-react';
import { useAetherSocket } from '../../contexts/SocketContext';

export default function MemoryManagement() {
  const { wsRef, sendMessage } = useAetherSocket();
  const [memoryTab, setMemoryTab] = useState('episodic');
  const [episodes, setEpisodes] = useState([]);
  const [facts, setFacts] = useState([]);
  const [selectedEpisode, setSelectedEpisode] = useState(null);
  const [episodesMeta, setEpisodesMeta] = useState(null);
  const [searchInput, setSearchInput] = useState('');
  const [searchQuery, setSearchQuery] = useState('');
  const [includeHistory, setIncludeHistory] = useState(false);
  const [editingFactId, setEditingFactId] = useState(null);
  const [editText, setEditText] = useState('');
  const [exportStatus, setExportStatus] = useState(null);
  // E4-F14: the engine's replies are truthful (E4-F08) — failures
  // carry success=False / an error field, and the UI must surface
  // them instead of rendering them as emptiness or silent success.
  const [memoryError, setMemoryError] = useState(null);
  const [editError, setEditError] = useState(null);

  // Debounce the fact search input into the query used for graph requests
  useEffect(() => {
    const timer = setTimeout(() => setSearchQuery(searchInput.trim()), 300);
    return () => clearTimeout(timer);
  }, [searchInput]);

  useEffect(() => {
    // Initial fetch when component mounts or memory tab changes
    if (memoryTab === 'episodic') {
      sendMessage('MEMORY_EPISODES_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: {} });
    } else if (memoryTab === 'kg') {
      const payload = { include_history: includeHistory };
      if (searchQuery) payload.query = searchQuery;
      sendMessage('MEMORY_GRAPH_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload });
    }

    const handleMessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'MEMORY_EPISODES_RESPONSE') {
          // E4-F14: a failed list load is an error state, not an
          // empty list — keep any rows already on screen and show
          // the engine's error instead of "No episodes found."
          if (data.payload.error) {
            setMemoryError(data.payload.error);
          } else {
            setMemoryError(null);
            setEpisodes(data.payload.episodes || []);
            if (data.payload.backend !== undefined || data.payload.embedding_stats) {
              setEpisodesMeta({ backend: data.payload.backend, embedding_stats: data.payload.embedding_stats });
            }
          }
        } else if (data.type === 'MEMORY_EPISODE_DELETE_RESPONSE') {
          // E4-F14: rows change only on the confirmed result. A
          // failed delete carries success=False plus an empty list
          // that must NOT be applied — keep the rows, show why.
          if (data.payload.success === false) {
            setMemoryError(data.payload.error || 'The episode could not be deleted.');
          } else {
            setMemoryError(null);
            setEpisodes(data.payload.episodes || []);
            setSelectedEpisode(prev => (prev && prev.task_id === data.payload.task_id ? null : prev));
            if (data.payload.backend !== undefined || data.payload.embedding_stats) {
              setEpisodesMeta({ backend: data.payload.backend, embedding_stats: data.payload.embedding_stats });
            }
          }
        } else if (data.type === 'MEMORY_EPISODES_CLEAR_RESPONSE') {
          if (data.payload.success === false) {
            setMemoryError(data.payload.error || 'Episodic memory could not be cleared.');
          } else {
            setMemoryError(null);
            setEpisodes(data.payload.episodes || []);
            setSelectedEpisode(null);
          }
        } else if (data.type === 'MEMORY_GRAPH_RESPONSE' || data.type === 'MEMORY_FACT_DELETE_RESPONSE' || data.type === 'MEMORY_ENTITY_DELETE_RESPONSE') {
          if (data.payload.success === false || data.payload.error) {
            setMemoryError(data.payload.error || 'The memory change was not applied.');
          } else {
            setMemoryError(null);
            setFacts(data.payload.facts || []);
          }
        } else if (data.type === 'MEMORY_FACT_UPDATE_RESPONSE') {
          if (data.payload.facts) setFacts(data.payload.facts);
          if (data.payload.success) {
            setEditingFactId(null);
            setEditError(null);
          } else {
            // E4-F14: a rejected edit keeps the editor open AND says
            // why — the reply carries success=False without a reason
            // string, so the explanation is stated plainly.
            setEditError('The update was rejected — the fact was not changed.');
          }
        } else if (data.type === 'MEMORY_EXPORT_RESPONSE') {
          if (data.payload.success && data.payload.export) {
            downloadExport(data.payload.export);
            setExportStatus(null);
          } else {
            setExportStatus(data.payload.error || 'Export failed.');
          }
        } else if (data.type === 'MEMORY_GRAPH_CLEAR_RESPONSE') {
          if (data.payload.success === false) {
            setMemoryError(data.payload.error || 'The knowledge graph could not be cleared.');
          } else {
            setMemoryError(null);
            setFacts(data.payload.facts || []);
          }
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
  }, [wsRef, sendMessage, memoryTab, searchQuery, includeHistory]);

  // E4-F14: none of the delete/clear actions below touch local
  // state optimistically — the response handler applies the
  // confirmed result, and a failure keeps the rows and surfaces
  // the error.
  const handleDeleteEpisode = (taskId) => {
    if (!taskId) return;
    sendMessage('MEMORY_EPISODE_DELETE_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: { task_id: taskId } });
  };

  const handleClearAllEpisodes = () => {
    if (window.confirm("Are you sure you want to clear all stored episodic memories?")) {
      sendMessage('MEMORY_EPISODES_CLEAR_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: {} });
    }
  };

  const handleDeleteEntity = (entityName) => {
    if (!entityName) return;
    if (window.confirm(`Delete all facts associated with "${entityName}"?`)) {
      sendMessage('MEMORY_ENTITY_DELETE_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: { entity_name: entityName } });
    }
  };

  const handleDeleteFact = (fact) => {
    if (!fact?.fact_id) return;
    // E4-F15: delete ONE fact through the engine's existing
    // MEMORY_FACT_DELETE handler (the entity trash deletes them
    // all). Same honesty rule as every other action here: the row
    // disappears when the response confirms the delete.
    sendMessage('MEMORY_FACT_DELETE_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: { fact_id: fact.fact_id } });
  };

  const handleClearAllFacts = () => {
    if (window.confirm("Are you sure you want to clear all facts from the Knowledge Graph?")) {
      sendMessage('MEMORY_GRAPH_CLEAR_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: {} });
    }
  };

  const downloadExport = (exportData) => {
    try {
      const blob = new Blob([JSON.stringify(exportData, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = 'aether-memory-export.json';
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      // Download is best-effort (e.g. unsupported in test environments)
    }
  };

  const handleExport = () => {
    setExportStatus(null);
    sendMessage('MEMORY_EXPORT_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: {} });
  };

  const handleStartEdit = (fact) => {
    setEditingFactId(fact.fact_id);
    setEditText(fact.fact_text ?? fact.content ?? '');
    setEditError(null);
  };

  const handleSaveEdit = (fact) => {
    sendMessage('MEMORY_FACT_UPDATE_REQUEST', {
      schema_version: 1,
      request_id: Date.now().toString(),
      payload: { fact_id: fact.fact_id, fact_text: editText, include_history: includeHistory }
    });
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

      {memoryError && (
        <div style={{ color: '#f87171', fontSize: '13px', marginBottom: '16px' }}>{memoryError}</div>
      )}

      {memoryTab === 'episodic' && (
        <div>
          {episodesMeta && (episodesMeta.backend || episodesMeta.embedding_stats) && (
            <div style={{ fontSize: '13px', color: '#9090a0', marginBottom: '16px' }}>
              {episodesMeta.backend && <span>Backend: {episodesMeta.backend}</span>}
              {episodesMeta.embedding_stats && (
                <span>
                  {episodesMeta.backend ? ' · ' : ''}
                  Embeddings: {episodesMeta.embedding_stats.embedded}/{episodesMeta.embedding_stats.episodes} embedded
                  {episodesMeta.embedding_stats.pending > 0 ? ` · ${episodesMeta.embedding_stats.pending} pending` : ''}
                </span>
              )}
              {episodesMeta.embedding_stats?.last_failure && (
                <div style={{ color: '#fbbf24', marginTop: '4px' }}>
                  ⚠ Last embedding failure: {episodesMeta.embedding_stats.last_failure.error_type}
                  {episodesMeta.embedding_stats.last_failure.error ? ` — ${episodesMeta.embedding_stats.last_failure.error}` : ''}
                </div>
              )}
            </div>
          )}
          <div style={{ display: 'flex', gap: '24px', height: '600px' }}>
          <div style={{ width: '320px', display: 'flex', flexDirection: 'column', gap: '8px', overflowY: 'auto', paddingRight: '8px' }} className="chat-scroll">
            {episodes.length === 0 ? (
              <div style={{ color: '#9090a0', fontSize: '14px', padding: '24px', textAlign: 'center' }}>{memoryError ? 'Episodes could not be loaded.' : 'No episodes found.'}</div>
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
        </div>
      )}

      {memoryTab === 'kg' && (
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: '16px', marginBottom: '16px', flexWrap: 'wrap' }}>
            <input
              type="text"
              value={searchInput}
              onChange={(e) => setSearchInput(e.target.value)}
              placeholder="Search facts…"
              style={{ background: 'rgba(0,0,0,0.25)', border: '1px solid rgba(255,255,255,0.1)', borderRadius: '8px', color: '#f0f0f5', padding: '8px 12px', fontSize: '13px', minWidth: '220px', outline: 'none' }}
            />
            <label style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '13px', color: '#c9c9d4', cursor: 'pointer' }}>
              <input
                type="checkbox"
                checked={includeHistory}
                onChange={(e) => setIncludeHistory(e.target.checked)}
              />
              Show superseded
            </label>
            <button
              onClick={handleExport}
              style={{ display: 'flex', alignItems: 'center', gap: '6px', background: 'rgba(124,58,237,0.15)', border: '1px solid rgba(124,58,237,0.35)', color: '#c4b5fd', padding: '8px 14px', borderRadius: '8px', fontSize: '13px', fontWeight: 500, cursor: 'pointer', transition: 'all 0.2s' }}
              onMouseEnter={(e) => e.currentTarget.style.background = 'rgba(124,58,237,0.28)'}
              onMouseLeave={(e) => e.currentTarget.style.background = 'rgba(124,58,237,0.15)'}
            >
              <Download size={14} />
              Export Memory
            </button>
          </div>
          {exportStatus && (
            <div style={{ color: '#f87171', fontSize: '13px', marginBottom: '16px' }}>{exportStatus}</div>
          )}
          {facts.length === 0 ? (
            <div style={{ color: '#9090a0', fontSize: '14px', padding: '24px', textAlign: 'center', background: 'rgba(0,0,0,0.2)', borderRadius: '12px' }}>
              {memoryError ? 'Facts could not be loaded.' : 'No facts in Knowledge Graph.'}
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
                    {entityFacts.map(fact => {
                      const isSuperseded = fact.status === 'superseded';
                      const isEditing = editingFactId === fact.fact_id;
                      return (
                        <div key={fact.fact_id} style={{ display: 'flex', gap: '12px', alignItems: 'flex-start', background: 'rgba(0,0,0,0.2)', padding: '12px', borderRadius: '8px', opacity: isSuperseded ? 0.55 : 1 }}>
                          {isEditing ? (
                            <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: '8px' }}>
                              <textarea
                                value={editText}
                                onChange={(e) => setEditText(e.target.value)}
                                rows={3}
                                style={{ background: 'rgba(0,0,0,0.3)', border: '1px solid rgba(124,58,237,0.4)', borderRadius: '6px', color: '#f0f0f5', padding: '8px', fontSize: '13px', lineHeight: '1.5', resize: 'vertical', outline: 'none', fontFamily: 'inherit' }}
                              />
                              <div style={{ display: 'flex', gap: '8px' }}>
                                <button
                                  onClick={() => handleSaveEdit(fact)}
                                  style={{ background: 'rgba(124,58,237,0.35)', border: '1px solid rgba(124,58,237,0.5)', color: '#fff', padding: '5px 12px', borderRadius: '6px', fontSize: '12px', fontWeight: 500, cursor: 'pointer' }}
                                >
                                  Save
                                </button>
                                <button
                                  onClick={() => { setEditingFactId(null); setEditError(null); }}
                                  style={{ background: 'rgba(255,255,255,0.06)', border: '1px solid rgba(255,255,255,0.12)', color: '#c9c9d4', padding: '5px 12px', borderRadius: '6px', fontSize: '12px', cursor: 'pointer' }}
                                >
                                  Cancel
                                </button>
                              </div>
                              {editError && (
                                <div style={{ color: '#f87171', fontSize: '12px' }}>{editError}</div>
                              )}
                            </div>
                          ) : (
                            <>
                              <div style={{ flex: 1, fontSize: '13px', lineHeight: '1.5' }}>
                                {fact.fact_text ?? fact.content}
                                {isSuperseded && (
                                  <span style={{ marginLeft: '8px', fontSize: '10px', background: 'rgba(251,191,36,0.15)', color: '#fbbf24', padding: '1px 6px', borderRadius: '8px', textTransform: 'uppercase', letterSpacing: '0.04em' }}>superseded</span>
                                )}
                              </div>
                              <button
                                title="Edit this fact"
                                onClick={() => handleStartEdit(fact)}
                                style={{ display: 'flex', alignItems: 'center', gap: '4px', background: 'transparent', border: '1px solid rgba(255,255,255,0.12)', color: '#9090a0', padding: '4px 8px', borderRadius: '6px', fontSize: '11px', cursor: 'pointer', flexShrink: 0, transition: 'all 0.2s' }}
                                onMouseEnter={(e) => { e.currentTarget.style.color = '#fff'; e.currentTarget.style.borderColor = 'rgba(124,58,237,0.5)'; }}
                                onMouseLeave={(e) => { e.currentTarget.style.color = '#9090a0'; e.currentTarget.style.borderColor = 'rgba(255,255,255,0.12)'; }}
                              >
                                <Pencil size={13} />
                                Edit
                              </button>
                              <button
                                title="Delete this fact"
                                onClick={() => handleDeleteFact(fact)}
                                style={{ display: 'flex', alignItems: 'center', gap: '4px', background: 'transparent', border: '1px solid rgba(255,255,255,0.12)', color: '#9090a0', padding: '4px 8px', borderRadius: '6px', fontSize: '11px', cursor: 'pointer', flexShrink: 0, transition: 'all 0.2s' }}
                                onMouseEnter={(e) => { e.currentTarget.style.color = '#ef4444'; e.currentTarget.style.borderColor = 'rgba(239,68,68,0.5)'; }}
                                onMouseLeave={(e) => { e.currentTarget.style.color = '#9090a0'; e.currentTarget.style.borderColor = 'rgba(255,255,255,0.12)'; }}
                              >
                                <Trash2 size={13} />
                                Delete
                              </button>
                            </>
                          )}
                        </div>
                      );
                    })}
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
