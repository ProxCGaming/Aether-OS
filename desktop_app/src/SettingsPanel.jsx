import React, { useState, useEffect } from 'react';
import { User, Key, Cpu, Settings as SettingsIcon, Activity, Download, Network, Box, Layers, Plug, Database, Sliders, CheckCircle2, Wrench, Server, Trash2, Lock, Scissors, Globe, CircleDollarSign, Mic, Info } from 'lucide-react';
import Dropdown from './Dropdown';
// E6-F14: the About section's version is read from the desktop
// package manifest — one of the three mirrors of the engine's
// APP_VERSION (aether_engine/version.py), pinned equal to it by the
// epic-10 hardening tests. Never a hardcoded copy in this file.
import appPackage from '../package.json';
import { resolveSnipHotkey, defaultSnipHotkey } from './snipHotkey';
import LocalModelsTab from './LocalModelsTab';
import './chat.css';
import ProviderSettings from './components/Settings/ProviderSettings';
import MemoryManagement from './components/Settings/MemoryManagement';
import DiagnosticsPanel from './components/Settings/DiagnosticsPanel';
import WorkspaceIndexingPanel from './components/Settings/WorkspaceIndexingPanel';
import BrowserPolicyPanel from './components/Settings/BrowserPolicyPanel';
import UsageSettingsPanel from './components/Settings/UsageSettingsPanel';
import VaultPanel from './components/Settings/VaultPanel';
import OrchestrationPanel from './components/Settings/OrchestrationPanel';
import PathRulesPanel from './components/Settings/PathRulesPanel';
import ProjectConfigPanel from './components/Settings/ProjectConfigPanel';

