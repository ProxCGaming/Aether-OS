import React from 'react';
import { Trash2, RefreshCw, X } from 'lucide-react';
import { formatBytes } from '../formatBytes';
import {
  FAMILY_LABELS,
  SOURCE_LABELS,
  TYPE_LABELS,
  chipStyle,
} from '../modelHubLabels';

// Wave 5 (Annex C row 9): the Model Hub's download-manager seam,
// split out of LocalModelsTab.jsx. The tab keeps the catalogue and
// every hub state machine (download progress, the offline queue,
// the set-active / embedder confirmations); these components are
// the inventory's presentation — the in-card progress row, one
// downloaded row, and the Downloaded section with its load states
// — driven entirely by props, in the ModelsPanel convention.

export function DownloadProgressRow({ progress, cancelQueued = false, onCancel }) {
  // RUX-A-19: a cancel sitting in the offline queue is marked on
  // the progress row it belongs to — the download is still
  // running until TASK_CANCELLED says otherwise.
  return (
    <>
      <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: 'rgb(var(--accent-rgb))', fontSize: '13px', fontWeight: 600 }}>
        <RefreshCw size={14} className="spin-animation" /> {Math.round(progress.percent || 0)}%
        {progress.status ? <span style={{ color: 'var(--text-secondary)', fontWeight: 500 }}>{progress.status}</span> : null}
        {cancelQueued && (
          <span data-testid={`cancel-queued-${progress.key}`} style={{ color: 'var(--accent)', fontWeight: 500 }}>
            Cancel queued — still downloading
          </span>
        )}
      </div>
      <button onClick={() => onCancel(progress.key)} disabled={cancelQueued} className="aether-solid-btn" style={{ padding: '8px', borderRadius: '10px', color: 'var(--danger)', cursor: cancelQueued ? 'default' : 'pointer', opacity: cancelQueued ? 0.45 : 1, border: '1px solid rgba(201,42,42,0.3)' }} title={cancelQueued ? 'Cancel already queued — will apply when the engine reconnects' : 'Cancel Download'}>
        <X size={14} />
      </button>
    </>
  );
}

