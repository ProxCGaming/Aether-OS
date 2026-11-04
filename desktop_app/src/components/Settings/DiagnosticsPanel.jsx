import React, { useState, useEffect } from 'react';
import { Download, Box } from 'lucide-react';
import { useAetherSocket } from '../../contexts/SocketContext';
import Dropdown from '../../Dropdown';
import MCPNotice from './MCPNotice';
import PluginsNotice from './PluginsNotice';
import LoadStateRow from './LoadStateRow';

export default function DiagnosticsPanel({ activeMenu, activeSession = null }) {
  const { wsRef, sendMessage, isConnected } = useAetherSocket();
  // Wave 3 slice 1 (RUX-C-06, under the RUX-C-01 umbrella): each
  // list below tracks whether its engine answer has arrived. Until
  // then the section renders a load state (loading / offline /
  // error) — the old code rendered "No MCP servers configured." /
  // "No plugins installed." / "Loading agents…" from the first
  // frame, so not-yet-loaded, genuinely-empty, failed and offline
  // were indistinguishable.
  const [mcpLoad, setMcpLoad] = useState('loading');
  const [pluginsLoad, setPluginsLoad] = useState('loading');
  const [agentsLoad, setAgentsLoad] = useState('loading');
  const [mcpServers, setMcpServers] = useState({});
  const [mcpResources, setMcpResources] = useState({}); // issue 006: server name -> [{uri, name, description, mimeType}]
  const [mcpPrompts, setMcpPrompts] = useState({}); // issue 007: server name -> [{name, description, arguments}]
  const [mcpPromptArgs, setMcpPromptArgs] = useState({}); // issue 007: "server:prompt" -> {arg: value}
  const [newMcpServer, setNewMcpServer] = useState({ name: '', command: '', args: '', url: '', headerKey: '', headerValue: '', envKey: '', envValue: '', cwd: '' });
  const [mcpTestResult, setMcpTestResult] = useState(null);
  const [plugins, setPlugins] = useState([]);
  const [pluginActionResult, setPluginActionResult] = useState(null);
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
          if (data.payload.config_error) {
            // B1: the whole config file failed to parse — surface it.
            setMcpTestResult(`MCP config file error: ${data.payload.config_error}`);
            // RUX-C-06: a broken config is a load ERROR, not an
            // empty server list (once answered, later errors keep
            // the list and report through the result line above).
            setMcpLoad((prev) => (prev === 'ready' ? prev : 'error'));
          } else {
            setMcpLoad('ready');
          }
          // Issue 006: list resources for every connected server, so
          // the tab can offer "Load into chat" per resource.
          // Issue 007: same for prompt templates.
          Object.entries(data.payload.servers || {}).forEach(([srvName, srv]) => {
            if (srv.status === 'connected') {
              sendMessage('MCP_RESOURCE_LIST_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: { name: srvName } });
              sendMessage('MCP_PROMPT_LIST_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: { name: srvName } });
            }
          });
        } else if (data.type === 'MCP_RESOURCE_LIST_RESPONSE') {
          if (data.payload.success) {
            setMcpResources(prev => ({ ...prev, [data.payload.name]: data.payload.resources || [] }));
          } else {
            // E3-F11: a failed list must be visible (the MCP
            // action-result idiom) and must not leave the previous
            // list on screen looking current — clear it, and refresh
            // the cached server status the failure may have changed.
            setMcpResources(prev => ({ ...prev, [data.payload.name]: [] }));
            setMcpTestResult(`Load resources ${data.payload.name} failed: ${data.payload.error || 'unknown error'}`);
            sendMessage('MCP_SERVER_LIST_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: {} });
          }
        } else if (data.type === 'MCP_PROMPT_LIST_RESPONSE') {
          if (data.payload.success) {
            setMcpPrompts(prev => ({ ...prev, [data.payload.name]: data.payload.prompts || [] }));
          } else {
            // E3-F11: same rule as the resource list above.
            setMcpPrompts(prev => ({ ...prev, [data.payload.name]: [] }));
            setMcpTestResult(`Load prompts ${data.payload.name} failed: ${data.payload.error || 'unknown error'}`);
            sendMessage('MCP_SERVER_LIST_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: {} });
          }
        } else if (data.type === 'MCP_PROMPT_RUN_RESPONSE') {
          setMcpTestResult(data.payload.success
            ? `Ran prompt '${data.payload.prompt}' into chat`
            : `Run prompt '${data.payload.prompt}' failed: ${data.payload.error || 'unknown error'}`);
        } else if (data.type === 'MCP_RESOURCE_READ_RESPONSE') {
          setMcpTestResult(data.payload.success
            ? `Loaded ${data.payload.uri} into chat${data.payload.truncated ? ' (truncated to 20000 chars)' : ''}`
            : `Load ${data.payload.uri} failed: ${data.payload.error || 'unknown error'}`);
        } else if (data.type === 'MCP_SERVER_ADD_RESPONSE' || data.type === 'MCP_SERVER_REMOVE_RESPONSE') {
          if (data.payload.success) {
            sendMessage('MCP_SERVER_LIST_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: {} });
          } else if (data.payload.error) {
            // A rejected config (e.g. both command and url) must be visible,
            // not silently ignored. E3-F11: a failed REMOVE gets the
            // same visibility — it previously displayed errors only
            // for ADD, so a refused removal looked like it worked.
            const verb = data.type === 'MCP_SERVER_ADD_RESPONSE' ? 'Add' : 'Remove';
            setMcpTestResult(`${verb} ${data.payload.name} failed: ${data.payload.error}`);
          }
        } else if (data.type === 'MCP_SERVER_TEST_RESPONSE') {
          setMcpTestResult(data.payload.success
            ? `Test ${data.payload.name}: connected — tools: ${(data.payload.tools || []).join(', ') || 'none'}`
            : `Test ${data.payload.name} failed: ${data.payload.error || 'unknown error'}`);
          sendMessage('MCP_SERVER_LIST_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: {} });
        } else if (data.type === 'MCP_SERVER_UPDATE_RESPONSE') {
          // Issue 004: a failed toggle must be visible, not silent.
          if (!data.payload.success) {
            setMcpTestResult(`Update ${data.payload.name} failed: ${data.payload.error || 'unknown error'}`);
          }
          sendMessage('MCP_SERVER_LIST_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: {} });
        } else if (data.type === 'MCP_SERVER_CONNECT_RESPONSE' || data.type === 'MCP_SERVER_DISCONNECT_RESPONSE') {
          // S9: a failed Connect/Disconnect must be visible, not silent.
          const verb = data.type === 'MCP_SERVER_CONNECT_RESPONSE' ? 'Connect' : 'Disconnect';
          if (!data.payload.success) {
            setMcpTestResult(`${verb} ${data.payload.name} failed: ${data.payload.error || 'unknown error'}`);
          }
          sendMessage('MCP_SERVER_LIST_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: {} });
        } else if (data.type === 'PLUGIN_LIST_RESPONSE') {
          const p = data.payload.plugins;
          setPlugins(Array.isArray(p) ? p : Object.values(p || {}));
          setPluginsLoad('ready');
        } else if (data.type === 'PLUGIN_INSTALL_RESPONSE' || data.type === 'PLUGIN_UNINSTALL_RESPONSE' || data.type === 'PLUGIN_TOGGLE_RESPONSE') {
          if (data.payload.success) {
            setPluginActionResult(null);
            sendMessage('PLUGIN_LIST_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: {} });
          } else if (data.payload.error) {
            // E7-F05: a failed install/toggle/uninstall must be
            // visible, not silently ignored (the MCP action-result
            // idiom above) — a refused uninstall previously left the
            // plugin installed with no sign anything went wrong.
            const verb = data.type === 'PLUGIN_UNINSTALL_RESPONSE' ? 'Uninstall'
              : data.type === 'PLUGIN_TOGGLE_RESPONSE' ? 'Toggle' : 'Install';
            setPluginActionResult(`${verb}${data.payload.name ? ` ${data.payload.name}` : ''} failed: ${data.payload.error}`);
          }
        } else if (data.type === 'AGENT_LIST_RESPONSE') {
          setAgents(data.payload.agents || []);
          setAgentsLoad('ready');
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
          
          <MCPNotice />

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
              <input
                type="text"
                value={newMcpServer.url}
                onChange={(e) => setNewMcpServer({...newMcpServer, url: e.target.value})}
                className="ios-glass-input"
                placeholder="Server URL for remote (Streamable HTTP, e.g. https://21st.dev/api/mcp)"
                style={{ padding: '12px 16px', color: '#fff', outline: 'none' }}
              />
              <div style={{ display: 'flex', gap: '12px' }}>
                <input
                  type="text"
                  value={newMcpServer.headerKey}
                  onChange={(e) => setNewMcpServer({...newMcpServer, headerKey: e.target.value})}
                  className="ios-glass-input"
                  placeholder="Header name (e.g. x-api-key)"
                  style={{ flex: 1, padding: '12px 16px', color: '#fff', outline: 'none' }}
                />
                <input
                  type="text"
                  value={newMcpServer.headerValue}
                  onChange={(e) => setNewMcpServer({...newMcpServer, headerValue: e.target.value})}
                  className="ios-glass-input"
                  placeholder="Header value (use ${VAR_NAME} for secrets)"
                  style={{ flex: 1, padding: '12px 16px', color: '#fff', outline: 'none' }}
                />
              </div>
              <div style={{ display: 'flex', gap: '12px' }}>
                <input
                  type="text"
                  value={newMcpServer.envKey}
                  onChange={(e) => setNewMcpServer({...newMcpServer, envKey: e.target.value})}
                  className="ios-glass-input"
                  placeholder="Env var name (local servers)"
                  style={{ flex: 1, padding: '12px 16px', color: '#fff', outline: 'none' }}
                />
                <input
                  type="text"
                  value={newMcpServer.envValue}
                  onChange={(e) => setNewMcpServer({...newMcpServer, envValue: e.target.value})}
                  className="ios-glass-input"
                  placeholder="Env var value"
                  style={{ flex: 1, padding: '12px 16px', color: '#fff', outline: 'none' }}
                />
              </div>
              <input
                type="text"
                value={newMcpServer.cwd}
                onChange={(e) => setNewMcpServer({...newMcpServer, cwd: e.target.value})}
                className="ios-glass-input"
                placeholder="Working directory (cwd, local servers)"
                style={{ padding: '12px 16px', color: '#fff', outline: 'none' }}
              />
              <button
                onClick={() => {
                  // S6: command AND url filled means two transports — warn
                  // instead of silently sending only the URL.
                  if (newMcpServer.command.trim() && newMcpServer.url.trim()) {
                    setMcpTestResult('Both a command and a URL are filled — pick one transport: a local command or a remote URL.');
                    return;
                  }
                  const resetForm = { name: '', command: '', args: '', url: '', headerKey: '', headerValue: '', envKey: '', envValue: '', cwd: '' };
                  if (newMcpServer.name && newMcpServer.url) {
                    const headers = newMcpServer.headerKey.trim()
                      ? { [newMcpServer.headerKey.trim()]: newMcpServer.headerValue }
                      : undefined;
                    sendMessage('MCP_SERVER_ADD_REQUEST', {
                      schema_version: 1,
                      request_id: Date.now().toString(),
                      payload: {
                        name: newMcpServer.name,
                        config: {
                          transport: 'streamable_http',
                          url: newMcpServer.url.trim(),
                          ...(headers ? { headers } : {}),
                        }
                      }
                    });
                    setNewMcpServer(resetForm);
                  } else if (newMcpServer.name && newMcpServer.command) {
                    const env = newMcpServer.envKey.trim()
                      ? { [newMcpServer.envKey.trim()]: newMcpServer.envValue }
                      : undefined;
                    sendMessage('MCP_SERVER_ADD_REQUEST', {
                      schema_version: 1,
                      request_id: Date.now().toString(),
                      payload: {
                        name: newMcpServer.name,
                        config: {
                          command: newMcpServer.command,
                          args: newMcpServer.args.split(',').map(s => s.trim()).filter(Boolean),
                          ...(env ? { env } : {}),
                          ...(newMcpServer.cwd.trim() ? { cwd: newMcpServer.cwd.trim() } : {}),
                        }
                      }
                    });
                    setNewMcpServer(resetForm);
                  }
                }}
                style={{ background: '#7c3aed', color: '#fff', border: 'none', padding: '12px 24px', borderRadius: '12px', cursor: 'pointer', fontWeight: 500, alignSelf: 'flex-start' }}
              >
                Add Server
              </button>
            </div>
          </div>

          {mcpTestResult && (
            <div style={{ color: '#fbbf24', fontSize: '13px', marginBottom: '16px' }}>{mcpTestResult}</div>
          )}

          <h3 style={{ fontSize: '16px', marginBottom: '16px' }}>Configured Servers</h3>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
            {Object.keys(mcpServers).length === 0 && mcpLoad !== 'ready' ? (
              <LoadStateRow
                loadState={mcpLoad}
                isConnected={isConnected}
                testId="mcp-servers-load-state"
                loadingText="Loading MCP servers…"
                offlineText="Waiting for the engine — configured servers will appear here once it connects."
                errorText="The MCP server list could not be loaded — the configuration file could not be read."
              />
            ) : Object.keys(mcpServers).length === 0 ? (
              <div style={{ color: '#9090a0', fontSize: '14px', padding: '24px', textAlign: 'center', background: 'rgba(0,0,0,0.2)', borderRadius: '12px' }}>
                No MCP servers configured.
              </div>
            ) : (
              Object.entries(mcpServers).map(([name, config]) => (
                <div key={name} style={{ background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.05)', borderRadius: '12px', padding: '16px', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                  <div>
                    <h4 style={{ fontSize: '15px', fontWeight: 500 }}>{name}</h4>
                    <p style={{ fontSize: '13px', color: '#9090a0', marginTop: '4px', fontFamily: 'monospace' }}>
                      {config.url ? config.url : `${config.command || ''} ${config.args?.join(' ') || ''}`}
                    </p>
                    <p style={{ fontSize: '13px', color: '#9090a0', marginTop: '4px' }}>
                      Status: {config.status || 'unknown'}{config.tool_count ? ` — ${config.tool_count} tool(s)` : ''}{config.error ? ` — ${config.error}` : ''}
                    </p>
                    <div style={{ display: 'flex', gap: '16px', marginTop: '8px' }}>
                      <label style={{ fontSize: '13px', color: '#c0c0d0', cursor: 'pointer' }}>
                        <input
                          type="checkbox"
                          checked={config.auto_connect !== false}
                          onChange={(e) => sendMessage('MCP_SERVER_UPDATE_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: { name, auto_connect: e.target.checked } })}
                          style={{ marginRight: '6px' }}
                        />
                        Auto-connect on startup
                      </label>
                      <label style={{ fontSize: '13px', color: '#c0c0d0', cursor: 'pointer' }}>
                        <input
                          type="checkbox"
                          checked={config.enabled !== false}
                          onChange={(e) => sendMessage('MCP_SERVER_UPDATE_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: { name, enabled: e.target.checked } })}
                          style={{ marginRight: '6px' }}
                        />
                        Enabled
                      </label>
                    </div>
                    {(mcpResources[name] || []).length > 0 && (
                      <div style={{ marginTop: '12px' }}>
                        <div style={{ fontSize: '13px', color: '#c0c0d0', marginBottom: '6px' }}>Resources:</div>
                        {(mcpResources[name] || []).map((res) => (
                          <div key={res.uri} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '8px', padding: '6px 0' }}>
                            <span style={{ fontSize: '13px', color: '#9090a0', fontFamily: 'monospace', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={res.description || res.uri}>
                              {res.name || res.uri}
                            </span>
                            <button
                              onClick={() => sendMessage('MCP_RESOURCE_READ_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: { name, uri: res.uri, session_id: activeSession } })}
                              style={{ background: 'rgba(124, 58, 237, 0.15)', color: '#a78bfa', border: '1px solid rgba(124, 58, 237, 0.4)', padding: '4px 12px', borderRadius: '8px', cursor: 'pointer', fontSize: '12px', whiteSpace: 'nowrap' }}
                            >
                              Load into chat
                            </button>
                          </div>
                        ))}
                      </div>
                    )}
                    {(mcpPrompts[name] || []).length > 0 && (
                      <div style={{ marginTop: '12px' }}>
                        <div style={{ fontSize: '13px', color: '#c0c0d0', marginBottom: '6px' }}>Prompts:</div>
                        {(mcpPrompts[name] || []).map((pr) => (
                          <div key={pr.name} style={{ padding: '6px 0' }}>
                            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '8px' }}>
                              <span style={{ fontSize: '13px', color: '#9090a0', fontFamily: 'monospace', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={pr.description || pr.name}>
                                {pr.name}
                              </span>
                              <button
                                onClick={() => sendMessage('MCP_PROMPT_RUN_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: { name, prompt: pr.name, arguments: mcpPromptArgs[`${name}:${pr.name}`] || {}, session_id: activeSession } })}
                                style={{ background: 'rgba(124, 58, 237, 0.15)', color: '#a78bfa', border: '1px solid rgba(124, 58, 237, 0.4)', padding: '4px 12px', borderRadius: '8px', cursor: 'pointer', fontSize: '12px', whiteSpace: 'nowrap' }}
                              >
                                Run in chat
                              </button>
                            </div>
                            {(pr.arguments || []).length > 0 && (
                              <div style={{ display: 'flex', gap: '8px', marginTop: '6px', flexWrap: 'wrap' }}>
                                {(pr.arguments || []).map((arg) => (
                                  <input
                                    key={arg.name}
                                    type="text"
                                    placeholder={arg.name}
                                    title={arg.description || arg.name}
                                    value={(mcpPromptArgs[`${name}:${pr.name}`] || {})[arg.name] || ''}
                                    onChange={(e) => setMcpPromptArgs(prev => ({
                                      ...prev,
                                      [`${name}:${pr.name}`]: {
                                        ...(prev[`${name}:${pr.name}`] || {}),
                                        [arg.name]: e.target.value,
                                      },
                                    }))}
                                    className="ios-glass-input"
                                    style={{ flex: 1, minWidth: '120px', padding: '8px 12px', color: '#fff', outline: 'none', fontSize: '13px' }}
                                  />
                                ))}
                              </div>
                            )}
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                  <div style={{ display: 'flex', gap: '8px' }}>
                    <button
                      onClick={() => sendMessage('MCP_SERVER_TEST_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: { name } })}
                      style={{ background: 'rgba(124, 58, 237, 0.15)', color: '#a78bfa', border: '1px solid rgba(124, 58, 237, 0.4)', padding: '6px 16px', borderRadius: '8px', cursor: 'pointer', fontSize: '13px' }}
                    >
                      Test
                    </button>
                    <button
                      onClick={() => sendMessage('MCP_SERVER_CONNECT_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: { name } })}
                      style={{ background: 'rgba(16, 185, 129, 0.15)', color: '#34d399', border: '1px solid rgba(16, 185, 129, 0.4)', padding: '6px 16px', borderRadius: '8px', cursor: 'pointer', fontSize: '13px' }}
                    >
                      Connect
                    </button>
                    <button
                      onClick={() => sendMessage('MCP_SERVER_DISCONNECT_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: { name } })}
                      style={{ background: 'rgba(245, 158, 11, 0.15)', color: '#fbbf24', border: '1px solid rgba(245, 158, 11, 0.4)', padding: '6px 16px', borderRadius: '8px', cursor: 'pointer', fontSize: '13px' }}
                    >
                      Disconnect
                    </button>
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
          
          <PluginsNotice />

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

          {pluginActionResult && (
            <div style={{ color: '#fbbf24', fontSize: '13px', marginBottom: '16px' }}>{pluginActionResult}</div>
          )}

          <h3 style={{ fontSize: '16px', marginBottom: '16px' }}>Installed Plugins</h3>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
            {plugins.length === 0 && pluginsLoad !== 'ready' ? (
              <LoadStateRow
                loadState={pluginsLoad}
                isConnected={isConnected}
                testId="plugins-load-state"
                loadingText="Loading plugins…"
                offlineText="Waiting for the engine — installed plugins will appear here once it connects."
                errorText="The plugin list could not be loaded."
              />
            ) : plugins.length === 0 ? (
              <div style={{ color: '#9090a0', fontSize: '14px', padding: '24px', textAlign: 'center', background: 'rgba(0,0,0,0.2)', borderRadius: '12px' }}>
                No plugins installed.
              </div>
            ) : (
              plugins.map(plugin => (
                <div key={plugin.manifest?.name || plugin.name} style={{ background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.05)', borderRadius: '12px', padding: '16px', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
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
                      onClick={() => sendMessage('PLUGIN_TOGGLE_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: { name: plugin.manifest?.name || plugin.name, enabled: !plugin.enabled } })}
                      style={{ background: plugin.enabled ? 'rgba(16, 185, 129, 0.15)' : 'rgba(255, 255, 255, 0.1)', color: plugin.enabled ? '#10b981' : '#fff', border: `1px solid ${plugin.enabled ? 'rgba(16, 185, 129, 0.3)' : 'rgba(255, 255, 255, 0.2)'}`, padding: '6px 16px', borderRadius: '8px', cursor: 'pointer', fontSize: '13px', width: '80px' }}
                    >
                      {plugin.enabled ? 'Enabled' : 'Disabled'}
                    </button>
                    <button 
                      onClick={() => sendMessage('PLUGIN_UNINSTALL_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: { name: plugin.manifest?.name || plugin.name } })}
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
                     {/* Epic 1, E1-05: the EFFECTIVE model — the agent's
                         own override when set, otherwise the global
                         default it inherits. */}
                     <p data-testid={`effective-model-${agent.id}`} style={{ color: '#a78bfa', fontSize: '12px', marginTop: '6px', fontFamily: 'monospace' }}>
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
               <div style={{ textAlign: 'center', color: '#9090a0', marginTop: '16px', fontSize: '13px' }}>
                 No agents were reported by the engine.
               </div>
             ))}
          </div>
        </div>
      )}
    </>
  );
}
