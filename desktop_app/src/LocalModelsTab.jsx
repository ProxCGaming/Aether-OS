import React, { useState, useEffect, useRef, useCallback } from 'react';
import { Search, Download, CheckCircle2, X, RefreshCw, ChevronDown, ChevronRight, AlertTriangle } from 'lucide-react';
import './chat.css';
import { formatBytes } from './formatBytes';
import {
  FAMILY_LABELS,
  SOURCE_LABELS,
  RUNNABILITY_BADGES,
  chipStyle,
  downloadButtonStyle,
} from './modelHubLabels';
import { DownloadProgressRow, DownloadedModelsSection } from './components/DownloadedModels';

// Model Hub (H4): this tab is the renderer's window onto the hub
// contracts built in H1–H3. Search runs against two live sources —
// Hugging Face (the engine queries its public API) and the shipped
// Ollama library — and every row is labelled by how Aether can run
// it (in-app / via Ollama / not locally runnable / unknown). The
// runnability label is the routing authority for downloads, exactly
// as in the engine: GGUF variants pull through Ollama, in-app speech
// repos land in the Hugging Face cache, and not-runnable rows get no
// download control at all.
//
// Search/detail/inventory are request/response pairs correlated by
// request_id. Download progress and lifecycle ride the shared task
// event stream (LOCAL_MODEL_DOWNLOAD_PROGRESS / TASK_COMPLETED /
// TASK_FAILED / TASK_CANCELLED) keyed by the engine's download key —
// the model name for Ollama pulls, the hf.co/<repo>:<quant> ref for
// GGUF pulls, the repo id for speech downloads — which the download
// RESPONSE hands back as `model`.

const SOURCES = [
  { value: 'hf', label: 'Hugging Face' },
  { value: 'ollama', label: 'Ollama' },
];

const FAMILY_CHIPS = [
  { value: 'all', label: 'All' },
  { value: 'brain', label: 'Brains' },
  { value: 'speech-to-text', label: 'Speech-to-text' },
  { value: 'text-to-speech', label: 'Text-to-speech' },
  { value: 'embedding', label: 'Embeddings' },
  { value: 'other', label: 'Other' },
];

function rowKey(row) {
  return `${row.source}:${row.id}`;
}

function formatDownloads(count) {
  if (count === null || count === undefined) return null;
  if (count >= 1_000_000) return `${(count / 1_000_000).toFixed(1)}M`;
  if (count >= 1_000) return `${(count / 1_000).toFixed(1)}k`;
  return `${count}`;
}

