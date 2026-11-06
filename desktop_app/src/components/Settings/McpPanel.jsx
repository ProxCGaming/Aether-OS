import React, { useState, useEffect } from 'react';
import { useAetherSocket } from '../../contexts/SocketContext';
import MCPNotice from './MCPNotice';
import LoadStateRow from './LoadStateRow';

// Wave 5 (Annex C row 8): the MCP Servers tab, extracted from
// DiagnosticsPanel.jsx — that file owned the Agents, MCP and
// Plugins tabs in one component; each tab is now its own panel and
// DiagnosticsPanel is only the dispatcher. Behaviour is unchanged:
// same requests on mount, same response handling, same rendering.
export default function McpPanel({ activeSession = null }) {
  const { wsRef, sendMessage, isConnected } = useAetherSocket();
  // Wave 3 slice 1 (RUX-C-06, under the RUX-C-01 umbrella): the
  // server list tracks whether its engine answer has arrived.
  // Until then the section renders a load state (loading / offline
  // / error) — the old code rendered "No MCP servers configured."
  // from the first frame, so not-yet-loaded, genuinely-empty,
  // failed and offline were indistinguishable.
  const [mcpLoad, setMcpLoad] = useState('loading');
  const [mcpServers, setMcpServers] = useState({});
  const [mcpResources, setMcpResources] = useState({}); // issue 006: server name -> [{uri, name, description, mimeType}]
  const [mcpPrompts, setMcpPrompts] = useState({}); // issue 007: server name -> [{name, description, arguments}]
  const [mcpPromptArgs, setMcpPromptArgs] = useState({}); // issue 007: "server:prompt" -> {arg: value}
  const [newMcpServer, setNewMcpServer] = useState({ name: '', command: '', args: '', url: '', headerKey: '', headerValue: '', envKey: '', envValue: '', cwd: '' });
  const [mcpTestResult, setMcpTestResult] = useState(null);

  useEffect(() => {
    sendMessage('MCP_SERVER_LIST_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: {} });
  }, [sendMessage]);

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
              } else {
                // D2-18: the chain used to end here — a form
                // with a name but neither transport (or no name
                // at all) produced no request, no message and
                // no reset, so the button looked dead. Say what
                // is missing; the form stays for fixing.
                setMcpTestResult(newMcpServer.name.trim()
                  ? 'Add a command (local server) or a URL (remote server) — a name alone is not enough; nothing was sent.'
                  : 'Give the server a name, plus a command (local server) or a URL (remote server) — nothing was sent.');
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
                  onClick={() => {
                    // D2-28: removing a server deletes its
                    // configuration — confirm first, with the
                    // same window.confirm pattern as the other
                    // Settings deletes (MemoryManagement);
                    // Projects confirms removals too. This was
                    // the one that fired immediately.
                    if (!window.confirm(`Remove the MCP server "${name}"? Its configuration will be deleted.`)) return;
                    sendMessage('MCP_SERVER_REMOVE_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: { name } });
                  }}
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
  );
}
