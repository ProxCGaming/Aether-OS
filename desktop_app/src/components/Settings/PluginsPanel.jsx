import React, { useState, useEffect } from 'react';
import { Download } from 'lucide-react';
import { useAetherSocket } from '../../contexts/SocketContext';
import PluginsNotice from './PluginsNotice';
import LoadStateRow from './LoadStateRow';

// Wave 5 (Annex C row 8): the Plugins tab, extracted from
// DiagnosticsPanel.jsx — that file owned the Agents, MCP and
// Plugins tabs in one component; each tab is now its own panel and
// DiagnosticsPanel is only the dispatcher. Behaviour is unchanged:
// same request on mount, same response handling, same rendering.
export default function PluginsPanel() {
  const { wsRef, sendMessage, isConnected } = useAetherSocket();
  // Wave 3 slice 1 (RUX-C-06, under the RUX-C-01 umbrella): the
  // plugin list tracks whether its engine answer has arrived.
  // Until then the section renders a load state (loading / offline
  // / error) — the old code rendered "No plugins installed." from
  // the first frame, so not-yet-loaded, genuinely-empty, failed
  // and offline were indistinguishable.
  const [pluginsLoad, setPluginsLoad] = useState('loading');
  const [plugins, setPlugins] = useState([]);
  const [pluginActionResult, setPluginActionResult] = useState(null);
  const [newPluginSource, setNewPluginSource] = useState('');

  useEffect(() => {
    sendMessage('PLUGIN_LIST_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: {} });
  }, [sendMessage]);

  useEffect(() => {
    const handleMessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'PLUGIN_LIST_RESPONSE') {
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
            // idiom) — a refused uninstall previously left the
            // plugin installed with no sign anything went wrong.
            const verb = data.type === 'PLUGIN_UNINSTALL_RESPONSE' ? 'Uninstall'
              : data.type === 'PLUGIN_TOGGLE_RESPONSE' ? 'Toggle' : 'Install';
            setPluginActionResult(`${verb}${data.payload.name ? ` ${data.payload.name}` : ''} failed: ${data.payload.error}`);
          }
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
      <h2 style={{ fontSize: '24px', marginBottom: '8px', fontWeight: 500 }}>Plugins</h2>
      <p style={{ color: 'var(--text-muted)', fontSize: '14px', marginBottom: '24px' }}>Manage active plugins and extensions for Aether-OS.</p>

      <PluginsNotice />

      <div style={{ background: 'var(--bg-raised)', border: '1px solid var(--border-default)', borderRadius: 'var(--radius-lg)', padding: '20px', marginBottom: '24px' }}>
        <h3 style={{ fontSize: '16px', marginBottom: '12px' }}>Install New Plugin</h3>
        <div style={{ display: 'flex', gap: '12px' }}>
          <input
            type="text"
            value={newPluginSource}
            onChange={(e) => setNewPluginSource(e.target.value)}
            className="ios-glass-input"
            placeholder="GitHub URL or local path"
            style={{ flex: 1, padding: '12px 16px', color: 'var(--text-primary)', outline: 'none' }}
          />
          <button
            onClick={() => {
              if (newPluginSource) {
                sendMessage('PLUGIN_INSTALL_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: { source: newPluginSource } });
                setNewPluginSource('');
              }
            }}
            style={{ background: 'var(--accent)', color: 'var(--accent-on)', border: 'none', padding: '0 24px', borderRadius: '12px', cursor: 'pointer', display: 'flex', alignItems: 'center', gap: '8px', fontWeight: 500 }}
          >
            <Download size={16} />
            Install
          </button>
        </div>
      </div>

      {pluginActionResult && (
        <div style={{ color: 'var(--accent)', fontSize: '13px', marginBottom: '16px' }}>{pluginActionResult}</div>
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
          <div style={{ color: 'var(--text-secondary)', fontSize: '14px', padding: '24px', textAlign: 'center', background: 'var(--bg-base)', borderRadius: '12px' }}>
            No plugins installed.
          </div>
        ) : (
          plugins.map(plugin => (
            <div key={plugin.manifest?.name || plugin.name} style={{ background: 'var(--bg-raised)', border: '1px solid var(--border-default)', borderRadius: '12px', padding: '16px', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <div>
                <h4 style={{ fontSize: '15px', fontWeight: 500, display: 'flex', alignItems: 'center', gap: '8px' }}>
                  {plugin.manifest?.name || plugin.name}
                  <span style={{ fontSize: '11px', background: 'var(--bg-hover)', padding: '2px 6px', borderRadius: '4px' }}>v{plugin.manifest?.version || '1.0'}</span>
                </h4>
                <p style={{ fontSize: '13px', color: 'var(--text-muted)', marginTop: '4px' }}>
                  {plugin.manifest?.description || 'No description available.'}
                </p>
              </div>
              <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
                <button
                  onClick={() => sendMessage('PLUGIN_TOGGLE_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: { name: plugin.manifest?.name || plugin.name, enabled: !plugin.enabled } })}
                  style={{ background: plugin.enabled ? 'rgba(76, 195, 138, 0.15)' : 'rgba(255, 255, 255, 0.1)', color: plugin.enabled ? 'var(--success)' : 'var(--text-primary)', border: `1px solid ${plugin.enabled ? 'rgba(76, 195, 138, 0.3)' : 'rgba(255, 255, 255, 0.2)'}`, padding: '6px 16px', borderRadius: '8px', cursor: 'pointer', fontSize: '13px', width: '80px' }}
                >
                  {plugin.enabled ? 'Enabled' : 'Disabled'}
                </button>
                <button
                  onClick={() => sendMessage('PLUGIN_UNINSTALL_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: { name: plugin.manifest?.name || plugin.name } })}
                  style={{ background: 'rgba(201, 42, 42, 0.15)', color: 'var(--danger)', border: '1px solid rgba(201, 42, 42, 0.28)', padding: '6px 16px', borderRadius: '8px', cursor: 'pointer', fontSize: '13px' }}
                >
                  Uninstall
                </button>
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  );
}
