import React, { useState, useEffect } from 'react';
import { Download, Box } from 'lucide-react';
import { useAetherSocket } from '../../contexts/SocketContext';
import Dropdown from '../../Dropdown';

export default function DiagnosticsPanel({ activeMenu }) {
  const { wsRef, sendMessage } = useAetherSocket();
  const [mcpServers, setMcpServers] = useState({});
  const [newMcpServer, setNewMcpServer] = useState({ name: '', command: '', args: '' });
  const [plugins, setPlugins] = useState([]);
  const [newPluginSource, setNewPluginSource] = useState('');
  const [agents, setAgents] = useState([]);
  const [tools, setTools] = useState([]);
  const [providers, setProviders] = useState([]);

  useEffect(() => {
    if (activeMenu === 'MCP') {
      sendMessage('MCP_SERVER_LIST_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: {} });
    } else if (activeMenu === 'Plugins') {
      sendMessage('PLUGIN_LIST_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: {} });
    } else if (activeMenu === 'Agents') {
      sendMessage('AGENT_LIST_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: {} });
      sendMessage('TOOL_LIST_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: {} });
    }
  }, [activeMenu, sendMessage]);

  useEffect(() => {
    const handleMessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'MCP_SERVER_LIST_RESPONSE') {
          setMcpServers(data.payload.servers || {});
        } else if (data.type === 'MCP_SERVER_ADD_RESPONSE' || data.type === 'MCP_SERVER_REMOVE_RESPONSE') {
          if (data.payload.success) {
            sendMessage('MCP_SERVER_LIST_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: {} });
          }
        } else if (data.type === 'PLUGIN_LIST_RESPONSE') {
          const p = data.payload.plugins;
          setPlugins(Array.isArray(p) ? p : Object.values(p || {}));
        } else if (data.type === 'PLUGIN_INSTALL_RESPONSE' || data.type === 'PLUGIN_UNINSTALL_RESPONSE' || data.type === 'PLUGIN_TOGGLE_RESPONSE') {
          if (data.payload.success) {
            sendMessage('PLUGIN_LIST_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: {} });
          }
        } else if (data.type === 'AGENT_LIST_RESPONSE') {
          setAgents(data.payload.agents || []);
        } else if (data.type === 'TOOL_LIST_RESPONSE') {
          setTools(data.payload.tools || []);
        } else if (data.type === 'SESSION_LIST_RESPONSE') {
          // just to silently ignore or handle if needed
        }
        // Grab providers for agents model override
        if (data.type === 'PROVIDER_LIST_RESPONSE') {
            setProviders(data.payload.providers || []);
        }
      } catch (e) {
        // ignore
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
  }, [wsRef, sendMessage]);

  if (activeMenu !== 'MCP' && activeMenu !== 'Plugins' && activeMenu !== 'Agents') {
    return null;
  }

  return (
    <>
      { activeMenu === 'MCP' && (
        <div style={{ color: '#f0f0f5' }}>
          <h2 style={{ fontSize: '24px', marginBottom: '8px', fontWeight: 500 }}>MCP Connections</h2>
          <p style={{ color: '#9090a0', fontSize: '14px', marginBottom: '24px' }}>Manage remote Model Context Protocol servers.</p>
          
          <div style={{ background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.08)', borderRadius: '16px', padding: '20px', marginBottom: '24px' }}>
            <h3 style={{ fontSize: '16px', marginBottom: '16px' }}>Add New Server</h3>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
              <input 
                type="text"
                value={newMcpServer.name}
                onChange={(e) => setNewMcpServer({...newMcpServer, name: e.target.value})}
                className="ios-glass-input" 
                placeholder="Server Name (e.g. SQLite DB)" 
                style={{ padding: '12px 16px', color: '#fff', outline: 'none' }}
              />
              <input 
                type="text"
                value={newMcpServer.command}
                onChange={(e) => setNewMcpServer({...newMcpServer, command: e.target.value})}
                className="ios-glass-input" 
                placeholder="Command (e.g. python, npx)" 
                style={{ padding: '12px 16px', color: '#fff', outline: 'none' }}
              />
              <input 
                type="text"
                value={newMcpServer.args}
                onChange={(e) => setNewMcpServer({...newMcpServer, args: e.target.value})}
                className="ios-glass-input" 
                placeholder="Arguments (comma separated)" 
                style={{ padding: '12px 16px', color: '#fff', outline: 'none' }}
              />
              <button 
                onClick={() => {
                  if (newMcpServer.name && newMcpServer.command) {
                    sendMessage('MCP_SERVER_ADD_REQUEST', {
                      schema_version: 1,
                      request_id: Date.now().toString(),
                      payload: { 
                        name: newMcpServer.name, 
                        config: { 
                          command: newMcpServer.command, 
                          args: newMcpServer.args.split(',').map(s => s.trim()).filter(Boolean) 
                        }
                      }
                    });
                    setNewMcpServer({ name: '', command: '', args: '' });
                  }
                }}
                style={{ background: '#7c3aed', color: '#fff', border: 'none', padding: '12px 24px', borderRadius: '12px', cursor: 'pointer', fontWeight: 500, alignSelf: 'flex-start' }}
              >
                Add Server
              </button>
            </div>
          </div>

          <h3 style={{ fontSize: '16px', marginBottom: '16px' }}>Configured Servers</h3>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
            {Object.keys(mcpServers).length === 0 ? (
              <div style={{ color: '#9090a0', fontSize: '14px', padding: '24px', textAlign: 'center', background: 'rgba(0,0,0,0.2)', borderRadius: '12px' }}>
                No MCP servers configured.
              </div>
            ) : (
              Object.entries(mcpServers).map(([name, config]) => (
                <div key={name} style={{ background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.05)', borderRadius: '12px', padding: '16px', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                  <div>
                    <h4 style={{ fontSize: '15px', fontWeight: 500 }}>{name}</h4>
                    <p style={{ fontSize: '13px', color: '#9090a0', marginTop: '4px', fontFamily: 'monospace' }}>
                      {config.command} {config.args?.join(' ')}
                    </p>
                  </div>
                  <div>
                    <button 
                      onClick={() => sendMessage('MCP_SERVER_REMOVE_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: { name } })}
                      style={{ background: 'rgba(242, 65, 91, 0.15)', color: '#FA5870', border: '1px solid rgba(242, 65, 91, 0.28)', padding: '6px 16px', borderRadius: '8px', cursor: 'pointer', fontSize: '13px' }}
                    >
                      Remove
                    </button>
                  </div>
                </div>
              ))
            )}
          </div>
        </div>
      )}

      {activeMenu === 'Plugins' && (
        <div style={{ color: '#f0f0f5' }}>
          <h2 style={{ fontSize: '24px', marginBottom: '8px', fontWeight: 500 }}>Plugins</h2>
          <p style={{ color: '#9090a0', fontSize: '14px', marginBottom: '24px' }}>Manage active plugins and extensions for Aether-OS.</p>
          
          <div style={{ background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.08)', borderRadius: '16px', padding: '20px', marginBottom: '24px' }}>
            <h3 style={{ fontSize: '16px', marginBottom: '12px' }}>Install New Plugin</h3>
            <div style={{ display: 'flex', gap: '12px' }}>
              <input 
                type="text"
                value={newPluginSource}
                onChange={(e) => setNewPluginSource(e.target.value)}
                className="ios-glass-input" 
                placeholder="GitHub URL or local path" 
                style={{ flex: 1, padding: '12px 16px', color: '#fff', outline: 'none' }}
              />
              <button 
                onClick={() => {
                  if (newPluginSource) {
                    sendMessage('PLUGIN_INSTALL_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: { source: newPluginSource } });
                    setNewPluginSource('');
                  }
                }}
                style={{ background: '#7c3aed', color: '#fff', border: 'none', padding: '0 24px', borderRadius: '12px', cursor: 'pointer', display: 'flex', alignItems: 'center', gap: '8px', fontWeight: 500 }}
              >
                <Download size={16} />
                Install
              </button>
            </div>
          </div>

          <h3 style={{ fontSize: '16px', marginBottom: '16px' }}>Installed Plugins</h3>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
            {plugins.length === 0 ? (
              <div style={{ color: '#9090a0', fontSize: '14px', padding: '24px', textAlign: 'center', background: 'rgba(0,0,0,0.2)', borderRadius: '12px' }}>
                No plugins installed.
              </div>
            ) : (
              plugins.map(plugin => (
                <div key={plugin.name} style={{ background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.05)', borderRadius: '12px', padding: '16px', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                  <div>
                    <h4 style={{ fontSize: '15px', fontWeight: 500, display: 'flex', alignItems: 'center', gap: '8px' }}>
                      {plugin.manifest?.name || plugin.name}
                      <span style={{ fontSize: '11px', background: 'rgba(255,255,255,0.1)', padding: '2px 6px', borderRadius: '4px' }}>v{plugin.manifest?.version || '1.0'}</span>
                    </h4>
                    <p style={{ fontSize: '13px', color: '#9090a0', marginTop: '4px' }}>
                      {plugin.manifest?.description || 'No description available.'}
                    </p>
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
                    <button 
                      onClick={() => sendMessage('PLUGIN_TOGGLE_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: { name: plugin.name, enabled: !plugin.enabled } })}
                      style={{ background: plugin.enabled ? 'rgba(16, 185, 129, 0.15)' : 'rgba(255, 255, 255, 0.1)', color: plugin.enabled ? '#10b981' : '#fff', border: `1px solid ${plugin.enabled ? 'rgba(16, 185, 129, 0.3)' : 'rgba(255, 255, 255, 0.2)'}`, padding: '6px 16px', borderRadius: '8px', cursor: 'pointer', fontSize: '13px', width: '80px' }}
                    >
                      {plugin.enabled ? 'Enabled' : 'Disabled'}
                    </button>
                    <button 
                      onClick={() => sendMessage('PLUGIN_UNINSTALL_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: { name: plugin.name } })}
                      style={{ background: 'rgba(242, 65, 91, 0.15)', color: '#FA5870', border: '1px solid rgba(242, 65, 91, 0.28)', padding: '6px 16px', borderRadius: '8px', cursor: 'pointer', fontSize: '13px' }}
                    >
                      Uninstall
                    </button>
                  </div>
                </div>
              ))
            )}
          </div>
        </div>
      )}

      {activeMenu === 'Agents' && (
        <div style={{ color: '#f0f0f5' }}>
          <h2 style={{ fontSize: '24px', marginBottom: '8px', fontWeight: 500 }}>Agents (LangGraph Nodes)</h2>
          <p style={{ color: '#9090a0', fontSize: '14px', marginBottom: '24px' }}>Assign specialized models to individual agent nodes in the Aether execution graph.</p>
          
          <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
             {agents.map(agent => (
               <div key={agent.id} style={{ background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(124, 58, 237, 0.2)', borderRadius: '16px', padding: '20px', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                 <div style={{ display: 'flex', alignItems: 'center', gap: '16px' }}>
                   <div style={{ background: 'rgba(124,58,237,0.2)', padding: '10px', borderRadius: '10px', color: '#a78bfa' }}>
                      <Box size={24} />
                   </div>
                   <div>
                     <h3 style={{ fontSize: '16px', marginBottom: '4px' }}>{agent.name}</h3>
                     <p style={{ color: '#9090a0', fontSize: '13px' }}>{agent.description}</p>
                   </div>
                 </div>
                 <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: '8px' }}>
                   <Dropdown 
                     value={agent.model || 'inherit'}
                     onChange={(val) => {
                       setAgents(prev => prev.map(a => a.id === agent.id ? { ...a, model: val } : a));
                       sendMessage('AGENT_UPDATE_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: { agent_id: agent.id, model: val } });
                     }}
                     options={[
                       { value: 'inherit', label: '[Use Global Default Model]' },
                       ...(providers || []).flatMap(p => 
                         (p.models || []).map(m => ({
                           value: `${p.name}:${m}`,
                           label: `${m} (${p.display_name})`
                         }))
                       )
                     ]}
                     style={{ width: '220px' }}
                     align="right"
                   />
                   
                   <div style={{ marginTop: '8px', width: '220px' }}>
                      <div style={{ fontSize: '12px', color: '#9090a0', marginBottom: '4px' }}>Allowed Tools:</div>
                      <div style={{ display: 'flex', flexWrap: 'wrap', gap: '4px', maxHeight: '100px', overflowY: 'auto', background: 'rgba(0,0,0,0.2)', padding: '8px', borderRadius: '8px' }}>
                        {tools.map(t => {
                          const isAllowed = !agent.tools || agent.tools.includes(t.function.name);
                          return (
                            <label key={t.function.name} style={{ display: 'flex', alignItems: 'center', gap: '4px', fontSize: '11px', cursor: 'pointer', color: isAllowed ? '#a78bfa' : '#9090a0' }}>
                              <input 
                                type="checkbox" 
                                checked={!!isAllowed} 
                                onChange={(e) => {
                                  const checked = e.target.checked;
                                  const currentTools = agent.tools || tools.map(tool => tool.function.name);
                                  const nextTools = checked 
                                    ? [...currentTools, t.function.name]
                                    : currentTools.filter(name => name !== t.function.name);
                                    
                                  setAgents(prev => prev.map(a => a.id === agent.id ? { ...a, tools: nextTools } : a));
                                  
                                  sendMessage('AGENT_UPDATE_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: { agent_id: agent.id, tools: nextTools } });
                                }}
                                style={{ accentColor: '#7c3aed', transform: 'scale(0.8)' }}
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
             {agents.length === 0 && (
               <div style={{ textAlign: 'center', color: '#9090a0', marginTop: '16px', fontSize: '13px' }}>
                 Loading agents...
               </div>
             )}
          </div>
        </div>
      )}
    </>
  );
}
