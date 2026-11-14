import React, { useState, useEffect } from 'react';
import { Trash2, Pencil, Download } from 'lucide-react';
import { useAetherSocket } from '../../contexts/SocketContext';
import LoadStateRow from './LoadStateRow';

// S5 provenance, surfaced in the facts list: the native knowledge
// graph records whether each fact was stated by the user or
// inferred by the engine, plus a 0..1 importance. The two known
// values get friendly labels; any other value a store returns is
// shown verbatim — never relabelled into a wrong one. The
// external backends (Mem0 / LangMem / Graphiti) do not track
// provenance at all: their fact dicts carry no provenance field,
// and those facts render NO chip (the panel shows one note
// instead — see the Knowledge Graph tab below).
const PROVENANCE_LABELS = { stated: 'Stated', inferred: 'Inferred' };

export default function MemoryManagement() {
  const { wsRef, sendMessage, isConnected } = useAetherSocket();
  // Wave 3 slice 1 (RUX-C-01 umbrella): whether each memory list
  // has been ANSWERED. Error replies were already honest here
  // (E4-F14); what was missing is the loading/offline distinction —
  // "No episodes found." used to render while the first reply was
  // still in flight or queued behind a dead engine.
  const [episodesLoad, setEpisodesLoad] = useState('loading');
  const [factsLoad, setFactsLoad] = useState('loading');
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
  // S9: the memory-backend picker. ALL four backends are listed
  // (owner decision, 14 Nov 2026 — never installed-only), each
  // with the engine's probed state and reason; selection rides
  // CONFIG_UPDATE_REQUEST and the engine refuses unavailable
  // ones server-side with the same reason shown here.
  const [backends, setBackends] = useState(null);
  const [backendsLoad, setBackendsLoad] = useState('loading');
  const [pendingBackend, setPendingBackend] = useState(null);
  const [backendError, setBackendError] = useState(null);
  const [backendNotice, setBackendNotice] = useState(null);
  const pendingSwitchRef = React.useRef(null);

  // Debounce the fact search input into the query used for graph requests
  useEffect(() => {
    const timer = setTimeout(() => setSearchQuery(searchInput.trim()), 300);
    return () => clearTimeout(timer);
  }, [searchInput]);

  // S9: fetch the backend status report on mount, and own the
  // CONFIG_UPDATE_RESPONSE for a switch this panel started
  // (pendingSwitchRef distinguishes it from other panels'
  // config writes).
  useEffect(() => {
    sendMessage('MEMORY_BACKENDS_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: {} });

    const handleBackendMessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'MEMORY_BACKENDS_RESPONSE') {
          setBackends(data.payload.backends || []);
          setBackendsLoad('ready');
        } else if (data.type === 'CONFIG_UPDATE_RESPONSE' && pendingSwitchRef.current) {
          const target = pendingSwitchRef.current;
          pendingSwitchRef.current = null;
          setPendingBackend(null);
          if (data.payload.success) {
            setBackendError(null);
            setBackendNotice(`Memory backend switched to ${target.name}. It takes effect on the next memory operation — no restart needed.`);
            // Re-read the report so the Current marker moves,
            // and refresh the lists: they now read from the
            // new backend's store.
            sendMessage('MEMORY_BACKENDS_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: {} });
            sendMessage('MEMORY_EPISODES_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: {} });
            sendMessage('MEMORY_GRAPH_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: {} });
          } else {
            // The server's refusal, verbatim — it is the same
            // reason the picker showed for that backend.
            setBackendError(data.payload.error || 'The backend switch was refused.');
          }
        }
      } catch (e) {
        // ignore
      }
    };

    if (wsRef.current) {
      wsRef.current.addEventListener('message', handleBackendMessage);
    }
    return () => {
      if (wsRef.current) {
        wsRef.current.removeEventListener('message', handleBackendMessage);
      }
    };
  }, [wsRef, sendMessage]);

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
            setEpisodesLoad('ready');
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
            setEpisodesLoad('ready');
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
            setEpisodesLoad('ready');
            setSelectedEpisode(null);
          }
        } else if (data.type === 'MEMORY_GRAPH_RESPONSE' || data.type === 'MEMORY_FACT_DELETE_RESPONSE' || data.type === 'MEMORY_ENTITY_DELETE_RESPONSE') {
          if (data.payload.success === false || data.payload.error) {
            setMemoryError(data.payload.error || 'The memory change was not applied.');
          } else {
            setMemoryError(null);
            setFacts(data.payload.facts || []);
            setFactsLoad('ready');
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
            setFactsLoad('ready');
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

  // S9: opening the confirmation is all a Switch click does —
  // nothing is sent until the user confirms there.
  const handleSwitchRequest = (backend) => {
    setBackendError(null);
    setBackendNotice(null);
    setPendingBackend(backend);
  };

  const handleSwitchCancel = () => {
    setPendingBackend(null);
  };

  const handleSwitchConfirm = () => {
    if (!pendingBackend) return;
    pendingSwitchRef.current = pendingBackend;
    sendMessage('CONFIG_UPDATE_REQUEST', {
      schema_version: 1,
      request_id: Date.now().toString(),
      payload: { config: { memory_backend: pendingBackend.id } }
    });
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
    <div style={{ color: 'var(--text-primary)' }}>
      <h2 style={{ fontSize: '24px', marginBottom: '8px', fontWeight: 500 }}>Memory & Storage</h2>
      <p style={{ color: 'var(--text-muted)', fontSize: '14px', marginBottom: '24px' }}>Manage the internal knowledge graph and episodic memory.</p>

      {/* S9: memory-backend picker — all four backends with
          their probed states; unavailable ones stay visible
          with the reason and cannot be selected. */}
      <div data-testid="memory-backend-picker" style={{ background: 'var(--bg-raised)', border: '1px solid var(--border-default)', borderRadius: 'var(--radius-lg)', padding: '20px', marginBottom: '24px' }}>
        <h3 style={{ fontSize: '16px', fontWeight: 500, margin: '0 0 4px 0' }}>Memory backend</h3>
        <p style={{ color: 'var(--text-secondary)', fontSize: '13px', margin: '0 0 16px 0', lineHeight: '1.5' }}>
          Aether can keep its long-term memory in different stores. Every backend is listed — including ones that cannot run on this machine, with the reason why.
        </p>
        {backendError && (
          <div data-testid="memory-backend-error" style={{ color: 'var(--danger)', fontSize: '13px', marginBottom: '12px' }}>{backendError}</div>
        )}
        {backendNotice && (
          <div data-testid="memory-backend-notice" style={{ color: 'var(--success)', fontSize: '13px', marginBottom: '12px' }}>{backendNotice}</div>
        )}
        {backendsLoad !== 'ready' ? (
          <div style={{ color: 'var(--text-secondary)', fontSize: '13px' }}>Loading backends…</div>
        ) : (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
            {(backends || []).map((b) => {
              const chipLabel = b.current ? 'Current' : b.state === 'available' ? 'Available' : b.state === 'not_installed' ? 'Not installed' : 'Not configured';
              const chipColor = b.current ? 'var(--accent)' : b.available ? 'var(--success)' : 'var(--text-secondary)';
              return (
                <div key={b.id} data-testid={`memory-backend-${b.id}`} style={{ background: 'var(--bg-base)', border: `1px solid ${b.current ? 'rgba(var(--accent-rgb),0.5)' : 'var(--border-default)'}`, borderRadius: '12px', padding: '14px 16px' }}>
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '12px', marginBottom: '6px' }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                      <span style={{ fontSize: '14px', fontWeight: 500 }}>{b.name}</span>
                      <span style={{ fontSize: '11px', background: 'var(--bg-hover)', padding: '2px 8px', borderRadius: '12px', color: chipColor }}>{chipLabel}</span>
                    </div>
                    <button
                      data-testid={`memory-backend-switch-${b.id}`}
                      disabled={b.current || !b.available}
                      onClick={() => handleSwitchRequest(b)}
                      style={{ background: b.current || !b.available ? 'var(--bg-hover)' : 'rgba(var(--accent-rgb),0.15)', border: '1px solid var(--border-default)', color: b.current || !b.available ? 'var(--text-secondary)' : 'var(--accent)', padding: '6px 14px', borderRadius: '8px', fontSize: '12px', fontWeight: 500, cursor: b.current || !b.available ? 'not-allowed' : 'pointer', flexShrink: 0 }}
                    >
                      {b.current ? 'Current backend' : b.available ? `Switch to ${b.name}` : 'Unavailable'}
                    </button>
                  </div>
                  <div style={{ fontSize: '13px', color: 'var(--text-secondary)', lineHeight: '1.5' }}>{b.description}</div>
                  <div style={{ fontSize: '12px', color: 'var(--text-muted)', marginTop: '4px' }}>Needs: {b.needs}</div>
                  {b.detail && (
                    <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginTop: '4px' }}>{b.detail}</div>
                  )}
                  {(b.warnings || []).map((w, i) => (
                    <div key={i} style={{ fontSize: '12px', color: 'var(--accent)', marginTop: '4px', lineHeight: '1.5' }}>⚠ {w}</div>
                  ))}
                  {!b.available && b.reason && (
                    <div style={{ fontSize: '12px', color: 'var(--danger)', marginTop: '4px', lineHeight: '1.5' }}>{b.reason}</div>
                  )}
                </div>
              );
            })}
          </div>
        )}
        {pendingBackend && (
          <div data-testid="memory-backend-confirm" style={{ background: 'rgba(var(--accent-rgb), 0.08)', border: '1px solid rgba(var(--accent-rgb), 0.35)', borderRadius: '12px', padding: '16px', marginTop: '16px' }}>
            <div style={{ fontSize: '14px', fontWeight: 500, marginBottom: '8px' }}>Switch memory backend to {pendingBackend.name}?</div>
            <ul style={{ fontSize: '13px', color: 'var(--text-secondary)', lineHeight: '1.6', margin: '0 0 8px 0', paddingLeft: '20px' }}>
              <li>Switching backends switches stores: your existing memories stay in {(backends || []).find((x) => x.current)?.name || 'the current backend'} and are <strong>not</strong> moved or copied.</li>
              <li>Nothing is deleted — switch back at any time and those memories are there again, untouched.</li>
              <li>Semantic embeddings do not transfer; the new backend builds its own from its own store.</li>
              <li>The switch takes effect on the next memory operation — no restart is needed.</li>
            </ul>
            {(pendingBackend.warnings || []).map((w, i) => (
              <div key={i} style={{ fontSize: '12px', color: 'var(--accent)', lineHeight: '1.5' }}>⚠ {w}</div>
            ))}
            <div style={{ display: 'flex', gap: '8px', marginTop: '12px' }}>
              <button
                data-testid="memory-backend-confirm-switch"
                onClick={handleSwitchConfirm}
                style={{ background: 'rgba(var(--accent-rgb),0.35)', border: '1px solid rgba(var(--accent-rgb),0.5)', color: 'var(--text-primary)', padding: '6px 14px', borderRadius: '8px', fontSize: '13px', fontWeight: 500, cursor: 'pointer' }}
              >
                Switch backend
              </button>
              <button
                data-testid="memory-backend-cancel-switch"
                onClick={handleSwitchCancel}
                style={{ background: 'var(--bg-hover)', border: '1px solid var(--border-default)', color: 'var(--text-secondary)', padding: '6px 14px', borderRadius: '8px', fontSize: '13px', cursor: 'pointer' }}
              >
                Cancel
              </button>
            </div>
          </div>
        )}
      </div>

      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '24px' }}>
        <div style={{ display: 'flex', background: 'var(--bg-overlay)', padding: '4px', borderRadius: '12px', width: 'fit-content' }}>
            <button 
              onClick={() => setMemoryTab('episodic')}
              style={{ background: memoryTab === 'episodic' ? 'rgba(var(--accent-rgb), 0.18)' : 'transparent', color: memoryTab === 'episodic' ? 'var(--accent)' : 'var(--text-secondary)', border: 'none', padding: '8px 24px', borderRadius: '8px', cursor: 'pointer', transition: 'all 0.2s' }}>
              Episodic Memory
            </button>
            <button 
              onClick={() => setMemoryTab('kg')}
              style={{ background: memoryTab === 'kg' ? 'rgba(var(--accent-rgb), 0.18)' : 'transparent', color: memoryTab === 'kg' ? 'var(--accent)' : 'var(--text-secondary)', border: 'none', padding: '8px 24px', borderRadius: '8px', cursor: 'pointer', transition: 'all 0.2s' }}>
              Knowledge Graph
            </button>
        </div>
        {memoryTab === 'episodic' && episodes.length > 0 && (
          <button
            onClick={handleClearAllEpisodes}
            style={{
              display: 'flex', alignItems: 'center', gap: '6px', background: 'var(--danger)', border: '1px solid var(--danger)', color: 'var(--text-primary)', padding: '8px 16px', borderRadius: '10px', fontSize: '13px', fontWeight: 500, cursor: 'pointer', transition: 'all 0.2s'
            }}
            onMouseEnter={(e) => e.currentTarget.style.background = 'rgba(201,42,42, 0.85)'}
            onMouseLeave={(e) => e.currentTarget.style.background = 'var(--danger)'}
          >
            <Trash2 size={15} />
            Clear All Memory
          </button>
        )}
        {memoryTab === 'kg' && facts.length > 0 && (
          <button
            onClick={handleClearAllFacts}
            style={{
              display: 'flex', alignItems: 'center', gap: '6px', background: 'var(--danger)', border: '1px solid var(--danger)', color: 'var(--text-primary)', padding: '8px 16px', borderRadius: '10px', fontSize: '13px', fontWeight: 500, cursor: 'pointer', transition: 'all 0.2s'
            }}
            onMouseEnter={(e) => e.currentTarget.style.background = 'rgba(201,42,42, 0.85)'}
            onMouseLeave={(e) => e.currentTarget.style.background = 'var(--danger)'}
          >
            <Trash2 size={15} />
            Clear All Knowledge
          </button>
        )}
      </div>

      {memoryError && (
        <div style={{ color: 'var(--danger)', fontSize: '13px', marginBottom: '16px' }}>{memoryError}</div>
      )}

      {memoryTab === 'episodic' && (
        <div>
          {episodesMeta && (episodesMeta.backend || episodesMeta.embedding_stats) && (
            <div style={{ fontSize: '13px', color: 'var(--text-secondary)', marginBottom: '16px' }}>
              {episodesMeta.backend && <span>Backend: {episodesMeta.backend}</span>}
              {episodesMeta.embedding_stats && (
                <span>
                  {episodesMeta.backend ? ' · ' : ''}
                  Embeddings: {episodesMeta.embedding_stats.embedded}/{episodesMeta.embedding_stats.episodes} embedded
                  {episodesMeta.embedding_stats.pending > 0 ? ` · ${episodesMeta.embedding_stats.pending} pending` : ''}
                </span>
              )}
              {episodesMeta.embedding_stats?.last_failure && (
                <div style={{ color: 'var(--accent)', marginTop: '4px' }}>
                  ⚠ Last embedding failure: {episodesMeta.embedding_stats.last_failure.error_type}
                  {episodesMeta.embedding_stats.last_failure.error ? ` — ${episodesMeta.embedding_stats.last_failure.error}` : ''}
                </div>
              )}
              {(() => {
                // The engine's startup embedder verdict: when the
                // embedder is unavailable, say so HERE, where the
                // user sees embedding health — embeddings silently
                // never happening read as "memory is broken".
                const embedderStatus = episodesMeta.embedding_stats?.embedder_status;
                if (!embedderStatus || (embedderStatus.state !== 'ollama_off' && embedderStatus.state !== 'pull_failed')) return null;
                const reason = embedderStatus.state === 'ollama_off'
                  ? "Ollama isn't running"
                  : 'the model could not be downloaded';
                return (
                  <div
                    data-testid="embedder-unavailable-note"
                    style={{ color: 'var(--accent)', background: 'rgba(var(--accent-rgb), 0.08)', border: '1px solid rgba(var(--accent-rgb), 0.25)', borderRadius: '8px', padding: '8px 12px', marginTop: '8px', fontSize: '13px', lineHeight: '1.5' }}
                  >
                    ⚠ Embedder unavailable — {reason}
                    {embedderStatus.model ? ` (model: ${embedderStatus.model})` : ''}.
                    Memory search is paused; nothing is lost — your episodes are stored and will be embedded once the embedder is available.
                  </div>
                );
              })()}
            </div>
          )}
          <div style={{ display: 'flex', gap: '24px', height: '600px' }}>
          <div style={{ width: '320px', display: 'flex', flexDirection: 'column', gap: '8px', overflowY: 'auto', paddingRight: '8px' }} className="chat-scroll">
            {episodes.length === 0 ? (
              memoryError ? (
                <div style={{ color: 'var(--text-secondary)', fontSize: '14px', padding: '24px', textAlign: 'center' }}>Episodes could not be loaded.</div>
              ) : episodesLoad !== 'ready' ? (
                <div style={{ padding: '24px', textAlign: 'center' }}>
                  <LoadStateRow
                    loadState={episodesLoad}
                    isConnected={isConnected}
                    testId="memory-episodes-load-state"
                    variant="plain"
                    style={{ fontSize: '14px' }}
                    loadingText="Loading episodes…"
                    offlineText="Waiting for the engine — your episodes will appear here once it connects."
                    errorText="Episodes could not be loaded."
                  />
                </div>
              ) : (
                <div style={{ color: 'var(--text-secondary)', fontSize: '14px', padding: '24px', textAlign: 'center' }}>No episodes found.</div>
              )
            ) : (
              episodes.map(ep => (
                <div 
                  key={ep.task_id || ep.id} 
                  onClick={() => setSelectedEpisode(ep)}
                  style={{ 
                    background: selectedEpisode?.task_id === ep.task_id ? 'rgba(var(--accent-rgb),0.2)' : 'var(--bg-raised)', 
                    border: `1px solid ${selectedEpisode?.task_id === ep.task_id ? 'rgba(var(--accent-rgb),0.5)' : 'var(--border-default)'}`, 
                    borderRadius: '12px', padding: '14px 16px', cursor: 'pointer', transition: 'all 0.2s', display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '12px'
                  }}
                >
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <h4 style={{ fontSize: '14px', fontWeight: 500, margin: 0, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                      {ep.user_prompt || 'System event'}
                    </h4>
                    <p style={{ fontSize: '12px', color: 'var(--text-secondary)', marginTop: '4px', margin: 0 }}>
                      {new Date(ep.timestamp * 1000).toLocaleString()}
                    </p>
                  </div>
                  <button
                    title="Delete this episode"
                    onClick={(e) => {
                      e.stopPropagation();
                      handleDeleteEpisode(ep.task_id);
                    }}
                    style={{ background: 'transparent', border: 'none', color: 'var(--text-secondary)', padding: '6px', borderRadius: '8px', cursor: 'pointer', display: 'flex', alignItems: 'center', justifyContent: 'center', transition: 'all 0.2s', flexShrink: 0 }}
                    onMouseEnter={(e) => { e.currentTarget.style.color = 'var(--danger)'; e.currentTarget.style.background = 'rgba(201,42,42, 0.15)'; }}
                    onMouseLeave={(e) => { e.currentTarget.style.color = 'var(--text-secondary)'; e.currentTarget.style.background = 'transparent'; }}
                  >
                    <Trash2 size={16} />
                  </button>
                </div>
              ))
            )}
          </div>
          <div style={{ flex: 1, background: 'var(--bg-raised)', border: '1px solid var(--border-default)', borderRadius: 'var(--radius-lg)', padding: '24px', overflowY: 'auto' }} className="chat-scroll">
            {selectedEpisode ? (
              <div>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
                  <h3 style={{ fontSize: '18px', fontWeight: 500, margin: 0 }}>Episode Details</h3>
                  <button
                    onClick={() => handleDeleteEpisode(selectedEpisode.task_id)}
                    style={{ display: 'flex', alignItems: 'center', gap: '6px', background: 'rgba(201,42,42, 0.12)', border: '1px solid rgba(201,42,42, 0.25)', color: 'var(--danger)', padding: '6px 14px', borderRadius: '8px', fontSize: '12px', fontWeight: 500, cursor: 'pointer', transition: 'all 0.2s' }}
                    onMouseEnter={(e) => e.currentTarget.style.background = 'rgba(201,42,42, 0.85)'}
                    onMouseLeave={(e) => e.currentTarget.style.background = 'var(--danger)'}
                  >
                    <Trash2 size={14} />
                    Delete Episode
                  </button>
                </div>
                <div style={{ marginBottom: '16px' }}>
                  <div style={{ fontSize: '12px', color: 'var(--accent)', marginBottom: '4px', textTransform: 'uppercase', letterSpacing: '0.05em' }}>User Prompt</div>
                  <div style={{ background: 'var(--bg-base)', padding: '12px', borderRadius: '8px', fontSize: '14px', whiteSpace: 'pre-wrap' }}>{selectedEpisode.user_prompt}</div>
                </div>
                <div style={{ marginBottom: '16px' }}>
                  <div style={{ fontSize: '12px', color: 'var(--success)', marginBottom: '4px', textTransform: 'uppercase', letterSpacing: '0.05em' }}>Response</div>
                  <div style={{ background: 'var(--bg-base)', padding: '12px', borderRadius: '8px', fontSize: '14px', whiteSpace: 'pre-wrap' }}>{selectedEpisode.outcome}</div>
                </div>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '16px' }}>
                  <div>
                    <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginBottom: '4px' }}>Task ID</div>
                    <div style={{ fontSize: '13px', fontFamily: 'var(--font-mono)' }}>{selectedEpisode.task_id}</div>
                  </div>
                  <div>
                    <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginBottom: '4px' }}>Timestamp</div>
                    <div style={{ fontSize: '13px' }}>{new Date(selectedEpisode.timestamp * 1000).toLocaleString()}</div>
                  </div>
                  <div>
                    <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginBottom: '4px' }}>Vector ID</div>
                    <div style={{ fontSize: '13px', fontFamily: 'var(--font-mono)' }}>{selectedEpisode.vector_id || 'N/A'}</div>
                  </div>
                </div>
              </div>
            ) : (
              <div style={{ display: 'flex', height: '100%', alignItems: 'center', justifyContent: 'center', color: 'var(--text-secondary)', fontSize: '14px' }}>
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
              style={{ background: 'var(--bg-base)', border: '1px solid var(--border-default)', borderRadius: '8px', color: 'var(--text-primary)', padding: '8px 12px', fontSize: '13px', minWidth: '220px', outline: 'none' }}
            />
            <label style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '13px', color: 'var(--text-secondary)', cursor: 'pointer' }}>
              <input
                type="checkbox"
                checked={includeHistory}
                onChange={(e) => setIncludeHistory(e.target.checked)}
                style={{ accentColor: 'var(--accent)' }}
              />
              Show superseded
            </label>
            <button
              onClick={handleExport}
              style={{ display: 'flex', alignItems: 'center', gap: '6px', background: 'rgba(var(--accent-rgb),0.15)', border: '1px solid rgba(var(--accent-rgb),0.35)', color: 'var(--accent)', padding: '8px 14px', borderRadius: '8px', fontSize: '13px', fontWeight: 500, cursor: 'pointer', transition: 'all 0.2s' }}
              onMouseEnter={(e) => e.currentTarget.style.background = 'rgba(var(--accent-rgb),0.28)'}
              onMouseLeave={(e) => e.currentTarget.style.background = 'rgba(var(--accent-rgb),0.15)'}
            >
              <Download size={14} />
              Export Memory
            </button>
          </div>
          {exportStatus && (
            <div style={{ color: 'var(--danger)', fontSize: '13px', marginBottom: '16px' }}>{exportStatus}</div>
          )}
          {factsLoad === 'ready' && facts.length > 0 && !facts.some(f => f.provenance != null && f.provenance !== '') && (
            <div
              data-testid="memory-provenance-note"
              style={{ color: 'var(--text-secondary)', background: 'var(--bg-base)', border: '1px solid var(--border-default)', borderRadius: '8px', padding: '8px 12px', marginBottom: '16px', fontSize: '13px', lineHeight: '1.5' }}
            >
              The active memory backend{(() => { const current = (backends || []).find(b => b.current); return current ? ` (${current.name})` : ''; })()} does not record provenance — whether a fact was stated by you or inferred by Aether — so no Stated / Inferred labels are shown for these facts.
            </div>
          )}
          {facts.length === 0 ? (
            memoryError ? (
              <div style={{ color: 'var(--text-secondary)', fontSize: '14px', padding: '24px', textAlign: 'center', background: 'var(--bg-base)', borderRadius: '12px' }}>
                Facts could not be loaded.
              </div>
            ) : factsLoad !== 'ready' ? (
              <LoadStateRow
                loadState={factsLoad}
                isConnected={isConnected}
                testId="memory-facts-load-state"
                loadingText="Loading facts…"
                offlineText="Waiting for the engine — facts will appear here once it connects."
                errorText="Facts could not be loaded."
              />
            ) : (
              <div style={{ color: 'var(--text-secondary)', fontSize: '14px', padding: '24px', textAlign: 'center', background: 'var(--bg-base)', borderRadius: '12px' }}>
                No facts in Knowledge Graph.
              </div>
            )
          ) : (
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(300px, 1fr))', gap: '16px' }}>
              {Object.entries(facts.reduce((acc, f) => {
                acc[f.entity_name] = acc[f.entity_name] || [];
                acc[f.entity_name].push(f);
                return acc;
              }, {})).map(([entityName, entityFacts]) => (
                <div key={entityName} style={{ background: 'var(--bg-raised)', border: '1px solid var(--border-default)', borderRadius: 'var(--radius-lg)', padding: '20px' }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '16px' }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                      <h3 style={{ fontSize: '16px', fontWeight: 500, margin: 0, color: 'var(--accent)' }}>{entityName}</h3>
                      <span style={{ fontSize: '11px', background: 'var(--bg-hover)', padding: '2px 8px', borderRadius: '12px', color: 'var(--text-secondary)' }}>
                        {entityFacts[0]?.entity_type || 'Entity'}
                      </span>
                    </div>
                    <button
                      title={`Delete all facts for ${entityName}`}
                      onClick={() => handleDeleteEntity(entityName)}
                      style={{ background: 'transparent', border: 'none', color: 'var(--text-secondary)', padding: '4px 6px', borderRadius: '6px', cursor: 'pointer', display: 'flex', alignItems: 'center', transition: 'all 0.2s' }}
                      onMouseEnter={(e) => { e.currentTarget.style.color = 'var(--danger)'; e.currentTarget.style.background = 'rgba(201,42,42, 0.15)'; }}
                      onMouseLeave={(e) => { e.currentTarget.style.color = 'var(--text-secondary)'; e.currentTarget.style.background = 'transparent'; }}
                    >
                      <Trash2 size={16} />
                    </button>
                  </div>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                    {entityFacts.map(fact => {
                      const isSuperseded = fact.status === 'superseded';
                      const isEditing = editingFactId === fact.fact_id;
                      // S5: render provenance only when the backend
                      // actually supplied the field (external
                      // backends omit it — no chip, never a guess),
                      // and importance only when it is a real
                      // number (the native store's 0..1 float).
                      const hasProvenance = fact.provenance != null && fact.provenance !== '';
                      const provenanceLabel = hasProvenance
                        ? (PROVENANCE_LABELS[fact.provenance] ?? fact.provenance)
                        : null;
                      const hasImportance = typeof fact.importance === 'number' && Number.isFinite(fact.importance);
                      return (
                        <div key={fact.fact_id} style={{ display: 'flex', gap: '12px', alignItems: 'flex-start', background: 'var(--bg-base)', padding: '12px', borderRadius: '8px', opacity: isSuperseded ? 0.55 : 1 }}>
                          {isEditing ? (
                            <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: '8px' }}>
                              <textarea
                                value={editText}
                                onChange={(e) => setEditText(e.target.value)}
                                rows={3}
                                style={{ background: 'var(--bg-base)', border: '1px solid rgba(var(--accent-rgb),0.4)', borderRadius: '6px', color: 'var(--text-primary)', padding: '8px', fontSize: '13px', lineHeight: '1.5', resize: 'vertical', outline: 'none', fontFamily: 'inherit' }}
                              />
                              <div style={{ display: 'flex', gap: '8px' }}>
                                <button
                                  onClick={() => handleSaveEdit(fact)}
                                  style={{ background: 'rgba(var(--accent-rgb),0.35)', border: '1px solid rgba(var(--accent-rgb),0.5)', color: 'var(--text-primary)', padding: '5px 12px', borderRadius: '6px', fontSize: '12px', fontWeight: 500, cursor: 'pointer' }}
                                >
                                  Save
                                </button>
                                <button
                                  onClick={() => { setEditingFactId(null); setEditError(null); }}
                                  style={{ background: 'var(--bg-hover)', border: '1px solid var(--border-default)', color: 'var(--text-secondary)', padding: '5px 12px', borderRadius: '6px', fontSize: '12px', cursor: 'pointer' }}
                                >
                                  Cancel
                                </button>
                              </div>
                              {editError && (
                                <div style={{ color: 'var(--danger)', fontSize: '12px' }}>{editError}</div>
                              )}
                            </div>
                          ) : (
                            <>
                              <div style={{ flex: 1, fontSize: '13px', lineHeight: '1.5' }}>
                                {fact.fact_text ?? fact.content}
                                {hasProvenance && (
                                  <span
                                    data-testid={`fact-provenance-${fact.fact_id}`}
                                    style={fact.provenance === 'stated'
                                      ? { marginLeft: '8px', fontSize: '10px', background: 'rgba(var(--accent-rgb),0.15)', color: 'var(--accent)', padding: '1px 6px', borderRadius: '8px', textTransform: 'uppercase', letterSpacing: '0.04em' }
                                      : { marginLeft: '8px', fontSize: '10px', background: 'var(--bg-hover)', color: 'var(--text-secondary)', padding: '1px 6px', borderRadius: '8px', textTransform: 'uppercase', letterSpacing: '0.04em' }}
                                  >
                                    {provenanceLabel}
                                  </span>
                                )}
                                {hasImportance && (
                                  <span data-testid={`fact-importance-${fact.fact_id}`} style={{ marginLeft: '8px', fontSize: '11px', color: 'var(--text-muted)' }}>
                                    importance {Math.round(fact.importance * 100)}%
                                  </span>
                                )}
                                {isSuperseded && (
                                  <span style={{ marginLeft: '8px', fontSize: '10px', background: 'rgba(var(--accent-rgb),0.15)', color: 'var(--accent)', padding: '1px 6px', borderRadius: '8px', textTransform: 'uppercase', letterSpacing: '0.04em' }}>superseded</span>
                                )}
                              </div>
                              <button
                                title="Edit this fact"
                                onClick={() => handleStartEdit(fact)}
                                style={{ display: 'flex', alignItems: 'center', gap: '4px', background: 'transparent', border: '1px solid var(--border-default)', color: 'var(--text-secondary)', padding: '4px 8px', borderRadius: '6px', fontSize: '11px', cursor: 'pointer', flexShrink: 0, transition: 'all 0.2s' }}
                                onMouseEnter={(e) => { e.currentTarget.style.color = 'var(--text-primary)'; e.currentTarget.style.borderColor = 'rgba(var(--accent-rgb),0.5)'; }}
                                onMouseLeave={(e) => { e.currentTarget.style.color = 'var(--text-secondary)'; e.currentTarget.style.borderColor = 'var(--border-default)'; }}
                              >
                                <Pencil size={13} />
                                Edit
                              </button>
                              <button
                                title="Delete this fact"
                                onClick={() => handleDeleteFact(fact)}
                                style={{ display: 'flex', alignItems: 'center', gap: '4px', background: 'transparent', border: '1px solid var(--border-default)', color: 'var(--text-secondary)', padding: '4px 8px', borderRadius: '6px', fontSize: '11px', cursor: 'pointer', flexShrink: 0, transition: 'all 0.2s' }}
                                onMouseEnter={(e) => { e.currentTarget.style.color = 'var(--danger)'; e.currentTarget.style.borderColor = 'rgba(201,42,42,0.5)'; }}
                                onMouseLeave={(e) => { e.currentTarget.style.color = 'var(--text-secondary)'; e.currentTarget.style.borderColor = 'var(--border-default)'; }}
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