export default function SettingsPanel({ wsRef, providers = [], providerHealth = {}, localModels = [], activeSession = null, dailyBudget = null, budgetAlert = null, initialMenu = null, onInitialMenuConsumed = null }) {
  const [activeMenu, setActiveMenu] = useState('Providers');
  const [modelTab, setModelTab] = useState('cloud'); // 'cloud' or 'local'
  const [keys, setKeys] = useState({});
  const [baseUrls, setBaseUrls] = useState({});
  const [expandedProvider, setExpandedProvider] = useState(null);
  const [validationState, setValidationState] = useState({}); // { provider: 'testing' | 'saving' | 'success' | 'error' }
  const [validationMsg, setValidationMsg] = useState({});
  const [customDisplayNames, setCustomDisplayNames] = useState({}); // for custom_openai
  
  const [tools, setTools] = useState([]);
  // Epic 2: approval mode ('ask' | 'write', from TOOL_LIST_RESPONSE) and
  // the remembered scoped-approval grants (E2-09 approval cache).
  const [approvalMode, setApprovalMode] = useState('ask');
  // Epic 6, E6-06: settings search + reset-to-defaults state.
  const [settingsQuery, setSettingsQuery] = useState('');
  const [resetArmed, setResetArmed] = useState(false);
  const [resetDone, setResetDone] = useState(false);
  // P-1 (E6-F07 flag): the engine's refusal of a reset (e.g. the
  // save failed) — surfaced instead of a false "done".
  const [resetError, setResetError] = useState('');
  // P-2 (E7-F05 follow-up): plugin action failures land here so they
  // are visible in this panel, as they already are in
  // DiagnosticsPanel (which renders the plugin UI).
  const [pluginActionResult, setPluginActionResult] = useState(null);
  // E6-F14: workspace roots for the About section's local-guide
  // entry point (from WORKSPACE_LIST_RESPONSE).
  const [aboutWorkspaces, setAboutWorkspaces] = useState([]);
  const [guideStatus, setGuideStatus] = useState('');
  // Epic 10: backup / restore + update check (Advanced tab).
  const [backupStatus, setBackupStatus] = useState('');
  const [includeSecrets, setIncludeSecrets] = useState(false);
  const [restorePath, setRestorePath] = useState('');
  const [restoreStatus, setRestoreStatus] = useState('');
  const [versionCheck, setVersionCheck] = useState('');
  const [approvalGrants, setApprovalGrants] = useState([]);
  const [mcpServers, setMcpServers] = useState({});
  const [newMcpServer, setNewMcpServer] = useState({ name: '', command: '', args: '' });

  const [plugins, setPlugins] = useState([]);
  const [newPluginSource, setNewPluginSource] = useState('');
  
  const [skills, setSkills] = useState([]);
  
  // Advanced Config State
  const [disableFallbacks, setDisableFallbacks] = useState(false);
  const [fallbackChain, setFallbackChain] = useState('');
  // E6-F05: the last applied engine config — the controlled panels
  // for the Epic 1/2 handed-off surfaces (OrchestrationPanel,
  // PathRulesPanel) render from it — plus the most recent strict
  // CONFIG_UPDATE refusal to show in them, and the project config
  // layer state for the Advanced editor.
  const [engineConfig, setEngineConfig] = useState(null);
  const [configUpdateError, setConfigUpdateError] = useState('');
  const [projectLayer, setProjectLayer] = useState({
    workspaceRoot: '', content: '', exists: false,
    forbiddenKeys: [], warning: '', status: '',
  });
  // Open Point 13 (DECIDED): snip hotkey defaults to Ctrl+S
  // (Cmd+S on macOS) and is user-changeable; resolution rules
  // live in ./snipHotkey (mirror of electron/snipShortcut.cjs).
  const [snipHotkey, setSnipHotkey] = useState(() => defaultSnipHotkey());
  const [snipHotkeyError, setSnipHotkeyError] = useState('');

  // Apply a snip hotkey through the main process first; only a
  // registration the main process confirms is persisted to the
  // engine config. On failure the previous working hotkey stays
  // active, the input reverts to it, and the failure is shown —
  // a hotkey that does not work is never saved.
  const applySnipHotkey = async (requested) => {
    const hotkey = (requested || '').trim() || defaultSnipHotkey();
    let actual = hotkey;
    let ok = true;
    if (window.electronAPI?.registerSnipShortcut) {
      try {
        const result = await window.electronAPI.registerSnipShortcut(hotkey);
        if (typeof result === 'string') {
          actual = result;
          ok = result === hotkey;
        } else if (result && typeof result === 'object') {
          actual = result.shortcut || hotkey;
          ok = result.ok !== false && actual === hotkey;
        }
      } catch (e) {
        ok = false;
        actual = snipHotkey;
      }
    }
    if (!ok) {
      setSnipHotkey(actual);
      setSnipHotkeyError(`Could not set "${hotkey}" — it may be invalid or already used by another app. "${actual}" is still your snip hotkey.`);
      return false;
    }
    setSnipHotkey(hotkey);
    setSnipHotkeyError('');
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'CONFIG_UPDATE_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: { config: { snip_hotkey: hotkey } }
      }));
    }
    return true;
  };
  // Voice Tuning State (E8-5.2)
  const [vadThreshold, setVadThreshold] = useState(0.5);
  const [silenceDurationMs, setSilenceDurationMs] = useState(700);
  
  // A5: State for Reasoning Effort and Node model override
  const [reasoningEffort, setReasoningEffort] = useState(() => localStorage.getItem('reasoning_effort') || 'standard');
  const [agents, setAgents] = useState([]);
  // Phase 9, B1: per-task auxiliary model overrides, task -> "provider:::model" (absent = auto)
  const [auxModels, setAuxModels] = useState({});

  const [memoryTab, setMemoryTab] = useState('episodic');
  const [episodes, setEpisodes] = useState([]);
  const [facts, setFacts] = useState([]);
  const [selectedEpisode, setSelectedEpisode] = useState(null);
  
  const [capabilityHistory, setCapabilityHistory] = useState([]);
  const [capabilityStatus, setCapabilityStatus] = useState(null);
  const [isDiagnosticsRunning, setIsDiagnosticsRunning] = useState(false);

  const handleDeleteEpisode = (taskId) => {
    if (!taskId) return;
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'MEMORY_EPISODE_DELETE_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: { task_id: taskId }
      }));
    }
    setEpisodes(prev => prev.filter(ep => ep.task_id !== taskId));
    if (selectedEpisode?.task_id === taskId) {
      setSelectedEpisode(null);
    }
  };

  const handleClearAllEpisodes = () => {
    if (window.confirm("Are you sure you want to clear all stored episodic memories?")) {
      if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify({
          type: 'MEMORY_EPISODES_CLEAR_REQUEST',
          schema_version: 1,
          request_id: Date.now().toString(),
          payload: {}
        }));
      }
      setEpisodes([]);
      setSelectedEpisode(null);
    }
  };

  const handleDeleteFact = (factId) => {
    if (!factId) return;
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'MEMORY_FACT_DELETE_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: { fact_id: factId }
      }));
    }
    setFacts(prev => prev.filter(f => f.fact_id !== factId));
  };

  const handleDeleteEntity = (entityName) => {
    if (!entityName) return;
    if (window.confirm(`Delete all facts associated with "${entityName}"?`)) {
      if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify({
          type: 'MEMORY_ENTITY_DELETE_REQUEST',
          schema_version: 1,
          request_id: Date.now().toString(),
          payload: { entity_name: entityName }
        }));
      }
      setFacts(prev => prev.filter(f => f.entity_name !== entityName));
    }
  };

  // E6-F05: the handed-off panels report edits as partial config
  // objects; they go out through the one CONFIG_UPDATE flow.
  //
  // E6-F15: settings edits are sent through the socket facade
  // UNCONDITIONALLY. The facade's send() queues while the engine is
  // unreachable and flushes in order on reconnect (the discipline
  // E6-F02 established for onboarding) — the readyState guards these
  // handlers used to carry threw the edit away whenever the socket
  // was mid-reconnect, so a change made offline silently vanished
  // when the next authoritative config reply landed.
  const sendConfigUpdate = (partial) => {
    if (wsRef.current) {
      wsRef.current.send(JSON.stringify({
        type: 'CONFIG_UPDATE_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: { config: partial }
      }));
    }
  };

  const handleProjectLayerSave = (content) => {
    if (wsRef.current) {
      wsRef.current.send(JSON.stringify({
        type: 'PROJECT_CONFIG_UPDATE_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: { workspace_root: projectLayer.workspaceRoot, content }
      }));
    }
  };

  const handleApprovalModeChange = (mode) => {
    // Optimistic: the selector reflects the choice immediately; the
    // APPROVAL_MODE_SET_RESPONSE confirms (or the next TOOL_LIST_RESPONSE
    // corrects) it. E6-F15: sent unconditionally (facade queues it
    // while offline) like every other settings edit.
    setApprovalMode(mode);
    if (wsRef.current) {
      wsRef.current.send(JSON.stringify({
        type: 'APPROVAL_MODE_SET_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: { mode }
      }));
    }
  };

  const handleRevokeGrant = (id) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'APPROVAL_GRANT_REVOKE_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: { id }
      }));
    }
  };

  const handleClearAllFacts = () => {
    if (window.confirm("Are you sure you want to clear all facts from the Knowledge Graph?")) {
      if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify({
          type: 'MEMORY_GRAPH_CLEAR_REQUEST',
          schema_version: 1,
          request_id: Date.now().toString(),
          payload: {}
        }));
      }
      setFacts([]);
    }
  };

  useEffect(() => {
    if (!wsRef.current) return;

    // Apply a full config payload (CONFIG_GET / CONFIG_UPDATE /
    // CONFIG_RESET replies all carry one) to the live controls.
    const applyConfigValues = (config) => {
      // E6-F05: the handed-off panels are controlled from the same
      // applied payload, and a successful apply clears any refusal.
      setEngineConfig(config);
      setConfigUpdateError('');
      setDisableFallbacks(config.disable_fallbacks || false);
      setFallbackChain((config.fallback_chain || []).join(', '));
      if (config.snip_hotkey) {
        const resolvedSnip = resolveSnipHotkey(config.snip_hotkey);
        setSnipHotkey(resolvedSnip.accelerator);
        if (resolvedSnip.storedInvalid) {
          setSnipHotkeyError(`Your saved snip hotkey "${config.snip_hotkey}" is not a valid shortcut, so the default (${resolvedSnip.accelerator}) is being used.`);
        }
      }
      if (config.vad_threshold !== undefined && config.vad_threshold !== null) {
        setVadThreshold(config.vad_threshold);
      }
      if (config.silence_duration_ms !== undefined && config.silence_duration_ms !== null) {
        setSilenceDurationMs(config.silence_duration_ms);
      }
      // Phase 9, B1/B2: engine-persisted auxiliary models and reasoning effort.
      setAuxModels(config.auxiliary_models || {});
      const serverEffort = config.reasoning_effort;
      if (serverEffort) {
        setReasoningEffort(serverEffort);
        localStorage.setItem('reasoning_effort', serverEffort);
      }
    };

    const handleMessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (data.type === 'PROVIDER_VALIDATE_RESPONSE' || data.type === 'SETTINGS_PROVIDER_VALIDATE_RESULT') {
          const provider = data.payload.provider;
          if (data.payload.success) {
            setValidationState(prev => ({ ...prev, [provider]: 'success' }));
            setValidationMsg(prev => ({ ...prev, [provider]: 'Test Successful' }));
            // Auto-clear success message after 3 seconds
            setTimeout(() => setValidationState(prev => ({ ...prev, [provider]: null })), 3000);
          } else {
            setValidationState(prev => ({ ...prev, [provider]: 'error' }));
            setValidationMsg(prev => ({ ...prev, [provider]: data.payload.error || data.payload.message || 'Validation failed' }));
          }
        } else if (data.type === 'PROVIDER_SAVE_RESPONSE') {
          const provider = data.payload.provider;
          const isCustomSave = provider.startsWith('custom_');
          if (data.payload.success) {
            setValidationState(prev => ({ ...prev, [provider]: 'success', ...(isCustomSave ? { custom_openai: 'success' } : {}) }));
            setValidationMsg(prev => ({ ...prev, [provider]: 'Saved!', ...(isCustomSave ? { custom_openai: 'Saved!' } : {}) }));
            setTimeout(() => setValidationState(prev => ({ ...prev, [provider]: null, ...(isCustomSave ? { custom_openai: null } : {}) })), 3000);
          } else {
            setValidationState(prev => ({ ...prev, [provider]: 'error', ...(isCustomSave ? { custom_openai: 'error' } : {}) }));
            setValidationMsg(prev => ({ ...prev, [provider]: data.payload.error || 'Failed to save', ...(isCustomSave ? { custom_openai: data.payload.error || 'Failed to save' } : {}) }));
          }
        } else if (data.type === 'TOOL_LIST_RESPONSE') {
          setTools(data.payload.tools || []);
          if (data.payload.approval_mode) {
            setApprovalMode(data.payload.approval_mode);
          }
        } else if (data.type === 'APPROVAL_MODE_SET_RESPONSE') {
          if (data.payload.success && data.payload.mode) {
            setApprovalMode(data.payload.mode);
          }
        } else if (data.type === 'APPROVAL_GRANTS_RESPONSE') {
          setApprovalGrants(data.payload.grants || []);
        } else if (data.type === 'APPROVAL_GRANT_REVOKE_RESPONSE') {
          // The revoke response carries the refreshed grants list.
          if (Array.isArray(data.payload.grants)) {
            setApprovalGrants(data.payload.grants);
          }
        } else if (data.type === 'TOOL_POLICY_SET_RESPONSE') {
          if (data.payload.success) {
            setTools(prev => prev.map(t => t.function.name === data.payload.tool_name ? { ...t, policy: data.payload.policy } : t));
          }
        } else if (data.type === 'AGENT_LIST_RESPONSE') {
          setAgents(data.payload.agents || []);
        } else if (data.type === 'AGENT_UPDATE_RESPONSE') {
          // Can optionally show a toast here
        } else if (data.type === 'MCP_SERVER_LIST_RESPONSE') {
          setMcpServers(data.payload.servers || {});
        } else if (data.type === 'MCP_SERVER_ADD_RESPONSE' || data.type === 'MCP_SERVER_REMOVE_RESPONSE') {
          if (data.payload.success) {
            if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
              wsRef.current.send(JSON.stringify({ type: 'MCP_SERVER_LIST_REQUEST', schema_version: 1, request_id: Date.now().toString(), payload: {} }));
            }
          }
        } else if (data.type === 'PLUGIN_LIST_RESPONSE') {
          const p = data.payload.plugins;
          setPlugins(Array.isArray(p) ? p : Object.values(p || {}));
        } else if (data.type === 'WORKSPACE_LIST_RESPONSE') {
          // E6-F14: feeds the About section's local-guide entry point.
          setAboutWorkspaces(data.payload.workspaces || []);
        } else if (data.type === 'SKILL_TOGGLE_RESPONSE') {
          if (data.payload.skills) setSkills(data.payload.skills);
        } else if (data.type === 'SKILL_LIST_RESPONSE') {
          setSkills(data.payload.skills || []);
        } else if (data.type === 'PLUGIN_INSTALL_RESPONSE' || data.type === 'PLUGIN_UNINSTALL_RESPONSE' || data.type === 'PLUGIN_TOGGLE_RESPONSE') {
          if (data.payload.success) {
            // P-2: success clears any earlier failure notice and
            // refetches, exactly as before.
            setPluginActionResult(null);
            if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
              wsRef.current.send(JSON.stringify({ type: 'PLUGIN_LIST_REQUEST', schema_version: 1, request_id: Date.now().toString(), payload: {} }));
            }
          } else if (data.payload.error) {
            // P-2 (E7-F05 follow-up): a failed install/toggle/
            // uninstall must be visible in this panel too, not
            // silently swallowed — the DiagnosticsPanel idiom.
            const verb = data.type === 'PLUGIN_UNINSTALL_RESPONSE' ? 'Uninstall'
              : data.type === 'PLUGIN_TOGGLE_RESPONSE' ? 'Toggle' : 'Install';
            setPluginActionResult(`${verb}${data.payload.name ? ` ${data.payload.name}` : ''} failed: ${data.payload.error}`);
          }
        } else if (data.type === 'MEMORY_EPISODES_RESPONSE' || data.type === 'MEMORY_EPISODE_DELETE_RESPONSE') {
          setEpisodes(data.payload.episodes || []);
        } else if (data.type === 'MEMORY_EPISODES_CLEAR_RESPONSE') {
          setEpisodes([]);
          setSelectedEpisode(null);
        } else if (data.type === 'MEMORY_GRAPH_RESPONSE' || data.type === 'MEMORY_FACT_DELETE_RESPONSE' || data.type === 'MEMORY_ENTITY_DELETE_RESPONSE') {
          setFacts(data.payload.facts || []);
        } else if (data.type === 'MEMORY_GRAPH_CLEAR_RESPONSE') {
          setFacts([]);
        } else if (data.type === 'ENVIRONMENT_DIAGNOSTIC_RESPONSE') {
          setCapabilityStatus(data.payload.summary);
          setCapabilityHistory(data.payload.history || []);
          setIsDiagnosticsRunning(false);
        } else if (data.type === 'CONFIG_RESET_RESPONSE') {
          setResetArmed(false);
          if (data.payload && data.payload.success === false) {
            // P-1 (E6-F07 flag): the reset's save failed — the
            // engine kept the previous config. Mirror the
            // CONFIG_UPDATE failure branch: surface the error and
            // do NOT show the "done" confirmation or apply anything.
            setResetDone(false);
            setResetError(data.payload.error || 'The reset could not be saved.');
          } else {
            setResetDone(true);
            setResetError('');
            // E6-F04: the reply carries the full default config —
            // apply it like any other config reply, or every control
            // keeps displaying the pre-reset values until the panel
            // is remounted.
            if (data.payload && data.payload.config) {
              applyConfigValues(data.payload.config);
            }
          }
        } else if (data.type === 'BACKUP_CREATE_RESPONSE') {
          setBackupStatus(data.payload.success
            ? `Backup saved: ${data.payload.path} (${data.payload.files} files)`
            : `Backup failed: ${data.payload.error}`);
        } else if (data.type === 'BACKUP_RESTORE_RESPONSE') {
          setRestoreStatus(data.payload.success
            ? `Restored ${data.payload.files} files. Restart Aether for the restore to take effect.`
            : `Restore failed: ${data.payload.error}`);
        } else if (data.type === 'DIAGNOSTICS_EXPORT_RESPONSE') {
          if (data.payload.success) {
            const blob = new Blob([JSON.stringify(data.payload.bundle, null, 2)], { type: 'application/json' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `aether-diagnostics-${new Date().toISOString().slice(0, 10)}.json`;
            a.click();
            URL.revokeObjectURL(url);
            setVersionCheck('Diagnostics exported — the file contains no API keys and no memory content.');
          } else {
            setVersionCheck(`Diagnostics export failed: ${data.payload.error}`);
          }
        } else if (data.type === 'VERSION_CHECK_RESPONSE') {
          setVersionCheck(data.payload.success
            ? (data.payload.update_available
              ? `A newer version is available: ${data.payload.latest} (you have ${data.payload.current}). Download it from the project's GitHub releases when you're ready — nothing is installed automatically.`
              : `You're on the latest version (${data.payload.current}).`)
            : `Couldn't check for updates: ${data.payload.error}`);
        } else if (data.type === 'CONFIG_GET_RESPONSE' || data.type === 'CONFIG_UPDATE_RESPONSE') {
          if (data.type === 'CONFIG_UPDATE_RESPONSE' && data.payload.success === false) {
            // E6-F05: a refused strict-field write carries no config
            // — surface the error to the handed-off panels instead
            // of applying anything.
            setConfigUpdateError(data.payload.error || 'The update was rejected.');
          } else if (data.payload.config) {
            applyConfigValues(data.payload.config);
          }
        } else if (data.type === 'PROJECT_CONFIG_GET_RESPONSE') {
          const p = data.payload || {};
          if (p.success === false) {
            setProjectLayer((prev) => ({
              ...prev, workspaceRoot: '',
              warning: p.error || 'The project layer could not be loaded.', status: '',
            }));
          } else {
            setProjectLayer({
              workspaceRoot: p.workspace_root || '',
              content: p.content || '',
              exists: Boolean(p.exists),
              forbiddenKeys: p.forbidden_keys || [],
              warning: p.parse_error || '',
              status: '',
            });
          }
        } else if (data.type === 'PROJECT_CONFIG_UPDATE_RESPONSE') {
          const p = data.payload || {};
          if (p.success === false) {
            setProjectLayer((prev) => ({
              ...prev,
              warning: p.error || 'The project layer could not be saved.', status: '',
            }));
          } else {
            setProjectLayer((prev) => ({
              ...prev,
              exists: true,
              warning: '',
              // The engine writes the validated layer in canonical
              // form — show exactly what is now on disk.
              content: JSON.stringify(p.config || {}, null, 2),
              status: 'Saved — the layer applies to tasks run in this workspace.',
            }));
          }
        } else if (data.type === 'AUXILIARY_MODEL_SET_RESPONSE') {
          if (data.payload.success) {
            setAuxModels(data.payload.auxiliary_models || {});
          }
        } else if (data.type === 'REASONING_EFFORT_SET_RESPONSE') {
          if (data.payload.success && data.payload.effort) {
            setReasoningEffort(data.payload.effort);
            localStorage.setItem('reasoning_effort', data.payload.effort);
          }
        }
      } catch (e) {
        // ignore JSON parse errors
      }
    };
    
    wsRef.current.addEventListener('message', handleMessage);
    return () => {
      if (wsRef.current) wsRef.current.removeEventListener('message', handleMessage);
    };
  }, [wsRef]);

  // E6-F08: a section requested from outside the panel (the command
  // palette's jump-to-section entries) becomes the active section
  // exactly once; the parent clears its request when this reports
  // consumption. (Deliberate prop-to-state sync, as with the
  // activeMenu fetch effect below.)
  useEffect(() => {
    if (initialMenu) {
      setActiveMenu(initialMenu);
      if (onInitialMenuConsumed) onInitialMenuConsumed();
    }
    // eslint-disable-next-line react-hooks/set-state-in-effect
  }, [initialMenu]); // eslint-disable-line react-hooks/exhaustive-deps

  // E6-F14: the About section's guide entry point needs the
  // workspace roots (the guide ships inside the project checkout).
  useEffect(() => {
    if (activeMenu === 'About' && wsRef.current) {
      wsRef.current.send(JSON.stringify({
        type: 'WORKSPACE_LIST_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: {}
      }));
    }
  }, [activeMenu, wsRef]);

  useEffect(() => {
    if (activeMenu === 'Tools' && wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'TOOL_LIST_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: {}
      }));
      wsRef.current.send(JSON.stringify({
        type: 'APPROVAL_GRANTS_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: {}
      }));
      // E6-F05: the path-rule editor is controlled from the applied
      // config, so opening Tools fetches it too.
      wsRef.current.send(JSON.stringify({
        type: 'CONFIG_GET_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: {}
      }));
    } else if (activeMenu === 'Agents' && wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'AGENT_LIST_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: {}
      }));
      // Also fetch tools for the agent tool selector
      wsRef.current.send(JSON.stringify({
        type: 'TOOL_LIST_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: {}
      }));
      // E6-F05: the orchestration panel is controlled from the
      // applied config, so opening Agents fetches it too.
      wsRef.current.send(JSON.stringify({
        type: 'CONFIG_GET_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: {}
      }));
    } else if (activeMenu === 'MCP' && wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'MCP_SERVER_LIST_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: {}
      }));
    } else if (activeMenu === 'Plugins' && wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'PLUGIN_LIST_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: {}
      }));
    } else if (activeMenu === 'Skills' && wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'SKILL_LIST_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: {}
      }));
    } else if (activeMenu === 'Memory' && wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'MEMORY_EPISODES_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: {}
      }));
      wsRef.current.send(JSON.stringify({
        type: 'MEMORY_GRAPH_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: {}
      }));
    } else if ((activeMenu === 'Advanced' || activeMenu === 'Models' || activeMenu === 'Voice') && wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'CONFIG_GET_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: {}
      }));
    }
    // E6-F05: opening Advanced also fetches the project config layer
    // for its editor.
    if (activeMenu === 'Advanced' && wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'PROJECT_CONFIG_GET_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: {}
      }));
    }
  }, [activeMenu, wsRef]);

  useEffect(() => {
    // Initialize local state with backend keys.
    // Phase 10.1 C1: we never receive the actual API key for security, and
    // keys are never mirrored to localStorage — the password field always
    // starts empty; the user re-enters a key to change it. Configured state
    // comes from the engine's has_key, never from a stored key.
    const initBaseUrls = {};
    if (Array.isArray(providers)) {
      providers.forEach(p => {
        initBaseUrls[p.name] = p.base_url || '';
      });
    }
    setBaseUrls(prev => ({...initBaseUrls, ...prev}));
  }, [providers]);

  const handleSaveKey = (providerName) => {
    let finalProviderName = providerName;
    let displayName = undefined;
    
    if (providerName === 'custom_openai') {
      const name = customDisplayNames[providerName];
      if (!name) {
        setValidationState(prev => ({ ...prev, [providerName]: 'error' }));
        setValidationMsg(prev => ({ ...prev, [providerName]: 'Display Name is required.' }));
        return;
      }
      finalProviderName = `custom_${name.toLowerCase().replace(/[^a-z0-9]/g, '_')}`;
      displayName = name;
    }

    setValidationState(prev => ({ ...prev, [providerName]: 'saving' }));
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      const apiKeyToSave = keys[providerName] || '';
      // Phase 10.1 C1: keys are NEVER mirrored to localStorage (or any other
      // renderer storage). The engine's secure secret store is the only
      // destination — the key goes out via PROVIDER_SAVE_REQUEST only.
      wsRef.current.send(JSON.stringify({
        type: 'PROVIDER_SAVE_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: {
          provider: finalProviderName,
          api_key: apiKeyToSave,
          base_url: baseUrls[providerName] || '',
          ...(displayName && { display_name: displayName, provider_type: 'openai_compatible' })
        }
      }));
    }
  };

  const handleTestKey = (providerName) => {
    setValidationState(prev => ({ ...prev, [providerName]: 'testing' }));
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'SETTINGS_PROVIDER_VALIDATE_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: {
          provider: providerName,
          api_key: keys[providerName] || '',
          base_url: baseUrls[providerName] || ''
        }
      }));
    }
  };

  const handleDisconnect = (e, providerName) => {
    e.stopPropagation(); // prevent accordion from expanding/collapsing
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'PROVIDER_REMOVE_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: {
          provider: providerName
        }
      }));
      setExpandedProvider(null);
    }
  };


  // NOTE (Phase 9, B3): the old llama.cpp "mock check for demo" download flow
  // was deleted here — it was dead code (never rendered, never called). Local
  // model downloads are handled for real by LocalModelsTab.jsx, which sends
  // LOCAL_MODEL_DOWNLOAD_START with destination_dir.

  const menuItems = [
    { keywords: 'api key provider connect model list', id: 'Providers', icon: <Key size={18} />, label: 'API Providers' },
    { keywords: 'default model local ollama tasks auxiliary vision', id: 'Models', icon: <Cpu size={18} />, label: 'Models & Tasks' },
    { keywords: 'agent models supervisor planner researcher coder spawn policy prewalk output schemas compaction delegation', id: 'Agents', icon: <Network size={18} />, label: 'Agents' },
    { keywords: 'tools approval grants policy risk path rules', id: 'Tools', icon: <Wrench size={18} />, label: 'Tools' },
    { keywords: 'mcp servers tools resources prompts', id: 'MCP', icon: <Server size={18} />, label: 'MCP Servers' },
    { keywords: 'plugins install enable disable', id: 'Plugins', icon: <Plug size={18} />, label: 'Plugins' },
    { keywords: 'skills slash commands enable disable', id: 'Skills', icon: <Box size={18} />, label: 'Skills' },
    { keywords: 'workspace indexing rag prose embedder files pause resume search watcher exclusions', id: 'WorkspaceIndexing', icon: <Layers size={18} />, label: 'Workspace Indexing' },
    { keywords: 'browser agent policy payments envelopes confirmation autofill categories', id: 'BrowserPolicy', icon: <Globe size={18} />, label: 'Browser Agent' },
    { keywords: 'usage cost budget tokens spend ledger models agents daily alert', id: 'Usage', icon: <CircleDollarSign size={18} />, label: 'Usage' },
    { keywords: 'password vault credentials logins totp ask every time autofill', id: 'PasswordVault', icon: <Lock size={18} />, label: 'Password Vault' },
    { keywords: 'memory episodes facts graph storage backend', id: 'Memory', icon: <Database size={18} />, label: 'Memory & Storage' },
    { keywords: 'capabilities diagnostics environment scheduler', id: 'Capabilities', icon: <Activity size={18} />, label: 'Capabilities' },
    { keywords: 'voice tuning audio vad endpointing silence threshold mic speech', id: 'Voice', icon: <Mic size={18} />, label: 'Voice Tuning' },
    { keywords: 'advanced routing fallbacks reset defaults developer project config layer overlay', id: 'Advanced', icon: <Sliders size={18} />, label: 'Advanced' },
    { keywords: 'about version guide documentation help', id: 'About', icon: <Info size={18} />, label: 'About' }
  ];

  // The sections surviving the search filter, in nav order. The list
  // render and the search box's Enter-to-jump (E6-F06) share this one
  // derivation so they can never disagree about what "first match" is.
  const visibleMenuItems = menuItems.filter(item => {
    const q = settingsQuery.trim().toLowerCase();
    if (!q) return true;
    return item.label.toLowerCase().includes(q) || (item.keywords || '').toLowerCase().includes(q);
  });

  return (
    <div style={{ display: 'flex', height: '100%', gap: '32px' }}>
      
      {/* Settings Sidebar */}
      <div style={{
        width: '240px',
        display: 'flex',
        flexDirection: 'column',
        gap: '8px',
        borderRight: '1px solid rgba(255,255,255,0.08)',
        paddingRight: '24px'
      }}>
        <input
          type="text"
          className="aether-search-input"
          aria-label="Search settings"
          placeholder="Search settings…"
          value={settingsQuery}
          onChange={(e) => setSettingsQuery(e.target.value)}
          onKeyDown={(e) => {
            // E6-F06: Enter jumps to the first section the filter leaves.
            if (e.key === 'Enter' && visibleMenuItems.length > 0) {
              setActiveMenu(visibleMenuItems[0].id);
            }
          }}
          style={{ padding: '10px 14px', borderRadius: '10px', background: 'rgba(255,255,255,0.05)', border: '1px solid rgba(255,255,255,0.1)', color: '#fff', fontSize: '13px', marginBottom: '4px' }}
        />
        {visibleMenuItems.map(item => (
          <div
            key={item.id}
            role="button"
            tabIndex={0}
            onClick={() => setActiveMenu(item.id)}
            onKeyDown={(e) => {
              // E6-F06: keyboard activation does exactly what a click
              // does. (role="button" also opts the item into the global
              // :focus-visible ring in index.css.)
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                setActiveMenu(item.id);
              }
            }}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: '12px',
              padding: '12px 16px',
              borderRadius: '12px',
              cursor: 'pointer',
              color: activeMenu === item.id ? '#fff' : '#9090a0',
              background: activeMenu === item.id ? 'rgba(255, 255, 255, 0.1)' : 'transparent',
              transition: 'all 0.2s',
              fontWeight: activeMenu === item.id ? '500' : '400'
            }}
            onMouseEnter={(e) => {
              if (activeMenu !== item.id) {
                e.currentTarget.style.background = 'rgba(255, 255, 255, 0.05)';
                e.currentTarget.style.color = '#fff';
              }
            }}
            onMouseLeave={(e) => {
              if (activeMenu !== item.id) {
                e.currentTarget.style.background = 'transparent';
                e.currentTarget.style.color = '#9090a0';
              }
            }}
          >
            {item.icon}
            {item.label}
          </div>
        ))}
      </div>

      {/* Settings Content Area */}
      <div style={{ flex: 1, overflowY: 'auto', paddingRight: '16px', paddingBottom: '80px' }} className="chat-scroll">
        
        {/* Phase 10.3 C2: the General / Appearance section was removed.
            Its theme buttons only set `data-theme` on <html> — no CSS rule
            ever read it, nothing persisted the choice, and the app's colours
            are hardcoded dark inline styles, so a real light theme would
            need a full re-theme, not a toggle. A control that looks
            functional must be functional, or it must go — this one went. */}

        {activeMenu === 'Providers' && (
          <ProviderSettings providers={providers} health={providerHealth} />
        )}

        {activeMenu === 'Models' && (
          <div style={{ color: '#f0f0f5' }}>
            <h2 style={{ fontSize: '24px', marginBottom: '8px', fontWeight: 500 }}>Models & Tasks</h2>
            <p style={{ color: '#9090a0', fontSize: '14px', marginBottom: '24px' }}>Assign default models globally, or route specific tasks to specialized models.</p>
            
            {/* Toggle Switch */}
            <div style={{ display: 'flex', background: 'rgba(255,255,255,0.05)', padding: '4px', borderRadius: '12px', marginBottom: '24px', width: 'fit-content' }}>
               <button 
                  onClick={() => setModelTab('cloud')}
                  style={{ background: modelTab === 'cloud' ? 'rgba(124,58,237,0.4)' : 'transparent', color: '#fff', border: 'none', padding: '8px 24px', borderRadius: '8px', cursor: 'pointer', transition: 'all 0.2s' }}>
                  Cloud Models
               </button>
               <button 
                  onClick={() => setModelTab('local')}
                  style={{ background: modelTab === 'local' ? 'rgba(124,58,237,0.4)' : 'transparent', color: '#fff', border: 'none', padding: '8px 24px', borderRadius: '8px', cursor: 'pointer', transition: 'all 0.2s' }}>
                  Local Models
               </button>
            </div>

            {modelTab === 'cloud' && (
               <>
                 <div style={{ background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.08)', borderRadius: '16px', padding: '20px', marginBottom: '24px' }}>
                   <div style={{ display: 'flex', gap: '24px' }}>
                     <div style={{ flex: 1 }}>
                       <h3 style={{ fontSize: '16px', marginBottom: '12px' }}>Global Default Model</h3>
                       <Dropdown 
                         value={
                           (Array.isArray(providers) ? providers : []).find(p => p.is_default)
                             ? `${(Array.isArray(providers) ? providers : []).find(p => p.is_default).name}:::${(Array.isArray(providers) ? providers : []).find(p => p.is_default).default_model}`
                             : ''
                         }
                         onChange={(val) => {
                           const [provider, model] = val.split(':::');
                           if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
                             wsRef.current.send(JSON.stringify({
                               type: 'MODEL_SET_DEFAULT',
                               schema_version: 1,
                               request_id: Date.now().toString(),
                               payload: { provider, model }
                             }));
                           }
                         }}
                         options={
                           (Array.isArray(providers) ? providers : [])
                             .filter(p => p.has_key && p.models && p.models.length > 0)
                             .map(p => ({
                               group: p.display_name || p.name,
                               items: p.models.map(m => ({ value: `${p.name}:::${m}`, label: m }))
                             }))
                         }
                         placeholder="Select a default model..."
                         style={{ width: '100%' }}
                       />
                     </div>
                     <div style={{ width: '200px' }}>
                       <h3 style={{ fontSize: '16px', marginBottom: '12px' }}>Reasoning Effort</h3>
                       <Dropdown 
                         value={reasoningEffort}
                         onChange={(val) => {
                           // Phase 9, B2: persist engine-side so it actually affects model calls.
                           // E6-F15: sent unconditionally — the facade
                           // queues it while offline (see sendConfigUpdate).
                           setReasoningEffort(val);
                           localStorage.setItem('reasoning_effort', val);
                           if (wsRef.current) {
                             wsRef.current.send(JSON.stringify({
                               type: 'REASONING_EFFORT_SET_REQUEST',
                               schema_version: 1,
                               request_id: Date.now().toString(),
                               payload: { effort: val }
                             }));
                           }
                         }}
                         options={[
                           { value: 'standard', label: 'Standard' },
                           { value: 'medium', label: 'Medium' },
                           { value: 'high', label: 'Deep Thinking' }
                         ]}
                         style={{ width: '100%' }}
                       />
                     </div>
                   </div>
                 </div>

                 <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: '32px', marginBottom: '16px' }}>
                   <h3 style={{ fontSize: '18px', fontWeight: 500, margin: 0 }}>⚙ Auxiliary models</h3>
                   <button
                     style={{ background: 'transparent', color: '#a78bfa', border: 'none', fontSize: '13px', cursor: 'pointer' }}
                     onClick={() => {
                       // Phase 9, B1: clear every override back to the main model.
                       // E6-F15: sent unconditionally (facade queues offline).
                       setAuxModels({});
                       if (wsRef.current) {
                         wsRef.current.send(JSON.stringify({
                           type: 'AUXILIARY_MODEL_SET_REQUEST',
                           schema_version: 1,
                           request_id: Date.now().toString(),
                           payload: { task: 'vision', provider: '', model: 'auto' }
                         }));
                       }
                     }}
                   >Reset all to main</button>
                 </div>
                 <p style={{ color: '#9090a0', fontSize: '13px', marginBottom: '16px' }}>Helper tasks run on the main model by default. Assign a dedicated model to any task to override.</p>
                 
                 <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                   {[
                     // Phase 9, B1 (UI honesty): only tasks with a real engine seam are
                     // shown. Vision overrides are honored by the routing policy; the
                     // other helper tasks have no engine call site yet, so no control
                     // is rendered for them instead of a placebo dropdown.
                     { id: 'vision', name: 'Vision', desc: 'Image analysis' },
                   ].map(task => (
                     <div key={task.id} style={{ background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.05)', borderRadius: '12px', padding: '12px 16px', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                        <div>
                          <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                            <h4 style={{ fontSize: '14px', fontWeight: 600, margin: 0 }}>{task.name}</h4>
                            <span style={{ fontSize: '11px', color: '#9090a0' }}>{task.desc}</span>
                          </div>
                          <p style={{ fontSize: '12px', color: '#a78bfa', marginTop: '4px', fontFamily: 'monospace' }}>{auxModels[task.id] ? auxModels[task.id].split(':::')[1] || auxModels[task.id] : 'auto · use main model'}</p>
                        </div>
                        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                          <Dropdown 
                            value={auxModels[task.id] || 'auto'}
                            onChange={(val) => {
                              const [provider, model] = val.split(':::');
                              // Phase 9, B1: optimistic update; engine confirms via AUXILIARY_MODEL_SET_RESPONSE.
                              setAuxModels(prev => {
                                const next = { ...prev };
                                if (!model || model === 'auto') delete next[task.id];
                                else next[task.id] = val;
                                return next;
                              });
                              // E6-F15: sent unconditionally (facade queues offline).
                              if (wsRef.current) {
                                wsRef.current.send(JSON.stringify({
                                  type: 'AUXILIARY_MODEL_SET_REQUEST',
                                  schema_version: 1,
                                  request_id: Date.now().toString(),
                                  payload: { task: task.id, provider, model: model || 'auto' }
                                }));
                              }
                            }}
                            options={[
                              { value: 'auto', label: 'Use main model' },
                              ...(Array.isArray(providers) ? providers : [])
                                .filter(p => p.has_key && p.models && p.models.length > 0)
                                .map(p => ({
                                  group: p.display_name || p.name,
                                  items: p.models.map(m => ({ value: `${p.name}:::${m}`, label: m }))
                                }))
                            ]}
                            style={{ width: '180px' }}
                            align="right"
                          />
                        </div>
                     </div>
                   ))}
                 </div>
               </>
            )}

            {modelTab === 'local' && (
               <LocalModelsTab
                 wsRef={wsRef}
                 localModels={localModels}
                 // Phase 10.3 C2: the active model lives on the providers
                 // array (the provider flagged is_default); LocalModelsTab
                 // needs it to show which downloaded model is Active.
                 defaultProvider={(Array.isArray(providers) ? providers : []).find(p => p.is_default)?.name || ''}
                 defaultModel={(Array.isArray(providers) ? providers : []).find(p => p.is_default)?.default_model || ''}
               />
            )}
          </div>
        )}

        {activeMenu === 'Agents' && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '32px' }}>
            <DiagnosticsPanel activeMenu='Agents' />
            <OrchestrationPanel config={engineConfig} configError={configUpdateError} onConfigUpdate={sendConfigUpdate} />
          </div>
        )}

        {activeMenu === 'Capabilities' && (
          <div style={{ color: '#f0f0f5' }}>
            <h2 style={{ fontSize: '24px', marginBottom: '8px', fontWeight: 500 }}>Engine Capabilities</h2>
            <p style={{ color: '#9090a0', fontSize: '14px', marginBottom: '24px' }}>Monitor the health and capabilities of the Aether execution environment.</p>
            
            <div style={{ background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.08)', borderRadius: '16px', padding: '24px' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '24px' }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
                  <div style={{ width: '12px', height: '12px', borderRadius: '50%', background: capabilityStatus && capabilityStatus.failed_count === 0 ? '#10b981' : (capabilityStatus && capabilityStatus.failed_count > 0 ? '#ef4444' : '#6b7280'), boxShadow: capabilityStatus && capabilityStatus.failed_count === 0 ? '0 0 10px rgba(16,185,129,0.5)' : 'none' }}></div>
                  <h3 style={{ fontSize: '16px' }}>{capabilityStatus ? (capabilityStatus.failed_count === 0 ? 'System Healthy' : `${capabilityStatus.failed_count} Issues Detected`) : 'Status Unknown'}</h3>
                </div>
                <button 
                  onClick={() => {
                    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN && !isDiagnosticsRunning) {
                      setIsDiagnosticsRunning(true);
                      wsRef.current.send(JSON.stringify({
                        type: 'ENVIRONMENT_DIAGNOSTIC_RUN_REQUEST',
                        schema_version: 1,
                        request_id: Date.now().toString(),
                        payload: {}
                      }));
                    }
                  }}
                  disabled={isDiagnosticsRunning}
                  style={{ 
                    background: isDiagnosticsRunning ? 'rgba(124, 58, 237, 0.2)' : 'transparent', 
                    color: isDiagnosticsRunning ? '#a78bfa' : '#7c3aed', 
                    border: '1px solid rgba(124, 58, 237, 0.5)', 
                    padding: '8px 16px', 
                    borderRadius: '8px', 
                    cursor: isDiagnosticsRunning ? 'wait' : 'pointer',
                    transition: 'all 0.2s ease',
                    display: 'flex',
                    alignItems: 'center',
                    gap: '8px'
                  }}>
                  {isDiagnosticsRunning ? (
                    <>
                      <svg style={{ width: '16px', height: '16px', animation: 'spin 1s linear infinite' }} xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24">
                        <circle style={{ opacity: 0.25 }} cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4"></circle>
                        <path style={{ opacity: 0.75 }} fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4zm2 5.291A7.962 7.962 0 014 12H0c0 3.042 1.135 5.824 3 7.938l3-2.647z"></path>
                      </svg>
                      Testing...
                    </>
                  ) : (
                    'Run Diagnostics Now'
                  )}
                </button>
              </div>
              
              <div style={{ background: 'rgba(0,0,0,0.5)', border: '1px solid rgba(255,255,255,0.1)', borderRadius: '12px', padding: '16px', fontFamily: 'monospace', fontSize: '13px', color: '#10b981', height: '200px', overflowY: 'auto' }} className="chat-scroll">
                {capabilityHistory.length === 0 ? (
                  <div style={{ color: '#9090a0', marginBottom: '8px' }}>[System] Ready to run diagnostics. Click the button above.</div>
                ) : (
                  capabilityHistory.map((log, idx) => (
                    <div key={idx} style={{ color: log.status === 'OK' || log.status === 'RUNNING' ? '#10b981' : (log.status === 'WARN' ? '#fbbf24' : '#ef4444') }}>
                      [{new Date(log.timestamp * 1000).toLocaleTimeString()}] {log.target_name}: {log.status} {log.details ? `(${log.details})` : ''}
                    </div>
                  ))
                )}
              </div>
            </div>
          </div>
        )}

        { activeMenu === 'Tools' && (
          <div style={{ color: '#f0f0f5' }}>
            <h2 style={{ fontSize: '24px', marginBottom: '8px', fontWeight: 500 }}>Tools Sandbox</h2>
            <p style={{ color: '#9090a0', fontSize: '14px', marginBottom: '24px' }}>Configure auto-approval policies for built-in tools.</p>

            <div style={{ background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.05)', borderRadius: '12px', padding: '16px', display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '12px' }}>
              <div>
                <h4 style={{ fontSize: '15px', fontWeight: 500 }}>Approval mode</h4>
                <p style={{ fontSize: '13px', color: '#9090a0', marginTop: '4px' }}>
                  {approvalMode === 'write'
                    ? 'Write tools run without asking. Execute and destructive tools still require approval.'
                    : 'Every non-read-only tool asks for approval before it runs.'}
                </p>
              </div>
              <div>
                <Dropdown
                  value={approvalMode}
                  onChange={handleApprovalModeChange}
                  options={[
                    { value: 'ask', label: 'Ask for approval' },
                    { value: 'write', label: 'Auto-approve writes' }
                  ]}
                  style={{ width: '220px' }}
                  align="right"
                />
              </div>
            </div>

            <div data-testid="approval-grants" style={{ marginBottom: '24px' }}>
              <h3 style={{ fontSize: '16px', fontWeight: 500, marginBottom: '4px' }}>Approval grants</h3>
              <p style={{ fontSize: '13px', color: '#9090a0', marginBottom: '12px' }}>Files and folders you approved with &quot;remember&quot; — matching tool calls run without asking until you revoke the grant.</p>
              {approvalGrants.length === 0 ? (
                <div style={{ color: '#9090a0', fontSize: '14px', padding: '16px', textAlign: 'center', background: 'rgba(0,0,0,0.2)', borderRadius: '12px' }}>
                  No approval grants remembered.
                </div>
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                  {approvalGrants.map(grant => (
                    <div key={grant.id} style={{ background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.05)', borderRadius: '12px', padding: '12px 16px', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                      <div style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap' }}>
                        <span style={{ fontFamily: 'monospace', fontSize: '13px' }}>{grant.prefix}</span>
                        <span style={{ fontSize: '11px', background: 'rgba(59, 130, 246, 0.2)', color: '#93c5fd', padding: '2px 6px', borderRadius: '4px', marginLeft: '8px' }}>up to {grant.ceiling}</span>
                        {grant.tool_name && (
                          <span style={{ fontSize: '11px', color: '#9090a0', marginLeft: '8px' }}>{grant.tool_name}</span>
                        )}
                      </div>
                      <button
                        onClick={() => handleRevokeGrant(grant.id)}
                        style={{ padding: '6px 12px', borderRadius: '8px', background: 'rgba(239, 68, 68, 0.15)', color: '#fca5a5', border: '1px solid rgba(239, 68, 68, 0.35)', cursor: 'pointer', fontSize: '12px', fontWeight: 500 }}
                      >
                        Revoke
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </div>

            <PathRulesPanel rules={engineConfig ? engineConfig.path_rules : undefined} configError={configUpdateError} onConfigUpdate={sendConfigUpdate} />

            <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
              {tools.length === 0 ? (
                <div style={{ color: '#9090a0', fontSize: '14px', padding: '24px', textAlign: 'center', background: 'rgba(0,0,0,0.2)', borderRadius: '12px' }}>
                  No tools registered.
                </div>
              ) : (
                tools.map(tool => {
                  const isWrite = tool.risk === 'write';
                  const isDestructive = tool.risk === 'destructive';
                  const isExecute = tool.risk === 'execute';
                  
                  let badge = null;
                  if (isWrite) {
                    badge = <span style={{ fontSize: '11px', background: 'rgba(245, 158, 11, 0.2)', color: '#fbbf24', padding: '2px 6px', borderRadius: '4px', marginLeft: '8px' }}>Write</span>;
                  } else if (isDestructive || isExecute) {
                    badge = <span style={{ fontSize: '11px', background: 'rgba(239, 68, 68, 0.2)', color: '#f87171', padding: '2px 6px', borderRadius: '4px', marginLeft: '8px', display: 'inline-flex', alignItems: 'center', gap: '4px' }}><Lock size={10} /> Risky</span>;
                  }

                  // Phase 10.3 C2: the old 'default' option is gone. It was
                  // un-sendable — the handler returned early on it, and no
                  // engine message exists to clear a stored override (TOOL_POLICY_SET_REQUEST only stores;
                  // policy_is_default means "no entry in tool_policies", and
                  // there is no reset event in the contracts). So the
                  // dropdown now lists only the three policies it can
                  // actually send, and when the tool is at its default the
                  // dropdown shows the *effective* policy the engine
                  // resolves for it, with a note that no override is set.
                  const effectivePolicyValue =
                    tool.effective_policy === 'allow' ? 'Always Allow'
                    : tool.effective_policy === 'deny' ? 'Deny'
                    : 'Require Approval';

                  const options = [
                    { value: 'Always Allow', label: 'Always Allow', disabled: !tool.always_allow_permitted },
                    { value: 'Require Approval', label: 'Require Approval' },
                    { value: 'Deny', label: 'Deny' }
                  ];

                  return (
                    <div key={tool.function.name} style={{ background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.05)', borderRadius: '12px', padding: '16px', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                      <div>
                        <h4 style={{ fontSize: '15px', fontWeight: 500, display: 'flex', alignItems: 'center' }}>
                          {tool.function.name}
                          {badge}
                        </h4>
                        <p style={{ fontSize: '13px', color: '#9090a0', marginTop: '4px' }}>{tool.function.description}</p>
                        {tool.policy_is_default && (
                           <p style={{ fontSize: '11px', color: '#9090a0', marginTop: '4px' }}>Default policy — no override set. Pick one to override it.</p>
                        )}
                        {!tool.always_allow_permitted && (
                           <p style={{ fontSize: '11px', color: '#fbbf24', marginTop: '4px' }}>Always Allow is disabled for risky tools.</p>
                        )}
                      </div>
                      <div>
                        <Dropdown 
                          value={tool.policy_is_default ? effectivePolicyValue : tool.policy}
                          onChange={(val) => {
                            if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
                              wsRef.current.send(JSON.stringify({
                                type: 'TOOL_POLICY_SET_REQUEST',
                                schema_version: 1,
                                request_id: Date.now().toString(),
                                payload: { tool_name: tool.function.name, policy: val }
                              }));
                            }
                          }}
                          options={options}
                          style={{ width: '220px' }}
                          align="right"
                        />
                      </div>
                    </div>
                  );
                })
              )}
            </div>
          </div>
        )}

        {activeMenu === 'MCP' && (
          <DiagnosticsPanel activeMenu='MCP' activeSession={activeSession} />
        )}

        {activeMenu === 'Plugins' && (
          <div>
            {/* P-2: plugin action failures surface here as well as in
                DiagnosticsPanel, which renders the plugin UI. */}
            {pluginActionResult && (
              <div style={{ color: '#fbbf24', fontSize: '13px', marginBottom: '16px' }}>{pluginActionResult}</div>
            )}
            <DiagnosticsPanel activeMenu='Plugins' />
          </div>
        )}

        {activeMenu === 'Skills' && (
          <div style={{ color: '#f0f0f5' }}>
            <h2 style={{ fontSize: '24px', marginBottom: '8px', fontWeight: 500 }}>Skills</h2>
            <p style={{ color: '#9090a0', fontSize: '14px', marginBottom: '24px' }}>
              Dynamic behaviors, prompts, and tool requirements loaded from `.agents/skills`. Trigger these in chat with slash commands.
            </p>
            <div style={{ display: 'flex', flexDirection: 'column', gap: '12px' }}>
              {skills.length === 0 ? (
                <div style={{ color: '#9090a0', fontSize: '14px', padding: '24px', textAlign: 'center', background: 'rgba(0,0,0,0.2)', borderRadius: '12px' }}>
                  No skills found in workspace. Create a skill in <code>.agents/skills/</code>.
                </div>
              ) : (
                skills.map(skill => (
                  <div key={skill.name} style={{ background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.05)', borderRadius: '12px', padding: '16px' }}>
                    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: '12px' }}>
                      <div>
                        <h4 style={{ fontSize: '15px', fontWeight: 500, display: 'flex', alignItems: 'center', gap: '8px' }}>
                          {skill.name}
                          <span style={{ fontSize: '11px', background: 'rgba(255,255,255,0.1)', padding: '2px 6px', borderRadius: '4px' }}>{skill.source}</span>
                        </h4>
                        <p style={{ fontSize: '13px', color: '#9090a0', marginTop: '4px' }}>
                          {skill.description}
                        </p>
                      </div>
                    </div>
                    <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
                      {skill.slash_commands?.map(cmd => (
                        <span key={cmd} style={{ fontSize: '12px', background: 'rgba(59, 130, 246, 0.2)', color: '#60a5fa', padding: '4px 8px', borderRadius: '6px' }}>
                          {cmd}
                        </span>
                      ))}
                      {skill.required_tools?.length > 0 && (
                        <span style={{ fontSize: '12px', background: 'rgba(251, 191, 36, 0.15)', color: '#fbbf24', padding: '4px 8px', borderRadius: '6px' }}>
                          {skill.required_tools.length} Tools Required
                        </span>
                      )}
                    </div>
                    <div style={{ marginTop: '12px', display: 'flex', alignItems: 'center', gap: '10px' }}>
                      <button
                        onClick={() => {
                          if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
                            wsRef.current.send(JSON.stringify({
                              type: 'SKILL_TOGGLE_REQUEST',
                              schema_version: 1,
                              request_id: Date.now().toString(),
                              payload: { name: skill.name, enabled: skill.enabled === false }
                            }));
                          }
                        }}
                        style={{ background: skill.enabled === false ? 'rgba(16,185,129,0.15)' : 'rgba(239,68,68,0.12)', color: skill.enabled === false ? '#10b981' : '#f87171', border: '1px solid rgba(255,255,255,0.12)', padding: '6px 14px', borderRadius: '8px', cursor: 'pointer', fontSize: '12px' }}
                      >
                        {skill.enabled === false ? 'Enable skill' : 'Disable skill'}
                      </button>
                      {skill.enabled === false && (
                        <span style={{ fontSize: '12px', color: '#9090a0' }}>Disabled — hidden from agents</span>
                      )}
                    </div>
                  </div>
                ))
              )}
            </div>
          </div>
        )}

        {activeMenu === 'Memory' && (
          <MemoryManagement />
        )}

        {activeMenu === 'WorkspaceIndexing' && (
          <WorkspaceIndexingPanel wsRef={wsRef} />
        )}

        {activeMenu === 'BrowserPolicy' && (
          <BrowserPolicyPanel wsRef={wsRef} />
        )}

        {activeMenu === 'Usage' && (
          <UsageSettingsPanel wsRef={wsRef} sessionId={activeSession} dailyBudget={dailyBudget} budgetAlert={budgetAlert} budgetExceeded={!!budgetAlert} />
        )}

        {activeMenu === 'PasswordVault' && (
          <VaultPanel wsRef={wsRef} />
        )}

        {activeMenu === 'Advanced' && (
          <div style={{ color: '#f0f0f5', display: 'flex', flexDirection: 'column', gap: '24px' }}>
            <div>
              <h2 style={{ fontSize: '24px', marginBottom: '8px', fontWeight: 500 }}>Advanced Settings</h2>
              <p style={{ color: '#9090a0', fontSize: '14px' }}>System configurations and developer options.</p>
            </div>

            <div style={{ background: 'rgba(239,68,68,0.05)', border: '1px solid rgba(239,68,68,0.25)', borderRadius: '12px', padding: '24px' }}>
              <h3 style={{ fontSize: '16px', fontWeight: 500, marginBottom: '8px', color: '#fff' }}>Reset all settings</h3>
              <p style={{ color: '#9090a0', fontSize: '13px', margin: '0 0 16px 0' }}>
                Restores every engine setting to its default and saves immediately. Your API keys (held in the
                encrypted secret store), your files, and your memory are NOT touched.
              </p>
              {resetError && (
                <div style={{ color: '#f87171', fontSize: '13px', marginBottom: '12px' }}>Reset failed: {resetError}</div>
              )}
              {resetDone ? (
                <div style={{ color: '#10b981', fontSize: '13px' }}>Settings were reset to defaults.</div>
              ) : (
                <button
                  onClick={() => {
                    if (!resetArmed) { setResetArmed(true); return; }
                    // E6-F15: sent unconditionally — a reset confirmed
                    // while offline is queued by the facade and lands
                    // on reconnect, not dropped.
                    if (wsRef.current) {
                      wsRef.current.send(JSON.stringify({ type: 'CONFIG_RESET_REQUEST', schema_version: 1, request_id: Date.now().toString(), payload: {} }));
                    }
                  }}
                  style={{ background: resetArmed ? '#ef4444' : 'rgba(239,68,68,0.15)', color: resetArmed ? '#fff' : '#f87171', border: '1px solid rgba(239,68,68,0.4)', padding: '9px 18px', borderRadius: '10px', cursor: 'pointer', fontWeight: 500 }}
                >
                  {resetArmed ? 'Click again to confirm reset' : 'Reset all settings'}
                </button>
              )}
            </div>
            
            <div style={{ background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.05)', borderRadius: '12px', padding: '24px' }}>
              <h3 style={{ fontSize: '16px', fontWeight: 500, marginBottom: '8px', color: '#fff' }}>Backup & restore</h3>
              <p style={{ color: '#9090a0', fontSize: '13px', margin: '0 0 16px 0' }}>
                A backup snapshots your settings, memory, sessions and skills into one zip in your workspace's
                AetherBackups folder. API keys stay out unless you include the (encrypted) secret store.
                Restoring replaces those files on disk — restart Aether afterwards for it to take effect.
              </p>
              <div style={{ display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap', marginBottom: '12px' }}>
                <button
                  onClick={() => {
                    setBackupStatus('Creating backup…');
                    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
                      wsRef.current.send(JSON.stringify({ type: 'BACKUP_CREATE_REQUEST', schema_version: 1, request_id: Date.now().toString(), payload: { include_secrets: includeSecrets } }));
                    }
                  }}
                  style={{ background: 'rgba(245,158,11,0.15)', color: '#f59e0b', border: '1px solid rgba(245,158,11,0.4)', padding: '9px 18px', borderRadius: '10px', cursor: 'pointer', fontWeight: 500 }}
                >
                  Create backup
                </button>
                <label style={{ display: 'flex', alignItems: 'center', gap: '6px', fontSize: '13px', color: '#c0c0cc', cursor: 'pointer' }}>
                  <input type="checkbox" checked={includeSecrets} onChange={(e) => setIncludeSecrets(e.target.checked)} />
                  Include encrypted secret store
                </label>
              </div>
              {backupStatus && <div style={{ fontSize: '13px', color: '#a0a0b0', marginBottom: '16px', wordBreak: 'break-all' }}>{backupStatus}</div>}
              <div style={{ display: 'flex', gap: '8px', flexWrap: 'wrap' }}>
                <input
                  value={restorePath}
                  onChange={(e) => setRestorePath(e.target.value)}
                  placeholder="Path to a backup zip…"
                  aria-label="Backup zip path"
                  style={{ flex: 1, minWidth: '220px', background: 'rgba(255,255,255,0.04)', border: '1px solid rgba(255,255,255,0.1)', borderRadius: '8px', padding: '9px 12px', color: '#f0f0f5', fontSize: '13px' }}
                />
                <button
                  onClick={() => {
                    if (!restorePath.trim()) { setRestoreStatus('Enter the path to a backup zip first.'); return; }
                    setRestoreStatus('Restoring…');
                    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
                      wsRef.current.send(JSON.stringify({ type: 'BACKUP_RESTORE_REQUEST', schema_version: 1, request_id: Date.now().toString(), payload: { path: restorePath.trim() } }));
                    }
                  }}
                  style={{ background: 'rgba(255,255,255,0.06)', color: '#f0f0f5', border: '1px solid rgba(255,255,255,0.15)', padding: '9px 18px', borderRadius: '10px', cursor: 'pointer', fontWeight: 500 }}
                >
                  Restore
                </button>
              </div>
              {restoreStatus && <div style={{ fontSize: '13px', color: '#a0a0b0', marginTop: '12px' }}>{restoreStatus}</div>}
              <div style={{ borderTop: '1px solid rgba(255,255,255,0.06)', marginTop: '20px', paddingTop: '16px', display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap' }}>
                <button
                  onClick={() => {
                    setVersionCheck('Checking…');
                    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
                      wsRef.current.send(JSON.stringify({ type: 'VERSION_CHECK_REQUEST', schema_version: 1, request_id: Date.now().toString(), payload: {} }));
                    }
                  }}
                  style={{ background: 'rgba(255,255,255,0.06)', color: '#f0f0f5', border: '1px solid rgba(255,255,255,0.15)', padding: '9px 18px', borderRadius: '10px', cursor: 'pointer', fontWeight: 500 }}
                >
                  Check for updates
                </button>
                <button
                  onClick={() => {
                    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
                      wsRef.current.send(JSON.stringify({ type: 'DIAGNOSTICS_EXPORT_REQUEST', schema_version: 1, request_id: Date.now().toString(), payload: {} }));
                    }
                  }}
                  style={{ background: 'rgba(255,255,255,0.06)', color: '#f0f0f5', border: '1px solid rgba(255,255,255,0.15)', padding: '9px 18px', borderRadius: '10px', cursor: 'pointer', fontWeight: 500 }}
                >
                  Export diagnostics
                </button>
                <button
                  onClick={() => { if (window.electronAPI && window.electronAPI.openLogsFolder) window.electronAPI.openLogsFolder(); }}
                  style={{ background: 'rgba(255,255,255,0.06)', color: '#f0f0f5', border: '1px solid rgba(255,255,255,0.15)', padding: '9px 18px', borderRadius: '10px', cursor: 'pointer', fontWeight: 500 }}
                >
                  Open logs folder
                </button>
                {versionCheck && <span style={{ fontSize: '13px', color: '#a0a0b0' }}>{versionCheck}</span>}
              </div>
            </div>

            <div style={{ background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.05)', borderRadius: '12px', padding: '24px' }}>
              <h3 style={{ fontSize: '16px', fontWeight: 500, marginBottom: '16px', color: '#fff', display: 'flex', alignItems: 'center', gap: '8px' }}>
                <Network size={18} style={{ color: '#a78bfa' }} /> Model Routing & Fallbacks
              </h3>
              
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '24px' }}>
                <div>
                  <div style={{ fontWeight: 500, fontSize: '14px', marginBottom: '4px' }}>Disable Fallback System</div>
                  <div style={{ fontSize: '12px', color: '#9090a0' }}>Prevent the system from automatically trying alternative models when the current one fails.</div>
                </div>
                <label style={{ display: 'flex', alignItems: 'center', cursor: 'pointer' }}>
                  <input
                    type="checkbox"
                    checked={disableFallbacks}
                    onChange={(e) => {
                      const val = e.target.checked;
                      setDisableFallbacks(val);
                      // E6-F15: through the one CONFIG_UPDATE flow,
                      // unconditionally (facade queues offline).
                      sendConfigUpdate({ disable_fallbacks: val });
                    }}
                    style={{ accentColor: '#7c3aed', width: '18px', height: '18px' }}
                  />
                </label>
              </div>

              {!disableFallbacks && (
                <div>
                  <div style={{ fontWeight: 500, fontSize: '14px', marginBottom: '8px' }}>Fallback Provider Chain (Optional)</div>
                  <div style={{ fontSize: '12px', color: '#9090a0', marginBottom: '12px' }}>
                    Comma-separated list of providers to try in order (e.g., <code>openrouter, groq, google_gemini</code>). Leave empty for automatic selection.
                  </div>
                  <input
                    type="text"
                    value={fallbackChain}
                    onChange={(e) => setFallbackChain(e.target.value)}
                    onBlur={() => {
                      // E6-F15: through the one CONFIG_UPDATE flow,
                      // unconditionally (facade queues offline).
                      const parsedChain = fallbackChain.split(',').map(s => s.trim()).filter(Boolean);
                      sendConfigUpdate({ fallback_chain: parsedChain });
                    }}
                    placeholder="E.g. openrouter, groq, anthropic"
                    style={{
                      width: '100%',
                      background: 'rgba(0,0,0,0.2)',
                      border: '1px solid rgba(255,255,255,0.1)',
                      borderRadius: '6px',
                      padding: '10px 12px',
                      color: '#fff',
                      fontSize: '13px'
                    }}
                  />
                </div>
              )}
            </div>

            <ProjectConfigPanel
              workspaceRoot={projectLayer.workspaceRoot}
              content={projectLayer.content}
              exists={projectLayer.exists}
              forbiddenKeys={projectLayer.forbiddenKeys}
              warning={projectLayer.warning}
              status={projectLayer.status}
              onSave={handleProjectLayerSave}
            />

            <div style={{
              background: 'rgba(255,255,255,0.02)',
              border: '1px solid rgba(255,255,255,0.06)',
              borderRadius: '8px',
              padding: '16px',
              marginTop: '16px'
            }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '8px' }}>
                <Scissors size={16} color="#7c3aed" />
                <div style={{ fontWeight: 500, fontSize: '14px' }}>Screen Snip Hotkey</div>
              </div>
              <div style={{ fontSize: '12px', color: '#9090a0', marginBottom: '12px' }}>
                Accelerator to trigger screen region snipping (e.g., <code>Ctrl+Alt+S</code>). Applies as soon as you leave the field — no restart needed.
              </div>
              <div data-testid="snip-hotkey-note" style={{ fontSize: '12px', color: '#9090a0', marginBottom: '12px' }}>
                This shortcut is global: it works system-wide while Aether is running, so it can override the same keys in other apps.
              </div>
              <input
                type="text"
                value={snipHotkey}
                data-testid="snip-hotkey-input"
                onChange={(e) => setSnipHotkey(e.target.value)}
                onBlur={() => { void applySnipHotkey(snipHotkey); }}
                placeholder={defaultSnipHotkey()}
                style={{
                  width: '100%',
                  background: 'rgba(0,0,0,0.2)',
                  border: '1px solid rgba(255,255,255,0.1)',
                  borderRadius: '6px',
                  padding: '10px 12px',
                  color: '#fff',
                  fontSize: '13px'
                }}
              />
              {snipHotkeyError && (
                <div data-testid="snip-hotkey-error" style={{ fontSize: '12px', color: '#f87171', marginTop: '8px' }}>
                  {snipHotkeyError}
                </div>
              )}
              <button
                type="button"
                data-testid="snip-hotkey-reset"
                onClick={() => { void applySnipHotkey(defaultSnipHotkey()); }}
                style={{
                  marginTop: '12px',
                  background: 'rgba(255,255,255,0.06)',
                  border: '1px solid rgba(255,255,255,0.1)',
                  borderRadius: '6px',
                  padding: '8px 12px',
                  color: '#fff',
                  fontSize: '12px',
                  cursor: 'pointer'
                }}
              >
                Reset to default ({defaultSnipHotkey()})
              </button>
            </div>
          </div>
        )}

        {activeMenu === 'About' && (
          <div style={{ color: '#f0f0f5' }}>
            <h2 style={{ fontSize: '24px', marginBottom: '8px', fontWeight: 500 }}>About Aether-OS</h2>
            <p style={{ color: '#9090a0', fontSize: '14px', marginBottom: '24px' }}>
              A local-first desktop agent: a Python engine with a persistent memory, driven from this shell.
            </p>

            <div style={{ background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.05)', borderRadius: '12px', padding: '24px', marginBottom: '20px' }}>
              {/* E6-F14: the version comes from the package manifest
                  (the APP_VERSION mirror), never a literal here. */}
              <div style={{ fontSize: '15px', fontWeight: 500, marginBottom: '4px' }}>Aether-OS</div>
              <div style={{ fontSize: '13px', color: '#9090a0' }}>Version {appPackage.version}</div>
            </div>

            <div style={{ background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.05)', borderRadius: '12px', padding: '24px' }}>
              <h3 style={{ fontSize: '16px', fontWeight: 500, marginBottom: '8px', color: '#fff' }}>Local documentation</h3>
              <p style={{ color: '#9090a0', fontSize: '13px', margin: '0 0 16px 0' }}>
                The system guide ships with the project (docs/AETHER_SYSTEM_GUIDE.md) and opens with your
                default Markdown reader — nothing is fetched from the network.
              </p>
              {aboutWorkspaces.length === 0 ? (
                <div style={{ fontSize: '13px', color: '#9090a0' }}>
                  Add a workspace that contains the Aether-OS project and the guide can be opened from here.
                </div>
              ) : (
                <div style={{ display: 'flex', flexDirection: 'column', gap: '8px' }}>
                  {aboutWorkspaces.map((root) => (
                    <div key={root.path} style={{ display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap' }}>
                      <button
                        onClick={async () => {
                          // The shell's open-path seam (as the canvas's
                          // "Open externally" uses): a failure comes
                          // back as the shell's error string and is
                          // shown, never swallowed.
                          if (!window.electronAPI?.openPath) {
                            setGuideStatus('Opening local files is not available in this shell.');
                            return;
                          }
                          const err = await window.electronAPI.openPath(`${root.path}/docs/AETHER_SYSTEM_GUIDE.md`);
                          setGuideStatus(err ? `Could not open the guide: ${err}` : '');
                        }}
                        style={{ background: 'rgba(124,58,237,0.15)', color: '#a78bfa', border: '1px solid rgba(124,58,237,0.4)', padding: '9px 18px', borderRadius: '10px', cursor: 'pointer', fontWeight: 500 }}
                      >
                        Open the system guide
                      </button>
                      <span style={{ fontSize: '12px', color: '#9090a0', fontFamily: 'monospace' }}>{root.path}</span>
                    </div>
                  ))}
                </div>
              )}
              {guideStatus && <div style={{ fontSize: '13px', color: '#f87171', marginTop: '12px' }}>{guideStatus}</div>}
            </div>
          </div>
        )}

        {activeMenu === 'Voice' && (
          <div style={{ color: '#f0f0f5' }}>
            <h2 style={{ fontSize: '24px', marginBottom: '8px', fontWeight: 500 }}>Voice Tuning</h2>
            <p style={{ color: '#9090a0', fontSize: '14px', marginBottom: '24px' }}>
              Fine-tune speech detection and endpointing thresholds. Changes take effect on the next voice session.
            </p>

            <div style={{
              background: 'rgba(255,255,255,0.02)',
              border: '1px solid rgba(255,255,255,0.05)',
              borderRadius: '12px',
              padding: '24px',
              marginBottom: '20px'
            }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '16px' }}>
                <Sliders size={18} style={{ color: '#a78bfa' }} />
                <h3 style={{ fontSize: '16px', fontWeight: 500, color: '#fff', margin: 0 }}>Endpointing Thresholds</h3>
              </div>

              <div style={{ marginBottom: '24px' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '6px' }}>
                  <label htmlFor="vad-threshold-slider" style={{ fontWeight: 500, fontSize: '14px', color: '#fff' }}>
                    VAD Speech Threshold
                  </label>
                  <span data-testid="vad-threshold-value" style={{ fontSize: '13px', color: '#a78bfa', fontWeight: 600 }}>
                    {vadThreshold.toFixed(2)}
                  </span>
                </div>
                <div style={{ fontSize: '12px', color: '#9090a0', marginBottom: '10px' }}>
                  Confidence threshold for Silero VAD to register voice activity (default: 0.50). Higher values reduce false triggers from background noise; lower values catch softer speech.
                </div>
                <input
                  id="vad-threshold-slider"
                  data-testid="vad-threshold-slider"
                  type="range"
                  min="0.1"
                  max="0.9"
                  step="0.05"
                  value={vadThreshold}
                  onChange={(e) => {
                    const val = parseFloat(e.target.value);
                    setVadThreshold(val);
                    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
                      wsRef.current.send(JSON.stringify({
                        type: 'CONFIG_UPDATE_REQUEST',
                        schema_version: 1,
                        request_id: Date.now().toString(),
                        payload: { config: { vad_threshold: val } }
                      }));
                    }
                  }}
                  style={{ width: '100%', accentColor: '#7c3aed', cursor: 'pointer' }}
                />
              </div>

              <div>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '6px' }}>
                  <label htmlFor="silence-duration-slider" style={{ fontWeight: 500, fontSize: '14px', color: '#fff' }}>
                    Silence Duration
                  </label>
                  <span data-testid="silence-duration-value" style={{ fontSize: '13px', color: '#a78bfa', fontWeight: 600 }}>
                    {silenceDurationMs} ms
                  </span>
                </div>
                <div style={{ fontSize: '12px', color: '#9090a0', marginBottom: '10px' }}>
                  Trailing silence window required before the turn is marked complete (default: 700 ms). Lower values produce faster turn-taking; higher values allow natural pauses while speaking.
                </div>
                <input
                  id="silence-duration-slider"
                  data-testid="silence-duration-slider"
                  type="range"
                  min="200"
                  max="2000"
                  step="50"
                  value={silenceDurationMs}
                  onChange={(e) => {
                    const val = parseInt(e.target.value, 10);
                    setSilenceDurationMs(val);
                    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
                      wsRef.current.send(JSON.stringify({
                        type: 'CONFIG_UPDATE_REQUEST',
                        schema_version: 1,
                        request_id: Date.now().toString(),
                        payload: { config: { silence_duration_ms: val } }
                      }));
                    }
                  }}
                  style={{ width: '100%', accentColor: '#7c3aed', cursor: 'pointer' }}
                />
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