export function DownloadedModelRow({
  row,
  rowKey,
  isActive = false,
  activeEmbedder = '',
  embedderSwitchPending = '',
  setActivePending = '',
  setActiveQueued = false,
  deleteQueued = false,
  embedderQueued = false,
  onSetActive,
  onUseAsEmbedder,
  onDeleteDownloaded,
}) {
  const sizeText = row.size_bytes ? formatBytes(row.size_bytes) : null;
  // Embedder eligibility: the daemon said this model can embed
  // (type), or the hub family already classes it as an embedding
  // model. The active one is the engine's configured embedder.
  const canEmbed = row.source === 'ollama'
    && (row.type === 'embedding' || row.type === 'both' || row.family === 'embedding');
  const isEmbedder = canEmbed && activeEmbedder === row.id;
  const embedderPending = embedderSwitchPending === row.id;
  return (
    <div
      data-testid={`downloaded-row-${rowKey}`}
      style={{ display: 'flex', alignItems: 'center', gap: '16px', padding: '14px 16px', background: 'var(--bg-raised)', border: '1px solid var(--border-default)', borderRadius: '12px' }}
    >
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
          <span style={{ fontSize: '14px', fontWeight: 600, color: 'var(--text-primary)' }}>{row.name}</span>
          <span style={chipStyle}>{SOURCE_LABELS[row.source] || row.source}</span>
          <span style={chipStyle}>{FAMILY_LABELS[row.family] || 'Other'}</span>
          {row.type && (
            <span data-testid={`downloaded-type-${rowKey}`} style={{ ...chipStyle, color: row.type === 'embedding' || row.type === 'both' ? 'var(--accent)' : chipStyle.color }}>
              {TYPE_LABELS[row.type] || row.type}
            </span>
          )}
          {row.params && <span style={chipStyle}>{row.params} Params</span>}
          {row.quant && <span style={chipStyle}>{row.quant}</span>}
          {sizeText && <span style={{ fontSize: '12px', color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>{sizeText}</span>}
        </div>
        {row.path && (
          <div style={{ fontSize: '11px', color: 'var(--text-muted)', fontFamily: 'var(--font-mono)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', marginTop: '4px' }} title={row.path}>{row.path}</div>
        )}
      </div>
      {canEmbed && (
        isEmbedder ? (
          <button disabled data-testid={`embedder-active-${rowKey}`} title="Currently the embedding model" className="aether-solid-btn" style={{ padding: '8px 16px', background: 'rgba(var(--accent-rgb), 0.15)', color: 'var(--accent)', border: '1px solid rgba(var(--accent-rgb), 0.3)', borderRadius: '10px', cursor: 'default', fontWeight: 500, fontSize: '13px' }}>
            Embedder
          </button>
        ) : embedderQueued ? (
          <button disabled data-testid={`embedder-queued-${rowKey}`} title="Embedder switch queued — will apply when the engine reconnects" className="aether-solid-btn" style={{ padding: '8px 16px', background: 'rgba(var(--accent-rgb), 0.12)', color: 'var(--accent)', border: '1px solid rgba(var(--accent-rgb), 0.35)', borderRadius: '10px', cursor: 'default', fontWeight: 500, fontSize: '13px' }}>
            Embedder Queued
          </button>
        ) : embedderPending ? (
          <button disabled data-testid={`embedder-pending-${rowKey}`} title="Waiting for the engine to confirm the embedder switch" className="aether-solid-btn" style={{ padding: '8px 16px', background: 'var(--bg-hover)', color: 'var(--text-secondary)', border: '1px solid var(--border-strong)', borderRadius: '10px', cursor: 'default', fontWeight: 500, fontSize: '13px' }}>
            Setting…
          </button>
        ) : (
          <button onClick={() => onUseAsEmbedder(row.id)} data-testid={`use-as-embedder-${rowKey}`} title="Use this model for memory + workspace embeddings" className="aether-solid-btn" style={{ padding: '8px 16px', background: 'rgba(var(--accent-rgb), 0.15)', color: 'var(--accent)', border: '1px solid rgba(var(--accent-rgb), 0.35)', borderRadius: '10px', cursor: 'pointer', fontWeight: 500, fontSize: '13px' }}>
            Use as Embedder
          </button>
        )
      )}
      {row.source === 'ollama' && (
        isActive ? (
          <button disabled title="Currently the active model" className="aether-solid-btn" style={{ padding: '8px 16px', background: 'rgba(76, 195, 138, 0.15)', color: 'var(--success)', border: '1px solid rgba(76, 195, 138, 0.3)', borderRadius: '10px', cursor: 'default', fontWeight: 500, fontSize: '13px' }}>
            Active
          </button>
        ) : setActiveQueued ? (
          <button disabled data-testid={`set-active-queued-${rowKey}`} title="Set-active queued — will apply when the engine reconnects" className="aether-solid-btn" style={{ padding: '8px 16px', background: 'rgba(var(--accent-rgb), 0.12)', color: 'var(--accent)', border: '1px solid rgba(var(--accent-rgb), 0.35)', borderRadius: '10px', cursor: 'default', fontWeight: 500, fontSize: '13px' }}>
            Set Active Queued
          </button>
        ) : setActivePending === row.id ? (
          <button disabled data-testid={`set-active-pending-${rowKey}`} title="Waiting for the engine to confirm the switch" className="aether-solid-btn" style={{ padding: '8px 16px', background: 'var(--bg-hover)', color: 'var(--text-secondary)', border: '1px solid var(--border-strong)', borderRadius: '10px', cursor: 'default', fontWeight: 500, fontSize: '13px' }}>
            Setting…
          </button>
        ) : (
          <button onClick={() => onSetActive(row.id)} title="Set as the active model" className="aether-solid-btn" style={{ padding: '8px 16px', background: 'var(--accent)', color: 'var(--accent-on)', border: '1px solid var(--accent)', borderRadius: '10px', cursor: 'pointer', fontWeight: 500, fontSize: '13px' }}>
            Set Active
          </button>
        )
      )}
      {deleteQueued && (
        <span data-testid={`hub-delete-queued-${rowKey}`} style={{ fontSize: '12px', color: 'var(--accent)', fontWeight: 500 }}>
          Delete queued
        </span>
      )}
      <button onClick={() => onDeleteDownloaded(row)} disabled={deleteQueued} className="aether-solid-btn" style={{ padding: '8px 10px', background: 'rgba(201,42,42,0.1)', color: 'var(--danger)', border: '1px solid rgba(201,42,42,0.2)', borderRadius: '10px', cursor: deleteQueued ? 'default' : 'pointer', opacity: deleteQueued ? 0.45 : 1 }} title={deleteQueued ? 'Delete already queued — will apply when the engine reconnects' : 'Delete Model'}>
        <Trash2 size={16} />
      </button>
    </div>
  );
}

export function DownloadedModelsSection({
  downloaded = [],
  inventoryLoad = 'ready',
  activeDefault = { provider: '', model: '' },
  activeEmbedder = '',
  embedderSwitchPending = '',
  setActivePending = '',
  pendingSetActiveFor,
  pendingDeleteFor,
  pendingEmbedderFor,
  onRefresh,
  onSetActive,
  onUseAsEmbedder,
  onDeleteDownloaded,
}) {
  return (
    <div data-testid="downloaded-section">
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
        <h3 style={{ fontSize: '16px', fontWeight: 600, margin: 0, color: 'var(--text-primary)' }}>Downloaded</h3>
        <button onClick={onRefresh} className="aether-solid-btn" style={{ padding: '8px', borderRadius: '10px', color: 'var(--text-secondary)', cursor: 'pointer', border: '1px solid var(--border-default)' }} title="Refresh downloaded models">
          <RefreshCw size={14} />
        </button>
      </div>
      {inventoryLoad === 'loading' ? (
        <div data-testid="downloaded-loading" style={{ color: 'var(--text-secondary)', fontSize: '14px', display: 'flex', alignItems: 'center', gap: '8px' }}>
          <RefreshCw size={14} className="spin-animation" /> Loading downloaded models…
        </div>
      ) : inventoryLoad === 'queued' ? (
        <div data-testid="downloaded-queued" style={{ color: 'var(--accent)', fontSize: '14px' }}>
          Downloaded list queued — will load when the engine reconnects.
        </div>
      ) : inventoryLoad === 'error' ? (
        <div data-testid="downloaded-error" role="alert" style={{ color: 'var(--danger)', fontSize: '14px' }}>
          The downloaded model list could not be loaded — use Refresh above to try again.
        </div>
      ) : downloaded.length === 0 ? (
        <div style={{ color: 'var(--text-secondary)', fontSize: '14px' }}>
          No models downloaded yet — download one from the catalogue above and it will appear here.
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
          {downloaded.map((row) => {
            // RUX-A-19: queued states for this row, from the real queue.
            const key = `${row.source}:${row.id}`;
            return (
              <DownloadedModelRow
                key={key}
                row={row}
                rowKey={key}
                isActive={row.source === 'ollama'
                  && activeDefault.provider === 'ollama' && activeDefault.model === row.id}
                activeEmbedder={activeEmbedder}
                embedderSwitchPending={embedderSwitchPending}
                setActivePending={setActivePending}
                setActiveQueued={Boolean(pendingSetActiveFor(row))}
                deleteQueued={Boolean(pendingDeleteFor(row))}
                embedderQueued={Boolean(pendingEmbedderFor(row))}
                onSetActive={onSetActive}
                onUseAsEmbedder={onUseAsEmbedder}
                onDeleteDownloaded={onDeleteDownloaded}
              />
            );
          })}
        </div>
      )}
    </div>
  );
}
