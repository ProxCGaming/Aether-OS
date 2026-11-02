import React, { useEffect, useState } from 'react';
import { Activity, Cpu, Database, HardDrive, HeartPulse, Network, Zap } from 'lucide-react';
import './chat.css';

export default function Dashboard({ status = 'Initializing', activeTasks = 0, pluginCount = 0, memoryMb = null, providerHealth = {}, wsRef = null, onStartWorkflow }) {
  const isOnline = status === 'Online' || status.startsWith('Online');
  // Epic 6, E6-05: memory stats come from the engine on mount
  // (MEMORY_EPISODES_RESPONSE carries backend + embedding stats).
  const [memoryStats, setMemoryStats] = useState(null);
  // E4-F14 family: engine memory replies can carry an error instead of
  // backend/stats (unavailable backend). Like MemoryManagement, that is
  // an error state — never a healthy-looking native/zero default.
  const [memoryError, setMemoryError] = useState(null);
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
    return () => {
      if (wsRef.current) wsRef.current.removeEventListener('message', handleMessage);
    };
  }, [wsRef]);
  const healthEntries = Object.entries(providerHealth || {});
  const healthCounts = healthEntries.reduce((acc, [, h]) => {
    const key = h && h.status ? h.status : 'unknown';
    acc[key] = (acc[key] || 0) + 1;
    return acc;
  }, {});
  
  return (
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', color: '#f0f0f5', padding: '16px', overflowY: 'auto' }} className="chat-scroll">
      <div style={{ marginBottom: '32px' }}>
        <h1 style={{ fontSize: '28px', margin: '0 0 8px 0', fontWeight: 600 }}>System Dashboard</h1>
        <p style={{ color: '#9090a0', margin: 0, fontSize: '14px' }}>Overview of Aether-OS active tasks and resources.</p>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: '16px', marginBottom: '32px' }}>
        {/* Engine Status — derived from real WS connection state */}
        <div style={{ background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.08)', borderRadius: '16px', padding: '20px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: '#a78bfa', marginBottom: '12px' }}>
            <Activity size={18} /> <span style={{ fontSize: '13px', textTransform: 'uppercase', letterSpacing: '1px' }}>Engine Status</span>
          </div>
          <div style={{ fontSize: '24px', fontWeight: 500, color: isOnline ? '#10b981' : '#f59e0b' }}>{isOnline ? 'Online' : status}</div>
        </div>

        {/* Active Tasks — real count from WS task lifecycle events */}
        <div style={{ background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.08)', borderRadius: '16px', padding: '20px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: '#a78bfa', marginBottom: '12px' }}>
            <Cpu size={18} /> <span style={{ fontSize: '13px', textTransform: 'uppercase', letterSpacing: '1px' }}>Active Tasks</span>
          </div>
          <div style={{ fontSize: '24px', fontWeight: 500 }}>{activeTasks}</div>
        </div>

        {/* Plugins — real count from PLUGIN_LIST_RESPONSE */}
        <div style={{ background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.08)', borderRadius: '16px', padding: '20px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: '#a78bfa', marginBottom: '12px' }}>
            <Network size={18} /> <span style={{ fontSize: '13px', textTransform: 'uppercase', letterSpacing: '1px' }}>Plugins</span>
          </div>
          <div style={{ fontSize: '24px', fontWeight: 500 }}>{pluginCount} Active</div>
        </div>

        {/* Provider Health — Epic 5 health snapshot via App state */}
        <div style={{ background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.08)', borderRadius: '16px', padding: '20px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: '#a78bfa', marginBottom: '12px' }}>
            <HeartPulse size={18} /> <span style={{ fontSize: '13px', textTransform: 'uppercase', letterSpacing: '1px' }}>Provider Health</span>
          </div>
          {healthEntries.length === 0 ? (
            <div style={{ fontSize: '15px', color: '#9090a0' }}>No providers reporting yet</div>
          ) : (
            <div style={{ fontSize: '15px', lineHeight: 1.7 }}>
              {Object.entries(healthCounts).map(([state, n]) => (
                <div key={state} style={{ textTransform: 'capitalize' }}>{state}: {n}</div>
              ))}
            </div>
          )}
        </div>

        {/* Memory — engine memory stats (Epic 4 facade reporting) */}
        <div style={{ background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.08)', borderRadius: '16px', padding: '20px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: '#a78bfa', marginBottom: '12px' }}>
            <Database size={18} /> <span style={{ fontSize: '13px', textTransform: 'uppercase', letterSpacing: '1px' }}>Memory</span>
          </div>
          {memoryError ? (
            <div style={{ fontSize: '15px', lineHeight: 1.7 }}>
              <div style={{ color: '#9090a0' }}>Episodes could not be loaded.</div>
              <div style={{ color: '#f87171', fontSize: '13px' }}>{memoryError}</div>
            </div>
          ) : memoryStats === null ? (
            <div style={{ fontSize: '15px', color: '#9090a0' }}>Loading…</div>
          ) : (
            <div style={{ fontSize: '15px', lineHeight: 1.7 }}>
              <div>Backend: {memoryStats.backend}</div>
              <div>Episodes: {memoryStats.episodes ?? 0} ({memoryStats.embedded ?? 0} embedded{memoryStats.pending ? `, ${memoryStats.pending} pending` : ''})</div>
            </div>
          )}
        </div>

        {/* Memory Usage — real metric from Electron process.memoryUsage() IPC */}
        <div style={{ background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.08)', borderRadius: '16px', padding: '20px' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', color: '#a78bfa', marginBottom: '12px' }}>
            <HardDrive size={18} /> <span style={{ fontSize: '13px', textTransform: 'uppercase', letterSpacing: '1px' }}>Memory Usage</span>
          </div>
          <div style={{ fontSize: '24px', fontWeight: 500 }}>{memoryMb !== null ? `${memoryMb} MB` : '—'}</div>
        </div>
      </div>

      <div>
        <h2 style={{ fontSize: '18px', fontWeight: 500, marginBottom: '16px' }}>Quick Actions</h2>
        <div style={{ display: 'flex', gap: '12px' }}>
          {/* Phase 9, B3: previously a dead button with no onClick — now navigates to Chat to start a task. */}
          <button onClick={onStartWorkflow} style={{ background: 'rgba(var(--accent-rgb), 0.2)', border: '1px solid rgba(var(--accent-rgb), 0.5)', color: '#fff', padding: '12px 20px', borderRadius: '12px', display: 'flex', alignItems: 'center', gap: '8px', cursor: 'pointer', transition: 'all 0.2s' }}>
            <Zap size={16} /> Start New Workflow
          </button>
        </div>
      </div>
    </div>
  );
}
