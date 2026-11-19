import React, { useState, useEffect } from 'react';
import { Search } from 'lucide-react';
import { useAetherSocket } from '../../contexts/SocketContext';
import Dropdown from '../../Dropdown';
import MCPNotice from './MCPNotice';
import LoadStateRow from './LoadStateRow';
import EmptyState from '../EmptyState';
import CapabilityFilterBar from './CapabilityFilterBar';
import CapabilityItem from './CapabilityItem';
import { capabilityFilterCounts, filterCapabilityItems } from './capabilityFilter';
import { parseServerJson } from './mcpServerJson';
import { ENABLE_TOGGLE_TYPES, pendingEnableChange, queuedEnableVerb } from './capabilityEnable';

// Header / env editors (add form and edit form) model pairs as a
// list of {key, value} rows so a server can carry any number of
// each — the engine has always accepted full dicts; the form was
// the narrow part. In the EDIT form a row whose value is left blank
// for an already-stored key means "keep the stored value": header
// and env values are secrets the engine never sends back (only key
// names), so keeping one must be expressible without its value.
const emptyPair = () => ({ key: '', value: '' });

function pairsToDict(pairs) {
  const dict = {};
  for (const pair of pairs || []) {
    const key = (pair.key || '').trim();
    if (key) dict[key] = pair.value;
  }
  return dict;
}

function PairRows({ pairs, onChange, kind }) {
  const isHeader = kind === 'header';
  const setPair = (idx, patch) => onChange(pairs.map((p, i) => (i === idx ? { ...p, ...patch } : p)));
  return (
    <>
      {pairs.map((pair, idx) => (
        <div key={idx} style={{ display: 'flex', gap: '12px' }}>
          <input
            type="text"
            value={pair.key}
            onChange={(e) => setPair(idx, { key: e.target.value })}
            className="ios-glass-input"
            placeholder={isHeader ? 'Header name (e.g. x-api-key)' : 'Env var name (local servers)'}
            style={{ flex: 1, padding: '12px 16px', color: 'var(--text-primary)', outline: 'none' }}
          />
          <input
            type="text"
            value={pair.value}
            onChange={(e) => setPair(idx, { value: e.target.value })}
            className="ios-glass-input"
            placeholder={isHeader ? 'Header value (use ${VAR_NAME} for secrets)' : 'Env var value'}
            style={{ flex: 1, padding: '12px 16px', color: 'var(--text-primary)', outline: 'none' }}
          />
          {pairs.length > 1 && (
            <button
              onClick={() => onChange(pairs.filter((_, i) => i !== idx))}
              title={isHeader ? 'Remove header' : 'Remove env var'}
              style={{ background: 'transparent', color: 'var(--text-secondary)', border: '1px solid var(--border-default)', padding: '6px 12px', borderRadius: '8px', cursor: 'pointer', fontSize: '13px' }}
            >
              Remove
            </button>
          )}
        </div>
      ))}
      <button
        onClick={() => onChange([...pairs, emptyPair()])}
        style={{ background: 'transparent', color: 'var(--accent)', border: '1px solid rgba(var(--accent-rgb), 0.4)', padding: '6px 14px', borderRadius: '8px', cursor: 'pointer', fontSize: '13px', alignSelf: 'flex-start' }}
      >
        {isHeader ? 'Add another header' : 'Add another env var'}
      </button>
    </>
  );
}

// Wave 5 (Annex C row 8): the MCP Servers tab, extracted from
// DiagnosticsPanel.jsx — that file owned the Agents, MCP and
// Plugins tabs in one component; each tab is now its own panel and
// DiagnosticsPanel is only the dispatcher. Behaviour is unchanged:
// same requests on mount, same response handling, same rendering.

