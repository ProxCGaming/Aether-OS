import React, { useState, useEffect } from 'react';
import { HardDrive } from 'lucide-react';
import { useAetherSocket } from '../../contexts/SocketContext';

// Master build S10: the "This machine" card in Settings →
// Capabilities. The card above it runs MODEL capability checks
// (aether_engine/capability/ — LLM benchmarks); this one shows
// the MACHINE the engine runs on: the hardware probe and the
// per-feature capability matrix from
// aether_engine/machine_capability.py. Different questions,
// deliberately different titles.
//
// The card is self-contained (its own socket request, like
// MemoryManagement's backend picker): it asks on mount and on
// reconnect, renders the engine's verdicts and reasons
// verbatim — it computes nothing itself — and its Re-probe
// button re-asks with force, bypassing the engine's report
// cache. The matrix is advisory: no feature's gating changes
// because of what is shown here.

const STATUS_STYLE = {
  available: { label: 'Available', color: 'var(--success)', border: 'rgba(76,195,138,0.4)' },
  degraded: { label: 'Degraded', color: 'var(--accent)', border: 'rgba(var(--accent-rgb),0.4)' },
  unavailable: { label: 'Unavailable', color: 'var(--danger)', border: 'rgba(201,42,42,0.4)' },
  unknown: { label: 'Unknown', color: 'var(--text-muted)', border: 'var(--border-default)' },
};

function formatGB(bytes) {
  if (bytes == null) return 'unknown';
  const gb = bytes / (1024 ** 3);
  const text = gb.toFixed(1).replace(/\.0$/, '');
  return `${text} GB`;
}

function machineSummary(machine) {
  if (!machine) return '';
  const parts = [];
  const platform = machine.platform || {};
  if (platform.system) {
    parts.push(`${platform.system}${platform.release ? ` ${platform.release}` : ''}`);
  }
  const cpu = machine.cpu || {};
  if (cpu.model || cpu.logical_cores != null) {
    parts.push(`${cpu.model || 'CPU'}${cpu.logical_cores != null ? ` · ${cpu.logical_cores} cores` : ''}`);
  }
  const ram = (machine.ram || {}).total_bytes;
  parts.push(`RAM ${formatGB(ram)}`);
  parts.push(machine.gpu ? `GPU ${machine.gpu.name || 'detected'}` : 'No discrete GPU detected');
  const disk = machine.disk || {};
  if (disk.free_bytes != null) parts.push(`${formatGB(disk.free_bytes)} free on the data volume`);
  const network = (machine.network || {}).state;
  if (network) parts.push(`Network ${network}`);
  parts.push(machine.ollama_reachable == null
    ? 'Ollama unmeasured'
    : machine.ollama_reachable ? 'Ollama answering' : 'Ollama not answering');
  return parts.join('  ·  ');
}

export default function MachineCapabilityCard() {
  const { wsRef, sendMessage, isConnected, reconnectCount = 0 } = useAetherSocket();
  const [report, setReport] = useState(null);
  const [probing, setProbing] = useState(false);

  useEffect(() => {
    sendMessage('MACHINE_CAPABILITY_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: {} });

    const handleMessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'MACHINE_CAPABILITY_RESPONSE') {
          setReport(data.payload);
          setProbing(false);
        }
      } catch (e) {
        // Not JSON / not ours — other panels own their replies.
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
  }, [wsRef, sendMessage, reconnectCount]);

  const reprobe = () => {
    setProbing(true);
    sendMessage('MACHINE_CAPABILITY_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: { force: true } });
  };

  return (
    <div
      data-testid="machine-capability-card"
      style={{ background: 'var(--bg-raised)', border: '1px solid var(--border-default)', borderRadius: 'var(--radius-lg)', padding: '24px', marginTop: '20px' }}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '8px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
          <HardDrive size={18} style={{ color: 'var(--accent)' }} />
          <h3 style={{ fontSize: '16px', margin: 0 }}>This machine</h3>
        </div>
        <button
          data-testid="machine-capability-reprobe"
          onClick={reprobe}
          disabled={probing || !isConnected}
          style={{
            background: 'transparent',
            color: 'var(--accent)',
            border: '1px solid rgba(var(--accent-rgb), 0.5)',
            padding: '8px 16px',
            borderRadius: '8px',
            cursor: probing ? 'wait' : 'pointer',
          }}
        >
          {probing ? 'Probing…' : 'Re-probe'}
        </button>
      </div>
      <p style={{ color: 'var(--text-muted)', fontSize: '13px', margin: '0 0 16px 0' }}>
        The hardware the engine is running on, and what it means for each feature. This is
        measured on demand and cached for a few minutes — it is not the model capability
        checks above, and nothing is gated by it yet: each feature still checks for itself.
      </p>

      {!report ? (
        <div style={{ fontSize: '13px', color: 'var(--text-secondary)' }}>
          {isConnected ? 'Probing this machine…' : 'Waiting for the engine — this machine\u2019s capabilities will appear here once it connects.'}
        </div>
      ) : (
        <>
          <div data-testid="machine-summary" style={{ fontSize: '13px', color: 'var(--text-secondary)', marginBottom: '4px' }}>
            {machineSummary(report.machine)}
          </div>
          <div style={{ fontSize: '12px', color: 'var(--text-muted)', marginBottom: '16px' }}>
            Measured {new Date(report.probed_at * 1000).toLocaleTimeString()}
            {report.cached ? ' (cached copy)' : ''}
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
            {(report.features || []).map((feature) => {
              const style = STATUS_STYLE[feature.status] || STATUS_STYLE.unknown;
              return (
                <div
                  key={feature.id}
                  data-testid={`machine-feature-${feature.id}`}
                  style={{ background: 'var(--bg-base)', border: '1px solid var(--border-default)', borderRadius: '12px', padding: '12px 16px' }}
                >
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '12px' }}>
                    <span style={{ fontSize: '14px', fontWeight: 500 }}>{feature.name}</span>
                    <span
                      data-testid={`machine-feature-status-${feature.id}`}
                      style={{ fontSize: '11px', fontWeight: 600, color: style.color, border: `1px solid ${style.border}`, borderRadius: '999px', padding: '2px 10px', whiteSpace: 'nowrap' }}
                    >
                      {style.label}
                    </span>
                  </div>
                  <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginTop: '4px' }}>
                    {feature.reason}
                  </div>
                </div>
              );
            })}
          </div>
        </>
      )}
    </div>
  );
}
