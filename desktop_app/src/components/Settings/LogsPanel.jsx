import React, { useCallback, useEffect, useMemo, useState } from 'react';
import LoadStateRow from './LoadStateRow';
// The About section's version source: the desktop package
// manifest, one of the mirrors of the engine's APP_VERSION.
import appPackage from '../../../package.json';

// Unified logging (5 Nov 2026) — Settings → Capabilities → Logs &
// Diagnostics. Three cards over the shell's read-logs IPC:
//
//   Status   — engine connection, app version, and the log file
//              inventory (name / size / last write per source), so
//              "is it recording?" is answerable at a glance.
//   Viewer   — app.log + engine.log tails, merged or per source,
//              with text search, a level filter and pause/resume
//              on the 3s live tail. Lines are parsed leniently:
//              app.log lines carry ISO [source] LEVEL; engine.log
//              lines carry ISO [engine] plus the engine's own text
//              (no level — read as INFO).
//   Controls — export the combined bundle (the parent owns the
//              engine request + merge), open the logs folder /
//              engine log, set the [main]/[ui] level, and clear
//              logs behind an inline two-step confirmation (a
//              destructive action is never one stray click, and
//              never queued silently).
//
// Everything degrades honestly: without the desktop bridge
// (web/dev) the panel says logs are unavailable instead of
// rendering an empty shell.

const POLL_MS = 3000;
const LEVEL_SEVERITY = { DEBUG: 10, INFO: 20, WARN: 30, ERROR: 40 };
const LEVEL_COLORS = { DEBUG: '#9090a0', INFO: '#d4d4d8', WARN: '#fbbf24', ERROR: '#fca5a5' };
const LINE_RE = /^(\S+) \[([a-z]+)\]\s+(?:(DEBUG|INFO|WARN|ERROR)\s+)?(.*)$/;

function parseLine(raw, fallbackSource) {
  const match = LINE_RE.exec(raw);
  if (!match) {
    return { raw, ts: '', source: fallbackSource, level: 'INFO', message: raw };
  }
  return { raw, ts: match[1], source: match[2], level: match[3] || 'INFO', message: match[4] };
}

// Parse one source's tail, giving lines that carry no timestamp
// prefix the timestamp of the line above them (E10-04): raw engine
// stdout lands in engine.log BETWEEN the lifecycle's ISO-prefixed
// lines, as continuations of the output above. They used to parse
// to ts '' and sort before every timestamped line in the merged
// view — to the top of the panel's default tab, however recent
// they were. A continuation at the very top of a tail has no line
// above it in the tail; it keeps '' and stays at the head of its
// own source, which is the honest placement for it.
function parseTail(lines, fallbackSource) {
  let lastTs = '';
  return (lines || []).map((line) => {
    const parsed = parseLine(line, fallbackSource);
    if (parsed.ts) {
      lastTs = parsed.ts;
    } else if (lastTs) {
      parsed.ts = lastTs;
    }
    return parsed;
  });
}

