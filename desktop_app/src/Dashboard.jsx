import React, { useEffect, useState } from 'react';
import { Activity, Cpu, Database, FileText, Folder, HardDrive, HeartPulse, MessageSquare, Network, Plus, Zap } from 'lucide-react';
import './chat.css';
import LoadStateRow, { useSlowLoad, DEFAULT_SLOW_TEXT } from './components/Settings/LoadStateRow';
import EmptyState from './components/EmptyState';
import TaskLedgerPanel from './components/TaskLedgerPanel';

// Recents timestamps arrive in mixed shapes: session rows carry
// epoch seconds (the store's INTEGER columns), artifact rows may
// carry epoch or ISO strings. Anything unparseable renders as no
// date at all rather than a wrong one.
function formatStamp(value) {
  if (value === null || value === undefined || value === '') return null;
  const ms = typeof value === 'number' ? (value > 1e11 ? value : value * 1000) : Date.parse(value);
  if (!Number.isFinite(ms)) return null;
  return new Date(ms).toLocaleString();
}

// Full Furnish S-4: Dashboard is a home first — quick starts and
// recents on top, the health cards demoted below them. The recents
// data belongs to the shell (it owns the session list and the tab
// state), so it arrives as props: recentSessions / recentArtifacts
// render only when the shell passes them (an empty array is a real
// "none yet" and gets the shared EmptyState; undefined means the
// shell has not wired the feed and the card stays out rather than
// claiming emptiness). The active workspace is the one exception:
// WORKSPACE_LIST is a shared feed the shell already consumes from
// any panel (the Projects pattern), so when no prop is passed the
// dashboard fetches it over the same socket it already uses.
export default function Dashboard({ status = 'Initializing', activeTasks = 0, pluginCount = null, memoryMb = null, providerHealth = null, providerHealthLoaded = null, wsRef = null, reconnectCount = 0, onStartWorkflow, onNewChat, recentSessions, recentArtifacts, activeWorkspace, onResumeSession }) {
  // RUX-C-14: a metric the engine has not answered yet is unknown
  // ('—'), never 0. Provider health counts as answered when App
  // says a snapshot arrived, or — for standalone callers — when a
  // health object was handed in at all.
  const healthLoaded = providerHealthLoaded !== null && providerHealthLoaded !== undefined
    ? providerHealthLoaded
    : providerHealth !== null && providerHealth !== undefined;
  const isOnline = status === 'Online' || status.startsWith('Online');
  // Epic 6, E6-05: memory stats come from the engine on mount
  // (MEMORY_EPISODES_RESPONSE carries backend + embedding stats).
  const [memoryStats, setMemoryStats] = useState(null);
  // E4-F14 family: engine memory replies can carry an error instead of
  // backend/stats (unavailable backend). Like MemoryManagement, that is
  // an error state — never a healthy-looking native/zero default.
  const [memoryError, setMemoryError] = useState(null);
  // Mechanism 5: the memory card's "Loading…" used to be able to
  // wait forever in silence (the request queues offline) — after
  // the shared slow interval it says so.
  const memorySlow = useSlowLoad(memoryStats === null && !memoryError);
  // S-4: the self-fetched active workspace (see the header note).
  // `undefined` prop = not wired, fetch it; any other value —
  // a row or explicit null — is the shell's answer and wins.
  const [fetchedWorkspace, setFetchedWorkspace] = useState(undefined);
  const workspaceWired = activeWorkspace !== undefined;
  const effectiveWorkspace = workspaceWired ? activeWorkspace : fetchedWorkspace;
  useEffect(() => {
    if (!wsRef?.current) return;
    const handleMessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'MEMORY_EPISODES_RESPONSE') {
          if (data.payload.error) {
            setMemoryError(data.payload.error);
            setMemoryStats(null);
          } else {
            setMemoryError(null);
            setMemoryStats({
              backend: data.payload.backend || 'native',
              ...(data.payload.embedding_stats || {}),
            });
          }
        } else if (data.type === 'WORKSPACE_LIST_RESPONSE' && !workspaceWired) {
          const rows = Array.isArray(data.payload.workspaces) ? data.payload.workspaces : [];
          setFetchedWorkspace(rows.find(w => w.is_active) || null);
        }
      } catch (e) {
        // ignore malformed frames
      }
    };
    wsRef.current.addEventListener('message', handleMessage);
    wsRef.current.send(JSON.stringify({
      type: 'MEMORY_EPISODES_REQUEST', schema_version: 1,
      request_id: Date.now().toString(), payload: {},
    }));
    if (!workspaceWired) {
      wsRef.current.send(JSON.stringify({
        type: 'WORKSPACE_LIST_REQUEST', schema_version: 1,
        request_id: Date.now().toString(), payload: {},
      }));
    }
    return () => {
      if (wsRef.current) wsRef.current.removeEventListener('message', handleMessage);
    };
    // D2-09: reconnectCount (the socket layer's recovery counter)
    // is a dep — a response lost across a disconnect is re-fetched
    // on reconnect instead of leaving the card stale for the whole
    // session (the Speech/Usage panels' RUX-A-17 pattern).
  }, [wsRef, reconnectCount]);
  const healthEntries = Object.entries(providerHealth || {});
  const healthCounts = healthEntries.reduce((acc, [, h]) => {
    const key = h && h.status ? h.status : 'unknown';
    acc[key] = (acc[key] || 0) + 1;
    return acc;
  }, {});
  
  return (
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', color: 'var(--text-primary)', padding: '16px', overflowY: 'auto' }} className="chat-scroll">
      <div style={{ marginBottom: '32px' }}>
        <h1 style={{ fontSize: '28px', margin: '0 0 8px 0', fontWeight: 600 }}>System Dashboard</h1>
        <p style={{ color: 'var(--text-muted)', margin: 0, fontSize: '14px' }}>Overview of Aether-OS active tasks and resources.</p>
      </div>

      {/* S-4: quick starts that act, at the top with the recents.
          "New chat" creates a chat through the shell's real create
          path. "Start New Workflow" keeps its existing wiring
          unchanged — see the report: the engine has no prompt-less
          workflow start to wire it to. */}
      <div style={{ marginBottom: '32px' }}>
        <h2 style={{ fontSize: '18px', fontWeight: 500, marginBottom: '16px' }}>Quick Actions</h2>
        <div style={{ display: 'flex', gap: '12px', flexWrap: 'wrap' }}>
          {typeof onNewChat === 'function' && (
            <button onClick={onNewChat} style={{ background: 'var(--accent)', border: '1px solid var(--accent)', color: 'var(--accent-on)', padding: '12px 20px', borderRadius: 'var(--radius-lg)', display: 'flex', alignItems: 'center', gap: '8px', cursor: 'pointer', transition: 'all var(--motion-base)' }}>
              <Plus size={16} /> New chat
            </button>
          )}
          {/* Phase 9, B3: previously a dead button with no onClick — now navigates to Chat to start a task. */}
          <button onClick={onStartWorkflow} style={{ background: 'var(--accent)', border: '1px solid var(--accent)', color: 'var(--accent-on)', padding: '12px 20px', borderRadius: 'var(--radius-lg)', display: 'flex', alignItems: 'center', gap: '8px', cursor: 'pointer', transition: 'all var(--motion-base)' }}>
            <Zap size={16} /> Start New Workflow
          </button>
        </div>
      </div>

      {/* S-4: recents — where you left off, before system health.
          The workspace card always renders (it can always be
          answered, by prop or by the shared list feed); the
          sessions / artifacts cards join it when the shell wires
          their feeds through as props. */}
      <div style={{ marginBottom: '32px' }}>
          <h2 style={{ fontSize: '18px', fontWeight: 500, marginBottom: '16px' }}>Recents</h2>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))', gap: '16px' }}>
            {Array.isArray(recentSessions) && (
              <div style={{ background: 'var(--bg-raised)', border: '1px solid var(--border-default)', borderRadius: 'var(--radius-lg)', padding: '20px' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: 'var(--text-muted)', marginBottom: '12px' }}>
                  <MessageSquare size={18} /> <span style={{ fontSize: '13px', textTransform: 'uppercase', letterSpacing: '1px' }}>Recent sessions</span>
                </div>
                {recentSessions.length === 0 ? (
                  <EmptyState
                    compact
                    icon={<MessageSquare size={20} color="var(--text-muted)" />}
                    title="No sessions yet"
                    description="Chats you start will appear here, ready to resume."
                    testId="dashboard-recents-sessions-empty"
                  />
                ) : (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                    {recentSessions.slice(0, 5).map((s) => {
                      const label = s.title || s.name || 'Untitled session';
                      const when = formatStamp(s.updated_at ?? s.created_at);
                      const rowContent = (
                        <>
                          <span style={{ fontSize: '14px', color: 'var(--text-primary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{label}</span>
                          {when && <span style={{ fontSize: '12px', color: 'var(--text-muted)' }}>{when}</span>}
                        </>
                      );
                      return typeof onResumeSession === 'function' ? (
                        <button
                          key={s.id}
                          onClick={() => onResumeSession(s.id)}
                          title={`Resume ${label}`}
                          style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-start', gap: '2px', background: 'transparent', border: 'none', borderRadius: 'var(--radius-md)', padding: '8px', cursor: 'pointer', textAlign: 'left', width: '100%' }}
                        >
                          {rowContent}
                        </button>
                      ) : (
                        <div key={s.id} style={{ display: 'flex', flexDirection: 'column', gap: '2px', padding: '8px' }}>
                          {rowContent}
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            )}

            <div style={{ background: 'var(--bg-raised)', border: '1px solid var(--border-default)', borderRadius: 'var(--radius-lg)', padding: '20px' }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: 'var(--text-muted)', marginBottom: '12px' }}>
                <Folder size={18} /> <span style={{ fontSize: '13px', textTransform: 'uppercase', letterSpacing: '1px' }}>Active workspace</span>
              </div>
              {effectiveWorkspace === undefined ? (
                <LoadStateRow
                  loadState="loading"
                  isConnected={isOnline}
                  variant="plain"
                  testId="dashboard-workspace-load-state"
                  loadingText="Loading workspace…"
                  offlineText="Waiting for the engine — the active workspace will appear here once it connects."
                />
              ) : effectiveWorkspace === null ? (
                <EmptyState
                  compact
                  icon={<Folder size={20} color="var(--text-muted)" />}
                  title="No active workspace"
                  description="Add a folder under Projects to give chats project context."
                  testId="dashboard-workspace-empty"
                />
              ) : (
                <div>
                  <div style={{ fontSize: '15px', fontWeight: 500 }}>{effectiveWorkspace.name || effectiveWorkspace.path}</div>
                  {effectiveWorkspace.path && (
                    <div style={{ fontSize: '12px', color: 'var(--text-muted)', fontFamily: 'var(--font-mono)', marginTop: '4px', wordBreak: 'break-all' }}>{effectiveWorkspace.path}</div>
                  )}
                </div>
              )}
            </div>

            {Array.isArray(recentArtifacts) && (
              <div style={{ background: 'var(--bg-raised)', border: '1px solid var(--border-default)', borderRadius: 'var(--radius-lg)', padding: '20px' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: 'var(--text-muted)', marginBottom: '12px' }}>
                  <FileText size={18} /> <span style={{ fontSize: '13px', textTransform: 'uppercase', letterSpacing: '1px' }}>Recent artifacts</span>
                </div>
                {recentArtifacts.length === 0 ? (
                  <EmptyState
                    compact
                    icon={<FileText size={20} color="var(--text-muted)" />}
                    title="No artifacts yet"
                    description="Files and outputs from tasks will appear here."
                    testId="dashboard-recents-artifacts-empty"
                  />
                ) : (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '4px' }}>
                    {recentArtifacts.slice(0, 5).map((a) => (
                      <div key={a.id} style={{ display: 'flex', flexDirection: 'column', gap: '2px', padding: '8px' }}>
                        <span style={{ fontSize: '14px', color: 'var(--text-primary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{a.title || a.file_path || a.id}</span>
                        {formatStamp(a.created_at) && <span style={{ fontSize: '12px', color: 'var(--text-muted)' }}>{formatStamp(a.created_at)}</span>}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>
      </div>

      <h2 style={{ fontSize: '18px', fontWeight: 500, marginBottom: '16px' }}>System status</h2>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: '16px', marginBottom: '32px' }}>
        {/* Engine Status — derived from real WS connection state */}
        <div style={{ background: 'var(--bg-raised)', border: '1px solid var(--border-default)', borderRadius: 'var(--radius-lg)', padding: '20px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: 'var(--text-muted)', marginBottom: '12px' }}>
            <Activity size={18} /> <span style={{ fontSize: '13px', textTransform: 'uppercase', letterSpacing: '1px' }}>Engine Status</span>
          </div>
          <div style={{ fontSize: '24px', fontWeight: 500, color: isOnline ? 'var(--success)' : 'var(--accent)' }}>{isOnline ? 'Online' : status}</div>
        </div>

        {/* Active Tasks — real count from WS task lifecycle events */}
        <div style={{ background: 'var(--bg-raised)', border: '1px solid var(--border-default)', borderRadius: 'var(--radius-lg)', padding: '20px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: 'var(--text-muted)', marginBottom: '12px' }}>
            <Cpu size={18} /> <span style={{ fontSize: '13px', textTransform: 'uppercase', letterSpacing: '1px' }}>Active Tasks</span>
          </div>
          <div style={{ fontSize: '24px', fontWeight: 500 }}>{isOnline ? activeTasks : '—'}</div>
        </div>

        {/* Plugins — real count from PLUGIN_LIST_RESPONSE */}
        <div style={{ background: 'var(--bg-raised)', border: '1px solid var(--border-default)', borderRadius: 'var(--radius-lg)', padding: '20px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: 'var(--text-muted)', marginBottom: '12px' }}>
            <Network size={18} /> <span style={{ fontSize: '13px', textTransform: 'uppercase', letterSpacing: '1px' }}>Plugins</span>
          </div>
          <div style={{ fontSize: '24px', fontWeight: 500 }}>{pluginCount === null || pluginCount === undefined ? '—' : `${pluginCount} Active`}</div>
        </div>

        {/* Provider Health — Epic 5 health snapshot via App state */}
        <div style={{ background: 'var(--bg-raised)', border: '1px solid var(--border-default)', borderRadius: 'var(--radius-lg)', padding: '20px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: 'var(--text-muted)', marginBottom: '12px' }}>
            <HeartPulse size={18} /> <span style={{ fontSize: '13px', textTransform: 'uppercase', letterSpacing: '1px' }}>Provider Health</span>
          </div>
          {!healthLoaded ? (
            <div style={{ fontSize: '15px', color: 'var(--text-muted)' }}>
              {isOnline ? 'Waiting for the first health report…' : 'Waiting for the engine — health will appear here once it connects.'}
            </div>
          ) : healthEntries.length === 0 ? (
            <div style={{ fontSize: '15px', color: 'var(--text-muted)' }}>No providers reporting yet</div>
          ) : (
            <div style={{ fontSize: '15px', lineHeight: 1.7 }}>
              {Object.entries(healthCounts).map(([state, n]) => (
                <div key={state} style={{ textTransform: 'capitalize' }}>{state}: {n}</div>
              ))}
            </div>
          )}
        </div>

        {/* Memory — engine memory stats (Epic 4 facade reporting) */}
        <div style={{ background: 'var(--bg-raised)', border: '1px solid var(--border-default)', borderRadius: 'var(--radius-lg)', padding: '20px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: 'var(--text-muted)', marginBottom: '12px' }}>
            <Database size={18} /> <span style={{ fontSize: '13px', textTransform: 'uppercase', letterSpacing: '1px' }}>Memory</span>
          </div>
          {memoryError ? (
            <div style={{ fontSize: '15px', lineHeight: 1.7 }}>
              <div style={{ color: 'var(--text-muted)' }}>Episodes could not be loaded.</div>
              <div style={{ color: 'var(--danger)', fontSize: '13px' }}>{memoryError}</div>
            </div>
          ) : memoryStats === null ? (
            <div style={{ fontSize: '15px', color: 'var(--text-muted)' }}>
              Loading…
              {memorySlow && (
                <div style={{ fontSize: '12px', marginTop: '4px' }}>{DEFAULT_SLOW_TEXT}</div>
              )}
            </div>
          ) : (
            <div style={{ fontSize: '15px', lineHeight: 1.7 }}>
              <div>Backend: {memoryStats.backend}</div>
              <div>Episodes: {memoryStats.episodes ?? 0} ({memoryStats.embedded ?? 0} embedded{memoryStats.pending ? `, ${memoryStats.pending} pending` : ''})</div>
            </div>
          )}
        </div>

        {/* Memory Usage — real metric from Electron process.memoryUsage() IPC */}
        <div style={{ background: 'var(--bg-raised)', border: '1px solid var(--border-default)', borderRadius: 'var(--radius-lg)', padding: '20px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: 'var(--text-muted)', marginBottom: '12px' }}>
            <HardDrive size={18} /> <span style={{ fontSize: '13px', textTransform: 'uppercase', letterSpacing: '1px' }}>Memory Usage</span>
          </div>
          <div style={{ fontSize: '24px', fontWeight: 500 }}>{memoryMb !== null ? `${memoryMb} MB` : '—'}</div>
        </div>
      </div>

      {/* Task Ledger (master build S3 UI): the engine's task
          ledger read model, previously engine-only — recent tasks
          plus the joined per-task detail, over the same wsRef. */}
      <div style={{ marginTop: '32px' }}>
        <TaskLedgerPanel wsRef={wsRef} reconnectCount={reconnectCount} isConnected={isOnline} />
      </div>
    </div>
  );
}
