import React, { useState, useEffect, useRef } from 'react';
import { Key, Cpu, Activity, Network, Box, Layers, Plug, Database, Sliders, Wrench, Server, Lock, Globe, CircleDollarSign, Mic, Info, CalendarDays, MessagesSquare, Monitor } from 'lucide-react';
import './chat.css';
import { resolveSnipHotkey, defaultSnipHotkey } from './snipHotkey';
import ProviderSettings from './components/Settings/ProviderSettings';
import MemoryManagement from './components/Settings/MemoryManagement';
import DiagnosticsPanel from './components/Settings/DiagnosticsPanel';
import WorkspaceIndexingPanel from './components/Settings/WorkspaceIndexingPanel';
import BrowserPolicyPanel from './components/Settings/BrowserPolicyPanel';
import UsageSettingsPanel from './components/Settings/UsageSettingsPanel';
import VaultPanel from './components/Settings/VaultPanel';
import OrchestrationPanel from './components/Settings/OrchestrationPanel';
import LoadStateRow from './components/Settings/LoadStateRow';
import ModelsPanel from './components/Settings/ModelsPanel';
import CapabilitiesPanel from './components/Settings/CapabilitiesPanel';
import ToolsPanel from './components/Settings/ToolsPanel';
import SkillsPanel from './components/Settings/SkillsPanel';
import AdvancedPanel from './components/Settings/AdvancedPanel';
import AboutPanel from './components/Settings/AboutPanel';
import VoicePanel from './components/Settings/VoicePanel';
import CalendarPanel from './components/Settings/CalendarPanel';
import ConversationPanel from './components/Settings/ConversationPanel';
import ComputerUsePanel from './components/Settings/ComputerUsePanel';

// Wave 5 (Annex C row 7): this file is the Settings shell —
// sidebar navigation, the one socket message handler, the
// shared config state and the sends. The tab bodies live in
// components/Settings/ as controlled panels (ModelsPanel,
// CapabilitiesPanel, ToolsPanel, SkillsPanel, AdvancedPanel,
// AboutPanel, VoicePanel, alongside the panels extracted
// earlier); the Agents menu stays a composition of
// DiagnosticsPanel + OrchestrationPanel here.

