import React, { useState, useEffect, useCallback } from 'react';
import { RefreshCw, Trash2 } from 'lucide-react';
import FitBadge, { LoadStateBadge } from './FitBadge';

// Master build S21 (R17 wiring): the engine's own Ollama list —
// LOCAL_MODEL_LIST_REQUEST's inventory (the daemon's models with
// their /api/show metadata, then the shipped catalogue) — with
// the delete its sibling contract LOCAL_MODEL_DELETE_REQUEST
// always offered and no UI ever sent. This is a different
// inventory from the Model Hub's downloaded section above it:
// the hub tracks what IT downloaded; this list is the daemon's
// own, so it also shows models pulled outside Aether (a terminal
// `ollama pull`), and deleting here removes the model from
// Ollama itself.
//
// Sends ride the tab's one-shot sendRequest (request_id
// correlation, offline queueing with honest dispositions), and a
// delete queued offline is derived from the real queue via
// pendingDeleteFor — never from a local flag.
export default function OllamaModelList({ sendRequest, pendingDeleteFor }) {
  const [models, setModels] = useState(null); // null = not yet answered
  const [loadError, setLoadError] = useState('');
  const [deletingName, setDeletingName] = useState('');
  const [deleteError, setDeleteError] = useState('');
  const [deleteNotice, setDeleteNotice] = useState('');

  const refresh = useCallback(() => {
    sendRequest('LOCAL_MODEL_LIST_REQUEST', {}, (payload) => {
      // The engine's error reply carries models: [] BESIDE the
      // error (E1-03) — the error wins, or a dead daemon would
      // render as an answered-empty inventory.
      if (payload && payload.error) {
        setLoadError(payload.error);
      } else if (payload && Array.isArray(payload.models)) {
        setModels(payload.models);
        setLoadError('');
      }
    });
  }, [sendRequest]);

  useEffect(() => { refresh(); }, [refresh]);

  const handleDelete = (model) => {
    // The hub delete's sibling discipline (D2-24): a destructive
    // removal confirms first, naming exactly what goes.
    if (!window.confirm(`Delete ${model.name} from Ollama? Its files are removed from this machine.`)) {
      return;
    }
    setDeleteError('');
    setDeleteNotice('');
    const disposition = sendRequest('LOCAL_MODEL_DELETE_REQUEST', { model: model.name }, (payload) => {
      setDeletingName('');
      if (payload && payload.deleted) {
        setDeleteNotice(`${model.name} deleted from Ollama.`);
        refresh();
      } else {
        setDeleteError(
          (payload && payload.error)
          || `The engine reports ${model.name} was not deleted — Ollama may not be running.`
        );
      }
    });
    if (disposition !== 'queued') {
      setDeletingName(model.name);
    }
  };

  const downloaded = (models || []).filter((m) => m.downloaded);
  const catalogueCount = (models || []).length - downloaded.length;

  return (
    <div data-testid="ollama-model-list">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '12px', marginBottom: '8px' }}>
        <h3 style={{ fontSize: '16px', fontWeight: 600, margin: 0, color: 'var(--text-primary)' }}>Ollama models (engine list)</h3>
        <button
          onClick={refresh}
          className="aether-solid-btn"
          style={{ padding: '6px 12px', fontSize: '12px', display: 'inline-flex', alignItems: 'center', gap: '6px', cursor: 'pointer', color: 'var(--text-primary)', border: '1px solid var(--border-default)', borderRadius: '10px' }}
        >
          <RefreshCw size={13} /> Refresh
        </button>
      </div>
      <p style={{ color: 'var(--text-secondary)', fontSize: '13px', margin: '0 0 16px 0', lineHeight: '1.5' }}>
        The engine&apos;s own list from the Ollama daemon — including any model you pulled
        outside Aether. Deleting here removes the model from Ollama itself.
      </p>

      {deleteNotice && (
        <div data-testid="ollama-delete-notice" style={{ fontSize: '13px', color: 'var(--success)', marginBottom: '12px' }}>
          {deleteNotice}
        </div>
      )}
      {deleteError && (
        <div data-testid="ollama-delete-error" role="alert" style={{ fontSize: '13px', color: 'var(--danger)', marginBottom: '12px' }}>
          {deleteError}
        </div>
      )}

      {models === null && !loadError ? (
        <div style={{ color: 'var(--text-secondary)', fontSize: '14px' }}>Loading the engine&apos;s Ollama list…</div>
      ) : loadError ? (
        <div data-testid="ollama-list-error" role="alert" style={{ color: 'var(--danger)', fontSize: '14px' }}>
          The engine could not list Ollama models: {loadError}
        </div>
      ) : downloaded.length === 0 ? (
        <div style={{ color: 'var(--text-secondary)', fontSize: '14px' }}>
          No models are downloaded in Ollama — the daemon reports none installed.
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
          {downloaded.map((model) => {
            const deleteQueued = Boolean(pendingDeleteFor && pendingDeleteFor(model.name));
            const deleting = deletingName === model.name;
            return (
              <div
                key={model.name}
                data-testid={`ollama-model-${model.name}`}
                style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '12px', padding: '10px 14px', background: 'var(--bg-raised)', border: '1px solid var(--border-default)', borderRadius: '12px' }}
              >
                <div style={{ minWidth: 0 }}>
                  <div style={{ fontSize: '14px', fontWeight: 500, color: 'var(--text-primary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    {model.name}
                  </div>
                  <div style={{ fontSize: '12px', color: 'var(--text-muted)' }}>
                    {[model.size, model.type, model.parameter_size, model.quantization].filter(Boolean).join(' · ') || 'Downloaded'}
                  </div>
                  {/* Spec D: the engine-judged fit verdict and the
                      backend's residency report ride the same
                      shared badges as the hub surfaces; both omit
                      while unjudged/unreported. */}
                  {(model.fit || model.loaded !== undefined) && (
                    <div style={{ display: 'flex', gap: '6px', marginTop: '6px', flexWrap: 'wrap' }}>
                      <FitBadge fit={model.fit} testId={`ollama-fit-${model.name}`} />
                      <LoadStateBadge loaded={model.loaded} testId={`ollama-load-state-${model.name}`} />
                    </div>
                  )}
                </div>
                {deleteQueued ? (
                  <span data-testid={`ollama-delete-queued-${model.name}`} style={{ fontSize: '12px', color: 'var(--accent)', fontWeight: 500, flexShrink: 0 }}>
                    Delete queued
                  </span>
                ) : (
                  <button
                    onClick={() => handleDelete(model)}
                    disabled={deleting}
                    data-testid={`ollama-delete-${model.name}`}
                    className="aether-solid-btn"
                    title="Delete from Ollama"
                    style={{ padding: '8px 10px', background: 'rgba(201,42,42,0.1)', color: 'var(--danger)', border: '1px solid rgba(201,42,42,0.2)', borderRadius: '10px', cursor: deleting ? 'default' : 'pointer', opacity: deleting ? 0.45 : 1, display: 'inline-flex', alignItems: 'center', gap: '6px', fontSize: '12px', flexShrink: 0 }}
                  >
                    <Trash2 size={14} /> {deleting ? 'Deleting…' : 'Delete'}
                  </button>
                )}
              </div>
            );
          })}
        </div>
      )}

      {models !== null && catalogueCount > 0 && (
        <div style={{ fontSize: '12px', color: 'var(--text-muted)', marginTop: '12px' }}>
          Plus {catalogueCount} catalogue model{catalogueCount === 1 ? '' : 's'} not downloaded — find them in the search above.
        </div>
      )}
    </div>
  );
}
