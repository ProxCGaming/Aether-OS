import React, { useState, useEffect } from 'react';
import { useAetherSocket } from '../../contexts/SocketContext';
import LoadStateRow from './LoadStateRow';
import { mcpIssueCount, pluginIssueCount, skillIssueCount } from './capabilityIssues';

// D4-9: the capability-issues roll-up on the Capabilities tab.
//
// The diagnostics card above answers "did the environment checks
// pass?" — it never knew about the four capability surfaces, so a
// broken MCP server was invisible here (FF4-09). This card closes
// that: it asks for the SAME list payloads the owning tabs
// consume (MCP_SERVER_LIST / PLUGIN_LIST / SKILL_LIST), counts
// their issues with the panels' own state rules (capabilityIssues.js),
// and links each count to the tab that owns it.
//
// Self-contained, on the MachineCapabilityCard pattern: its own
// socket requests on mount and on reconnect, no state lifted
// into the shell. Until a surface's answer lands it contributes
// nothing — the card never guesses a count.
export default function CapabilityIssuesCard({ onOpenCapabilityTab = null }) {
  const { wsRef, sendMessage, isConnected, reconnectCount = 0 } = useAetherSocket();
  const [mcpServers, setMcpServers] = useState(null);
  const [pluginPayload, setPluginPayload] = useState(null);
  const [skills, setSkills] = useState(null);

  useEffect(() => {
    sendMessage('MCP_SERVER_LIST_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: {} });
    sendMessage('PLUGIN_LIST_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: {} });
    sendMessage('SKILL_LIST_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: {} });

    const handleMessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'MCP_SERVER_LIST_RESPONSE') {
          setMcpServers(data.payload.servers || {});
        } else if (data.type === 'PLUGIN_LIST_RESPONSE') {
          setPluginPayload(data.payload || {});
        } else if (data.type === 'SKILL_LIST_RESPONSE') {
          setSkills(data.payload.skills || []);
        }
      } catch {
        // Not JSON / not ours — the owning panels handle theirs.
      }
    };

    const ws = wsRef.current;
    if (ws) {
      ws.addEventListener('message', handleMessage);
    }
    return () => {
      if (ws) {
        ws.removeEventListener('message', handleMessage);
      }
    };
  }, [wsRef, sendMessage, reconnectCount]);

  const answered = mcpServers !== null || pluginPayload !== null || skills !== null;
  const allAnswered = mcpServers !== null && pluginPayload !== null && skills !== null;

  const mcpCount = mcpServers !== null ? mcpIssueCount(mcpServers) : 0;
  const pluginCount = pluginPayload !== null ? pluginIssueCount(pluginPayload) : 0;
  const skillCount = skills !== null ? skillIssueCount(skills) : 0;
  const total = mcpCount + pluginCount + skillCount;

  const rows = [];
  if (mcpCount > 0) {
    rows.push({
      key: 'mcp',
      menu: 'MCP',
      text: `${mcpCount} MCP server${mcpCount === 1 ? '' : 's'} failing or invalid`,
      actionLabel: 'Open MCP',
    });
  }
  if (pluginCount > 0) {
    rows.push({
      key: 'plugins',
      menu: 'Plugins',
      text: 'The plugin registry could not be read',
      actionLabel: 'Open Plugins',
    });
  }
  if (skillCount > 0) {
    rows.push({
      key: 'skills',
      menu: 'Skills',
      text: `${skillCount} skill${skillCount === 1 ? '' : 's'} failed to load`,
      actionLabel: 'Open Skills',
    });
  }

  return (
    <div data-testid="capability-issues-card" style={{ background: 'var(--bg-raised)', border: '1px solid var(--border-default)', borderRadius: 'var(--radius-lg)', padding: '24px', marginTop: '24px' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '8px' }}>
        <div style={{ width: '12px', height: '12px', borderRadius: '50%', background: !answered ? 'var(--text-muted)' : (total > 0 ? 'var(--danger)' : 'var(--success)'), boxShadow: answered && total === 0 ? '0 0 10px rgba(76, 195, 138,0.5)' : 'none' }}></div>
        <h3 style={{ fontSize: '16px', margin: 0 }}>
          {!answered
            ? 'Capability Issues'
            : total > 0
              ? `${total} Capability Issue${total === 1 ? '' : 's'}`
              : 'No Capability Issues'}
        </h3>
      </div>
      <p style={{ color: 'var(--text-muted)', fontSize: '13px', margin: '0 0 16px 0' }}>
        MCP servers, plugins and skills, counted from the same lists their own tabs show.
      </p>

      {!answered ? (
        <LoadStateRow
          loadState="loading"
          isConnected={isConnected}
          testId="capability-issues-load-state"
          loadingText="Checking MCP servers, plugins and skills…"
          offlineText="Waiting for the engine — capability issues will appear here once it connects."
        />
      ) : rows.length === 0 ? (
        <div data-testid="capability-issues-clear" style={{ color: 'var(--text-secondary)', fontSize: '14px' }}>
          {allAnswered
            ? 'Every MCP server, plugin and skill loaded cleanly.'
            : 'No issues in the answers received so far.'}
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
          {rows.map((row) => (
            <div
              key={row.key}
              data-testid={`capability-issues-${row.key}`}
              style={{ background: 'var(--bg-base)', border: '1px solid rgba(201,42,42, 0.25)', borderRadius: '12px', padding: '12px 16px', display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '16px' }}
            >
              <span style={{ fontSize: '14px', color: 'var(--danger)' }}>{row.text}</span>
              {onOpenCapabilityTab && (
                <button
                  onClick={() => onOpenCapabilityTab(row.menu)}
                  data-testid={`capability-issues-open-${row.key}`}
                  style={{ background: 'transparent', color: 'var(--accent)', border: '1px solid rgba(var(--accent-rgb), 0.5)', padding: '6px 14px', borderRadius: '8px', cursor: 'pointer', fontSize: '13px', flexShrink: 0 }}
                >
                  {row.actionLabel}
                </button>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