export default function SettingsPanel({ wsRef, safeSend = null, pendingMessages = [], reconnectCount = 0, providers = [], providersLoaded = false, isConnected = null, providerHealth = {}, activeSession = null, dailyBudget = null, budgetAlert = null, initialMenu = null, onInitialMenuConsumed = null }) {
  const [activeMenu, setActiveMenu] = useState('Providers');

  const [tools, setTools] = useState([]);
  // Epic 2: approval mode ('ask' | 'write', from TOOL_LIST_RESPONSE) and
  // the remembered scoped-approval grants (E2-09 approval cache).
  const [approvalMode, setApprovalMode] = useState('ask');
  // FUP-1 (xref S2-M1): approval binding for hook-rewritten calls
  // ('rebind' | 'carry', from the engine config). 'rebind' is the
  // strict default: an approval covers only the pre-hook arguments.
  const [approvalBindingMode, setApprovalBindingMode] = useState('rebind');
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
  // Epic 10: backup / restore + update check (Advanced tab).
  const [backupStatus, setBackupStatus] = useState('');
  const [includeSecrets, setIncludeSecrets] = useState(false);
  const [restorePath, setRestorePath] = useState('');
  const [restoreStatus, setRestoreStatus] = useState('');
  const [versionCheck, setVersionCheck] = useState('');
  const [approvalGrants, setApprovalGrants] = useState([]);

  const [skills, setSkills] = useState([]);
  
  // Advanced Config State
  const [disableFallbacks, setDisableFallbacks] = useState(false);
  // Calendar layer C5 (14 Nov 2026): the calendar switches, held
  // here like every other config value so they ride the one
  // applyConfigValues point and the one CONFIG_UPDATE flow.
  const [calendarEnabled, setCalendarEnabled] = useState(true);
  const [calendarDayBefore, setCalendarDayBefore] = useState(true);
  const [calendarLeadMinutes, setCalendarLeadMinutes] = useState(30);
  // Master build S11: the EQ switch lives here on the same rule.
  const [eqEnabled, setEqEnabled] = useState(true);
  // S19: Computer Use — all default OFF, matching the engine.
  const [computerUseEnabled, setComputerUseEnabled] = useState(false);
  const [computerUseObserve, setComputerUseObserve] = useState(false);
  const [computerUseVisionCapture, setComputerUseVisionCapture] = useState('off');
  const [fallbackChain, setFallbackChain] = useState('');
  // E6-F05: the last applied engine config — the controlled panels
  // for the Epic 1/2 handed-off surfaces (OrchestrationPanel,
  // PathRulesPanel) render from it — plus the most recent strict
  // CONFIG_UPDATE refusal to show in them, and the project config
  // layer state for the Advanced editor.
  const [engineConfig, setEngineConfig] = useState(null);
  const [configUpdateError, setConfigUpdateError] = useState('');
  // D2-05: a CONFIG_GET that fails (or never carries a config) is
  // a load ERROR on the config-gated tabs — distinct from a refused
  // UPDATE above — so they stop presenting "Loading…" forever.
  const [configLoadError, setConfigLoadError] = useState('');
  // D2-06: the refusal surface for this panel's optimistic
  // selectors (approval mode, reasoning effort, auxiliary model):
  // on a refusal the control rolls back to the last confirmed
  // value (the refs below) and the error renders in the tab where
  // the change was made.
  const [controlRefusal, setControlRefusal] = useState('');
  const confirmedApprovalModeRef = useRef('ask');
  const confirmedFallbackRef = useRef({ disable: false, chain: '' });
  const [projectLayer, setProjectLayer] = useState({
    workspaceRoot: '', content: '', exists: false,
    forbiddenKeys: [], warning: '', status: '',
  });
  // Open Point 13 (DECIDED): snip hotkey defaults to Ctrl+S
  // (Cmd+S on macOS) and is user-changeable; resolution rules
  // live in ./snipHotkey (mirror of electron/snipShortcut.cjs).
  const [snipHotkey, setSnipHotkey] = useState(() => defaultSnipHotkey());
  const [snipHotkeyError, setSnipHotkeyError] = useState('');
  // D2-21: the hotkey the main process last CONFIRMED (or the
  // config last reported) — applySnipHotkey compares against it so
  // an unchanged blur registers and persists nothing.
  const snipAppliedRef = useRef(snipHotkey);

  // Apply a snip hotkey through the main process first; only a
  // registration the main process confirms is persisted to the
  // engine config. On failure the previous working hotkey stays
  // active, the input reverts to it, and the failure is shown —
  // a hotkey that does not work is never saved.
  const applySnipHotkey = async (requested) => {
    const hotkey = (requested || '').trim() || defaultSnipHotkey();
    // D2-21: the field applies on every blur, including a
    // tab-through that changed nothing — an unchanged value has
    // nothing to register and nothing to persist.
    if (hotkey === snipAppliedRef.current) {
      setSnipHotkey(hotkey);
      setSnipHotkeyError('');
      return true;
    }
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
        actual = snipAppliedRef.current;
      }
    } else {
      // D2-21: without the main-process bridge there is no
      // confirmation, so per the rule above ("only a registration
      // the main process confirms is persisted") the apply fails:
      // the previous hotkey stays and the failure is shown — the
      // new value is never saved on spec.
      ok = false;
      actual = snipAppliedRef.current;
    }
    if (!ok) {
      setSnipHotkey(actual);
      setSnipHotkeyError(`Could not set "${hotkey}" — it may be invalid or already used by another app. "${actual}" is still your snip hotkey.`);
      return false;
    }
    snipAppliedRef.current = hotkey;
    setSnipHotkey(hotkey);
    setSnipHotkeyError('');
    // RUX-A-12: persisting the hotkey engine-side is a user action —
    // it goes through the shared safe-send helper (defined below),
    // so a change made while the engine is unreachable is QUEUED
    // and lands on reconnect instead of being discarded by the
    // readyState guard this used to carry (the field showed the new
    // value while the engine kept the old one, and the change
    // silently reverted on the next config load). The queued state
    // is shown under the field via pendingConfigValue('snip_hotkey').
    sendSettingsAction('CONFIG_UPDATE_REQUEST', { config: { snip_hotkey: hotkey } });
    return true;
  };
  // Voice Tuning State (E8-5.2)
  const [vadThreshold, setVadThreshold] = useState(0.5);
  const [silenceDurationMs, setSilenceDurationMs] = useState(700);
  // D2-08: the last slider value actually committed (sent, or
  // applied from a config reply). A drag updates only the local
  // state; the commit handlers below send once, on release, when
  // the value differs from this.
  const vadCommittedRef = useRef(0.5);
  const silenceCommittedRef = useRef(700);

  // A5: State for Reasoning Effort and Node model override
  const [reasoningEffort, setReasoningEffort] = useState(() => localStorage.getItem('reasoning_effort') || 'standard');
  const confirmedEffortRef = useRef(reasoningEffort);
  // Phase 9, B1: per-task auxiliary model overrides, task -> "provider:::model" (absent = auto)
  const [auxModels, setAuxModels] = useState({});
  const confirmedAuxModelsRef = useRef({});

  const [capabilityHistory, setCapabilityHistory] = useState([]);
  const [capabilityStatus, setCapabilityStatus] = useState(null);
  const [isDiagnosticsRunning, setIsDiagnosticsRunning] = useState(false);
  // D2-07: a diagnostics run's failure surface (no-answer timeout
  // or a result-less reply) and its 35 s no-answer timer — the
  // same rule ProviderSettings' save/test paths carry.
  const [diagnosticsError, setDiagnosticsError] = useState('');
  const diagnosticsTimerRef = useRef(null);

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

  // D2-05: the config-gated tabs' retry — clear the load error
  // and ask again (the fetch effect's reconnect/menu paths retry
  // the same request on their own schedule).
  const retryConfigLoad = () => {
    setConfigLoadError('');
    if (wsRef.current) {
      wsRef.current.send(JSON.stringify({
        type: 'CONFIG_GET_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: {}
      }));
    }
  };

  // D2-08: slider commits — fired on release (pointer up, key up,
  // blur), never per drag tick, and only when the value actually
  // moved from the last committed one.
  const commitVadThreshold = () => {
    if (vadCommittedRef.current === vadThreshold) return;
    vadCommittedRef.current = vadThreshold;
    sendSettingsAction('CONFIG_UPDATE_REQUEST', { config: { vad_threshold: vadThreshold } });
  };
  const commitSilenceDuration = () => {
    if (silenceCommittedRef.current === silenceDurationMs) return;
    silenceCommittedRef.current = silenceDurationMs;
    sendSettingsAction('CONFIG_UPDATE_REQUEST', { config: { silence_duration_ms: silenceDurationMs } });
  };

  // D2-07: the diagnostics run's no-answer timer bookkeeping.
  const clearDiagnosticsTimer = () => {
    if (diagnosticsTimerRef.current) {
      clearTimeout(diagnosticsTimerRef.current);
      diagnosticsTimerRef.current = null;
    }
  };

  // Wave 2 slice 1 (REF, the runtime/UX audit's reference pattern):
  // the Global Default switch is a user action, so it goes through
  // the shared safeSend App passes down — a switch picked while the
  // engine is unreachable is QUEUED and applies on reconnect, instead
  // of being silently discarded by the readyState guard this handler
  // used to carry (the dropdown is controlled from the engine's reply,
  // so a discarded switch looked exactly like a switch that "didn't
  // stick"). The wsRef fallback keeps the same unconditional facade
  // send (the E6-F15 discipline) for standalone mounts that are not
  // given the prop. While a switch sits in the queue, the dropdown
  // below is inert and says why — queuing a second, conflicting
  // switch on top of the pending one would only confuse the outcome.
  const sendModelDefault = (provider, model) => {
    if (safeSend) return safeSend('MODEL_SET_DEFAULT', { provider, model });
    if (wsRef.current) {
      wsRef.current.send(JSON.stringify({
        type: 'MODEL_SET_DEFAULT',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: { provider, model }
      }));
    }
    return undefined;
  };
  const pendingModelSwitch = (pendingMessages || []).find(
    (m) => m && m.type === 'MODEL_SET_DEFAULT'
  );
  const modelSwitchQueued = Boolean(pendingModelSwitch);

  // Wave 2 slice 3a (RUX-A-10/A-11/A-12/A-14/A-15): the same
  // treatment for the remaining Settings user actions that still
  // carried readyState guards — tool policy changes, approval-grant
  // revokes, the snip-hotkey persist, skill toggles and the Voice
  // Tuning sliders. sendSettingsAction is sendModelDefault generalised:
  // safeSend when App handed it down, the unconditional facade send
  // (E6-F15 discipline, same wire envelope) otherwise. The pending*
  // derivations read the REAL offline queue through pendingMessages,
  // so every affordance below clears itself the moment the queue
  // flushes on reconnect — no local flag can drift from the truth.
  const sendSettingsAction = (type, payload) => {
    if (safeSend) return safeSend(type, payload);
    if (wsRef.current) {
      // The facade's send reports its own disposition ('sent' /
      // 'queued'); slice 3b's callers (RUX-A-13) read it to resolve
      // their pending states honestly even on a standalone mount.
      return wsRef.current.send(JSON.stringify({
        type,
        schema_version: 1,
        request_id: Date.now().toString(),
        payload
      }));
    }
    return undefined;
  };
  const pendingOfType = (type) => (pendingMessages || []).filter(
    (m) => m && m.type === type
  );
  const pendingToolPolicyFor = (toolName) => pendingOfType('TOOL_POLICY_SET_REQUEST')
    .find((m) => m.payload && m.payload.tool_name === toolName);
  const pendingGrantRevokeFor = (id) => pendingOfType('APPROVAL_GRANT_REVOKE_REQUEST')
    .some((m) => m.payload && m.payload.id === id);
  const pendingSkillToggleFor = (name) => pendingOfType('SKILL_TOGGLE_REQUEST')
    .find((m) => m.payload && m.payload.name === name);
  // CONFIG_UPDATE_REQUEST entries carry { config: { key: value } };
  // the value a queued edit will land, or undefined when no queued
  // edit touches that key. Shared by the snip field (A-12), the
  // Voice sliders (A-15) and — via the pendingMessages prop — the
  // Speech dropdowns (A-22).
  const pendingConfigValue = (key) => {
    const entry = pendingOfType('CONFIG_UPDATE_REQUEST')
      .find((m) => m.payload && m.payload.config && key in m.payload.config);
    return entry ? entry.payload.config[key] : undefined;
  };
  // RUX-A-13: a diagnostics run sitting in the offline queue — the
  // Run button derives its inert/queued state from this, and the
  // ENVIRONMENT_DIAGNOSTIC_RESPONSE handler resolves the run when
  // the flushed request is answered after reconnect.
  const diagnosticsQueued = pendingOfType('ENVIRONMENT_DIAGNOSTIC_RUN_REQUEST').length > 0;

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

  // Wave 5 (Annex C row 7): the sends behind the extracted tab
  // panels' controls — the panels are controlled components, so
  // the sends live here beside the state they update and reach
  // the panels as named handlers.
  const handleModelDefaultChange = (val) => {
    const [provider, model] = val.split(':::');
    if (provider && model) sendModelDefault(provider, model);
  };

  const handleReasoningEffortChange = (val) => {
    // Phase 9, B2: persist engine-side so it actually affects model calls.
    // E6-F15: sent unconditionally — the facade
    // queues it while offline (see sendConfigUpdate).
    // D2-06: a refusal rolls this (and
    // localStorage) back to the confirmed effort.
    setControlRefusal('');
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
  };

  const handleAuxModelChange = (taskId, val) => {
    const [provider, model] = val.split(':::');
    // Phase 9, B1: optimistic update; engine confirms via AUXILIARY_MODEL_SET_RESPONSE.
    // D2-06: a refusal rolls back to the confirmed overrides.
    setControlRefusal('');
    setAuxModels(prev => {
      const next = { ...prev };
      if (!model || model === 'auto') delete next[taskId];
      else next[taskId] = val;
      return next;
    });
    // E6-F15: sent unconditionally (facade queues offline).
    if (wsRef.current) {
      wsRef.current.send(JSON.stringify({
        type: 'AUXILIARY_MODEL_SET_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: { task: taskId, provider, model: model || 'auto' }
      }));
    }
  };

  const handleAuxResetAll = () => {
    // Phase 9, B1: clear every override back to the main model.
    // E6-F15: sent unconditionally (facade queues offline).
    setControlRefusal('');
    setAuxModels({});
    if (wsRef.current) {
      wsRef.current.send(JSON.stringify({
        type: 'AUXILIARY_MODEL_SET_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: { task: 'vision', provider: '', model: 'auto' }
      }));
    }
  };

  const handleRunDiagnostics = () => {
    if (isDiagnosticsRunning || diagnosticsQueued) return;
    // RUX-A-13: through sendSettingsAction the run
    // QUEUES while the engine is unreachable instead
    // of being discarded by the readyState guard it
    // used to sit behind. The running state arms only
    // for a live send; a queued run is inert with its
    // own note (derived from the real queue above)
    // until the flush + response land the result.
    const disposition = sendSettingsAction('ENVIRONMENT_DIAGNOSTIC_RUN_REQUEST', {});
    if (disposition !== 'queued') {
      setIsDiagnosticsRunning(true);
      setDiagnosticsError('');
      // D2-07: the codebase's 35 s no-answer rule
      // (ProviderSettings' save/test paths) — a run
      // whose response never arrives must resolve,
      // not spin for the life of the mount.
      clearDiagnosticsTimer();
      diagnosticsTimerRef.current = setTimeout(() => {
        diagnosticsTimerRef.current = null;
        setIsDiagnosticsRunning(false);
        setDiagnosticsError('No answer from the engine in 35s — the run may only be queued, or the engine may be busy or offline. Try again.');
      }, 35000);
    }
  };

  const handleLogsExportBundle = () => {
    // Same request + queued honesty as the Advanced
    // tab's export button; the response handler merges
    // the desktop half into the bundle before download.
    const disposition = sendSettingsAction('DIAGNOSTICS_EXPORT_REQUEST', {});
    if (disposition === 'queued') {
      setVersionCheck('Diagnostics export queued — the file will download when the engine reconnects.');
    }
    return disposition;
  };

  const handleToolPolicyChange = (toolName, policy) => {
    sendSettingsAction('TOOL_POLICY_SET_REQUEST', { tool_name: toolName, policy });
  };

  const handleSkillToggle = (skill) => {
    // RUX-A-14: through safeSend the toggle
    // QUEUES while the engine is unreachable
    // instead of being discarded by the guard
    // this used to carry (the click visibly did
    // nothing). The label stays driven by the
    // engine's list — no fake state — and the
    // pending note beside the button carries the
    // honesty until the refreshed list lands.
    sendSettingsAction('SKILL_TOGGLE_REQUEST', { name: skill.name, enabled: skill.enabled === false });
  };

  const handleResetClick = () => {
    if (!resetArmed) { setResetArmed(true); return; }
    // E6-F15: sent unconditionally — a reset confirmed
    // while offline is queued by the facade and lands
    // on reconnect, not dropped.
    if (wsRef.current) {
      wsRef.current.send(JSON.stringify({ type: 'CONFIG_RESET_REQUEST', schema_version: 1, request_id: Date.now().toString(), payload: {} }));
    }
  };

  const handleCreateBackup = () => {
    // RUX-A-13: through sendSettingsAction the
    // request QUEUES while the engine is unreachable
    // instead of being discarded after the status was
    // already armed — 'Creating backup…' used to sit
    // on screen forever offline. A queued backup says
    // so; the BACKUP_CREATE_RESPONSE overwrites the
    // status with the real outcome when it lands.
    const disposition = sendSettingsAction('BACKUP_CREATE_REQUEST', { include_secrets: includeSecrets });
    setBackupStatus(disposition === 'queued'
      ? 'Backup queued — will run when the engine reconnects.'
      : 'Creating backup…');
  };

  const handleRestoreBackup = () => {
    if (!restorePath.trim()) { setRestoreStatus('Enter the path to a backup zip first.'); return; }
    // RUX-A-13: same honest-status treatment as
    // backup create — queue instead of discard, and
    // never leave 'Restoring…' armed for a restore
    // that has not started.
    const disposition = sendSettingsAction('BACKUP_RESTORE_REQUEST', { path: restorePath.trim() });
    setRestoreStatus(disposition === 'queued'
      ? 'Restore queued — will run when the engine reconnects.'
      : 'Restoring…');
  };

  const handleCheckUpdates = () => {
    // RUX-A-13: queue instead of discard; 'Checking…'
    // only for a check that is actually running.
    const disposition = sendSettingsAction('VERSION_CHECK_REQUEST', {});
    setVersionCheck(disposition === 'queued'
      ? 'Update check queued — will run when the engine reconnects.'
      : 'Checking…');
  };

  const handleExportDiagnostics = () => {
    // RUX-A-13: the export used to be discarded in
    // complete silence (no state at all). Queued, it
    // says where it is; sent, the response produces
    // the download + confirmation as before.
    const disposition = sendSettingsAction('DIAGNOSTICS_EXPORT_REQUEST', {});
    if (disposition === 'queued') {
      setVersionCheck('Diagnostics export queued — the file will download when the engine reconnects.');
    }
  };

  const handleDisableFallbacksChange = (val) => {
    setDisableFallbacks(val);
    // E6-F15: through the one CONFIG_UPDATE flow,
    // unconditionally (facade queues offline).
    sendConfigUpdate({ disable_fallbacks: val });
  };

  // Calendar layer C5: same one-flow rule as every settings edit.
  const handleCalendarEnabledChange = (val) => {
    setCalendarEnabled(val);
    sendConfigUpdate({ calendar_enabled: val });
  };

  const handleCalendarDayBeforeChange = (val) => {
    setCalendarDayBefore(val);
    sendConfigUpdate({ calendar_reminder_day_before: val });
  };

  // Master build S11: same one-flow rule as every settings edit.
  const handleEqEnabledChange = (val) => {
    setEqEnabled(val);
    sendConfigUpdate({ eq_enabled: val });
  };

  // S19: Computer Use fields ride the same CONFIG_UPDATE path;
  // the engine strictly validates all three (bools must be
  // bools, the vision mode is a closed set).
  const handleComputerUseEnabledChange = (val) => {
    setComputerUseEnabled(val);
    sendConfigUpdate({ computer_use_enabled: val });
  };

  const handleComputerUseObserveChange = (val) => {
    setComputerUseObserve(val);
    sendConfigUpdate({ computer_use_observe: val });
  };

  const handleComputerUseVisionCaptureChange = (val) => {
    setComputerUseVisionCapture(val);
    sendConfigUpdate({ computer_use_vision_capture: val });
  };

  const handleCalendarLeadMinutesCommit = (val) => {
    setCalendarLeadMinutes(val);
    sendConfigUpdate({ calendar_reminder_lead_minutes: val });
  };

  const handleFallbackChainCommit = () => {
    // E6-F15: through the one CONFIG_UPDATE flow,
    // unconditionally (facade queues offline).
    const parsedChain = fallbackChain.split(',').map(s => s.trim()).filter(Boolean);
    sendConfigUpdate({ fallback_chain: parsedChain });
  };

  const handleApprovalModeChange = (mode) => {
    // Optimistic: the selector reflects the choice immediately; the
    // APPROVAL_MODE_SET_RESPONSE confirms (or the next TOOL_LIST_RESPONSE
    // corrects) it. E6-F15: sent unconditionally (facade queues it
    // while offline) like every other settings edit.
    // D2-06: on a refusal the response branch rolls back to the
    // confirmed mode and names the error (controlRefusal).
    setControlRefusal('');
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

  const handleApprovalBindingChange = (mode) => {
    // FUP-1: optimistic like the approval-mode selector, and sent
    // through the one CONFIG_UPDATE flow (sendConfigUpdate queues
    // while offline and flushes on reconnect, E6-F15). The engine
    // applies it live — the next tool call reads the new value.
    setApprovalBindingMode(mode);
    sendConfigUpdate({ approval_binding_mode: mode });
  };

  // RUX-A-11: a revoke is a security-relevant user action — through
  // safeSend it QUEUES while the engine is unreachable instead of
  // being silently discarded by the guard this used to carry. The
  // grant row stays (it is still in force) and its button shows the
  // queued state until the revoke response replaces the list.
  const handleRevokeGrant = (id) => {
    sendSettingsAction('APPROVAL_GRANT_REVOKE_REQUEST', { id });
  };

  // Wave 3 slice 1 (RUX-C-01, the Settings umbrella): which engine
  // answers this panel has actually RECEIVED, keyed by data set.
  // Wave 2 slice 4 made the fetch-on-open effects refire on
  // reconnect; this is the render-side half — until a data set is
  // marked answered, its section renders a load state (loading, or
  // offline when the socket is known to be down) instead of the
  // false content Lane C catalogued: "No tools registered." before
  // the tool list lands, remembered-grants emptiness before the
  // grants reply, factory defaults presented as saved config. The
  // message handler below marks each set as its response arrives;
  // an answered-empty list is a genuine empty and renders as one.
  const [answered, setAnswered] = useState({});
  const markAnswered = (key) => {
    setAnswered(prev => (prev[key] ? prev : { ...prev, [key]: true }));
  };

  // D2-20: an armed reset must not stay armed — a stray second
  // click long after arming (and forgetting) would execute it.
  // The arm expires after 10 s, and leaving the tab disarms it.
  useEffect(() => {
    if (!resetArmed) return undefined;
    const id = setTimeout(() => setResetArmed(false), 10000);
    return () => clearTimeout(id);
  }, [resetArmed]);
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setResetArmed(false);
  }, [activeMenu]);

  // D2-07: never leave a diagnostics no-answer timer behind.
  useEffect(() => () => {
    if (diagnosticsTimerRef.current) clearTimeout(diagnosticsTimerRef.current);
  }, []);

  useEffect(() => {
    if (!wsRef.current) return;

    // Apply a full config payload (CONFIG_GET / CONFIG_UPDATE /
    // CONFIG_RESET replies all carry one) to the live controls.
    const applyConfigValues = (config) => {
      // RUX-C-01: a full config payload IS the config answer — the
      // config-driven sections stop showing their load state.
      markAnswered('config');
      // E6-F05: the handed-off panels are controlled from the same
      // applied payload, and a successful apply clears any refusal.
      setEngineConfig(config);
      setConfigUpdateError('');
      // D2-05: an applied config also clears a previous load error.
      setConfigLoadError('');
      setDisableFallbacks(config.disable_fallbacks || false);
      setCalendarEnabled(config.calendar_enabled !== false);
      setEqEnabled(config.eq_enabled !== false);
    setComputerUseEnabled(config.computer_use_enabled === true);
    setComputerUseObserve(config.computer_use_observe === true);
    setComputerUseVisionCapture(
      ['off', 'ask', 'on_demand'].includes(config.computer_use_vision_capture)
        ? config.computer_use_vision_capture
        : 'off');
      setCalendarDayBefore(config.calendar_reminder_day_before !== false);
      setCalendarLeadMinutes(
        Number.isInteger(config.calendar_reminder_lead_minutes)
          ? config.calendar_reminder_lead_minutes
          : 30
      );
      setFallbackChain((config.fallback_chain || []).join(', '));
      // D2-06: the applied payload is the confirmed state the
      // optimistic Advanced controls roll back to on a refusal.
      confirmedFallbackRef.current = {
        disable: config.disable_fallbacks || false,
        chain: (config.fallback_chain || []).join(', '),
      };
      if (config.snip_hotkey) {
        const resolvedSnip = resolveSnipHotkey(config.snip_hotkey);
        setSnipHotkey(resolvedSnip.accelerator);
        // D2-21: config is the main process's registration source —
        // what it reports is the applied hotkey.
        snipAppliedRef.current = resolvedSnip.accelerator;
        if (resolvedSnip.storedInvalid) {
          setSnipHotkeyError(`Your saved snip hotkey "${config.snip_hotkey}" is not a valid shortcut, so the default (${resolvedSnip.accelerator}) is being used.`);
        }
      }
      if (config.vad_threshold !== undefined && config.vad_threshold !== null) {
        setVadThreshold(config.vad_threshold);
        // D2-08: an applied value is by definition committed.
        vadCommittedRef.current = config.vad_threshold;
      }
      if (config.silence_duration_ms !== undefined && config.silence_duration_ms !== null) {
        setSilenceDurationMs(config.silence_duration_ms);
        silenceCommittedRef.current = config.silence_duration_ms;
      }
      if (config.approval_binding_mode) {
        setApprovalBindingMode(config.approval_binding_mode);
      }
      // Phase 9, B1/B2: engine-persisted auxiliary models and reasoning effort.
      setAuxModels(config.auxiliary_models || {});
      // D2-06: the applied overrides are the confirmed rollback base.
      confirmedAuxModelsRef.current = config.auxiliary_models || {};
      const serverEffort = config.reasoning_effort;
      if (serverEffort) {
        setReasoningEffort(serverEffort);
        localStorage.setItem('reasoning_effort', serverEffort);
        confirmedEffortRef.current = serverEffort;
      }
    };

    const handleMessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        // D2-04: the PROVIDER_VALIDATE / PROVIDER_SAVE branches
        // that used to sit here wrote only this panel's fossil
        // validation state — ProviderSettings owns those replies
        // (and renders them) since the panels were extracted.
        if (data.type === 'TOOL_LIST_RESPONSE') {
          setTools(data.payload.tools || []);
          markAnswered('tools');
          if (data.payload.approval_mode) {
            setApprovalMode(data.payload.approval_mode);
            confirmedApprovalModeRef.current = data.payload.approval_mode;
          }
        } else if (data.type === 'APPROVAL_MODE_SET_RESPONSE') {
          if (data.payload.success) {
            if (data.payload.mode) {
              setApprovalMode(data.payload.mode);
              confirmedApprovalModeRef.current = data.payload.mode;
            }
            setControlRefusal('');
          } else {
            // D2-06: a refusal restores the confirmed mode — the
            // payload carries the engine's current mode even on
            // failure; the last confirmed value is the backstop —
            // and the error renders in the Tools tab.
            const confirmedMode = data.payload.mode || confirmedApprovalModeRef.current;
            setApprovalMode(confirmedMode);
            confirmedApprovalModeRef.current = confirmedMode;
            setControlRefusal(data.payload.error || 'The approval mode change was refused.');
          }
        } else if (data.type === 'APPROVAL_GRANTS_RESPONSE') {
          setApprovalGrants(data.payload.grants || []);
          markAnswered('grants');
        } else if (data.type === 'APPROVAL_GRANT_REVOKE_RESPONSE') {
          // The revoke response carries the refreshed grants list.
          if (Array.isArray(data.payload.grants)) {
            setApprovalGrants(data.payload.grants);
            markAnswered('grants');
          }
        } else if (data.type === 'TOOL_POLICY_SET_RESPONSE') {
          if (data.payload.success) {
            setTools(prev => prev.map(t => t.function.name === data.payload.tool_name ? { ...t, policy: data.payload.policy } : t));
          }
        // D2-04: the AGENT_LIST / AGENT_UPDATE / MCP list +
        // add-remove / PLUGIN_LIST branches that used to sit here
        // fed only fossil state (or, for agent updates, an empty
        // comment) — DiagnosticsPanel owns those replies now; its
        // AGENT_UPDATE branch is the live refusal surface (D2-02).
        } else if (data.type === 'WORKSPACE_LIST_RESPONSE') {
          // E6-F14: feeds the About section's local-guide entry point.
          // Lane C grouped S4: the reply is also the About section's
          // load answer — until it lands, the guide area shows a load
          // state instead of claiming no workspace exists.
          markAnswered('workspaces');
          setAboutWorkspaces(data.payload.workspaces || []);
        } else if (data.type === 'SKILL_TOGGLE_RESPONSE') {
          if (data.payload.skills) {
            setSkills(data.payload.skills);
            markAnswered('skills');
          }
        } else if (data.type === 'SKILL_LIST_RESPONSE') {
          setSkills(data.payload.skills || []);
          markAnswered('skills');
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
        // D2-04: the MEMORY_* branches that used to sit here fed
        // only fossil episodes/facts state — MemoryManagement owns
        // those replies since the panels were extracted.
        } else if (data.type === 'ENVIRONMENT_DIAGNOSTIC_RESPONSE') {
          // D2-07: any reply resolves the run (and its no-answer
          // timer). A reply without a summary object is a failure
          // shape — surface it instead of letting an undefined
          // summary render as a silent "Status Unknown".
          clearDiagnosticsTimer();
          setIsDiagnosticsRunning(false);
          if (!data.payload || !data.payload.summary || typeof data.payload.summary !== 'object') {
            setDiagnosticsError('The diagnostics run returned no result — the engine may be busy or offline. Try again.');
          } else {
            setDiagnosticsError('');
            setCapabilityStatus(data.payload.summary);
            setCapabilityHistory(data.payload.history || []);
          }
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
            // Bundle v2 (unified logging, 5 Nov 2026): the engine
            // bundle gains the desktop half — app.log / engine.log /
            // crash-electron tails + inventory, secret-scrubbed in
            // main — merged under `desktop` before the download.
            // Without the bridge (web/dev), or if the desktop half
            // fails, the engine bundle downloads exactly as before:
            // an export must never be lost to its own enrichment.
            const engineBundle = data.payload.bundle;
            const downloadBundle = (bundle) => {
              const blob = new Blob([JSON.stringify(bundle, null, 2)], { type: 'application/json' });
              const url = URL.createObjectURL(blob);
              const a = document.createElement('a');
              a.href = url;
              a.download = `aether-diagnostics-${new Date().toISOString().slice(0, 10)}.json`;
              a.click();
              URL.revokeObjectURL(url);
              setVersionCheck('Diagnostics exported — the file contains no API keys and no memory content.');
            };
            if (window.electronAPI && typeof window.electronAPI.getDesktopDiagnostics === 'function') {
              window.electronAPI.getDesktopDiagnostics()
                .then((desktop) => downloadBundle(desktop ? { ...engineBundle, desktop } : engineBundle))
                .catch(() => downloadBundle(engineBundle));
            } else {
              downloadBundle(engineBundle);
            }
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
          if (data.payload.success === false) {
            if (data.type === 'CONFIG_UPDATE_RESPONSE') {
              // E6-F05: a refused strict-field write carries no config
              // — surface the error to the handed-off panels instead
              // of applying anything.
              setConfigUpdateError(data.payload.error || 'The update was rejected.');
              // D2-06: the Advanced tab's own controls are
              // optimistic; a refused update restores the last
              // applied fallback settings instead of leaving the
              // refused values on screen.
              setDisableFallbacks(confirmedFallbackRef.current.disable);
              setFallbackChain(confirmedFallbackRef.current.chain);
            } else {
              // D2-05: a failed config LOAD is an error state on
              // the config-gated tabs — not an endless "Loading…".
              setConfigLoadError(data.payload.error || 'Your saved settings could not be loaded.');
            }
          } else if (data.payload.config) {
            applyConfigValues(data.payload.config);
          } else if (data.type === 'CONFIG_GET_RESPONSE' && data.payload.error) {
            setConfigLoadError(data.payload.error);
          }
        } else if (data.type === 'PROJECT_CONFIG_GET_RESPONSE') {
          // RUX-C-11: success or refusal, this reply IS the project
          // layer's answer — the editor stops presenting its
          // pre-answer state ("No active workspace.", empty editor)
          // as fact from here on.
          markAnswered('projectConfig');
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
            const appliedAux = data.payload.auxiliary_models || {};
            setAuxModels(appliedAux);
            confirmedAuxModelsRef.current = appliedAux;
            setControlRefusal('');
          } else {
            // D2-06: a refusal restores the confirmed overrides
            // and surfaces the engine's own error text.
            setAuxModels({ ...confirmedAuxModelsRef.current });
            setControlRefusal(data.payload.error || 'The auxiliary model change was refused.');
          }
        } else if (data.type === 'REASONING_EFFORT_SET_RESPONSE') {
          if (data.payload.success) {
            if (data.payload.effort) {
              setReasoningEffort(data.payload.effort);
              localStorage.setItem('reasoning_effort', data.payload.effort);
              confirmedEffortRef.current = data.payload.effort;
            }
            setControlRefusal('');
          } else {
            // D2-06: a refusal restores the confirmed effort in
            // the control AND in localStorage, which the optimistic
            // onChange had already overwritten.
            setReasoningEffort(confirmedEffortRef.current);
            localStorage.setItem('reasoning_effort', confirmedEffortRef.current);
            setControlRefusal(data.payload.error || 'The reasoning effort change was refused.');
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
  // Lane C grouped S4: reconnectCount is a dep for the same reason
  // as the section fetch effect below (Wave 2 slice 4) — a visit
  // that began while the engine was down retries on reconnect
  // instead of sitting on the load state for the whole visit.
  useEffect(() => {
    if (activeMenu === 'About' && wsRef.current) {
      wsRef.current.send(JSON.stringify({
        type: 'WORKSPACE_LIST_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: {}
      }));
    }
  }, [activeMenu, wsRef, reconnectCount]);

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
      // D2-04: no AGENT_LIST_REQUEST from here — DiagnosticsPanel
      // (rendered above the orchestration controls on this menu)
      // requests the agent list itself; this panel's send was a
      // pre-extraction duplicate feeding fossil state.
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
      // D2-04: the MCP / Plugins branches that used to sit here
      // re-requested lists DiagnosticsPanel fetches itself on
      // those menus — pre-extraction duplicates, removed.
    } else if (activeMenu === 'Skills' && wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'SKILL_LIST_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: {}
      }));
      // D2-04: the Memory branch that used to sit here sent
      // MEMORY_EPISODES_REQUEST + MEMORY_GRAPH_REQUEST for fossil
      // state — MemoryManagement fetches both itself on mount.
    } else if ((activeMenu === 'Advanced' || activeMenu === 'Models' || activeMenu === 'Voice' || activeMenu === 'Calendar') && wsRef.current?.readyState === WebSocket.OPEN) {
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
    // Wave 2 slice 4 (RUX-A-16 / RUX-C-01): every branch above is
    // OPEN-guarded, so a section opened while the engine was down
    // sent nothing — and with deps of only [activeMenu, wsRef] it
    // never tried again, leaving the section empty or stale for the
    // whole visit. reconnectCount (the socket layer's recovery
    // counter) is now a dep: on reconnect the effect re-runs for the
    // section currently open and its fetch finally goes out.
  }, [activeMenu, wsRef, reconnectCount]);

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
    { keywords: 'calendar events reminders schedule itinerary upcoming', id: 'Calendar', icon: <CalendarDays size={18} />, label: 'Calendar & Reminders' },
    { keywords: 'conversation adaptive replies tone pace eq signals chat behaviour', id: 'Conversation', icon: <MessagesSquare size={18} />, label: 'Conversation' },
    { keywords: 'computer use desktop observe screen accessibility windows uia control vision capture', id: 'ComputerUse', icon: <Monitor size={18} />, label: 'Computer Use' },
    { keywords: 'capabilities diagnostics environment scheduler machine hardware cpu ram gpu disk this machine', id: 'Capabilities', icon: <Activity size={18} />, label: 'Capabilities' },
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
      
      {/* Settings Sidebar. 13 Nov fix: the nav column itself must
          scroll — it previously had no overflow at all, so at
          shorter window heights the sections below Password Vault
          (Memory & Storage, Capabilities, Voice Tuning, Advanced,
          About) were clipped by the panel and unreachable: no
          scrollbar, no wheel response (owner report). The content
          area on the right already scrolled; the nav now matches
          it, with items pinned to their natural height. */}
      <div className="chat-scroll" style={{
        width: '240px',
        display: 'flex',
        flexDirection: 'column',
        gap: '8px',
        borderRight: '1px solid var(--border-default)',
        paddingRight: '24px',
        paddingBottom: '16px',
        overflowY: 'auto',
        minHeight: 0,
        flexShrink: 0
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
          style={{ padding: '10px 14px', borderRadius: 'var(--radius-md)', background: 'var(--bg-base)', border: '1px solid var(--border-default)', color: 'var(--text-primary)', fontSize: '13px', marginBottom: '4px', flexShrink: 0 }}
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
              flexShrink: 0,
              cursor: 'pointer',
              color: activeMenu === item.id ? 'var(--text-primary)' : 'var(--text-secondary)',
              background: activeMenu === item.id ? 'var(--bg-hover)' : 'transparent',
              transition: 'all 0.2s',
              fontWeight: activeMenu === item.id ? '500' : '400'
            }}
            onMouseEnter={(e) => {
              if (activeMenu !== item.id) {
                e.currentTarget.style.background = 'var(--bg-overlay)';
                e.currentTarget.style.color = 'var(--text-primary)';
              }
            }}
            onMouseLeave={(e) => {
              if (activeMenu !== item.id) {
                e.currentTarget.style.background = 'transparent';
                e.currentTarget.style.color = 'var(--text-secondary)';
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
          <ProviderSettings providers={providers} health={providerHealth} providersLoaded={providersLoaded} />
        )}

        {activeMenu === 'Models' && (
          <ModelsPanel
            providers={providers}
            wsRef={wsRef}
            safeSend={safeSend}
            pendingMessages={pendingMessages}
            reconnectCount={reconnectCount}
            isConnected={isConnected}
            configAnswered={!!answered.config}
            configLoadError={configLoadError}
            onRetryConfigLoad={retryConfigLoad}
            modelSwitchQueued={modelSwitchQueued}
            pendingModelSwitch={pendingModelSwitch}
            onModelDefaultChange={handleModelDefaultChange}
            reasoningEffort={reasoningEffort}
            onReasoningEffortChange={handleReasoningEffortChange}
            controlRefusal={controlRefusal}
            auxModels={auxModels}
            onAuxModelChange={handleAuxModelChange}
            onAuxResetAll={handleAuxResetAll}
            engineConfig={engineConfig}
            configUpdateError={configUpdateError}
            onConfigUpdate={sendConfigUpdate}
          />
        )}

        {activeMenu === 'Agents' && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: '32px' }}>
            <DiagnosticsPanel activeMenu='Agents' />
            {/* RUX-C-01: the orchestration controls below are driven
                by the applied config; until the first config answer
                lands they would present factory defaults as the
                saved configuration, so the section says so. */}
            {!answered.config && (
              <div>
                <LoadStateRow
                  loadState={configLoadError ? 'error' : 'loading'}
                  isConnected={isConnected}
                  testId="agents-config-load-state"
                  variant="plain"
                  loadingText="Loading your saved agent settings…"
                  offlineText="Waiting for the engine — your saved agent settings will appear here once it connects."
                  errorText={configLoadError || 'Your saved agent settings could not be loaded.'}
                />
                {configLoadError && (
                  <button type="button" onClick={retryConfigLoad} style={{ marginTop: '8px', background: 'transparent', color: 'var(--accent)', border: '1px solid rgba(var(--accent-rgb),0.4)', padding: '6px 14px', borderRadius: '8px', cursor: 'pointer', fontSize: '13px' }}>
                    Retry
                  </button>
                )}
              </div>
            )}
            <OrchestrationPanel config={engineConfig} configError={configUpdateError} onConfigUpdate={sendConfigUpdate} configReady={!!answered.config} />
          </div>
        )}

        {activeMenu === 'Capabilities' && (
          <CapabilitiesPanel
            capabilityStatus={capabilityStatus}
            capabilityHistory={capabilityHistory}
            isDiagnosticsRunning={isDiagnosticsRunning}
            diagnosticsQueued={diagnosticsQueued}
            diagnosticsError={diagnosticsError}
            onRunDiagnostics={handleRunDiagnostics}
            isConnected={isConnected}
            sendSettingsAction={sendSettingsAction}
            onExportBundle={handleLogsExportBundle}
          />
        )}

        { activeMenu === 'Tools' && (
          <ToolsPanel
            approvalMode={approvalMode}
            handleApprovalModeChange={handleApprovalModeChange}
            controlRefusal={controlRefusal}
            approvalBindingMode={approvalBindingMode}
            handleApprovalBindingChange={handleApprovalBindingChange}
            approvalGrants={approvalGrants}
            grantsAnswered={!!answered.grants}
            isConnected={isConnected}
            pendingGrantRevokeFor={pendingGrantRevokeFor}
            handleRevokeGrant={handleRevokeGrant}
            configAnswered={!!answered.config}
            engineConfig={engineConfig}
            configUpdateError={configUpdateError}
            onConfigUpdate={sendConfigUpdate}
            tools={tools}
            toolsAnswered={!!answered.tools}
            pendingToolPolicyFor={pendingToolPolicyFor}
            onToolPolicyChange={handleToolPolicyChange}
          />
        )}

        {activeMenu === 'MCP' && (
          <DiagnosticsPanel activeMenu='MCP' activeSession={activeSession} />
        )}

        {activeMenu === 'Plugins' && (
          <div>
            {/* P-2: plugin action failures surface here as well as in
                DiagnosticsPanel, which renders the plugin UI. */}
            {pluginActionResult && (
              <div style={{ color: 'var(--accent)', fontSize: '13px', marginBottom: '16px' }}>{pluginActionResult}</div>
            )}
            <DiagnosticsPanel activeMenu='Plugins' />
          </div>
        )}

        {activeMenu === 'Skills' && (
          <SkillsPanel
            skills={skills}
            skillsAnswered={!!answered.skills}
            isConnected={isConnected}
            pendingSkillToggleFor={pendingSkillToggleFor}
            onSkillToggle={handleSkillToggle}
          />
        )}

        {activeMenu === 'Memory' && (
          <MemoryManagement />
        )}

        {activeMenu === 'Calendar' && (
          <CalendarPanel
            configAnswered={!!answered.config}
            isConnected={isConnected}
            configError={configUpdateError}
            calendarEnabled={calendarEnabled}
            onCalendarEnabledChange={handleCalendarEnabledChange}
            calendarDayBefore={calendarDayBefore}
            onCalendarDayBeforeChange={handleCalendarDayBeforeChange}
            calendarLeadMinutes={calendarLeadMinutes}
            onCalendarLeadMinutesCommit={handleCalendarLeadMinutesCommit}
          />
        )}

        {activeMenu === 'Conversation' && (
          <ConversationPanel
            configAnswered={!!answered.config}
            isConnected={isConnected}
            configError={configUpdateError}
            eqEnabled={eqEnabled}
            onEqEnabledChange={handleEqEnabledChange}
          />
        )}

        {activeMenu === 'ComputerUse' && (
          <ComputerUsePanel
            configAnswered={!!answered.config}
            isConnected={isConnected}
            configError={configUpdateError}
            computerUseEnabled={computerUseEnabled}
            onComputerUseEnabledChange={handleComputerUseEnabledChange}
            computerUseObserve={computerUseObserve}
            onComputerUseObserveChange={handleComputerUseObserveChange}
            visionCapture={computerUseVisionCapture}
            onVisionCaptureChange={handleComputerUseVisionCaptureChange}
            approvalMode={approvalMode}
          />
        )}

        {activeMenu === 'WorkspaceIndexing' && (
          <WorkspaceIndexingPanel wsRef={wsRef} />
        )}

        {activeMenu === 'BrowserPolicy' && (
          <BrowserPolicyPanel wsRef={wsRef} />
        )}

        {activeMenu === 'Usage' && (
          <UsageSettingsPanel wsRef={wsRef} safeSend={safeSend} pendingMessages={pendingMessages} reconnectCount={reconnectCount} sessionId={activeSession} dailyBudget={dailyBudget} budgetAlert={budgetAlert} budgetExceeded={!!budgetAlert} isConnected={isConnected} />
        )}

        {activeMenu === 'PasswordVault' && (
          <VaultPanel wsRef={wsRef} />
        )}

        {activeMenu === 'Advanced' && (
          <AdvancedPanel
            configAnswered={!!answered.config}
            isConnected={isConnected}
            resetArmed={resetArmed}
            resetDone={resetDone}
            resetError={resetError}
            onResetClick={handleResetClick}
            includeSecrets={includeSecrets}
            onIncludeSecretsChange={setIncludeSecrets}
            backupStatus={backupStatus}
            onCreateBackup={handleCreateBackup}
            restorePath={restorePath}
            onRestorePathChange={setRestorePath}
            restoreStatus={restoreStatus}
            onRestoreBackup={handleRestoreBackup}
            versionCheck={versionCheck}
            onCheckUpdates={handleCheckUpdates}
            onExportDiagnostics={handleExportDiagnostics}
            configUpdateError={configUpdateError}
            disableFallbacks={disableFallbacks}
            onDisableFallbacksChange={handleDisableFallbacksChange}
            fallbackChain={fallbackChain}
            onFallbackChainChange={setFallbackChain}
            onFallbackChainCommit={handleFallbackChainCommit}
            projectConfigReady={!!answered.projectConfig}
            projectLayer={projectLayer}
            onProjectLayerSave={handleProjectLayerSave}
            snipHotkey={snipHotkey}
            onSnipHotkeyChange={setSnipHotkey}
            onSnipHotkeyBlur={() => { void applySnipHotkey(snipHotkey); }}
            onSnipHotkeyReset={() => { void applySnipHotkey(defaultSnipHotkey()); }}
            snipHotkeyError={snipHotkeyError}
            pendingSnipValue={pendingConfigValue('snip_hotkey')}
          />
        )}

        {activeMenu === 'About' && (
          <AboutPanel
            workspaces={aboutWorkspaces}
            workspacesAnswered={!!answered.workspaces}
            isConnected={isConnected}
          />
        )}

        {activeMenu === 'Voice' && (
          <VoicePanel
            configAnswered={!!answered.config}
            configLoadError={configLoadError}
            isConnected={isConnected}
            onRetryConfigLoad={retryConfigLoad}
            vadThreshold={vadThreshold}
            setVadThreshold={setVadThreshold}
            commitVadThreshold={commitVadThreshold}
            pendingVadValue={pendingConfigValue('vad_threshold')}
            silenceDurationMs={silenceDurationMs}
            setSilenceDurationMs={setSilenceDurationMs}
            commitSilenceDuration={commitSilenceDuration}
            pendingSilenceValue={pendingConfigValue('silence_duration_ms')}
          />
        )}
      </div>
    </div>
  );
}
