import React from 'react';
import AgentsPanel from './AgentsPanel';
import McpPanel from './McpPanel';
import PluginsPanel from './PluginsPanel';

// Wave 5 (Annex C row 8): this file used to *be* the Agents, MCP
// and Plugins tabs — one component whose name said none of them.
// The tabs now live in their own panels (AgentsPanel, McpPanel,
// PluginsPanel, each with its own socket state and requests) and
// this file is only the dispatcher SettingsPanel renders for
// those three menus. The path and name stay because the Settings
// test suites import the component from here; the file itself
// contains no tab logic any more.
export default function DiagnosticsPanel({ activeMenu, activeSession = null }) {
  if (activeMenu === 'MCP') {
    return <McpPanel activeSession={activeSession} />;
  }
  if (activeMenu === 'Plugins') {
    return <PluginsPanel />;
  }
  if (activeMenu === 'Agents') {
    return <AgentsPanel />;
  }
  return null;
}