function formatSize(bytes) {
  if (typeof bytes !== 'number') return '—';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

function formatWhen(iso) {
  if (!iso) return 'no writes yet';
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? 'no writes yet' : date.toLocaleString();
}

const cardStyle = {
  background: 'rgba(255,255,255,0.03)',
  border: '1px solid rgba(255,255,255,0.08)',
  borderRadius: '16px',
  padding: '24px',
  marginTop: '24px',
};

const buttonStyle = {
  background: 'rgba(255,255,255,0.06)',
  color: '#f0f0f5',
  border: '1px solid rgba(255,255,255,0.15)',
  padding: '9px 18px',
  borderRadius: '10px',
  cursor: 'pointer',
  fontWeight: 500,
};

const tabStyle = (active) => ({
  background: active ? 'rgba(124,58,237,0.4)' : 'transparent',
  color: '#fff',
  border: 'none',
  padding: '7px 18px',
  borderRadius: '8px',
  cursor: 'pointer',
  transition: 'all 0.2s',
});

const selectStyle = {
  background: 'rgba(255,255,255,0.06)',
  color: '#f0f0f5',
  border: '1px solid rgba(255,255,255,0.15)',
  borderRadius: '8px',
  padding: '7px 10px',
  fontSize: '13px',
};

export default function LogsPanel({ isConnected = null, sendSettingsAction = null, onExportBundle = null }) {
  const api = typeof window !== 'undefined' ? window.electronAPI : null;
  const logsAvailable = Boolean(api && typeof api.readLogs === 'function');

  const [data, setData] = useState(null);
  const [loadState, setLoadState] = useState('loading');
  const [tab, setTab] = useState('all');
  const [search, setSearch] = useState('');
  const [levelFilter, setLevelFilter] = useState('ALL');
  const [paused, setPaused] = useState(false);
  const [copied, setCopied] = useState(false);
  const [logLevel, setLogLevel] = useState('INFO');
  const [clearArmed, setClearArmed] = useState(false);
  const [clearStatus, setClearStatus] = useState('');
  const [exportNote, setExportNote] = useState('');

  const fetchLogs = useCallback(async () => {
    if (!logsAvailable) return;
    try {
      const result = await api.readLogs();
      if (result) {
        setData(result);
        setLoadState('ready');
        // E10-03: the applied level is re-read on EVERY poll, not
        // seeded once — a level change whose IPC failed (bridge
        // teardown mid-change) leaves the select showing an
        // unapplied level, and the next poll is the correction.
        if (result.level) setLogLevel(result.level);
      }
    } catch {
      // A failed refresh keeps whatever is on screen; only a
      // never-loaded panel reports the error state.
      setLoadState((prev) => (prev === 'ready' ? prev : 'error'));
    }
  }, [api, logsAvailable]);

  // Mount fetch (the interval below reuses fetchLogs): the result
  // is applied inline so no setState runs synchronously in the
  // effect — every update waits for the IPC promise.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (!logsAvailable) return;
      try {
        const result = await api.readLogs();
        if (cancelled || !result) return;
        setData(result);
        setLoadState('ready');
        if (result.level) setLogLevel(result.level);
      } catch {
        if (!cancelled) {
          setLoadState((prev) => (prev === 'ready' ? prev : 'error'));
        }
      }
    })();
    return () => { cancelled = true; };
  }, [api, logsAvailable]);

  useEffect(() => {
    if (paused || !logsAvailable) return undefined;
    const timer = setInterval(fetchLogs, POLL_MS);
    return () => clearInterval(timer);
  }, [paused, fetchLogs, logsAvailable]);

  // The clear confirmation disarms itself — an armed destructive
  // button must not sit waiting for an accidental second click.
  useEffect(() => {
    if (!clearArmed) return undefined;
    const timer = setTimeout(() => setClearArmed(false), 5000);
    return () => clearTimeout(timer);
  }, [clearArmed]);

  const entries = useMemo(() => {
    if (!data) return [];
    const appEntries = parseTail(data.appTail, 'main');
    const engineEntries = parseTail(data.engineTail, 'engine');
    if (tab === 'app') return appEntries;
    if (tab === 'engine') return engineEntries;
    return [...appEntries, ...engineEntries].sort((a, b) => {
      if (a.ts === b.ts) return 0;
      return a.ts < b.ts ? -1 : 1;
    });
  }, [data, tab]);

  const visible = useMemo(() => {
    const minSeverity = levelFilter === 'ALL' ? 0 : LEVEL_SEVERITY[levelFilter] || 0;
    const query = search.trim().toLowerCase();
    return entries.filter((entry) => {
      if ((LEVEL_SEVERITY[entry.level] || 20) < minSeverity) return false;
      if (query && !entry.raw.toLowerCase().includes(query)) return false;
      return true;
    });
  }, [entries, levelFilter, search]);

  const inventoryByName = useMemo(() => {
    const map = {};
    (data && data.inventory ? data.inventory : []).forEach((item) => {
      map[item.name] = item;
    });
    return map;
  }, [data]);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(visible.map((entry) => entry.raw).join('\n'));
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopied(false);
    }
  };

  const handleExport = () => {
    let disposition;
    if (onExportBundle) {
      disposition = onExportBundle();
    } else if (sendSettingsAction) {
      disposition = sendSettingsAction('DIAGNOSTICS_EXPORT_REQUEST', {});
    }
    setExportNote(disposition === 'queued'
      ? 'Export queued — the combined bundle will download when the engine reconnects.'
      : 'Export requested — the combined bundle (engine + desktop logs) will download when the engine answers.');
  };

  const handleClear = async () => {
    if (!clearArmed) {
      setClearArmed(true);
      setClearStatus('');
      return;
    }
    setClearArmed(false);
    if (!api || typeof api.clearLogs !== 'function') return;
    try {
      const result = await api.clearLogs();
      setClearStatus(result && result.ok
        ? 'Logs cleared. New lines will appear as the app runs.'
        : `Clear failed: ${(result && result.error) || 'unknown error'}`);
    } catch (err) {
      setClearStatus(`Clear failed: ${(err && err.message) || err}`);
    }
    fetchLogs();
  };

  const handleLevelChange = async (level) => {
    setLogLevel(level);
    if (api && typeof api.setLogLevel === 'function') {
      try {
        const result = await api.setLogLevel(level);
        if (result && result.level) setLogLevel(result.level);
      } catch {
        // The select keeps the user's choice until the next
        // read-logs poll reports the level main actually has
        // applied (fetchLogs applies result.level on every poll).
      }
    }
  };

  if (!logsAvailable) {
    return (
      <div data-testid="logs-panel" style={{ ...cardStyle, color: '#f0f0f5' }}>
        <h3 style={{ fontSize: '16px', marginBottom: '8px' }}>Logs &amp; Diagnostics</h3>
        <p style={{ color: '#9090a0', fontSize: '14px', margin: 0 }}>
          Logs are only available in the desktop app — this view is running without the desktop bridge.
        </p>
      </div>
    );
  }

  const sourceRows = [
    { label: '[engine] engine lifecycle + engine output', file: 'engine.log' },
    { label: '[main] app shell · [ui] this window', file: 'app.log' },
    { label: 'crashes (shell)', file: 'crash-electron.log' },
  ];

  return (
    <div data-testid="logs-panel" style={{ color: '#f0f0f5' }}>
      {/* Status card */}
      <div data-testid="logs-status-card" style={cardStyle}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '16px' }}>
          <div style={{ width: '12px', height: '12px', borderRadius: '50%', background: isConnected ? '#10b981' : '#6b7280', boxShadow: isConnected ? '0 0 10px rgba(16,185,129,0.5)' : 'none' }} />
          <h3 style={{ fontSize: '16px', margin: 0 }}>{isConnected ? 'Engine Connected' : 'Waiting for the Engine'}</h3>
          <span style={{ fontSize: '13px', color: '#9090a0' }}>App version {appPackage.version}</span>
        </div>
        {loadState !== 'ready' ? (
          <LoadStateRow
            loadState={loadState}
            isConnected={isConnected}
            testId="logs-inventory-load-state"
            loadingText="Loading log inventory…"
            offlineText="Waiting for the engine — the log inventory will appear here once it connects."
            errorText="Could not load the log inventory."
            variant="plain"
          />
        ) : (
          <div data-testid="logs-inventory" style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            {data && data.logBroken ? (
              <p data-testid="logs-broken-note" style={{ fontSize: '13px', color: '#fca5a5', margin: 0 }}>
                app.log writes are failing — new shell lines are not being recorded. Restarting the app usually clears it; the lines already on disk are unaffected.
              </p>
            ) : null}
            {sourceRows.map((row) => {
              const item = inventoryByName[row.file];
              return (
                <div key={row.file} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.05)', borderRadius: '10px', padding: '10px 14px' }}>
                  <span style={{ fontSize: '13px' }}>{row.label}</span>
                  <span style={{ fontSize: '12px', color: '#9090a0' }}>
                    {item ? `${row.file} · ${formatSize(item.size)} · last write ${formatWhen(item.mtime)}` : `${row.file} · no writes yet`}
                  </span>
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* Log viewer card */}
      <div data-testid="logs-viewer-card" style={cardStyle}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', flexWrap: 'wrap', gap: '12px', marginBottom: '16px' }}>
          <div style={{ display: 'flex', background: 'rgba(255,255,255,0.05)', padding: '4px', borderRadius: '10px' }}>
            <button type="button" style={tabStyle(tab === 'all')} onClick={() => setTab('all')}>All</button>
            <button type="button" style={tabStyle(tab === 'app')} onClick={() => setTab('app')}>App</button>
            <button type="button" style={tabStyle(tab === 'engine')} onClick={() => setTab('engine')}>Engine</button>
          </div>
          <div style={{ display: 'flex', gap: '8px', alignItems: 'center', flexWrap: 'wrap' }}>
            <input
              type="text"
              aria-label="Search logs"
              placeholder="Search logs…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              style={{ ...selectStyle, minWidth: '170px' }}
            />
            <select aria-label="Level filter" value={levelFilter} onChange={(e) => setLevelFilter(e.target.value)} style={selectStyle}>
              <option value="ALL">All levels</option>
              <option value="INFO">Info +</option>
              <option value="WARN">Warnings +</option>
              <option value="ERROR">Errors</option>
            </select>
            <button type="button" style={buttonStyle} onClick={() => setPaused((prev) => !prev)}>
              {paused ? 'Resume' : 'Pause'}
            </button>
            <button type="button" style={buttonStyle} onClick={handleCopy}>
              {copied ? 'Copied' : 'Copy visible lines'}
            </button>
          </div>
        </div>
        {loadState !== 'ready' ? (
          <LoadStateRow
            loadState={loadState}
            isConnected={isConnected}
            testId="logs-viewer-load-state"
            loadingText="Loading logs…"
            offlineText="Waiting for the engine — logs will appear here once it connects."
            errorText="Could not load the logs."
          />
        ) : (
          <div
            data-testid="logs-lines"
            className="chat-scroll"
            style={{ background: 'rgba(0,0,0,0.5)', border: '1px solid rgba(255,255,255,0.1)', borderRadius: '12px', padding: '16px', fontFamily: 'monospace', fontSize: '12px', lineHeight: 1.7, height: '320px', overflowY: 'auto' }}
          >
            {visible.length === 0 ? (
              <div style={{ color: '#9090a0' }}>No log lines match the current filters.</div>
            ) : (
              visible.map((entry, idx) => (
                <div key={idx} data-log-level={entry.level} style={{ color: LEVEL_COLORS[entry.level] || '#d4d4d8', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                  {entry.raw}
                </div>
              ))
            )}
          </div>
        )}
        {paused && (
          <div style={{ fontSize: '12px', color: '#fbbf24', marginTop: '8px' }}>Live tail paused — new lines are still being recorded.</div>
        )}
      </div>

      {/* Controls card */}
      <div data-testid="logs-controls-card" style={cardStyle}>
        <h3 style={{ fontSize: '16px', marginBottom: '16px' }}>Log Controls</h3>
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap' }}>
          <button type="button" style={{ ...buttonStyle, background: 'rgba(124,58,237,0.25)', border: '1px solid rgba(124,58,237,0.5)', color: '#e9d5ff' }} onClick={handleExport}>
            Export full bundle
          </button>
          {typeof api.openLogsFolder === 'function' && (
            <button type="button" style={buttonStyle} onClick={() => api.openLogsFolder()}>
              Open logs folder
            </button>
          )}
          {typeof api.openEngineLog === 'function' && (
            <button type="button" style={buttonStyle} onClick={() => api.openEngineLog()}>
              Open engine log
            </button>
          )}
          {typeof api.setLogLevel === 'function' && (
            <label style={{ display: 'flex', alignItems: 'center', gap: '8px', fontSize: '13px', color: '#9090a0' }}>
              Log level
              <select aria-label="Log level" value={logLevel} onChange={(e) => handleLevelChange(e.target.value)} style={selectStyle}>
                <option value="INFO">Info</option>
                <option value="DEBUG">Debug</option>
                <option value="WARN">Warnings</option>
                <option value="ERROR">Errors</option>
              </select>
            </label>
          )}
          {typeof api.clearLogs === 'function' && (
            <button
              type="button"
              onClick={handleClear}
              style={clearArmed
                ? { ...buttonStyle, background: 'rgba(239,68,68,0.2)', border: '1px solid rgba(239,68,68,0.5)', color: '#fca5a5' }
                : { ...buttonStyle, background: 'rgba(239,68,68,0.08)', border: '1px solid rgba(239,68,68,0.3)', color: '#fca5a5' }}
            >
              {clearArmed ? 'Confirm clear logs' : 'Clear logs'}
            </button>
          )}
        </div>
        {clearArmed && (
          <div style={{ fontSize: '13px', color: '#fbbf24', marginTop: '12px' }}>
            This empties app.log and engine.log (crash logs are kept). Click again to confirm.
          </div>
        )}
        {exportNote && <div style={{ fontSize: '13px', color: '#a0a0b0', marginTop: '12px' }}>{exportNote}</div>}
        {clearStatus && <div style={{ fontSize: '13px', color: '#a0a0b0', marginTop: '12px' }}>{clearStatus}</div>}
        <p style={{ fontSize: '12px', color: '#9090a0', marginTop: '16px', marginBottom: 0 }}>
          The exported bundle contains no API keys and no memory content — log lines are scrubbed for key-shaped values before they are included.
        </p>
      </div>
    </div>
  );
}