// D4-3: the ONE trust card for "this server will run". The add flow
// shows it before anything is saved; confirming a server that was
// added before confirmation existed shows the same card for that
// server's stored config. Both name exactly what will run — the
// command + args (and cwd) for a local server, the URL for a
// remote one — before the user confirms.
function McpRunCard({ name, config, mode, confirmLabel, onConfirm, onCancel }) {
  return (
    <div style={{ background: 'var(--bg-raised)', border: '1px solid rgba(var(--accent-rgb), 0.4)', borderRadius: 'var(--radius-lg)', padding: '20px', marginBottom: '24px' }}>
      <h3 style={{ fontSize: '16px', marginBottom: '12px' }}>Before it runs</h3>
      {config.command ? (
        <p style={{ color: 'var(--text-secondary)', fontSize: '14px', marginTop: 0 }}>
          {mode === 'add' ? <>Adding <strong>{name}</strong> will run this command on this machine:</>
            : mode === 'edit' ? <>Saving changes to <strong>{name}</strong> will run this command on this machine:</>
            : <><strong>{name}</strong> will run this command on this machine:</>}
          <span style={{ display: 'block', fontFamily: 'var(--font-mono)', fontSize: '13px', color: 'var(--text-primary)', margin: '8px 0', wordBreak: 'break-all' }}>
            {config.command} {(config.args || []).join(' ')}
          </span>
          {config.cwd && (
            <>It runs from the working directory <span style={{ fontFamily: 'var(--font-mono)' }}>{config.cwd}</span>. </>
          )}
          It runs when you connect the server — and automatically at startup while
          &ldquo;Auto-connect on startup&rdquo; is on. Its tools will be available to the agent.
          {mode === 'edit' && (
            <> Saving disconnects the server — reconnect it to run the new configuration.</>
          )}
        </p>
      ) : (
        <p style={{ color: 'var(--text-secondary)', fontSize: '14px', marginTop: 0 }}>
          {mode === 'add' ? <>Adding <strong>{name}</strong> will connect Aether to this remote server:</>
            : mode === 'edit' ? <>Saving changes to <strong>{name}</strong> will connect Aether to this remote server:</>
            : <>Confirming <strong>{name}</strong> will connect Aether to this remote server:</>}
          <span style={{ display: 'block', fontFamily: 'var(--font-mono)', fontSize: '13px', color: 'var(--text-primary)', margin: '8px 0', wordBreak: 'break-all' }}>
            {config.url}
          </span>
          It connects when you connect the server — and automatically at startup while
          &ldquo;Auto-connect on startup&rdquo; is on. Its tools will be available to the agent.
          {mode === 'edit' && (
            <> Saving disconnects the server — reconnect it to run the new configuration.</>
          )}
        </p>
      )}
      <div style={{ display: 'flex', gap: '8px' }}>
        <button
          onClick={onCancel}
          style={{ background: 'transparent', color: 'var(--text-secondary)', border: '1px solid var(--border-default)', padding: '10px 20px', borderRadius: '12px', cursor: 'pointer', fontWeight: 500 }}
        >
          Cancel
        </button>
        <button
          onClick={onConfirm}
          style={{ background: 'var(--accent)', color: 'var(--accent-on)', border: 'none', padding: '10px 20px', borderRadius: '12px', cursor: 'pointer', fontWeight: 500 }}
        >
          {confirmLabel}
        </button>
      </div>
    </div>
  );
}

