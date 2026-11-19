import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import {
  Layers,
  Pause,
  Play,
  RefreshCw,
  Search,
  AlertTriangle,
  FileText,
  FileX,
  CheckCircle2,
  Clock,
  Sliders,
  Folder,
} from 'lucide-react';
import { useAetherSocket } from '../../contexts/SocketContext';
import LoadStateRow from './LoadStateRow';
import { formatBytes } from '../../formatBytes';

const REASON_METADATA = {
  gitignore: { label: 'Gitignored', color: 'var(--accent)', bg: 'rgba(var(--accent-rgb), 0.15)' },
  too_large: { label: 'Too large (>512 KB)', color: 'var(--danger)', bg: 'rgba(201,42,42, 0.15)' },
  binary: { label: 'Binary', color: 'var(--text-secondary)', bg: 'rgba(148, 148, 143, 0.15)' },
  lockfile: { label: 'Lockfile', color: 'var(--accent)', bg: 'rgba(var(--accent-rgb), 0.15)' },
  not_prose: { label: 'Not prose', color: 'var(--text-muted)', bg: 'rgba(148, 148, 143, 0.15)' },
};

export default function WorkspaceIndexingPanel({ wsRef: propWsRef, initialWorkspace = '' }) {
  const socket = useAetherSocket();
  const wsRef = propWsRef || socket?.wsRef;
  const contextSendMessage = socket?.sendMessage;
  const isConnected = socket?.isConnected;
  // RUX-C-07: the status record below starts as a fabricated
  // placeholder (an embedder the user may not have, zero files) —
  // until the first real status reply lands, the panel renders a
  // load state instead of those invented numbers. A status reply
  // carrying an error is the error state; progress events only
  // exist once the engine knows the status, so they answer it too.
  const [statusLoad, setStatusLoad] = useState('loading');

  const [activeWorkspace, setActiveWorkspace] = useState(initialWorkspace);
  const [workspaces, setWorkspaces] = useState([]);
  // D2-15: indexing actions (pause / re-index / embedder switch)
  // write optimistic state before the engine answers. A refusal —
  // or no answer at all — must roll that state back and say so;
  // this is the panel's error surface for action outcomes.
  const [actionError, setActionError] = useState('');
  // D2-16: request_id -> workspace_root for every request this
  // panel sends with a workspace, so a late response can be
  // correlated to the workspace it belongs to (the engine echoes
  // request_id; its status payload carries no workspace field).
  const requestWorkspaceRef = useRef(new Map());
  // The one optimistic action still awaiting the engine's answer:
  // { kind: 'pause' | 'start' | 'embedder', workspace, snapshot,
  // requestedPaused? } — the snapshot is the last confirmed status,
  // restored verbatim on a refusal or a confirm timeout.
  const pendingActionRef = useRef(null);
  const actionTimerRef = useRef(null);
  const activeWorkspaceRef = useRef(initialWorkspace);
  const [status, setStatus] = useState({
    status: 'idle',
    total_files: 0,
    indexed_files: 0,
    total_chunks: 0,
    embedder: 'qwen3-embedding:0.6b',
    paused: false,
    indexed_docs: [],
    excluded_files: [],
  });
  const [progress, setProgress] = useState(null);
  const [activeTab, setActiveTab] = useState('indexed'); // 'indexed' | 'excluded'
  const [filterQuery, setFilterQuery] = useState('');
  const [warningModal, setWarningModal] = useState({ isOpen: false, pendingEmbedder: '' });
  // Downloaded embedding models, from the engine's local-model
  // list (daemon-typed via /api/show): the embedder picker below
  // is fed from what the user actually HAS, not only the two
  // built-in names it used to hardcode.
  const [downloadedEmbedders, setDownloadedEmbedders] = useState([]);

  const sendEvent = useCallback(
    (type, payload = {}) => {
      // Unique per request (the hub panels' format): the old
      // Date.now() id could repeat within a millisecond, which
      // would defeat the D2-16 correlation map below.
      const requestId = `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
      const msg = {
        type,
        schema_version: 1,
        request_id: requestId,
        payload,
      };
      if (typeof payload?.workspace_root === 'string' && payload.workspace_root) {
        requestWorkspaceRef.current.set(requestId, payload.workspace_root);
      }
      if (contextSendMessage) {
        contextSendMessage(type, {
          schema_version: 1,
          request_id: requestId,
          payload,
        });
      } else if (wsRef?.current?.readyState === 1 || wsRef?.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify(msg));
      }
      return requestId;
    },
    [contextSendMessage, wsRef]
  );

  const requestStatus = useCallback(
    (workspaceRoot) => {
      if (!workspaceRoot) return;
      sendEvent('WORKSPACE_INDEX_STATUS_REQUEST', { workspace_root: workspaceRoot });
    },
    [sendEvent]
  );

  // ---- D2-15 optimistic-action bookkeeping ----------------------
  const ACTION_CONFIRM_TIMEOUT_MS = 10000;

  const clearPendingAction = useCallback(() => {
    pendingActionRef.current = null;
    if (actionTimerRef.current) {
      clearTimeout(actionTimerRef.current);
      actionTimerRef.current = null;
    }
  }, []);

  const rollbackPendingAction = useCallback(
    (message) => {
      const pending = pendingActionRef.current;
      clearPendingAction();
      if (pending) setStatus(pending.snapshot);
      setActionError(message);
    },
    [clearPendingAction]
  );

  const armPendingAction = useCallback(
    (action) => {
      clearPendingAction();
      pendingActionRef.current = action;
      actionTimerRef.current = setTimeout(() => {
        actionTimerRef.current = null;
        const pending = pendingActionRef.current;
        pendingActionRef.current = null;
        if (!pending) return;
        // The user has moved to another workspace: the optimistic
        // state on screen is no longer this action's to restore.
        if (pending.workspace !== activeWorkspaceRef.current) return;
        setStatus(pending.snapshot);
        setActionError(
          'The engine did not answer — the change was not applied. The last confirmed state has been restored.'
        );
        requestStatus(pending.workspace);
      }, ACTION_CONFIRM_TIMEOUT_MS);
    },
    [clearPendingAction, requestStatus]
  );

  useEffect(() => () => {
    if (actionTimerRef.current) clearTimeout(actionTimerRef.current);
  }, []);

  // Request initial workspace list if active workspace not specified
  useEffect(() => {
    if (!activeWorkspace) {
      sendEvent('WORKSPACE_LIST_REQUEST', {});
    }
  }, [activeWorkspace, sendEvent]);

  // Feed the embedder picker from the downloaded embedding models.
  useEffect(() => {
    sendEvent('LOCAL_MODEL_LIST_REQUEST', {});
  }, [sendEvent]);

  // Request status when activeWorkspace is ready
  useEffect(() => {
    activeWorkspaceRef.current = activeWorkspace;
    if (activeWorkspace) {
      // A (re)fetch for this workspace is unanswered until its
      // status reply lands — including workspace switches.
      setStatusLoad('loading');
      requestStatus(activeWorkspace);
    }
  }, [activeWorkspace, requestStatus]);

  // Listen for socket events
  useEffect(() => {
    if (!wsRef?.current) return;

    const handleMessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        // D2-16: which workspace does this response belong to?
        // Tracked requests answer with the workspace they were
        // sent for; a response tracked to a DIFFERENT workspace
        // than the selected one is stale and must not merge into
        // the current view. Untracked responses (no request_id, or
        // one this panel never sent) keep the legacy apply-by-type
        // behaviour.
        const trackedWorkspace = data.request_id
          ? requestWorkspaceRef.current.get(data.request_id)
          : undefined;
        if (data.request_id) requestWorkspaceRef.current.delete(data.request_id);
        const forOtherWorkspace =
          trackedWorkspace !== undefined && trackedWorkspace !== activeWorkspace;
        if (data.type === 'LOCAL_MODEL_LIST_RESPONSE') {
          const models = Array.isArray(data.payload?.models) ? data.payload.models : [];
          setDownloadedEmbedders(
            models
              .filter((m) => m && m.downloaded && (m.type === 'embedding' || m.type === 'both'))
              .map((m) => m.name)
              .filter(Boolean)
          );
        } else if (data.type === 'WORKSPACE_LIST_RESPONSE') {
          const list = data.payload?.workspaces || [];
          setWorkspaces(list);
          if (!activeWorkspace && list.length > 0) {
            const active = list.find((w) => w.is_active) || list[0];
            setActiveWorkspace(active.path);
          } else if (list.length === 0) {
            // No workspace exists, so no status fetch will ever be
            // sent — the placeholder gate must not spin forever.
            setStatusLoad('ready');
          }
        } else if (data.type === 'WORKSPACE_INDEX_STATUS_RESPONSE') {
          if (forOtherWorkspace) {
            // A late answer for a workspace the user has left —
            // dropping it is the whole D2-16 fix; the selected
            // workspace's own fetch is still outstanding/answered
            // separately.
          } else if (data.payload?.error) {
            setStatusLoad('error');
          } else if (data.payload) {
            setStatusLoad('ready');
            // An authoritative status for the selected workspace
            // supersedes any earlier action error.
            setActionError('');
            setStatus((prev) => ({
              ...prev,
              ...data.payload,
              paused: data.payload.paused ?? prev.paused,
              indexed_docs: data.payload.indexed_docs || prev.indexed_docs || [],
              excluded_files: data.payload.excluded_files || prev.excluded_files || [],
            }));
            if (data.payload.status !== 'indexing') {
              setProgress(null);
            }
          }
        } else if (data.type === 'WORKSPACE_INDEX_PROGRESS') {
          // Progress pushes carry their workspace in the payload
          // (and the start request's id) — same correlation rule.
          const progressWorkspace = data.payload?.workspace_root;
          if (
            forOtherWorkspace ||
            (progressWorkspace && progressWorkspace !== activeWorkspace)
          ) {
            // Another workspace's run — not this view's progress.
          } else {
            // Progress is only emitted for a workspace whose status
            // the engine is tracking — it answers the gate too.
            setStatusLoad('ready');
            setProgress(data.payload);
            if (data.payload.status) {
              setStatus((prev) => ({ ...prev, status: data.payload.status }));
            }
          }
        } else if (data.type === 'WORKSPACE_INDEX_PAUSE_RESPONSE') {
          if (!forOtherWorkspace) {
            const isPaused = Boolean(data.payload?.paused);
            const pending = pendingActionRef.current;
            if (pending?.kind === 'pause') {
              if (isPaused === pending.requestedPaused) {
                // Confirmed: the echo below applies the same value.
                clearPendingAction();
                setActionError('');
                setStatus((prev) => ({
                  ...prev,
                  paused: isPaused,
                  status: isPaused ? 'paused' : 'idle',
                }));
              } else {
                // The engine's state is not what was asked for —
                // the optimistic flip was not applied (D2-15).
                rollbackPendingAction(
                  'The engine did not apply the pause change — the last confirmed state has been restored.'
                );
              }
            } else {
              setStatus((prev) => ({
                ...prev,
                paused: isPaused,
                status: isPaused ? 'paused' : 'idle',
              }));
            }
          }
        } else if (data.type === 'WORKSPACE_INDEX_CONFIG_RESPONSE') {
          if (!forOtherWorkspace) {
            if (data.payload?.ok) {
              if (pendingActionRef.current?.kind === 'embedder') {
                clearPendingAction();
                setActionError('');
              }
              setStatusLoad('loading');
              requestStatus(activeWorkspace);
            } else if (pendingActionRef.current?.kind === 'embedder') {
              // D2-15: the refused embedder switch must not stand —
              // restore the embedder and the counts it zeroed.
              rollbackPendingAction(
                data.payload?.error ||
                  'The engine refused the embedder change — the last confirmed state has been restored.'
              );
            } else {
              setActionError(
                data.payload?.error || 'The engine refused the indexing change.'
              );
            }
          }
        } else if (data.type === 'WORKSPACE_INDEX_START_RESPONSE') {
          if (!forOtherWorkspace) {
            const pending = pendingActionRef.current;
            if (pending?.kind === 'start' && !data.payload?.started) {
              // D2-15: indexing never started — the optimistic
              // "indexing" state was never the engine's.
              rollbackPendingAction(
                'The engine did not start indexing — the last confirmed state has been restored.'
              );
            } else {
              if (pending?.kind === 'start') {
                clearPendingAction();
                setActionError('');
              }
              setStatusLoad('loading');
              requestStatus(activeWorkspace);
            }
          }
        }
      } catch {
        // ignore parse error
      }
    };

    const target = wsRef.current;
    target.addEventListener('message', handleMessage);
    return () => {
      if (target?.removeEventListener) {
        target.removeEventListener('message', handleMessage);
      }
    };
  }, [wsRef, activeWorkspace, sendEvent, requestStatus, clearPendingAction, rollbackPendingAction]);

  const handleTogglePause = () => {
    const nextPause = !status.paused;
    // D2-15: snapshot the confirmed state BEFORE the optimistic
    // flip — a refused or unanswered pause restores it.
    armPendingAction({
      kind: 'pause',
      workspace: activeWorkspace,
      snapshot: status,
      requestedPaused: nextPause,
    });
    setActionError('');
    sendEvent('WORKSPACE_INDEX_PAUSE_REQUEST', {
      workspace_root: activeWorkspace,
      pause: nextPause,
    });
    // optimistic update
    setStatus((prev) => ({
      ...prev,
      paused: nextPause,
      status: nextPause ? 'paused' : 'idle',
    }));
  };

  const handleReindex = () => {
    armPendingAction({
      kind: 'start',
      workspace: activeWorkspace,
      snapshot: status,
    });
    setActionError('');
    sendEvent('WORKSPACE_INDEX_START_REQUEST', {
      workspace_root: activeWorkspace,
      confirm: true,
      embedder: status.embedder,
    });
    setStatus((prev) => ({ ...prev, status: 'indexing' }));
  };

  const handleEmbedderSelectChange = (e) => {
    const selected = e.target.value;
    if (selected === status.embedder) return;
    // Show warning modal BEFORE executing switch
    setWarningModal({
      isOpen: true,
      pendingEmbedder: selected,
    });
  };

  const handleCancelEmbedderSwitch = () => {
    setWarningModal({ isOpen: false, pendingEmbedder: '' });
  };

  const handleConfirmEmbedderSwitch = () => {
    const newEmbedder = warningModal.pendingEmbedder;
    setWarningModal({ isOpen: false, pendingEmbedder: '' });

    // D2-15: the optimistic write below (new embedder, zeroed
    // counts) is only a claim until the CONFIG answer lands — keep
    // the confirmed state to restore on a refusal or a timeout.
    armPendingAction({
      kind: 'embedder',
      workspace: activeWorkspace,
      snapshot: status,
    });
    setActionError('');

    // Send config update
    sendEvent('WORKSPACE_INDEX_CONFIG_REQUEST', {
      workspace_root: activeWorkspace,
      embedder: newEmbedder,
    });

    // Also trigger re-index
    sendEvent('WORKSPACE_INDEX_START_REQUEST', {
      workspace_root: activeWorkspace,
      confirm: true,
      embedder: newEmbedder,
    });

    setStatus((prev) => ({
      ...prev,
      embedder: newEmbedder,
      status: 'indexing',
      indexed_files: 0,
      total_chunks: 0,
    }));
  };

  // Embedder picker options: the two built-ins, plus every
  // downloaded embedding-typed model, plus the currently active
  // embedder (always selectable, even before the list lands or if
  // it was pulled outside the app).
  const embedderOptions = useMemo(() => {
    const options = [
      { value: 'qwen3-embedding:0.6b', label: 'qwen3-embedding:0.6b (Standard, 1024 dims)' },
      { value: 'qwen3-embedding:4b', label: 'qwen3-embedding:4b (Quality, 1024 dims)' },
    ];
    for (const name of downloadedEmbedders) {
      if (!options.some((o) => o.value === name)) {
        options.push({ value: name, label: `${name} (Downloaded)` });
      }
    }
    const current = status.embedder || 'qwen3-embedding:0.6b';
    if (!options.some((o) => o.value === current)) {
      options.push({ value: current, label: current });
    }
    return options;
  }, [downloadedEmbedders, status.embedder]);

  // Filtered lists
  const filteredIndexedDocs = useMemo(() => {
    const docs = Array.isArray(status.indexed_docs) ? status.indexed_docs : [];
    if (!filterQuery) return docs;
    const q = filterQuery.toLowerCase();
    return docs.filter((d) => (d.file_path || '').toLowerCase().includes(q));
  }, [status.indexed_docs, filterQuery]);

  const filteredExcludedFiles = useMemo(() => {
    const files = Array.isArray(status.excluded_files) ? status.excluded_files : [];
    if (!filterQuery) return files;
    const q = filterQuery.toLowerCase();
    return files.filter(
      (f) =>
        (f.file_path || '').toLowerCase().includes(q) ||
        (f.reason || '').toLowerCase().includes(q)
    );
  }, [status.excluded_files, filterQuery]);

  return (
    <div style={{ color: 'var(--text-primary)', display: 'flex', flexDirection: 'column', gap: '20px' }}>
      {/* Header */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
            <Layers size={22} style={{ color: 'var(--accent)' }} />
            <h2 style={{ fontSize: '20px', fontWeight: 600, margin: 0 }}>Workspace Indexing</h2>
          </div>
          <p style={{ color: 'var(--text-secondary)', fontSize: '13px', margin: '4px 0 0 0' }}>
            Local prose embedding index for workspace RAG and deep file search citations.
          </p>
        </div>

        {/* Workspace Root Selector */}
        {workspaces.length > 1 && (
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <Folder size={14} style={{ color: 'var(--text-secondary)' }} />
            <select
              value={activeWorkspace}
              onChange={(e) => setActiveWorkspace(e.target.value)}
              aria-label="Workspace Root"
              style={{
                background: 'var(--bg-hover)',
                border: '1px solid var(--border-default)',
                borderRadius: '8px',
                color: 'var(--text-primary)',
                padding: '6px 12px',
                fontSize: '12px',
                outline: 'none',
              }}
            >
              {workspaces.map((w) => (
                <option key={w.path} value={w.path} style={{ background: 'var(--bg-raised)' }}>
                  {w.name || w.path}
                </option>
              ))}
            </select>
          </div>
        )}
      </div>

      {/* D2-15: a refused or unanswered indexing action reports
          here (and the optimistic state has already been rolled
          back by the time this renders). */}
      {actionError && (
        <div
          data-testid="indexing-action-error"
          role="alert"
          style={{
            background: 'rgba(201,42,42,0.08)',
            border: '1px solid rgba(201,42,42,0.3)',
            borderRadius: '10px',
            padding: '12px 16px',
            color: 'var(--danger)',
            fontSize: '13px',
          }}
        >
          {actionError}
        </div>
      )}

      {/* RUX-C-07: until the first real status reply lands, a load
          state stands where the status card would be — the
          placeholder record (idle, 0 files, a default embedder) is
          never rendered as if the engine had reported it. */}
      {statusLoad !== 'ready' ? (
        <LoadStateRow
          loadState={statusLoad}
          isConnected={isConnected}
          testId="indexing-status-load-state"
          loadingText="Loading indexing status…"
          offlineText="Waiting for the engine — indexing status will appear here once it connects."
          errorText="Indexing status could not be loaded."
        />
      ) : (
      <div
        style={{
          background: 'var(--bg-raised)',
          border: '1px solid var(--border-default)',
          borderRadius: '14px',
          padding: '16px 20px',
          display: 'flex',
          flexDirection: 'column',
          gap: '16px',
        }}
      >
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: '12px' }}>
          {/* Status Badge */}
          <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
            <div
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: '6px',
                padding: '4px 10px',
                borderRadius: '20px',
                fontSize: '12px',
                fontWeight: 500,
                background:
                  status.status === 'indexing'
                    ? 'rgba(var(--accent-rgb), 0.2)'
                    : status.status === 'paused' || status.paused
                    ? 'rgba(148, 148, 143, 0.2)'
                    : 'rgba(76, 195, 138, 0.2)',
                color:
                  status.status === 'indexing'
                    ? 'var(--accent)'
                    : status.status === 'paused' || status.paused
                    ? 'var(--text-secondary)'
                    : 'var(--success)',
              }}
            >
              <span
                style={{
                  width: '7px',
                  height: '7px',
                  borderRadius: '50%',
                  background: 'currentColor',
                }}
              />
              {status.status === 'indexing'
                ? 'Indexing'
                : status.status === 'paused' || status.paused
                ? 'Paused'
                : 'Idle / Up-to-date'}
            </div>

            {/* Path indicator */}
            {activeWorkspace && (
              <span style={{ fontSize: '12px', color: 'var(--text-secondary)', fontFamily: 'var(--font-mono)' }}>
                {activeWorkspace}
              </span>
            )}
          </div>

          {/* Action Buttons */}
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <button
              onClick={handleTogglePause}
              aria-label={status.paused || status.status === 'paused' ? 'Resume Indexing' : 'Pause Indexing'}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '6px',
                padding: '7px 14px',
                borderRadius: '8px',
                border: '1px solid var(--border-default)',
                background: status.paused ? 'rgba(76, 195, 138, 0.15)' : 'var(--bg-hover)',
                color: status.paused ? 'var(--success)' : 'var(--text-primary)',
                fontSize: '12px',
                fontWeight: 500,
                cursor: 'pointer',
                transition: 'all 0.15s',
              }}
            >
              {status.paused || status.status === 'paused' ? (
                <>
                  <Play size={13} />
                  Resume Indexing
                </>
              ) : (
                <>
                  <Pause size={13} />
                  Pause Indexing
                </>
              )}
            </button>

            <button
              onClick={handleReindex}
              aria-label="Re-index Workspace"
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '6px',
                padding: '7px 14px',
                borderRadius: '8px',
                border: '1px solid rgba(var(--accent-rgb), 0.4)',
                background: 'rgba(var(--accent-rgb), 0.2)',
                color: 'var(--accent)',
                fontSize: '12px',
                fontWeight: 500,
                cursor: 'pointer',
                transition: 'all 0.15s',
              }}
            >
              <RefreshCw size={13} />
              Re-index
            </button>
          </div>
        </div>

        {/* Progress Bar (Visible while indexing) */}
        {progress && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '12px', color: 'var(--accent)' }}>
              <span>
                {progress.current_file
                  ? `Processing ${progress.current_file}`
                  : 'Updating workspace index...'}
              </span>
              <span>
                {progress.indexed} / {progress.total} files
              </span>
            </div>
            <div
              style={{
                width: '100%',
                height: '6px',
                borderRadius: '3px',
                background: 'var(--bg-hover)',
                overflow: 'hidden',
              }}
            >
              <div
                style={{
                  height: '100%',
                  width: `${progress.total > 0 ? (progress.indexed / progress.total) * 100 : 0}%`,
                  background: 'var(--accent)',
                  transition: 'width 0.3s ease',
                }}
              />
            </div>
          </div>
        )}

        {/* Metrics Grid */}
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fit, minmax(130px, 1fr))',
            gap: '12px',
            borderTop: '1px solid var(--border-default)',
            paddingTop: '12px',
          }}
        >
          <div>
            <div style={{ fontSize: '11px', color: 'var(--text-secondary)', textTransform: 'uppercase' }}>Indexed Files</div>
            <div style={{ fontSize: '18px', fontWeight: 600, color: 'var(--text-primary)', marginTop: '2px' }}>
              {status.indexed_files || 0}
            </div>
          </div>

          <div>
            <div style={{ fontSize: '11px', color: 'var(--text-secondary)', textTransform: 'uppercase' }}>Excluded Files</div>
            <div style={{ fontSize: '18px', fontWeight: 600, color: 'var(--danger)', marginTop: '2px' }}>
              {Array.isArray(status.excluded_files) ? status.excluded_files.length : 0}
            </div>
          </div>

          <div>
            <div style={{ fontSize: '11px', color: 'var(--text-secondary)', textTransform: 'uppercase' }}>Total Chunks</div>
            <div style={{ fontSize: '18px', fontWeight: 600, color: 'var(--text-primary)', marginTop: '2px' }}>
              {status.total_chunks || 0}
            </div>
          </div>

          <div>
            <div style={{ fontSize: '11px', color: 'var(--text-secondary)', textTransform: 'uppercase' }}>Embedder Model</div>
            <div style={{ fontSize: '13px', fontWeight: 500, color: 'var(--accent)', marginTop: '4px' }}>
              {status.embedder || 'qwen3-embedding:0.6b'}
            </div>
          </div>
        </div>
      </div>
      )}

      {/* Embedder Configuration Box */}
      <div
        style={{
          background: 'var(--bg-raised)',
          border: '1px solid var(--border-default)',
          borderRadius: '14px',
          padding: '16px 20px',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          gap: '16px',
        }}
      >
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
            <Sliders size={15} style={{ color: 'var(--accent)' }} />
            <span style={{ fontSize: '14px', fontWeight: 500 }}>Embedding Model Selection</span>
          </div>
          <p style={{ color: 'var(--text-secondary)', fontSize: '12px', margin: '3px 0 0 0' }}>
            Unified local model for workspace RAG and episodic memory. Changing models requires a full re-index.
          </p>
        </div>

        <div style={{ minWidth: '220px' }}>
          <select
            id="embedder-select"
            aria-label="Embedding Model"
            value={status.embedder || 'qwen3-embedding:0.6b'}
            disabled={statusLoad !== 'ready'}
            onChange={handleEmbedderSelectChange}
            style={{
              width: '100%',
              background: 'var(--bg-hover)',
              border: '1px solid var(--border-default)',
              borderRadius: '8px',
              color: 'var(--text-primary)',
              padding: '8px 12px',
              fontSize: '13px',
              outline: 'none',
              cursor: 'pointer',
            }}
          >
            {embedderOptions.map((option) => (
              <option key={option.value} value={option.value} style={{ background: 'var(--bg-raised)' }}>
                {option.label}
              </option>
            ))}
          </select>
        </div>
      </div>

      {/* Files Browser Tabs & Filter */}
      <div
        style={{
          background: 'var(--bg-raised)',
          border: '1px solid var(--border-default)',
          borderRadius: '14px',
          overflow: 'hidden',
          display: 'flex',
          flexDirection: 'column',
        }}
      >
        {/* Navigation & Search Bar */}
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'center',
            padding: '12px 16px',
            borderBottom: '1px solid var(--border-default)',
            flexWrap: 'wrap',
            gap: '10px',
          }}
        >
          {/* Tab buttons */}
          <div style={{ display: 'flex', gap: '8px' }}>
            <button
              onClick={() => setActiveTab('indexed')}
              role="button"
              aria-label="Indexed Files"
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '6px',
                padding: '6px 14px',
                borderRadius: '8px',
                border: 'none',
                background: activeTab === 'indexed' ? 'rgba(var(--accent-rgb), 0.3)' : 'transparent',
                color: activeTab === 'indexed' ? 'var(--text-primary)' : 'var(--text-secondary)',
                fontSize: '13px',
                fontWeight: 500,
                cursor: 'pointer',
              }}
            >
              <FileText size={14} />
              Indexed Files ({Array.isArray(status.indexed_docs) ? status.indexed_docs.length : status.indexed_files || 0})
            </button>

            <button
              onClick={() => setActiveTab('excluded')}
              role="button"
              aria-label="Excluded Files"
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '6px',
                padding: '6px 14px',
                borderRadius: '8px',
                border: 'none',
                background: activeTab === 'excluded' ? 'rgba(201,42,42, 0.2)' : 'transparent',
                color: activeTab === 'excluded' ? 'var(--danger)' : 'var(--text-secondary)',
                fontSize: '13px',
                fontWeight: 500,
                cursor: 'pointer',
              }}
            >
              <FileX size={14} />
              Excluded Files ({Array.isArray(status.excluded_files) ? status.excluded_files.length : 0})
            </button>
          </div>

          {/* Search Filter */}
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: '6px',
              background: 'var(--bg-base)',
              border: '1px solid var(--border-default)',
              borderRadius: 'var(--radius-md)',
              padding: '6px 12px',
              width: '240px',
            }}
          >
            <Search size={13} style={{ color: 'var(--text-secondary)' }} />
            <input
              type="text"
              placeholder="Filter files or reasons…"
              value={filterQuery}
              onChange={(e) => setFilterQuery(e.target.value)}
              style={{
                background: 'transparent',
                border: 'none',
                color: 'var(--text-primary)',
                fontSize: '12px',
                outline: 'none',
                width: '100%',
              }}
            />
          </div>
        </div>

        {/* Files Content List */}
        <div style={{ maxHeight: '380px', overflowY: 'auto' }}>
          {activeTab === 'indexed' && (
            <div data-testid="indexed-files-list">
              {filteredIndexedDocs.length === 0 ? (
                statusLoad !== 'ready' ? (
                  <div style={{ padding: '24px 16px' }}>
                    <LoadStateRow
                      loadState={statusLoad}
                      isConnected={isConnected}
                      testId="indexing-files-load-state"
                      variant="plain"
                      style={{ textAlign: 'center' }}
                      loadingText="Loading indexed files…"
                      offlineText="Waiting for the engine — the indexed file list will appear here once it connects."
                      errorText="The indexed file list could not be loaded."
                    />
                  </div>
                ) : (
                  <div style={{ padding: '36px 16px', textAlign: 'center', color: 'var(--text-secondary)', fontSize: '13px' }}>
                    No indexed prose files found.
                  </div>
                )
              ) : (
                <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '13px' }}>
                  <thead>
                    <tr style={{ borderBottom: '1px solid var(--border-default)', color: 'var(--text-secondary)', textAlign: 'left' }}>
                      <th style={{ padding: '10px 16px', fontWeight: 500 }}>File Path</th>
                      <th style={{ padding: '10px 16px', fontWeight: 500, width: '110px' }}>Size</th>
                      <th style={{ padding: '10px 16px', fontWeight: 500, width: '110px' }}>Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredIndexedDocs.map((doc, idx) => (
                      <tr
                        key={doc.file_path || idx}
                        style={{
                          borderBottom: '1px solid var(--border-default)',
                          background: idx % 2 === 0 ? 'transparent' : 'var(--bg-raised)',
                        }}
                      >
                        <td style={{ padding: '10px 16px', fontFamily: 'var(--font-mono)', color: 'var(--text-primary)' }}>
                          {doc.file_path}
                        </td>
                        <td style={{ padding: '10px 16px', color: 'var(--text-secondary)' }}>
                          {formatBytes(doc.file_size)}
                        </td>
                        <td style={{ padding: '10px 16px' }}>
                          <span
                            style={{
                              display: 'inline-flex',
                              alignItems: 'center',
                              gap: '4px',
                              padding: '2px 8px',
                              borderRadius: '12px',
                              fontSize: '11px',
                              background:
                                doc.status === 'pending'
                                  ? 'rgba(var(--accent-rgb), 0.15)'
                                  : 'rgba(76, 195, 138, 0.15)',
                              color: doc.status === 'pending' ? 'var(--accent)' : 'var(--success)',
                            }}
                          >
                            {doc.status === 'pending' ? (
                              <>
                                <Clock size={11} />
                                pending
                              </>
                            ) : (
                              <>
                                <CheckCircle2 size={11} />
                                indexed
                              </>
                            )}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          )}

          {activeTab === 'excluded' && (
            <div data-testid="excluded-files-list">
              {filteredExcludedFiles.length === 0 ? (
                statusLoad !== 'ready' ? (
                  <div style={{ padding: '24px 16px' }}>
                    <LoadStateRow
                      loadState={statusLoad}
                      isConnected={isConnected}
                      variant="plain"
                      style={{ textAlign: 'center' }}
                      loadingText="Loading excluded files…"
                      offlineText="Waiting for the engine — the excluded file list will appear here once it connects."
                      errorText="The excluded file list could not be loaded."
                    />
                  </div>
                ) : (
                  <div style={{ padding: '36px 16px', textAlign: 'center', color: 'var(--text-secondary)', fontSize: '13px' }}>
                    No excluded files found.
                  </div>
                )
              ) : (
                <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '13px' }}>
                  <thead>
                    <tr style={{ borderBottom: '1px solid var(--border-default)', color: 'var(--text-secondary)', textAlign: 'left' }}>
                      <th style={{ padding: '10px 16px', fontWeight: 500 }}>File Path</th>
                      <th style={{ padding: '10px 16px', fontWeight: 500, width: '180px' }}>Exclusion Reason</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredExcludedFiles.map((file, idx) => {
                      const reasonMeta = REASON_METADATA[file.reason] || {
                        label: file.reason || 'Excluded',
                        color: 'var(--text-muted)',
                        bg: 'rgba(148, 148, 143, 0.15)',
                      };
                      return (
                        <tr
                          key={file.file_path || idx}
                          style={{
                            borderBottom: '1px solid var(--border-default)',
                            background: idx % 2 === 0 ? 'transparent' : 'var(--bg-raised)',
                          }}
                        >
                          <td style={{ padding: '10px 16px', fontFamily: 'var(--font-mono)', color: 'var(--text-secondary)' }}>
                            {file.file_path}
                          </td>
                          <td style={{ padding: '10px 16px' }}>
                            <span
                              style={{
                                display: 'inline-block',
                                padding: '2px 8px',
                                borderRadius: '12px',
                                fontSize: '11px',
                                fontWeight: 500,
                                background: reasonMeta.bg,
                                color: reasonMeta.color,
                              }}
                            >
                              {reasonMeta.label}
                            </span>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              )}
            </div>
          )}
        </div>
      </div>

      {/* Full Re-index Warning Modal */}
      {warningModal.isOpen && (
        <div
          data-testid="reindex-warning-modal"
          className="aether-scrim"
          style={{
            position: 'fixed',
            inset: 0,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            zIndex: 1000,
          }}
        >
          <div
            className="aether-glass"
            style={{
              padding: '24px',
              maxWidth: '460px',
              width: '90%',
              display: 'flex',
              flexDirection: 'column',
              gap: '16px',
            }}
          >
            <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
              <div
                style={{
                  background: 'rgba(201,42,42, 0.15)',
                  padding: '10px',
                  borderRadius: '10px',
                  color: 'var(--danger)',
                }}
              >
                <AlertTriangle size={22} />
              </div>
              <h3 style={{ margin: 0, fontSize: '17px', fontWeight: 600, color: 'var(--text-primary)' }}>
                Full Re-index Required
              </h3>
            </div>

            <p style={{ margin: 0, fontSize: '13px', color: 'var(--text-secondary)', lineHeight: '1.5' }}>
              Switching the embedder model from <strong>{status.embedder}</strong> to{' '}
              <strong>{warningModal.pendingEmbedder}</strong> will drop all existing vector embeddings. Vectors are
              incomparable across different model architectures, requiring a full re-index of the workspace.
            </p>

            <div
              style={{
                display: 'flex',
                justifyContent: 'flex-end',
                gap: '10px',
                marginTop: '8px',
              }}
            >
              <button
                onClick={handleCancelEmbedderSwitch}
                style={{
                  padding: '8px 16px',
                  borderRadius: '8px',
                  border: '1px solid var(--border-default)',
                  background: 'transparent',
                  color: 'var(--text-secondary)',
                  fontSize: '13px',
                  cursor: 'pointer',
                }}
              >
                Cancel
              </button>

              <button
                onClick={handleConfirmEmbedderSwitch}
                style={{
                  padding: '8px 16px',
                  borderRadius: '8px',
                  border: 'none',
                  background: 'var(--danger)',
                  color: 'var(--text-primary)',
                  fontSize: '13px',
                  fontWeight: 500,
                  cursor: 'pointer',
                }}
              >
                Confirm & Re-index
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
