import React, { useState, useEffect, useRef } from 'react';
import { Download, Search } from 'lucide-react';
import { useAetherSocket } from '../../contexts/SocketContext';
import PluginsNotice from './PluginsNotice';
import LoadStateRow from './LoadStateRow';
import EmptyState from '../EmptyState';
import CapabilityFilterBar from './CapabilityFilterBar';
import CapabilityItem from './CapabilityItem';
import { capabilityFilterCounts, filterCapabilityItems } from './capabilityFilter';
import PluginTrustCard from '../PluginTrustCard';

// Wave 5 (FF4-04 / PC-13): plain-language summary of what an
// update check found changed between the installed record and a
// fresh prepare of the install source. "Grew" means the plugin
// would be able to do MORE than what was approved — the row says
// so, and the install itself re-asks for approval (existing
// semantics), so nothing here auto-updates anything.
function describeUpdateChanges(check) {
  const parts = [];
  const list = (label, names) => {
    if (Array.isArray(names) && names.length > 0) parts.push(`${label}: ${names.join(', ')}`);
  };
  list('adds skill(s)', check.added_skills);
  list('adds MCP server(s)', check.added_mcp_servers);
  list('changes what MCP server(s) run', check.changed_mcp_servers);
  list('changes skill(s)', check.changed_skills);
  list('removes skill(s)', check.removed_skills);
  list('removes MCP server(s)', check.removed_mcp_servers);
  return parts.join('; ');
}

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
  // D4-5 (FF4-07): the engine's registry load error, carried on
  // the list reply. A corrupt registry file loads as an empty
  // registry — without this the panel claimed "No plugins
  // installed." for a registry it never managed to read.
  const [registryLoadError, setRegistryLoadError] = useState(null);
  const [pluginActionResult, setPluginActionResult] = useState(null);
  const [newPluginSource, setNewPluginSource] = useState('');
  // Wave 5 (D4-4): the in-panel approval. An install started from
  // THIS panel mints its own request id (kept in a ref) and claims
  // the approval the engine sends back for that id, rendering the
  // shared trust card here. Installs started anywhere else (the
  // chat approval card) are left alone — the panel only claims
  // what it started, and the chat card's semantics do not change.
  const [pendingApproval, setPendingApproval] = useState(null);
  const pendingInstallIdRef = useRef(null);
  const installSeqRef = useRef(0);
  // Wave 5 (FF4-04): per-plugin update-check results, keyed by
  // plugin name — the row renders from these and the filter's
  // Updates chip reads hasUpdate from the same data.
  const [updateChecks, setUpdateChecks] = useState({});
  // D4-1: the installed list runs through the shared capability
  // pattern (CapabilityFilterBar + capabilityFilter + CapabilityItem
  // rows) — same search and chips as the other capability panels.
  const [capQuery, setCapQuery] = useState('');
  const [capFilter, setCapFilter] = useState('all');
  const pluginEntries = plugins.map((plugin) => {
    const name = plugin.manifest?.name || plugin.name;
    return {
      item: plugin,
      name,
      // D4-5 remainder: a plugin whose stored record fails to
      // load carries the invalid-entry shape on the list reply
      // and renders as an error row, not an enabled/disabled one.
      state: plugin.state === 'load_error' ? 'error' : (plugin.enabled ? 'enabled' : 'disabled'),
      hasUpdate: Boolean(updateChecks[name]?.has_update),
    };
  });
  const pluginCounts = capabilityFilterCounts(pluginEntries);
  const visiblePluginEntries = filterCapabilityItems(pluginEntries, capQuery, capFilter);
  const clearCapabilityFilter = () => { setCapQuery(''); setCapFilter('all'); };

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
          setRegistryLoadError(data.payload.registry_load_error || null);
          setPluginsLoad('ready');
        } else if (data.type === 'PLUGIN_APPROVAL_REQUEST') {
          // D4-4: claim only the approval this panel started (the
          // request id it minted for its own install). Approvals
          // for installs started elsewhere stay with the chat
          // card, untouched.
          if (pendingInstallIdRef.current && data.request_id === pendingInstallIdRef.current) {
            setPendingApproval({
              requestId: data.request_id,
              pluginName: data.payload.plugin_name,
              details: data.payload.plugin_details,
            });
          }
        } else if (data.type === 'PLUGIN_UPDATE_CHECK_RESPONSE') {
          if (data.payload?.name) {
            setUpdateChecks((prev) => ({ ...prev, [data.payload.name]: data.payload }));
          }
        } else if (data.type === 'PLUGIN_INSTALL_RESPONSE' || data.type === 'PLUGIN_UNINSTALL_RESPONSE' || data.type === 'PLUGIN_TOGGLE_RESPONSE') {
          if (data.type === 'PLUGIN_INSTALL_RESPONSE') {
            // The install this panel was tracking has resolved —
            // the approval card (if still open) is done either way.
            setPendingApproval(null);
            pendingInstallIdRef.current = null;
          }
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

  // The uninstall button is shared by the normal row and the
  // load-error row (a broken plugin can still be removed).
  const uninstallButtonFor = (name) => (
    <button
      onClick={() => {
        // D4-4 confirm half (FF4-10): uninstalling deletes the
        // plugin's files and removes the MCP servers and skills
        // it installed — confirm first, naming what will be
        // removed, with the same window.confirm pattern as the
        // other Settings deletes (D2-28). Cancelling sends
        // nothing. This was the destructive action that fired on
        // click.
        if (!window.confirm(`Uninstall the plugin "${name}"? Its files will be deleted, along with any MCP servers and skills it installed.`)) return;
        sendMessage('PLUGIN_UNINSTALL_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: { name } });
      }}
      style={{ background: 'rgba(201, 42, 42, 0.15)', color: 'var(--danger)', border: '1px solid rgba(201, 42, 42, 0.28)', padding: '6px 16px', borderRadius: '8px', cursor: 'pointer', fontSize: '13px' }}
    >
      Uninstall
    </button>
  );

  // D4-4: the in-panel decision travels under the approval
  // request's own id — the same envelope the chat card sends, so
  // the engine cannot tell (and does not need to) which surface
  // answered.
  const answerApproval = (approved) => {
    if (!pendingApproval) return;
    sendMessage(approved ? 'PLUGIN_APPROVAL_GRANTED' : 'PLUGIN_APPROVAL_REJECTED', {
      schema_version: 1,
      request_id: pendingApproval.requestId,
      payload: {},
    });
    setPendingApproval(null);
    pendingInstallIdRef.current = null;
  };

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
                // D4-4: mint an id this panel can recognise when
                // the approval comes back, so the trust card can
                // render here. Starting a new install supersedes
                // the tracked one (the engine keeps each pending
                // install under its own id either way).
                installSeqRef.current += 1;
                const requestId = `settings-install-${Date.now()}-${installSeqRef.current}`;
                pendingInstallIdRef.current = requestId;
                setPendingApproval(null);
                sendMessage('PLUGIN_INSTALL_REQUEST', { schema_version: 1, request_id: requestId, payload: { source: newPluginSource } });
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

      {pendingApproval && (
        <div
          data-testid="plugin-install-approval"
          style={{ background: 'var(--bg-raised)', border: '1px solid rgba(var(--accent-rgb), 0.35)', borderRadius: 'var(--radius-lg)', padding: '20px', marginBottom: '24px' }}
        >
          <h3 style={{ fontSize: '16px', marginBottom: '4px' }}>Install {pendingApproval.pluginName}?</h3>
          <p style={{ color: 'var(--text-muted)', fontSize: '13px', margin: '0 0 12px 0' }}>
            Review what this plugin adds before it is installed. Nothing has been written yet.
          </p>
          <PluginTrustCard details={pendingApproval.details} />
          <div style={{ display: 'flex', gap: '12px', justifyContent: 'flex-end', marginTop: '16px' }}>
            <button
              onClick={() => answerApproval(false)}
              style={{ background: 'var(--bg-hover)', border: '1px solid var(--border-strong)', padding: '8px 16px', borderRadius: '8px', cursor: 'pointer', fontSize: '13px', fontWeight: 500, color: 'var(--text-primary)' }}
            >
              Reject
            </button>
            <button
              onClick={() => answerApproval(true)}
              style={{ background: 'var(--accent)', border: '1px solid var(--accent)', color: 'var(--accent-on)', padding: '8px 16px', borderRadius: '8px', cursor: 'pointer', fontSize: '13px', fontWeight: 500 }}
            >
              Approve install
            </button>
          </div>
        </div>
      )}

      {pluginActionResult && (
        <div style={{ color: 'var(--accent)', fontSize: '13px', marginBottom: '16px' }}>{pluginActionResult}</div>
      )}

      <h3 style={{ fontSize: '16px', marginBottom: '16px' }}>Installed Plugins</h3>
      {plugins.length > 0 && (
        <CapabilityFilterBar
          query={capQuery}
          onQueryChange={setCapQuery}
          filter={capFilter}
          onFilterChange={setCapFilter}
          counts={pluginCounts}
          resultCount={visiblePluginEntries.length}
          totalCount={pluginEntries.length}
          searchPlaceholder="Search plugins by name…"
          testId="plugins-filter-bar"
        />
      )}
      <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
        {registryLoadError && (
          <LoadStateRow
            loadState="error"
            testId="plugins-registry-error"
            errorText={registryLoadError}
          />
        )}
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
          // A registry that failed to load is not an empty
          // registry: the error row above is the state, and the
          // "No plugins installed." claim stays suppressed.
          registryLoadError ? null : (
            <div style={{ color: 'var(--text-secondary)', fontSize: '14px', padding: '24px', textAlign: 'center', background: 'var(--bg-base)', borderRadius: '12px' }}>
              No plugins installed.
            </div>
          )
        ) : visiblePluginEntries.length === 0 ? (
          <EmptyState
            variant="no-results"
            icon={<Search size={26} color="var(--text-muted)" />}
            title="No matching plugins"
            description="Nothing matches the current search and filters."
            actionLabel="Show all plugins"
            onAction={clearCapabilityFilter}
            testId="plugins-no-results"
          />
        ) : (
          visiblePluginEntries.map(({ item: plugin }) => {
            const name = plugin.manifest?.name || plugin.name;
            const versionBadge = (
              <span style={{ fontSize: '11px', background: 'var(--bg-hover)', padding: '2px 6px', borderRadius: '4px' }}>v{plugin.manifest?.version || '1.0'}</span>
            );
            if (plugin.state === 'load_error') {
              // D4-5 remainder: the per-plugin load-error row. The
              // record could not be turned back into a manifest,
              // so there is nothing to toggle — the error and the
              // way out (uninstall) are the row.
              return (
                <CapabilityItem
                  key={name}
                  name={name}
                  tone="error"
                  badges={(
                    <>
                      {versionBadge}
                      <span style={{ fontSize: '11px', background: 'rgba(201, 42, 42, 0.15)', color: 'var(--danger)', padding: '2px 6px', borderRadius: '4px' }}>Load error</span>
                    </>
                  )}
                  description={plugin.manifest?.description || 'This plugin could not be loaded.'}
                  actions={uninstallButtonFor(name)}
                >
                  <p style={{ color: 'var(--danger)', margin: 0 }}>{plugin.error}</p>
                  <p style={{ color: 'var(--text-muted)', margin: '4px 0 0 0' }}>
                    None of this plugin's skills or MCP servers are active.
                  </p>
                </CapabilityItem>
              );
            }
            const update = updateChecks[name];
            const changes = update?.checked ? describeUpdateChanges(update) : '';
            return (
              <CapabilityItem
                key={name}
                name={name}
                badges={(
                  <>
                    {versionBadge}
                    {update?.has_update && (
                      <span style={{ fontSize: '11px', background: 'rgba(var(--accent-rgb), 0.15)', color: 'var(--accent)', padding: '2px 6px', borderRadius: '4px' }}>Update available</span>
                    )}
                  </>
                )}
                description={plugin.manifest?.description || 'No description available.'}
                actions={(
                  <>
                    <button
                      onClick={() => sendMessage('PLUGIN_TOGGLE_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: { name, enabled: !plugin.enabled } })}
                      style={{ background: plugin.enabled ? 'rgba(76, 195, 138, 0.15)' : 'rgba(255, 255, 255, 0.1)', color: plugin.enabled ? 'var(--success)' : 'var(--text-primary)', border: `1px solid ${plugin.enabled ? 'rgba(76, 195, 138, 0.3)' : 'rgba(255, 255, 255, 0.2)'}`, padding: '6px 16px', borderRadius: '8px', cursor: 'pointer', fontSize: '13px', width: '80px' }}
                    >
                      {plugin.enabled ? 'Enabled' : 'Disabled'}
                    </button>
                    <button
                      onClick={() => sendMessage('PLUGIN_UPDATE_CHECK_REQUEST', { schema_version: 1, request_id: Date.now().toString(), payload: { name } })}
                      style={{ background: 'var(--bg-hover)', border: '1px solid var(--border-strong)', padding: '6px 16px', borderRadius: '8px', cursor: 'pointer', fontSize: '13px', color: 'var(--text-primary)' }}
                    >
                      Check for updates
                    </button>
                    {uninstallButtonFor(name)}
                  </>
                )}
              >
                {update && (
                  <div data-testid={`plugin-update-${name}`}>
                    {update.error && (
                      <p style={{ color: 'var(--danger)', margin: 0 }}>{update.error}</p>
                    )}
                    {update.checked && !update.has_update && (
                      <p style={{ color: 'var(--text-muted)', margin: 0 }}>
                        Up to date — checked against the source it was installed from.
                      </p>
                    )}
                    {update.has_update && (
                      <>
                        <p style={{ margin: 0 }}>
                          Version {update.latest_version} is available (you have {update.installed_version}).
                          {changes ? ` Changes — ${changes}.` : ''}
                        </p>
                        {update.permissions_grew && (
                          <p style={{ color: 'var(--danger)', margin: '4px 0 0 0' }}>
                            This update can do more than the version you approved. Installing it will ask for your approval again before anything changes.
                          </p>
                        )}
                        {plugin.source && (
                          <button
                            onClick={() => setNewPluginSource(plugin.source)}
                            style={{ background: 'var(--bg-hover)', border: '1px solid var(--border-strong)', padding: '6px 12px', borderRadius: '8px', cursor: 'pointer', fontSize: '12px', color: 'var(--text-primary)', marginTop: '8px' }}
                          >
                            Review update
                          </button>
                        )}
                      </>
                    )}
                  </div>
                )}
              </CapabilityItem>
            );
          })
        )}
      </div>
    </div>
  );
}