export default function McpPanel({ activeSession = null }) {
  const { wsRef, sendMessage, isConnected, safeSend, pendingMessages } = useAetherSocket();
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
  const emptyForm = () => ({ name: '', command: '', args: '', url: '', cwd: '', headerPairs: [emptyPair()], envPairs: [emptyPair()] });
  const [newMcpServer, setNewMcpServer] = useState(emptyForm);
  const [mcpTestResult, setMcpTestResult] = useState(null);
  // Registry import: the pasted server.json text, and the
  // provenance ({registryName, version}) of the last successful
  // import. The provenance is saved with the server on Add, so the
  // row can later be checked for a newer published version.
  const [importText, setImportText] = useState('');
  const [importedFrom, setImportedFrom] = useState(null);
  // Trust step: a staged add ({name, config}) waiting on the
  // confirmation card below. Nothing is sent to the engine — and so
  // nothing is saved or connected — until the user confirms the
  // card naming what the server will run. Cancelling drops the
  // staged add and keeps the form for editing.
  const [pendingMcpAdd, setPendingMcpAdd] = useState(null);
  // The same trust step for a server that already exists but was
  // added before confirmation existed: {name, config} staged from
  // the server's row, confirmed through the same card.
  const [pendingMcpConfirm, setPendingMcpConfirm] = useState(null);
  // Edit-in-place: the server whose inline edit form is open, the
  // form itself, and a staged edit ({name, config}) whose run
  // configuration changed — those wait on the same card, naming
  // the NEW command/URL, before anything is sent.
  const [editingName, setEditingName] = useState(null);
  const [editForm, setEditForm] = useState(null);
  const [pendingMcpEdit, setPendingMcpEdit] = useState(null);
  // Per-server expansion of the tool detail (the server's tools
  // ride the list answer; expanding is purely local).
  const [expandedTools, setExpandedTools] = useState({});
  // D4-1: the server list runs through the shared capability
  // pattern (CapabilityFilterBar + capabilityFilter + CapabilityItem
  // rows). A server classifies as 'error' when it is in a failed
  // state or carries an error, 'disabled' when its enabled flag is
  // off, 'enabled' otherwise.
  const [capQuery, setCapQuery] = useState('');
  const [capFilter, setCapFilter] = useState('all');
  const serverEntries = Object.entries(mcpServers).map(([name, config]) => ({
    item: { name, config },
    name,
    state: (config.status === 'failed' || config.error) ? 'error' : (config.enabled === false ? 'disabled' : 'enabled'),
    // FF4-04: the engine's recorded update state (a registry check
    // found a newer published version) drives the Updates chip —
    // absent data reads as no update, never a guessed one.
    hasUpdate: config.has_update === true,
  }));
  const serverCounts = capabilityFilterCounts(serverEntries);
  const visibleServerEntries = filterCapabilityItems(serverEntries, capQuery, capFilter);
  const clearCapabilityFilter = () => { setCapQuery(''); setCapFilter('all'); };

  const confirmPendingMcpConfirm = () => {
    if (!pendingMcpConfirm) return;
    sendMessage('MCP_SERVER_UPDATE_REQUEST', {
      schema_version: 1,
      request_id: Date.now().toString(),
      payload: { name: pendingMcpConfirm.name, confirmed: true },
    });
    setPendingMcpConfirm(null);
  };

  const confirmPendingMcpAdd = () => {
    if (!pendingMcpAdd) return;
    sendMessage('MCP_SERVER_ADD_REQUEST', {
      schema_version: 1,
      request_id: Date.now().toString(),
      payload: {
        name: pendingMcpAdd.name,
        // `confirmed` is the marker the engine's auto-connect gate
        // reads: this add was confirmed on the card, so the server
        // may connect at startup.
        config: { ...pendingMcpAdd.config, confirmed: true },
      }
    });
    setPendingMcpAdd(null);
    setNewMcpServer(emptyForm());
    setImportedFrom(null);
    setImportText('');
  };

  const confirmPendingMcpEdit = () => {
    if (!pendingMcpEdit) return;
    sendMessage('MCP_SERVER_UPDATE_REQUEST', {
      schema_version: 1,
      request_id: Date.now().toString(),
      // The card named the new command/URL and the user confirmed
      // it, so the edit carries the confirmation with it — the
      // engine keeps the marker only for a confirmed run change.
      payload: { name: pendingMcpEdit.name, config: pendingMcpEdit.config, confirmed: true },
    });
    setPendingMcpEdit(null);
    setEditingName(null);
    setEditForm(null);
  };

  const startEdit = (name, config) => {
    setPendingMcpEdit(null);
    setEditingName(name);
    setEditForm({
      command: config.command || '',
      args: (config.args || []).join(', '),
      url: config.url || '',
      cwd: config.cwd || '',
      timeout: config.timeout != null ? String(config.timeout) : '',
      // The list answer redacts header/env values to key names —
      // the rows start with those names and a blank value, which
      // the save reads as "keep the stored value".
      headerPairs: (config.headers || []).map((key) => ({ key, value: '' })),
      envPairs: (config.env || []).map((key) => ({ key, value: '' })),
    });
  };

  const cancelEdit = () => {
    setEditingName(null);
    setEditForm(null);
    setPendingMcpEdit(null);
  };

  // Build the replacement config from the edit form. Header/env
  // pairs resolve to the engine's merge vocabulary: a typed value
  // is the new value, a blank value for a stored key becomes null
  // ("keep the stored value" — resolved engine-side, since the
  // value itself never reaches this panel), a stored key left out
  // of the form is deleted. Returns {config} or {error}.
  const buildEditConfig = (config) => {
    const hasCommand = editForm.command.trim() !== '';
    const hasUrl = editForm.url.trim() !== '';
    if (hasCommand && hasUrl) {
      return { error: 'Both a command and a URL are filled — pick one transport: a local command or a remote URL.' };
    }
    if (!hasCommand && !hasUrl) {
      return { error: 'An edit needs a command (local server) or a URL (remote server) — nothing was sent.' };
    }
    const resolvePairs = (pairs, storedKeys, label) => {
      const dict = {};
      for (const pair of pairs || []) {
        const key = (pair.key || '').trim();
        if (!key) continue;
        if (pair.value !== '') {
          dict[key] = pair.value;
        } else if (storedKeys.includes(key)) {
          dict[key] = null;
        } else {
          return { error: `${label} "${key}" needs a value — it has no stored value to keep.` };
        }
      }
      return { dict };
    };
    const timeout = editForm.timeout.trim() === '' ? undefined : Number(editForm.timeout);
    if (timeout !== undefined && (!Number.isFinite(timeout) || timeout <= 0 || timeout > 300)) {
      return { error: 'Timeout must be between 1 and 300 seconds.' };
    }
    if (hasUrl) {
      const headers = resolvePairs(editForm.headerPairs, config.headers || [], 'Header');
      if (headers.error) return headers;
      return {
        config: {
          transport: 'streamable_http',
          url: editForm.url.trim(),
          headers: headers.dict,
          // Explicit removals: the stdio fields of the old config.
          command: null, args: null, env: null, cwd: null,
          ...(timeout !== undefined ? { timeout } : {}),
        },
      };
    }
    const env = resolvePairs(editForm.envPairs, config.env || [], 'Env var');
    if (env.error) return env;
    return {
      config: {
        transport: 'stdio',
        command: editForm.command.trim(),
        args: editForm.args.split(',').map((s) => s.trim()).filter(Boolean),
        env: env.dict,
        cwd: editForm.cwd.trim() || null,
        url: null, headers: null,
        ...(timeout !== undefined ? { timeout } : {}),
      },
    };
  };

  // Did the edit change what the server runs? The panel cannot
  // compare secret values (it never sees them), so any typed
  // header/env value counts as a change, as does any key-set or
  // transport-field difference. The engine re-checks with the real
  // values (its fingerprint gate) — this only decides whether the
  // confirmation card is shown first.
  const editChangesRun = (config, newConfig) => {
    if ((config.command || '') !== (newConfig.command || '')) return true;
    if ((config.url || '') !== (newConfig.url || '')) return true;
    if ((config.cwd || '') !== (newConfig.cwd || '')) return true;
    if (JSON.stringify(config.args || []) !== JSON.stringify(newConfig.args || [])) return true;
    const pairsChanged = (dict, storedKeys) => {
      if (!dict) return false;
      const keys = Object.keys(dict);
      if (keys.length !== storedKeys.length || keys.some((k) => !storedKeys.includes(k))) return true;
      return keys.some((k) => dict[k] !== null);
    };
    if (pairsChanged(newConfig.headers, config.headers || [])) return true;
    if (pairsChanged(newConfig.env, config.env || [])) return true;
    return false;
  };

  const saveEdit = (name, config) => {
    const built = buildEditConfig(config);
    if (built.error) {
      setMcpTestResult(built.error);
      return;
    }
    if (editChangesRun(config, built.config)) {
      // What runs changed: stage behind the card naming the NEW
      // command/URL. Nothing is sent until it is confirmed.
      setPendingMcpEdit({ name, config: built.config });
      return;
    }
    sendMessage('MCP_SERVER_UPDATE_REQUEST', {
      schema_version: 1,
      request_id: Date.now().toString(),
      payload: { name, config: built.config },
    });
    setEditingName(null);
    setEditForm(null);
  };

  const importServerJson = () => {
    try {
      const parsed = parseServerJson(importText);
      setNewMcpServer({
        name: parsed.name,
        command: parsed.command,
        args: parsed.args,
        url: parsed.url,
        cwd: '',
        headerPairs: parsed.headerPairs,
        envPairs: parsed.envPairs,
      });
      setImportedFrom({ registryName: parsed.registryName, version: parsed.version });
      setMcpTestResult(
        `Imported ${parsed.registryName}${parsed.version ? ` (version ${parsed.version})` : ''} — check the details below, then Add Server.`
      );
    } catch (e) {
      setImportedFrom(null);
      setMcpTestResult(e.message);
    }
  };

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
        } else if (data.type === 'TOOL_POLICY_SET_RESPONSE') {
          // A policy set from the MCP tool detail writes the same
          // override the Tools panel shows (one store, two views).
          // The reply carries the FINAL policy — the engine may
          // downgrade "Always Allow" for risky tools and says so in
          // `message`; surface that, never the requested value.
          if (data.payload.success) {
            setMcpTestResult(data.payload.message
              || `Policy for ${data.payload.tool_name} set to ${data.payload.policy}.`);
          } else {
            setMcpTestResult(`Set policy for ${data.payload.tool_name} failed: ${data.payload.error || 'unknown error'}`);
          }
          sendMessage('MCP_SERVER_LIST_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: {} });
        } else if (data.type === 'MCP_SERVER_UPDATE_CHECK_RESPONSE') {
          if (data.payload.success) {
            setMcpTestResult(data.payload.has_update
              ? `Update available for ${data.payload.name}: version ${data.payload.latest_version} (installed: ${data.payload.version}).`
              : `${data.payload.name} is up to date (version ${data.payload.latest_version}).`);
          } else {
            setMcpTestResult(`Update check for ${data.payload.name} failed: ${data.payload.error || 'unknown error'}`);
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
    <div style={{ color: 'var(--text-primary)' }}>
      <h2 style={{ fontSize: '24px', marginBottom: '8px', fontWeight: 500 }}>MCP Connections</h2>
      <p style={{ color: 'var(--text-muted)', fontSize: '14px', marginBottom: '24px' }}>Manage remote Model Context Protocol servers.</p>

      <MCPNotice />

      <div style={{ background: 'var(--bg-raised)', border: '1px solid var(--border-default)', borderRadius: 'var(--radius-lg)', padding: '20px', marginBottom: '24px' }}>
        <h3 style={{ fontSize: '16px', marginBottom: '16px' }}>Add New Server</h3>
        <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
          <input
            type="text"
            value={newMcpServer.name}
            onChange={(e) => setNewMcpServer({...newMcpServer, name: e.target.value})}
            className="ios-glass-input"
            placeholder="Server Name (e.g. SQLite DB)"
            style={{ padding: '12px 16px', color: 'var(--text-primary)', outline: 'none' }}
          />
          <input
            type="text"
            value={newMcpServer.command}
            onChange={(e) => setNewMcpServer({...newMcpServer, command: e.target.value})}
            className="ios-glass-input"
            placeholder="Command (e.g. python, npx)"
            style={{ padding: '12px 16px', color: 'var(--text-primary)', outline: 'none' }}
          />
          <input
            type="text"
            value={newMcpServer.args}
            onChange={(e) => setNewMcpServer({...newMcpServer, args: e.target.value})}
            className="ios-glass-input"
            placeholder="Arguments (comma separated)"
            style={{ padding: '12px 16px', color: 'var(--text-primary)', outline: 'none' }}
          />
          <input
            type="text"
            value={newMcpServer.url}
            onChange={(e) => setNewMcpServer({...newMcpServer, url: e.target.value})}
            className="ios-glass-input"
            placeholder="Server URL for remote (Streamable HTTP, e.g. https://21st.dev/api/mcp)"
            style={{ padding: '12px 16px', color: 'var(--text-primary)', outline: 'none' }}
          />
          <PairRows
            kind="header"
            pairs={newMcpServer.headerPairs}
            onChange={(pairs) => setNewMcpServer({ ...newMcpServer, headerPairs: pairs })}
          />
          <PairRows
            kind="env"
            pairs={newMcpServer.envPairs}
            onChange={(pairs) => setNewMcpServer({ ...newMcpServer, envPairs: pairs })}
          />
          <input
            type="text"
            value={newMcpServer.cwd}
            onChange={(e) => setNewMcpServer({...newMcpServer, cwd: e.target.value})}
            className="ios-glass-input"
            placeholder="Working directory (cwd, local servers)"
            style={{ padding: '12px 16px', color: 'var(--text-primary)', outline: 'none' }}
          />
          <div style={{ borderTop: '1px solid var(--border-default)', paddingTop: '12px' }}>
            <div style={{ fontSize: '13px', color: 'var(--text-secondary)', marginBottom: '8px' }}>
              Or import a registry entry — paste its server.json and the form fills in:
            </div>
            <textarea
              value={importText}
              onChange={(e) => setImportText(e.target.value)}
              className="ios-glass-input"
              placeholder="Paste a server.json from the MCP registry…"
              rows={4}
              style={{ width: '100%', padding: '12px 16px', color: 'var(--text-primary)', outline: 'none', fontFamily: 'var(--font-mono)', fontSize: '12px', resize: 'vertical', boxSizing: 'border-box' }}
            />
            <button
              onClick={importServerJson}
              style={{ background: 'transparent', color: 'var(--accent)', border: '1px solid rgba(var(--accent-rgb), 0.4)', padding: '8px 16px', borderRadius: '8px', cursor: 'pointer', fontSize: '13px', marginTop: '8px' }}
            >
              Import server.json
            </button>
            {importedFrom && (
              <div style={{ fontSize: '13px', color: 'var(--text-secondary)', marginTop: '8px' }}>
                Imported from registry entry <span style={{ fontFamily: 'var(--font-mono)' }}>{importedFrom.registryName}</span>
                {importedFrom.version ? <> (version {importedFrom.version})</> : null}
                {' '}— the entry and its version are saved with the server, so it can be checked for updates later.{' '}
                <button
                  onClick={() => setImportedFrom(null)}
                  style={{ background: 'transparent', color: 'var(--accent)', border: 'none', padding: 0, cursor: 'pointer', fontSize: '13px' }}
                >
                  Clear import
                </button>
              </div>
            )}
          </div>
          <button
            onClick={() => {
              // S6: command AND url filled means two transports — warn
              // instead of silently sending only the URL.
              if (newMcpServer.command.trim() && newMcpServer.url.trim()) {
                setMcpTestResult('Both a command and a URL are filled — pick one transport: a local command or a remote URL.');
                return;
              }
              // Trust step: a valid form STAGES the add and shows
              // the confirmation card — the ADD request is sent only
              // from the card's Confirm (confirmPendingMcpAdd).
              // A registry import's provenance rides the staged
              // config, so the saved server remembers which entry
              // (and version) it came from.
              const provenance = importedFrom
                ? {
                    registry_name: importedFrom.registryName,
                    ...(importedFrom.version ? { version: importedFrom.version } : {}),
                  }
                : {};
              if (newMcpServer.name && newMcpServer.url) {
                const headers = pairsToDict(newMcpServer.headerPairs);
                setPendingMcpAdd({
                  name: newMcpServer.name,
                  config: {
                    transport: 'streamable_http',
                    url: newMcpServer.url.trim(),
                    ...(Object.keys(headers).length > 0 ? { headers } : {}),
                    ...provenance,
                  }
                });
              } else if (newMcpServer.name && newMcpServer.command) {
                const env = pairsToDict(newMcpServer.envPairs);
                setPendingMcpAdd({
                  name: newMcpServer.name,
                  config: {
                    command: newMcpServer.command,
                    args: newMcpServer.args.split(',').map(s => s.trim()).filter(Boolean),
                    ...(Object.keys(env).length > 0 ? { env } : {}),
                    ...(newMcpServer.cwd.trim() ? { cwd: newMcpServer.cwd.trim() } : {}),
                    ...provenance,
                  }
                });
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
            style={{ background: 'var(--accent)', color: 'var(--accent-on)', border: 'none', padding: '12px 24px', borderRadius: '12px', cursor: 'pointer', fontWeight: 500, alignSelf: 'flex-start' }}
          >
            Add Server
          </button>
        </div>
      </div>

      {pendingMcpAdd && (
        <McpRunCard
          name={pendingMcpAdd.name}
          config={pendingMcpAdd.config}
          mode="add"
          confirmLabel="Confirm and add"
          onConfirm={confirmPendingMcpAdd}
          onCancel={() => setPendingMcpAdd(null)}
        />
      )}

      {pendingMcpConfirm && (
        <McpRunCard
          name={pendingMcpConfirm.name}
          config={pendingMcpConfirm.config}
          mode="confirm"
          confirmLabel="Confirm server"
          onConfirm={confirmPendingMcpConfirm}
          onCancel={() => setPendingMcpConfirm(null)}
        />
      )}

      {pendingMcpEdit && (
        <McpRunCard
          name={pendingMcpEdit.name}
          config={pendingMcpEdit.config}
          mode="edit"
          confirmLabel="Confirm and save changes"
          onConfirm={confirmPendingMcpEdit}
          onCancel={() => setPendingMcpEdit(null)}
        />
      )}

      {mcpTestResult && (
        <div style={{ color: 'var(--accent)', fontSize: '13px', marginBottom: '16px' }}>{mcpTestResult}</div>
      )}

      <h3 style={{ fontSize: '16px', marginBottom: '16px' }}>Configured Servers</h3>
      {Object.keys(mcpServers).length > 0 && (
        <CapabilityFilterBar
          query={capQuery}
          onQueryChange={setCapQuery}
          filter={capFilter}
          onFilterChange={setCapFilter}
          counts={serverCounts}
          resultCount={visibleServerEntries.length}
          totalCount={serverEntries.length}
          searchPlaceholder="Search servers by name…"
          testId="mcp-filter-bar"
        />
      )}
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
          <div style={{ color: 'var(--text-secondary)', fontSize: '14px', padding: '24px', textAlign: 'center', background: 'var(--bg-base)', borderRadius: '12px' }}>
            No MCP servers configured.
          </div>
        ) : visibleServerEntries.length === 0 ? (
          <EmptyState
            variant="no-results"
            icon={<Search size={26} color="var(--text-muted)" />}
            title="No matching servers"
            description="Nothing matches the current search and filters."
            actionLabel="Show all servers"
            onAction={clearCapabilityFilter}
            testId="mcp-no-results"
          />
        ) : (
          visibleServerEntries.map(({ item: { name, config } }) => (
            <CapabilityItem
              key={name}
              name={name}
              description={(
                <span style={{ fontFamily: 'var(--font-mono)' }}>
                  {config.url ? config.url : `${config.command || ''} ${config.args?.join(' ') || ''}`}
                </span>
              )}
              actions={(
                <>
                  <button
                    onClick={() => sendMessage('MCP_SERVER_TEST_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: { name } })}
                    style={{ background: 'rgba(var(--accent-rgb), 0.15)', color: 'var(--accent)', border: '1px solid rgba(var(--accent-rgb), 0.4)', padding: '6px 16px', borderRadius: '8px', cursor: 'pointer', fontSize: '13px' }}
                  >
                    Test
                  </button>
                  <button
                    onClick={() => sendMessage('MCP_SERVER_CONNECT_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: { name } })}
                    style={{ background: 'rgba(76, 195, 138, 0.15)', color: 'var(--success)', border: '1px solid rgba(76, 195, 138, 0.4)', padding: '6px 16px', borderRadius: '8px', cursor: 'pointer', fontSize: '13px' }}
                  >
                    Connect
                  </button>
                  <button
                    onClick={() => sendMessage('MCP_SERVER_DISCONNECT_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: { name } })}
                    style={{ background: 'rgba(var(--accent-rgb), 0.15)', color: 'var(--accent)', border: '1px solid rgba(var(--accent-rgb), 0.4)', padding: '6px 16px', borderRadius: '8px', cursor: 'pointer', fontSize: '13px' }}
                  >
                    Disconnect
                  </button>
                  <button
                    onClick={() => (editingName === name ? cancelEdit() : startEdit(name, config))}
                    style={{ background: 'rgba(var(--accent-rgb), 0.15)', color: 'var(--accent)', border: '1px solid rgba(var(--accent-rgb), 0.4)', padding: '6px 16px', borderRadius: '8px', cursor: 'pointer', fontSize: '13px' }}
                  >
                    {editingName === name ? 'Close edit' : 'Edit'}
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
                    style={{ background: 'rgba(201, 42, 42, 0.15)', color: 'var(--danger)', border: '1px solid rgba(201, 42, 42, 0.28)', padding: '6px 16px', borderRadius: '8px', cursor: 'pointer', fontSize: '13px' }}
                  >
                    Remove
                  </button>
                </>
              )}
            >
                <p style={{ fontSize: '13px', color: 'var(--text-muted)', marginTop: '4px' }}>
                  Status: {config.status || 'unknown'}{config.tool_count ? ` — ${config.tool_count} tool(s)` : ''}{config.error ? ` — ${config.error}` : ''}
                </p>
                {config.registry_name && (
                  <p style={{ fontSize: '13px', color: 'var(--text-muted)', marginTop: '4px' }}>
                    Imported from <span style={{ fontFamily: 'var(--font-mono)' }}>{config.registry_name}</span>
                    {config.version ? <> · version {config.version}</> : null}
                    {config.has_update
                      ? <span style={{ color: 'var(--accent)' }}> · update available: {config.registry_latest_version}</span>
                      : config.registry_latest_version
                        ? <> · up to date{config.registry_checked_at ? ` (checked ${String(config.registry_checked_at).slice(0, 10)})` : ''}</>
                        : null}
                    {' '}
                    <button
                      onClick={() => sendMessage('MCP_SERVER_UPDATE_CHECK_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: { name } })}
                      style={{ background: 'transparent', color: 'var(--accent)', border: '1px solid rgba(var(--accent-rgb), 0.4)', padding: '2px 10px', borderRadius: '8px', cursor: 'pointer', fontSize: '13px' }}
                    >
                      Check for updates
                    </button>
                  </p>
                )}
                {config.confirmed === false && (
                  <p style={{ fontSize: '13px', color: 'var(--accent)', marginTop: '4px' }}>
                    Awaiting confirmation — this server will not auto-connect at startup.{' '}
                    <button
                      onClick={() => setPendingMcpConfirm({ name, config })}
                      style={{ background: 'transparent', color: 'var(--accent)', border: '1px solid rgba(var(--accent-rgb), 0.4)', padding: '2px 10px', borderRadius: '8px', cursor: 'pointer', fontSize: '13px' }}
                    >
                      Review and confirm
                    </button>
                  </p>
                )}
                {editingName === name && editForm ? (
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '12px', marginTop: '12px' }}>
                    <input
                      type="text"
                      value={editForm.command}
                      onChange={(e) => setEditForm({ ...editForm, command: e.target.value })}
                      className="ios-glass-input"
                      placeholder="Command (e.g. python, npx)"
                      style={{ padding: '10px 14px', color: 'var(--text-primary)', outline: 'none' }}
                    />
                    <input
                      type="text"
                      value={editForm.args}
                      onChange={(e) => setEditForm({ ...editForm, args: e.target.value })}
                      className="ios-glass-input"
                      placeholder="Arguments (comma separated)"
                      style={{ padding: '10px 14px', color: 'var(--text-primary)', outline: 'none' }}
                    />
                    <input
                      type="text"
                      value={editForm.url}
                      onChange={(e) => setEditForm({ ...editForm, url: e.target.value })}
                      className="ios-glass-input"
                      placeholder="Server URL for remote (Streamable HTTP)"
                      style={{ padding: '10px 14px', color: 'var(--text-primary)', outline: 'none' }}
                    />
                    <PairRows
                      kind="header"
                      pairs={editForm.headerPairs}
                      onChange={(pairs) => setEditForm({ ...editForm, headerPairs: pairs })}
                    />
                    <PairRows
                      kind="env"
                      pairs={editForm.envPairs}
                      onChange={(pairs) => setEditForm({ ...editForm, envPairs: pairs })}
                    />
                    <div style={{ display: 'flex', gap: '12px' }}>
                      <input
                        type="text"
                        value={editForm.cwd}
                        onChange={(e) => setEditForm({ ...editForm, cwd: e.target.value })}
                        className="ios-glass-input"
                        placeholder="Working directory (cwd, local servers)"
                        style={{ flex: 1, padding: '10px 14px', color: 'var(--text-primary)', outline: 'none' }}
                      />
                      <input
                        type="text"
                        value={editForm.timeout}
                        onChange={(e) => setEditForm({ ...editForm, timeout: e.target.value })}
                        className="ios-glass-input"
                        placeholder="Timeout (seconds)"
                        style={{ width: '150px', padding: '10px 14px', color: 'var(--text-primary)', outline: 'none' }}
                      />
                    </div>
                    <p style={{ fontSize: '12px', color: 'var(--text-muted)', margin: 0 }}>
                      Header and env values are stored secrets and are never shown here: leave a value
                      blank to keep the stored one, remove the row to delete it. Changing the command,
                      URL, arguments or any value asks for confirmation first, naming what will run.
                    </p>
                    <div style={{ display: 'flex', gap: '8px' }}>
                      <button
                        onClick={cancelEdit}
                        style={{ background: 'transparent', color: 'var(--text-secondary)', border: '1px solid var(--border-default)', padding: '8px 18px', borderRadius: '10px', cursor: 'pointer', fontWeight: 500 }}
                      >
                        Cancel
                      </button>
                      <button
                        onClick={() => saveEdit(name, config)}
                        style={{ background: 'var(--accent)', color: 'var(--accent-on)', border: 'none', padding: '8px 18px', borderRadius: '10px', cursor: 'pointer', fontWeight: 500 }}
                      >
                        Save changes
                      </button>
                    </div>
                  </div>
                ) : (
                  <>
                <div style={{ display: 'flex', gap: '16px', marginTop: '8px' }}>
                  <label style={{ fontSize: '13px', color: 'var(--text-secondary)', cursor: 'pointer' }}>
                    <input
                      type="checkbox"
                      checked={config.auto_connect !== false}
                      onChange={(e) => sendMessage('MCP_SERVER_UPDATE_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: { name, auto_connect: e.target.checked } })}
                      style={{ marginRight: '6px', accentColor: 'var(--accent)' }}
                    />
                    Auto-connect on startup
                  </label>
                  <label style={{ fontSize: '13px', color: 'var(--text-secondary)', cursor: 'pointer' }}>
                    <input
                      type="checkbox"
                      checked={config.enabled !== false}
                      disabled={Boolean(pendingEnableChange(pendingMessages, ENABLE_TOGGLE_TYPES.mcpServer, 'name', name))}
                      onChange={(e) => safeSend('MCP_SERVER_UPDATE_REQUEST', { name, enabled: e.target.checked })}
                      style={{ marginRight: '6px', accentColor: 'var(--accent)' }}
                    />
                    Enabled
                  </label>
                </div>
                {pendingEnableChange(pendingMessages, ENABLE_TOGGLE_TYPES.mcpServer, 'name', name) && (
                  <div style={{ fontSize: '12px', color: 'var(--accent)', marginTop: '6px' }}>
                    {queuedEnableVerb(pendingEnableChange(pendingMessages, ENABLE_TOGGLE_TYPES.mcpServer, 'name', name))} queued — will apply when the engine reconnects
                  </div>
                )}
                {(mcpResources[name] || []).length > 0 && (
                  <div style={{ marginTop: '12px' }}>
                    <div style={{ fontSize: '13px', color: 'var(--text-secondary)', marginBottom: '6px' }}>Resources:</div>
                    {(mcpResources[name] || []).map((res) => (
                      <div key={res.uri} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '8px', padding: '6px 0' }}>
                        <span style={{ fontSize: '13px', color: 'var(--text-secondary)', fontFamily: 'var(--font-mono)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={res.description || res.uri}>
                          {res.name || res.uri}
                        </span>
                        <button
                          onClick={() => sendMessage('MCP_RESOURCE_READ_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: { name, uri: res.uri, session_id: activeSession } })}
                          style={{ background: 'rgba(var(--accent-rgb), 0.15)', color: 'var(--accent)', border: '1px solid rgba(var(--accent-rgb), 0.4)', padding: '4px 12px', borderRadius: '8px', cursor: 'pointer', fontSize: '12px', whiteSpace: 'nowrap' }}
                        >
                          Load into chat
                        </button>
                      </div>
                    ))}
                  </div>
                )}
                {(mcpPrompts[name] || []).length > 0 && (
                  <div style={{ marginTop: '12px' }}>
                    <div style={{ fontSize: '13px', color: 'var(--text-secondary)', marginBottom: '6px' }}>Prompts:</div>
                    {(mcpPrompts[name] || []).map((pr) => (
                      <div key={pr.name} style={{ padding: '6px 0' }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: '8px' }}>
                          <span style={{ fontSize: '13px', color: 'var(--text-secondary)', fontFamily: 'var(--font-mono)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={pr.description || pr.name}>
                            {pr.name}
                          </span>
                          <button
                            onClick={() => sendMessage('MCP_PROMPT_RUN_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: { name, prompt: pr.name, arguments: mcpPromptArgs[`${name}:${pr.name}`] || {}, session_id: activeSession } })}
                            style={{ background: 'rgba(var(--accent-rgb), 0.15)', color: 'var(--accent)', border: '1px solid rgba(var(--accent-rgb), 0.4)', padding: '4px 12px', borderRadius: '8px', cursor: 'pointer', fontSize: '12px', whiteSpace: 'nowrap' }}
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
                                style={{ flex: 1, minWidth: '120px', padding: '8px 12px', color: 'var(--text-primary)', outline: 'none', fontSize: '13px' }}
                              />
                            ))}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                )}
                <div style={{ marginTop: '12px' }}>
                  <button
                    onClick={() => setExpandedTools((prev) => ({ ...prev, [name]: !prev[name] }))}
                    style={{ background: 'transparent', color: 'var(--accent)', border: '1px solid rgba(var(--accent-rgb), 0.4)', padding: '4px 12px', borderRadius: '8px', cursor: 'pointer', fontSize: '13px' }}
                  >
                    {expandedTools[name]
                      ? 'Hide tools'
                      : `Show tools${Array.isArray(config.tools) ? ` (${config.tools.length})` : ''}`}
                  </button>
                  {expandedTools[name] && (
                    <div style={{ marginTop: '8px' }}>
                      {!Array.isArray(config.tools) ? (
                        <p style={{ fontSize: '13px', color: 'var(--text-muted)', margin: 0 }}>
                          {config.status === 'failed'
                            ? 'The server failed to connect, so its tools are unavailable.'
                            : config.enabled === false
                              ? 'This server is disabled — enable it and connect to see its tools.'
                              : config.status === 'invalid'
                                ? 'This server\u2019s configuration is invalid, so its tools are unavailable.'
                                : 'Connect this server to see the tools it offers.'}
                        </p>
                      ) : config.tools.length === 0 ? (
                        <p style={{ fontSize: '13px', color: 'var(--text-muted)', margin: 0 }}>
                          This server offers no tools.
                        </p>
                      ) : (
                        config.tools.map((tool) => {
                          const effectivePolicyValue =
                            tool.effective_policy === 'allow' ? 'Always Allow'
                            : tool.effective_policy === 'deny' ? 'Deny'
                            : 'Require Approval';
                          return (
                            <div key={tool.name} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: '12px', padding: '8px 0', borderTop: '1px solid var(--border-default)' }}>
                              <div style={{ minWidth: 0 }}>
                                <div style={{ fontSize: '13px', color: 'var(--text-primary)', fontFamily: 'var(--font-mono)' }}>
                                  {tool.tool_name}
                                  <span style={{ color: 'var(--text-muted)', fontFamily: 'inherit' }}> · risk: {tool.risk}</span>
                                </div>
                                {tool.description && (
                                  <div style={{ fontSize: '12px', color: 'var(--text-secondary)', marginTop: '2px' }}>{tool.description}</div>
                                )}
                                {tool.policy_is_default && (
                                  <div style={{ fontSize: '11px', color: 'var(--text-secondary)', marginTop: '2px' }}>Default policy — no override set. Pick one to override it.</div>
                                )}
                                {!tool.always_allow_permitted && (
                                  <div style={{ fontSize: '11px', color: 'var(--accent)', marginTop: '2px' }}>Always Allow is disabled for risky tools.</div>
                                )}
                                {tool.name_collision && (
                                  <div style={{ fontSize: '11px', color: 'var(--danger)', marginTop: '2px' }}>
                                    Another server offers a tool with the same combined name — this one is refused until the ambiguity is resolved.
                                  </div>
                                )}
                              </div>
                              <Dropdown
                                value={tool.policy_is_default ? effectivePolicyValue : tool.policy}
                                onChange={(val) => sendMessage('TOOL_POLICY_SET_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: { tool_name: tool.name, policy: val } })}
                                options={[
                                  { value: 'Always Allow', label: 'Always Allow', disabled: !tool.always_allow_permitted },
                                  { value: 'Require Approval', label: 'Require Approval' },
                                  { value: 'Deny', label: 'Deny' },
                                ]}
                                style={{ width: '200px' }}
                                align="right"
                              />
                            </div>
                          );
                        })
                      )}
                    </div>
                  )}
                </div>
                  </>
                )}
            </CapabilityItem>
          ))
        )}
      </div>
    </div>
  );
}
