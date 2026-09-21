import React from 'react';
import { Activity, Cpu, HardDrive, Network, Zap } from 'lucide-react';
import './chat.css';

export default function Dashboard({ status = 'Initializing', activeTasks = 0, pluginCount = 0, memoryMb = null }) {
  const isOnline = status === 'Online' || status.startsWith('Online');
  
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
          <button style={{ background: 'rgba(var(--accent-rgb), 0.2)', border: '1px solid rgba(var(--accent-rgb), 0.5)', color: '#fff', padding: '12px 20px', borderRadius: '12px', display: 'flex', alignItems: 'center', gap: '8px', cursor: 'pointer', transition: 'all 0.2s' }}>
            <Zap size={16} /> Start New Workflow
          </button>
        </div>
      </div>
    </div>
  );
}