export default function LocalModelsTab({ wsRef, safeSend = null, pendingMessages = [], defaultProvider = '', defaultModel = '', currentEmbedder = '' }) {
  const [source, setSource] = useState('hf');
  const [searchQuery, setSearchQuery] = useState('');
  const [family, setFamily] = useState('all');
  const [results, setResults] = useState([]);
  const [resultsError, setResultsError] = useState(null);
  const [searching, setSearching] = useState(false);
  const [searchQueued, setSearchQueued] = useState(false);
  const [searched, setSearched] = useState(false);

  const [expandedKey, setExpandedKey] = useState(null);
  const [details, setDetails] = useState({}); // rowKey -> { detail, error }

  const [downloaded, setDownloaded] = useState([]);
  // D2-13: the inventory tracks whether the engine has ANSWERED.
  // It used to initialise to [] with no load state, so "No models
  // downloaded yet" rendered from the first frame — and for the
  // whole time a refresh sat queued offline — impersonating an
  // answered-empty inventory. 'ready' is only ever set by a real
  // answer carrying the model list.
  const [inventoryLoad, setInventoryLoad] = useState('loading'); // loading | queued | ready | error
  const [downloadProgress, setDownloadProgress] = useState({}); // engine key -> { percent, status }
  const [hubError, setHubError] = useState(null);

  // Phase 10.3 C2 (preserved): which model is the engine's current
  // default, as { provider, model }. Seeded from props (derived from
  // the providers array by SettingsPanel) and kept live from
  // MODEL_DEFAULT_CHANGED broadcasts.
  const [activeDefault, setActiveDefault] = useState({ provider: defaultProvider, model: defaultModel });

  // D2-14: a Set Active click is only a request until the engine
  // confirms it. The pending model shows a "Setting…" state; an
  // engine ERROR refusal (correlated by request_id) or a confirm
  // window with no answer at all surfaces through hubError instead
  // of the old behaviour — the row simply never changed and
  // nothing told the user why.
  const SET_ACTIVE_CONFIRM_TIMEOUT_MS = 10000;
  const [setActivePending, setSetActivePending] = useState('');
  const setActiveTimerRef = useRef(null);
  const clearSetActivePending = useCallback(() => {
    if (setActiveTimerRef.current) {
      clearTimeout(setActiveTimerRef.current);
      setActiveTimerRef.current = null;
    }
    setSetActivePending('');
  }, []);

  useEffect(() => () => {
    if (setActiveTimerRef.current) clearTimeout(setActiveTimerRef.current);
  }, []);

  // The engine's configured embedder (UserConfig.embedding_model,
  // seeded by SettingsPanel from the applied config) and the
  // in-flight "Use as embedder" switch, confirmed by the engine's
  // WORKSPACE_INDEX_CONFIG_RESPONSE before the row claims it.
  const [activeEmbedder, setActiveEmbedder] = useState(currentEmbedder);
  const [embedderSwitchPending, setEmbedderSwitchPending] = useState('');
  const embedderRequestRef = useRef('');

  // request_id -> one-shot response handler for the hub pairs.
  const pendingRef = useRef(new Map());
  // rowKey -> engine download keys, learned from
  // MODEL_HUB_DOWNLOAD_RESPONSE (the GGUF key is an hf.co ref, not
  // the repo id, so progress events can't be matched by id alone).
  const [keysByRow, setKeysByRow] = useState({});
  const searchSeqRef = useRef(0);

  useEffect(() => {
    setActiveDefault({ provider: defaultProvider, model: defaultModel });
  }, [defaultProvider, defaultModel]);

  // Re-seed the active embedder when the applied config's value
  // lands/changes (render-time adjustment, React's documented
  // pattern for prop-derived state).
  const [embedderSeed, setEmbedderSeed] = useState(currentEmbedder);
  if (currentEmbedder !== embedderSeed) {
    setEmbedderSeed(currentEmbedder);
    setActiveEmbedder(currentEmbedder);
  }

  // RUX-A-19: every send in this tab used to route through one of
  // two readyState-gated helpers whose discarded/null result every
  // caller ignored — offline, the search spinner never cleared,
  // download / delete / detail clicks silently did nothing, cancel
  // cleared the progress UI while the engine kept downloading, and
  // set-active (the REF model-switch pattern's fourth path) was
  // thrown away. Both helpers now go through safeSend and return
  // the disposition ('sent' | 'queued'), so callers can keep local
  // state honest about what is merely queued. The hub pairs are
  // answered against their request_id, so the id rides along as
  // safeSend's correlation id and the one-shot handler stays
  // registered: a request queued offline is answered for real when
  // the queue flushes on reconnect. The standalone-mount fallback
  // keeps the exact legacy envelopes on the unconditional facade
  // send (the E6-F15 discipline).
  const sendRequest = useCallback((type, payload, onResponse) => {
    if (!safeSend && !wsRef.current) return null;
    const requestId = `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
    if (onResponse) pendingRef.current.set(requestId, onResponse);
    if (safeSend) return safeSend(type, payload, requestId);
    return wsRef.current.send(JSON.stringify({ type, schema_version: 1, request_id: requestId, payload }));
  }, [wsRef, safeSend]);

  const sendAction = useCallback((type, payload) => {
    if (safeSend) return safeSend(type, payload);
    const ws = wsRef.current;
    if (ws) {
      return ws.send(JSON.stringify({
        type, schema_version: 1, request_id: Date.now().toString(), payload,
      }));
    }
    return undefined;
  }, [wsRef, safeSend]);

  // Queued affordances derive from the REAL offline queue
  // (pendingMessages), so each one clears itself the moment the
  // queue flushes — no local flag can drift from the truth.
  const pendingHubEntry = useCallback((type, match) => (pendingMessages || []).find(
    (m) => m && m.type === type && m.payload && match(m.payload)
  ), [pendingMessages]);
  const pendingDownloadFor = useCallback((row) => pendingHubEntry(
    'MODEL_HUB_DOWNLOAD_REQUEST',
    (p) => p.source === row.source && p.id === row.id
  ), [pendingHubEntry]);
  const pendingDeleteFor = useCallback((row) => pendingHubEntry(
    'MODEL_HUB_DELETE_REQUEST',
    (p) => p.source === row.source && p.id === row.id
  ), [pendingHubEntry]);
  const pendingSetActiveFor = useCallback((row) => pendingHubEntry(
    'MODEL_SET_DEFAULT',
    (p) => p.provider === 'ollama' && p.model === row.id
  ), [pendingHubEntry]);
  const pendingCancelFor = useCallback((engineKey) => pendingHubEntry(
    'LOCAL_MODEL_DOWNLOAD_CANCEL',
    (p) => p.model === engineKey
  ), [pendingHubEntry]);
  const pendingEmbedderFor = useCallback((row) => pendingHubEntry(
    'WORKSPACE_INDEX_CONFIG_REQUEST',
    (p) => p.embedder === row.id
  ), [pendingHubEntry]);

  const refreshDownloaded = useCallback(() => {
    const disposition = sendRequest('MODEL_HUB_DOWNLOADED_REQUEST', {}, (payload) => {
      if (payload && Array.isArray(payload.models)) {
        setDownloaded(payload.models);
        setInventoryLoad('ready');
      }
      if (payload && payload.error) {
        setHubError(payload.error);
        // An errored answer before the first good one is the error
        // state; once the inventory is on screen, a failed refresh
        // keeps the rows and reports through the hub error banner.
        setInventoryLoad((prev) => (prev === 'ready' ? prev : 'error'));
      }
    });
    if (disposition === 'queued') {
      setInventoryLoad((prev) => (prev === 'ready' ? prev : 'queued'));
    } else if (disposition) {
      setInventoryLoad((prev) => (prev === 'ready' ? prev : 'loading'));
    }
  }, [sendRequest]);

  const runSearch = useCallback(() => {
    const seq = ++searchSeqRef.current;
    setSearching(true);
    const disposition = sendRequest(
      'MODEL_HUB_SEARCH_REQUEST',
      { source, query: searchQuery, family: family === 'all' ? null : family },
      (payload) => {
        if (seq !== searchSeqRef.current) return; // a newer search owns the list
        setSearching(false);
        setSearchQueued(false);
        setSearched(true);
        setResults(payload && Array.isArray(payload.models) ? payload.models : []);
        setResultsError(payload && payload.error ? payload.error : null);
      }
    );
    if (disposition === 'queued') {
      // RUX-A-19: end the spinner NOW with the honest outcome — the
      // search is queued, not running. Its response handler above
      // stays registered and completes the search for real when the
      // flushed request is answered after reconnect.
      setSearching(false);
      setSearchQueued(true);
    } else if (disposition) {
      setSearchQueued(false);
    }
  }, [sendRequest, source, searchQuery, family]);

  useEffect(() => { runSearch(); }, [runSearch]);
  useEffect(() => { refreshDownloaded(); }, [refreshDownloaded]);

  // One socket listener: hub responses (correlated by request_id),
  // download progress + task lifecycle, and the default-changed
  // broadcast.
  useEffect(() => {
    if (!wsRef.current) return;
    const clearProgress = (model) => {
      setDownloadProgress(prev => {
        if (!model || !(model in prev)) return prev;
        const next = { ...prev };
        delete next[model];
        return next;
      });
    };
    const handleMessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        const payload = data.payload || {};
        if (data.request_id && pendingRef.current.has(data.request_id)) {
          const handler = pendingRef.current.get(data.request_id);
          pendingRef.current.delete(data.request_id);
          handler(payload);
          return;
        }
        if (data.type === 'LOCAL_MODEL_DOWNLOAD_PROGRESS' && payload.model) {
          setDownloadProgress(prev => ({
            ...prev,
            [payload.model]: { percent: payload.download_percent, status: payload.status },
          }));
        } else if (data.type === 'MODEL_DEFAULT_CHANGED') {
          setActiveDefault({ provider: payload.provider || '', model: payload.model || '' });
          // A default change landing from anywhere resolves this
          // tab's wait too (its own correlated answer is consumed
          // by the pending handler above and never reaches here).
          clearSetActivePending();
        } else if (data.type === 'WORKSPACE_INDEX_CONFIG_RESPONSE') {
          // The embedder switch is confirmed only now — the row
          // must not claim "Embedder" from the click alone.
          if (payload.ok && embedderRequestRef.current) {
            setActiveEmbedder(embedderRequestRef.current);
          }
          embedderRequestRef.current = '';
          setEmbedderSwitchPending('');
        } else if (data.type === 'TASK_COMPLETED' && payload.model) {
          clearProgress(payload.model);
          refreshDownloaded();
        } else if (data.type === 'TASK_FAILED' && payload.model) {
          clearProgress(payload.model);
          if (payload.error) setHubError(payload.error);
          refreshDownloaded();
        } else if (data.type === 'TASK_CANCELLED' && payload.model) {
          clearProgress(payload.model);
          refreshDownloaded();
        }
      } catch (e) {}
    };
    wsRef.current.addEventListener('message', handleMessage);
    return () => {
      if (wsRef.current) wsRef.current.removeEventListener('message', handleMessage);
    };
  }, [wsRef, refreshDownloaded, clearSetActivePending]);

  // ---- downloads -------------------------------------------------

  const isDownloaded = useCallback((row) => {
    return downloaded.some(d => {
      if (d.source === row.source && d.id === row.id) return true;
      // A downloaded HF GGUF lives in the Ollama store under its
      // hf.co/<repo>:<quant> name.
      return row.source === 'hf' && d.source === 'ollama'
        && typeof d.id === 'string' && d.id.startsWith(`hf.co/${row.id}`);
    });
  }, [downloaded]);

  const progressForRow = useCallback((row) => {
    const keys = [row.id, ...(keysByRow[rowKey(row)] || [])];
    for (const key of keys) {
      if (key && downloadProgress[key]) return { key, ...downloadProgress[key] };
    }
    return null;
  }, [downloadProgress, keysByRow]);

  const handleDownload = (row, variant) => {
    const payload = { source: row.source, id: row.id };
    // The engine matches the variant as a STRING (quant label or
    // filename) — never the variant object.
    if (variant) payload.variant = variant.quant || variant.file;
    setHubError(null);
    sendRequest('MODEL_HUB_DOWNLOAD_REQUEST', payload, (resp) => {
      if (resp && resp.success) {
        const engineKey = resp.model || row.id;
        const rk = rowKey(row);
        setKeysByRow(prev => {
          const existing = prev[rk] || [];
          return existing.includes(engineKey) ? prev : { ...prev, [rk]: [...existing, engineKey] };
        });
        setDownloadProgress(prev => ({ ...prev, [engineKey]: { percent: 0, status: 'starting' } }));
      } else {
        setHubError((resp && resp.error) || 'The engine refused the download.');
      }
    });
  };

  const handleCancel = (engineKey) => {
    const disposition = sendAction('LOCAL_MODEL_DOWNLOAD_CANCEL', { model: engineKey });
    if (disposition === 'queued') {
      // RUX-A-19: the engine is STILL downloading — clearing the
      // progress UI here (the old behaviour) told the user the
      // download had stopped when it had not. The progress stays
      // visible with a "Cancel queued" marking derived from the
      // real queue, and TASK_CANCELLED clears it for real when the
      // flushed cancel lands.
      return;
    }
    setDownloadProgress(prev => {
      const next = { ...prev };
      delete next[engineKey];
      return next;
    });
  };

  const handleDeleteDownloaded = (row) => {
    setHubError(null);
    sendRequest('MODEL_HUB_DELETE_REQUEST', { source: row.source, id: row.id }, (resp) => {
      if (resp && resp.success) {
        refreshDownloaded();
      } else {
        setHubError((resp && resp.error) || 'The engine could not delete that model.');
      }
    });
  };

  // Phase 10.3 C2 (preserved): set a downloaded Ollama model as the
  // engine's default via MODEL_SET_DEFAULT (provider 'ollama').
  // RUX-A-19: through safeSend this QUEUES while the engine is
  // unreachable (it was the REF pattern's discarded fourth path);
  // the row derives its "Set Active queued" state from the queue
  // and the MODEL_DEFAULT_CHANGED broadcast confirms it for real.
  // D2-14: the request now rides sendRequest, so the engine's
  // answer is correlated by request_id: MODEL_DEFAULT_CHANGED
  // confirms, an ERROR reply refuses (the engine refuses an empty
  // model and an unsavable switch that way), and silence past the
  // confirm window is reported instead of standing forever.
  const handleSetActive = (modelName) => {
    setHubError(null);
    const disposition = sendRequest('MODEL_SET_DEFAULT', { provider: 'ollama', model: modelName }, (payload) => {
      clearSetActivePending();
      if (payload && payload.error) {
        setHubError(payload.error);
      } else if (payload) {
        setActiveDefault({ provider: payload.provider || 'ollama', model: payload.model || modelName });
      }
    });
    if (disposition === 'queued') {
      // No local pending flag — the row derives "Set Active
      // queued" from the real queue, and the one-shot handler
      // above resolves the switch when the flushed request is
      // answered after reconnect.
      return;
    }
    setSetActivePending(modelName);
    if (setActiveTimerRef.current) clearTimeout(setActiveTimerRef.current);
    setActiveTimerRef.current = setTimeout(() => {
      setActiveTimerRef.current = null;
      setSetActivePending((prev) => (prev === modelName ? '' : prev));
      setHubError(
        `The engine did not confirm ${modelName} as the active model — it may be busy or offline. Try again.`
      );
    }, SET_ACTIVE_CONFIRM_TIMEOUT_MS);
  };

  // Set a downloaded embedding model as THE embedder (episodic
  // memory + workspace index share it) through the engine's
  // existing embedder config flow — the same
  // WORKSPACE_INDEX_CONFIG_REQUEST the Workspace Indexing panel's
  // picker sends: it persists UserConfig.embedding_model and
  // invalidates vectors stamped by the old embedder. The click
  // only starts the switch; the response marks it active.
  const handleUseAsEmbedder = (modelName) => {
    embedderRequestRef.current = modelName;
    const disposition = sendAction('WORKSPACE_INDEX_CONFIG_REQUEST', { embedder: modelName });
    if (disposition !== 'queued') {
      setEmbedderSwitchPending(modelName);
    }
    // Queued: no local pending flag — the button derives "Embedder
    // queued" from the real queue (pendingMessages), and the
    // flushed request's response confirms the switch for real.
  };

  // ---- detail ----------------------------------------------------

  const toggleDetails = (row) => {
    const key = rowKey(row);
    if (expandedKey === key) {
      setExpandedKey(null);
      return;
    }
    setExpandedKey(key);
    if (!details[key]) {
      const disposition = sendRequest('MODEL_HUB_DETAIL_REQUEST', { source: row.source, id: row.id }, (payload) => {
        setDetails(prev => ({
          ...prev,
          [key]: {
            detail: payload && payload.detail ? payload.detail : null,
            error: payload && payload.error ? payload.error : null,
          },
        }));
      });
      if (disposition === 'queued') {
        // RUX-A-19: say the details are queued instead of leaving
        // "Loading details…" up for a request that has not run yet;
        // the response handler above replaces this entry when the
        // flushed request is answered.
        setDetails(prev => ({
          ...prev,
          [key]: { detail: null, error: null, queued: true },
        }));
      }
    }
  };

  // ---- rendering -------------------------------------------------

  const renderProgress = (progress) => (
    <DownloadProgressRow
      progress={progress}
      cancelQueued={Boolean(pendingCancelFor(progress.key))}
      onCancel={handleCancel}
    />
  );

  const renderVariant = (row, variant) => {
    const label = variant.quant || (variant.kind === 'gguf' ? 'GGUF' : variant.kind);
    const downloadable = row.runnability === 'ollama' && variant.kind === 'gguf'
      && !isDownloaded(row) && !progressForRow(row) && !pendingDownloadFor(row);
    return (
      <div
        key={variant.file || variant.kind}
        data-testid={variant.quant ? `variant-${variant.quant}` : undefined}
        style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '12px', padding: '8px 10px', background: 'var(--bg-raised)', border: '1px solid var(--border-default)', borderRadius: '10px' }}
      >
        <div style={{ minWidth: 0 }}>
          <div style={{ fontSize: '13px', fontWeight: 600, color: 'var(--text-primary)' }}>
            {label}
            {variant.kind ? <span style={{ color: 'var(--text-secondary)', fontWeight: 500 }}> · {variant.kind}</span> : null}
          </div>
          {variant.file && (
            <div style={{ fontSize: '11px', color: 'var(--text-muted)', fontFamily: 'var(--font-mono)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{variant.file}</div>
          )}
          {Array.isArray(variant.files) && (
            <div style={{ fontSize: '11px', color: 'var(--text-muted)', fontFamily: 'var(--font-mono)' }}>{variant.files.length} files</div>
          )}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px', flexShrink: 0 }}>
          <span style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>
            {variant.size_bytes ? formatBytes(variant.size_bytes) : 'Size not reported'}
          </span>
          {downloadable && (
            <button onClick={() => handleDownload(row, variant)} className="ios-glass" style={{ ...downloadButtonStyle, padding: '6px 12px', fontSize: '12px' }}>
              <Download size={14} /> Download
            </button>
          )}
        </div>
      </div>
    );
  };

  const renderDetail = (row) => {
    const entry = details[rowKey(row)];
    if (!entry) {
      return <div style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>Loading details…</div>;
    }
    if (entry.queued && !entry.detail) {
      return (
        <div data-testid={`detail-queued-${rowKey(row)}`} style={{ fontSize: '13px', color: 'var(--accent)' }}>
          Details queued — will load when the engine reconnects.
        </div>
      );
    }
    if (entry.error) {
      return <div style={{ fontSize: '13px', color: 'var(--danger)' }}>{entry.error}</div>;
    }
    const detail = entry.detail;
    if (!detail) return null;
    const variants = Array.isArray(detail.variants) ? detail.variants : [];
    return (
      <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
        {detail.size_bytes ? (
          <div style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>
            Total size: {formatBytes(detail.size_bytes)}
          </div>
        ) : null}
        {variants.length === 0 && (
          <div style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>No downloadable variants listed.</div>
        )}
        {variants.map(v => renderVariant(row, v))}
      </div>
    );
  };

  const renderResultCard = (row) => {
    const badge = RUNNABILITY_BADGES[row.runnability] || RUNNABILITY_BADGES.unknown;
    const progress = progressForRow(row);
    const alreadyDownloaded = isDownloaded(row);
    const expanded = expandedKey === rowKey(row);
    const sizeText = row.size_bytes ? formatBytes(row.size_bytes) : null;
    const downloadsText = formatDownloads(row.downloads);
    // Whole-repo downloads: Ollama library rows and in-app speech
    // repos. HF GGUF repos download per variant from the detail.
    const wholeRepoDownloadable = !alreadyDownloaded && !progress && (
      row.source === 'ollama' || row.runnability === 'in-app'
    );

    return (
      <div
        key={rowKey(row)}
        data-testid={`result-row-${rowKey(row)}`}
        className="ios-glass"
        style={{ padding: '20px', borderRadius: 'var(--radius-lg)', display: 'flex', flexDirection: 'column', transition: 'all 0.3s cubic-bezier(0.16, 1, 0.3, 1)', position: 'relative', overflow: 'hidden' }}
      >
        {progress && (
          <div style={{ position: 'absolute', bottom: 0, left: 0, height: '4px', background: 'var(--accent)', width: `${progress.percent || 0}%`, transition: 'width 0.3s linear' }} />
        )}

        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: '8px', gap: '12px' }}>
          <h4 style={{ fontSize: '16px', fontWeight: 600, margin: 0, color: 'var(--text-primary)', overflow: 'hidden', textOverflow: 'ellipsis' }}>{row.name}</h4>
          {alreadyDownloaded ? (
            <div style={{ display: 'flex', alignItems: 'center', gap: '4px', background: 'rgba(76, 195, 138, 0.15)', color: 'var(--success)', padding: '4px 8px', borderRadius: '20px', fontSize: '11px', fontWeight: 600, letterSpacing: '0.05em', flexShrink: 0 }}>
              <CheckCircle2 size={12} /> DOWNLOADED
            </div>
          ) : sizeText ? (
            <div style={{ color: 'var(--text-muted)', fontSize: '12px', fontWeight: 500, flexShrink: 0, fontFamily: 'var(--font-mono)' }}>{sizeText}</div>
          ) : null}
        </div>

        {row.id !== row.name && (
          <div style={{ fontSize: '11px', color: 'var(--text-muted)', fontFamily: 'var(--font-mono)', marginBottom: '8px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{row.id}</div>
        )}

        {row.description && (
          <p style={{ color: 'var(--text-muted)', fontSize: '13px', lineHeight: '1.5', margin: '0 0 12px 0' }}>{row.description}</p>
        )}

        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px', marginBottom: '12px' }}>
          <span style={chipStyle}>{FAMILY_LABELS[row.family] || 'Other'}</span>
          {downloadsText && <span style={chipStyle}>{downloadsText} downloads</span>}
          {Array.isArray(row.formats) && row.formats.map(f => (
            <span key={f} style={chipStyle}>{f}</span>
          ))}
          {row.params && <span style={chipStyle}>{row.params} Params</span>}
          {row.quant && <span style={chipStyle}>{row.quant}</span>}
        </div>

        <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: '8px', marginBottom: '16px' }}>
          <span title={row.runnability_reason || ''} style={{ background: badge.bg, color: badge.color, border: `1px solid ${badge.border}`, padding: '4px 8px', borderRadius: '20px', fontSize: '11px', fontWeight: 600, letterSpacing: '0.03em' }}>
            {badge.label}
          </span>
          <span style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>
            {row.licence ? `Licence: ${row.licence}` : 'Licence unknown'}
          </span>
        </div>

        {row.runnability === 'not-runnable' && row.runnability_reason && (
          <p style={{ color: 'var(--danger)', fontSize: '12px', lineHeight: '1.5', margin: '0 0 16px 0' }}>{row.runnability_reason}</p>
        )}
        {row.runnability === 'unknown' && row.runnability_reason && (
          <p style={{ color: 'var(--text-secondary)', fontSize: '12px', lineHeight: '1.5', margin: '0 0 16px 0' }}>{row.runnability_reason}</p>
        )}

        <div style={{ display: 'flex', gap: '12px', alignItems: 'center', justifyContent: 'space-between', marginTop: 'auto' }}>
          {progress ? renderProgress(progress) : pendingDownloadFor(row) ? (
            <span data-testid={`download-queued-${rowKey(row)}`} style={{ fontSize: '12px', color: 'var(--accent)', fontWeight: 500 }}>
              Download queued — will start when the engine reconnects.
            </span>
          ) : wholeRepoDownloadable ? (
            <button onClick={() => handleDownload(row)} className="ios-glass" style={{ ...downloadButtonStyle, flex: 1 }}>
              <Download size={16} /> Download
            </button>
          ) : row.runnability === 'ollama' && row.source === 'hf' && !alreadyDownloaded ? (
            <span style={{ fontSize: '12px', color: 'var(--text-secondary)' }}>Pick a variant below to download via Ollama.</span>
          ) : <span />}
          <button
            onClick={() => toggleDetails(row)}
            className="ios-glass"
            aria-expanded={expanded}
            style={{ padding: '8px 12px', borderRadius: '10px', color: 'var(--text-primary)', cursor: 'pointer', border: '1px solid var(--border-default)', display: 'flex', alignItems: 'center', gap: '6px', fontSize: '12px', fontWeight: 500, flexShrink: 0 }}
          >
            {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />} Details
          </button>
        </div>

        {expanded && (
          <div style={{ marginTop: '16px', borderTop: '1px solid var(--border-default)', paddingTop: '16px' }}>
            {renderDetail(row)}
          </div>
        )}
      </div>
    );
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: '24px', paddingBottom: '32px' }}>

      {/* Header */}
      <div style={{ background: 'var(--bg-raised)', border: '1px solid var(--border-default)', borderRadius: 'var(--radius-lg)', padding: '20px' }}>
        <h3 style={{ fontSize: '18px', fontWeight: 600, margin: '0 0 8px 0', color: 'var(--text-primary)' }}>Model Hub</h3>
        <p style={{ color: 'var(--text-secondary)', fontSize: '14px', margin: 0, maxWidth: '560px', lineHeight: '1.5' }}>
          Search Hugging Face and the Ollama library, and download models in one click.
          Every model is labelled by how it runs: in the app itself (speech),
          via Ollama (GGUF brains), or not locally runnable — those are shown
          honestly, with the reason, and can't be downloaded from here.
        </p>
      </div>

      {/* Source switch + search */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
        <div style={{ display: 'flex', gap: '16px', alignItems: 'center', flexWrap: 'wrap' }}>
          <div role="group" aria-label="Model source" style={{ display: 'flex', background: 'var(--bg-overlay)', border: '1px solid var(--border-default)', borderRadius: '12px', padding: '4px', gap: '4px' }}>
            {SOURCES.map(s => (
              <button
                key={s.value}
                onClick={() => setSource(s.value)}
                aria-pressed={source === s.value}
                style={{
                  padding: '8px 16px', borderRadius: '9px', border: 'none', cursor: 'pointer',
                  fontSize: '13px', fontWeight: 600,
                  background: source === s.value ? 'rgba(var(--accent-rgb), 0.18)' : 'transparent',
                  color: source === s.value ? 'var(--accent)' : 'var(--text-secondary)',
                  transition: 'all 0.2s',
                }}
              >
                {s.label}
              </button>
            ))}
          </div>
          <div style={{ position: 'relative', flex: 1, minWidth: '220px' }}>
            <Search size={16} color="var(--text-secondary)" style={{ position: 'absolute', left: '16px', top: '50%', transform: 'translateY(-50%)' }} />
            <input
              type="text"
              placeholder="Search models…"
              value={searchQuery}
              onChange={e => setSearchQuery(e.target.value)}
              className="ios-glass-input"
              style={{ width: '100%', padding: '12px 16px 12px 42px', color: 'var(--text-primary)', fontSize: '14px', background: 'var(--bg-base)' }}
            />
          </div>
        </div>
        <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
          {FAMILY_CHIPS.map(chip => (
            <button
              key={chip.value}
              onClick={() => setFamily(chip.value)}
              aria-pressed={family === chip.value}
              style={{
                padding: '6px 12px', borderRadius: '20px', cursor: 'pointer', fontSize: '12px', fontWeight: 600,
                background: family === chip.value ? 'rgba(var(--accent-rgb), 0.18)' : 'var(--bg-raised)',
                color: family === chip.value ? 'var(--accent)' : 'var(--text-secondary)',
                border: `1px solid ${family === chip.value ? 'rgba(var(--accent-rgb), 0.4)' : 'var(--border-default)'}`,
                transition: 'all 0.2s',
              }}
            >
              {chip.label}
            </button>
          ))}
        </div>
      </div>

      {/* Inline hub error (download refusals, failures, delete errors) */}
      {hubError && (
        <div role="alert" style={{ display: 'flex', gap: '10px', alignItems: 'flex-start', background: 'rgba(201,42,42, 0.1)', border: '1px solid rgba(201,42,42, 0.3)', color: 'var(--danger)', borderRadius: '12px', padding: '12px 16px', fontSize: '13px', lineHeight: '1.5' }}>
          <AlertTriangle size={16} style={{ flexShrink: 0, marginTop: '1px' }} />
          <span style={{ flex: 1 }}>{hubError}</span>
          <button onClick={() => setHubError(null)} aria-label="Dismiss" style={{ background: 'none', border: 'none', color: 'var(--danger)', cursor: 'pointer', padding: 0 }}>
            <X size={14} />
          </button>
        </div>
      )}

      {/* Results */}
      <div>
        {resultsError ? (
          <div role="alert" style={{ background: 'rgba(201,42,42, 0.08)', border: '1px solid rgba(201,42,42, 0.25)', color: 'var(--danger)', borderRadius: '12px', padding: '16px', fontSize: '14px', lineHeight: '1.5' }}>
            {resultsError}
          </div>
        ) : searching && results.length === 0 ? (
          <div style={{ color: 'var(--text-secondary)', fontSize: '14px', display: 'flex', alignItems: 'center', gap: '8px' }}>
            <RefreshCw size={14} className="spin-animation" /> Searching {SOURCE_LABELS[source]}…
          </div>
        ) : searchQueued && results.length === 0 ? (
          <div data-testid="hub-search-queued" style={{ color: 'var(--accent)', fontSize: '14px' }}>
            Search queued — the engine is offline. Results will load when it reconnects.
          </div>
        ) : searched && results.length === 0 ? (
          <div style={{ color: 'var(--text-secondary)', fontSize: '14px' }}>
            No models found on {SOURCE_LABELS[source]} for this search.
          </div>
        ) : (
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(320px, 1fr))', gap: '16px' }}>
            {results.map(renderResultCard)}
          </div>
        )}
      </div>

      {/* Downloaded inventory — the download-manager seam lives in
          components/DownloadedModels; this tab passes the hub
          state and handlers down as props. */}
      <DownloadedModelsSection
        downloaded={downloaded}
        inventoryLoad={inventoryLoad}
        activeDefault={activeDefault}
        activeEmbedder={activeEmbedder}
        embedderSwitchPending={embedderSwitchPending}
        setActivePending={setActivePending}
        pendingSetActiveFor={pendingSetActiveFor}
        pendingDeleteFor={pendingDeleteFor}
        pendingEmbedderFor={pendingEmbedderFor}
        onRefresh={refreshDownloaded}
        onSetActive={handleSetActive}
        onUseAsEmbedder={handleUseAsEmbedder}
        onDeleteDownloaded={handleDeleteDownloaded}
      />

    </div>
  );
}
