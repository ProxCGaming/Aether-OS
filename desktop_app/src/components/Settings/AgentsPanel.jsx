import React, { useState, useEffect, useRef } from 'react';
import { Box } from 'lucide-react';
import { useAetherSocket } from '../../contexts/SocketContext';
import ModelPicker from '../ModelPicker';
import LoadStateRow from './LoadStateRow';

// Wave 5 (Annex C row 8): the Agents tab, extracted from
// DiagnosticsPanel.jsx — that file owned the Agents, MCP and
// Plugins tabs in one component; each tab is now its own panel and
// DiagnosticsPanel is only the dispatcher. Behaviour is unchanged:
// same requests on mount, same response handling, same rendering.
export default function AgentsPanel() {
  const { wsRef, sendMessage, isConnected } = useAetherSocket();
  // Wave 3 slice 1 (RUX-C-06, under the RUX-C-01 umbrella): the
  // agent list tracks whether its engine answer has arrived.
  // Until then the section renders a load state (loading / offline
  // / error) — the old code rendered "Loading agents…" from the
  // first frame, so not-yet-loaded, genuinely-empty, failed and
  // offline were indistinguishable.
  const [agentsLoad, setAgentsLoad] = useState('loading');
  const [agents, setAgents] = useState([]);
  const [tools, setTools] = useState([]);
  const [providers, setProviders] = useState([]);
  // D2-02: the last AGENT_UPDATE refusal, shown in the Agents view
  // until the next update is confirmed.
  const [agentUpdateError, setAgentUpdateError] = useState('');
  // D2-02: per-agent field values as they were BEFORE the first
  // still-unconfirmed optimistic update. The engine's
  // AGENT_UPDATE_RESPONSE carries no echoed values, so a refusal
  // can only roll back to a snapshot taken at send time.
  const pendingAgentUpdatesRef = useRef({});
  const rememberAgentSnapshot = (agent, fields) => {
    const entry = pendingAgentUpdatesRef.current[agent.id] || {};
    fields.forEach((field) => {
      if (!(field in entry)) entry[field] = agent[field];
    });
    pendingAgentUpdatesRef.current[agent.id] = entry;
  };

  useEffect(() => {
    sendMessage('AGENT_LIST_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: {} });
    sendMessage('TOOL_LIST_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: {} });
    // D2-01: the per-agent model picker is built from the
    // provider list, which used to reach this panel only if a
    // broadcast happened to pass while it was mounted — socket
    // messages are live-only, with no replay for a panel opened
    // later. Request it on mount like every other list here.
    sendMessage('PROVIDER_LIST_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: {} });
  }, [sendMessage]);

  useEffect(() => {
    const handleMessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'AGENT_LIST_RESPONSE') {
          // RUX-C-10 (residual): an errored reply is the error
          // state — marking it ready rendered "No agents were
          // reported by the engine" for a failed fetch.
          if (data.payload.error) {
            setAgentsLoad('error');
          } else {
            setAgents(data.payload.agents || []);
            setAgentsLoad('ready');
          }
        } else if (data.type === 'TOOL_LIST_RESPONSE') {
          setTools(data.payload.tools || []);
        } else if (data.type === 'AGENT_UPDATE_RESPONSE') {
          // D2-02: the engine refuses with success:false (unknown
          // agent, malformed model, unknown provider, save
          // failure). The optimistic value must not survive a
          // refusal: restore the send-time snapshot for the fields
          // still unconfirmed and name the refusal.
          const agentId = data.payload.agent_id;
          if (data.payload.success) {
            delete pendingAgentUpdatesRef.current[agentId];
            setAgentUpdateError('');
          } else {
            const prior = pendingAgentUpdatesRef.current[agentId];
            if (prior) {
              setAgents(prev => prev.map(a => (a.id === agentId ? { ...a, ...prior } : a)));
              delete pendingAgentUpdatesRef.current[agentId];
            }
            setAgentUpdateError(`Agent update refused: ${data.payload.error || 'the engine rejected the change.'}`);
          }
        }
        // Grab providers for agents model override
        if (data.type === 'PROVIDER_LIST_RESPONSE') {
            setProviders(data.payload.providers || []);
        }
      } catch {
        // ignore
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
  }, [wsRef, sendMessage]);

  return (
    <div style={{ color: 'var(--text-primary)' }}>
      <h2 style={{ fontSize: '24px', marginBottom: '8px', fontWeight: 500 }}>Agents (LangGraph Nodes)</h2>
      <p style={{ color: 'var(--text-muted)', fontSize: '14px', marginBottom: '24px' }}>Assign specialized models to individual agent nodes in the Aether execution graph.</p>

      {agentUpdateError && (
        <div data-testid="agent-update-error" style={{ color: 'var(--danger)', fontSize: '13px', marginBottom: '16px' }}>
          {agentUpdateError}
        </div>
      )}

      <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
         {agents.map(agent => (
           <div key={agent.id} style={{ background: 'var(--bg-raised)', border: '1px solid var(--border-default)', borderRadius: 'var(--radius-lg)', padding: '20px', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
             <div style={{ display: 'flex', alignItems: 'center', gap: '16px' }}>
               <div style={{ background: 'rgba(var(--accent-rgb),0.2)', padding: '10px', borderRadius: '10px', color: 'var(--accent)' }}>
                  <Box size={24} />
               </div>
               <div>
                 <h3 style={{ fontSize: '16px', marginBottom: '4px' }}>{agent.name}</h3>
                 <p style={{ color: 'var(--text-muted)', fontSize: '13px' }}>{agent.description}</p>
                 {/* Epic 1, E1-05: the EFFECTIVE model — the agent's
                     own override when set, otherwise the global
                     default it inherits. */}
                 <p data-testid={`effective-model-${agent.id}`} style={{ color: 'var(--accent)', fontSize: '12px', marginTop: '6px', fontFamily: 'var(--font-mono)' }}>
                   {(() => {
                     const override = agent.model && agent.model !== 'inherit' ? agent.model : null;
                     if (override) {
                       const sep = override.indexOf(':');
                       const prov = sep === -1 ? '' : override.slice(0, sep);
                       const model = sep === -1 ? override : override.slice(sep + 1);
                       const provLabel = (providers || []).find(p => p.name === prov)?.display_name || prov;
                       return prov ? `Effective model: ${model} (${provLabel})` : `Effective model: ${model}`;
                     }
                     const def = (providers || []).find(p => p.is_default);
                     return def
                       ? `Effective model: Inherits ${def.display_name || def.name} / ${def.default_model}`
                       : 'Effective model: Inherits global default';
                   })()}
                 </p>
               </div>
             </div>
             <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: '8px' }}>
               <ModelPicker
                 format="agent"
                 providers={providers}
                 value={agent.model || 'inherit'}
                 onChange={(val) => {
                   rememberAgentSnapshot(agent, ['model']);
                   setAgents(prev => prev.map(a => a.id === agent.id ? { ...a, model: val } : a));
                   sendMessage('AGENT_UPDATE_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: { agent_id: agent.id, model: val } });
                 }}
                 leadingOptions={[{ value: 'inherit', label: '[Use Global Default Model]' }]}
                 style={{ width: '220px' }}
                 align="right"
               />

               <div style={{ marginTop: '8px', width: '220px' }}>
                  <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginBottom: '4px' }}>Allowed Tools:</div>
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px', maxHeight: '100px', overflowY: 'auto', background: 'var(--bg-base)', padding: '8px', borderRadius: '8px' }}>
                    {tools.map(t => {
                      const isAllowed = !agent.tools || agent.tools.includes(t.function.name);
                      return (
                        <label key={t.function.name} style={{ display: 'flex', alignItems: 'center', gap: '4px', fontSize: '11px', cursor: 'pointer', color: isAllowed ? 'var(--accent)' : 'var(--text-secondary)' }}>
                          <input
                            type="checkbox"
                            checked={!!isAllowed}
                            onChange={(e) => {
                              const checked = e.target.checked;
                              const currentTools = agent.tools || tools.map(tool => tool.function.name);
                              const nextTools = checked
                                ? [...currentTools, t.function.name]
                                : currentTools.filter(name => name !== t.function.name);

                              rememberAgentSnapshot(agent, ['tools']);
                              setAgents(prev => prev.map(a => a.id === agent.id ? { ...a, tools: nextTools } : a));

                              sendMessage('AGENT_UPDATE_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: { agent_id: agent.id, tools: nextTools } });
                            }}
                            style={{ accentColor: 'var(--accent)', transform: 'scale(0.8)' }}
                          />
                          {t.function.name}
                        </label>
                      );
                    })}
                  </div>
               </div>
             </div>
           </div>
         ))}
         {agents.length === 0 && (agentsLoad !== 'ready' ? (
           <div style={{ marginTop: '16px' }}>
             <LoadStateRow
               loadState={agentsLoad}
               isConnected={isConnected}
               testId="agents-load-state"
               variant="plain"
               style={{ textAlign: 'center' }}
               loadingText="Loading agents…"
               offlineText="Waiting for the engine — agents will appear here once it connects."
               errorText="The agent list could not be loaded."
             />
           </div>
         ) : (
           <div style={{ textAlign: 'center', color: 'var(--text-secondary)', marginTop: '16px', fontSize: '13px' }}>
             No agents were reported by the engine.
           </div>
         ))}
      </div>
    </div>
  );
}
