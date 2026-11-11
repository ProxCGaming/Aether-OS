import React, { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import { SocketProvider, useAetherSocket } from './contexts/SocketContext';
import { useEngineMessages } from './hooks/useEngineMessages';
import { useSessionStore } from './hooks/useSessionStore';
import { useVoiceBridge } from './hooks/useVoiceBridge';
// Epic 9: the 3D graph (three.js + force-graph, ~1 MB) is the ambient
// background, not the app shell — load it lazily so first paint and
// interactivity don't wait on it. The Suspense fallback paints the
// same background colour, so the graph simply fades in when ready.
const ForceGraph3D = React.lazy(() => import('react-force-graph-3d'));
import { Settings, Trash2, X, Plus, MessageSquare, LayoutDashboard, Folder, FolderInput, Minus, Square, LogIn, Menu, Pencil, Upload, Layers } from 'lucide-react';
import DraggableChatWindow from './DraggableChatWindow';
import SettingsPanel from './SettingsPanel';
import { resolveSnipHotkey } from './snipHotkey';
import Dashboard from './Dashboard';
import Projects from './Projects';
import Onboarding from './components/Onboarding';
import StartupGate, { deriveStartupPhase } from './components/StartupGate';
import CommandPalette from './components/CommandPalette';
import ArtifactsCanvas from './components/ArtifactsCanvas';
import GlobalArtifactsView from './components/GlobalArtifactsView';
import RewindConfirmModal from './components/RewindConfirmModal';
import PendingQueueIndicator from './components/PendingQueueIndicator';
import HeldActionsPrompt from './components/HeldActionsPrompt';
import './chat.css';

// Decorative background graph (branding, not live data — see Phase 9 B3)
const initGraphData = {
  nodes: [
    { id: 'AETHER-OS', group: 1, val: 20 },
    { id: 'CORE_BRAIN', group: 2, val: 10 },
    { id: 'LOG_INIT', group: 3, val: 5 },
    { id: 'SYSTEM_STATE', group: 2, val: 8 },
    { id: 'KNOWLEDGE_BASE', group: 2, val: 12 }
  ],
  links: [
    { source: 'AETHER-OS', target: 'CORE_BRAIN' },
    { source: 'AETHER-OS', target: 'LOG_INIT' },
    { source: 'CORE_BRAIN', target: 'SYSTEM_STATE' },
    { source: 'CORE_BRAIN', target: 'KNOWLEDGE_BASE' },
    { source: 'SYSTEM_STATE', target: 'LOG_INIT' }
  ]
};

// S3-M7: the floating-window transfer state (full chat history +
// draft input) lives in Electron main-process memory, reached only
// through these IPC-backed helpers. It must NEVER go through
// localStorage again: web storage is readable by any script running
// in the renderer. Reads return the parsed state object (or null);
// a string payload is tolerated for older bridges that JSON-encoded.
async function readFloatingTransferState() {
  try {
    if (!window.electronAPI || !window.electronAPI.getFloatingTransferState) return null;
    const data = await window.electronAPI.getFloatingTransferState();
    if (!data) return null;
    return typeof data === 'string' ? JSON.parse(data) : data;
  } catch (err) {
    return null;
  }
}

async function writeFloatingTransferState(state) {
  try {
    if (window.electronAPI && window.electronAPI.setFloatingTransferState) {
      await window.electronAPI.setFloatingTransferState(state);
    }
  } catch (err) {
    // Best-effort: a failed handover leaves the floating window
    // empty rather than falling back to web storage.
  }
}

// E6-F08: the Settings section inventory the command palette
// can jump straight to. Ids and labels mirror SettingsPanel's own menu
// items (SettingsPanel.jsx `menuItems`); the palette entries run
// through App's tab state + the panel's initial-section prop.
// 'About' is last, matching its place at the end of the panel nav.
const SETTINGS_SECTIONS = [
  { id: 'Providers', label: 'API Providers' },
  { id: 'Models', label: 'Models & Tasks' },
  { id: 'Agents', label: 'Agents' },
  { id: 'Tools', label: 'Tools' },
  { id: 'MCP', label: 'MCP Servers' },
  { id: 'Plugins', label: 'Plugins' },
  { id: 'Skills', label: 'Skills' },
  { id: 'WorkspaceIndexing', label: 'Workspace Indexing' },
  { id: 'BrowserPolicy', label: 'Browser Agent' },
  { id: 'Usage', label: 'Usage' },
  { id: 'PasswordVault', label: 'Password Vault' },
  { id: 'Memory', label: 'Memory & Storage' },
  { id: 'Capabilities', label: 'Capabilities' },
  { id: 'Voice', label: 'Voice Tuning' },
  { id: 'Advanced', label: 'Advanced' },
  { id: 'About', label: 'About' },
];

// D3-19: how long an artifact-content request may go unanswered
// before the canvas loading state resolves itself (the load may
// still land later; the response simply clears content in). Same
// 35s window as the D3-03 list rule.
const ARTIFACT_LOAD_NO_ANSWER_MS = 35000;

function AppContent() {
  // Phase 10.3 C1: the engine-message facade lives in SocketContext, not
  // here. AppContent consumes its wsRef/isConnected; it builds no mock of
  // its own and subscribes to nothing directly.
  const { wsRef, isConnected, sendMessage, safeSend, pendingMessages, reconnectCount } = useAetherSocket();

  const [status, setStatus] = useState('Initializing');
  const [isThinking, setIsThinking] = useState(false);
  const [chatMessages, setChatMessages] = useState([]);
  // D1-A1: the engine-message handler is registered once (mount-only
  // effect), so anything it reads must come through a ref — render
  // state captured by that closure is frozen at its mount-time
  // value. isThinkingRef mirrors isThinking for the handler's
  // SESSION_GET_RESPONSE merge-back; chatMessagesRef mirrors the
  // visible list for the handler's THINKING branch (D1-A11).
  const isThinkingRef = useRef(false);
  useEffect(() => { isThinkingRef.current = isThinking; }, [isThinking]);
  const chatMessagesRef = useRef([]);
  useEffect(() => { chatMessagesRef.current = chatMessages; }, [chatMessages]);
  const [isChatDetached, setIsChatDetached] = useState(false);
  
  // Settings State
  // Phase 10.3 C1: providers is an ARRAY everywhere (the engine sends a
  // list; see PROVIDER_LIST_RESPONSE below). Children (DraggableChatWindow,
  // SettingsPanel, ProviderSettings) all expect [], and MODEL_DEFAULT_CHANGED
  // maps over it — it silently no-op'd while this was the initial {}.
  const [providers, setProviders] = useState([]);
  const [providerHealth, setProviderHealth] = useState({});
  // Epic 6: engine config (onboarding flag, reset), palette + shortcuts UI.
  const [engineConfig, setEngineConfig] = useState(null);
  // E6-F02: the onboarding completion send is issued at most once per
  // renderer session (see handleOnboardingDone).
  const onboardingCompletionSentRef = useRef(false);
  // Open Point 13: apply the stored snip hotkey (engine config
  // `snip_hotkey`) to the main process as soon as the config
  // loads, and whenever it changes — main registers its platform
  // default at startup, so this is what makes a user's own
  // choice the live global accelerator without an app restart.
  // Re-applying the live value is a no-op in main.
  const snipAccelerator = engineConfig ? resolveSnipHotkey(engineConfig.snip_hotkey).accelerator : null;
  useEffect(() => {
    if (!snipAccelerator) return;
    if (window.electronAPI?.registerSnipShortcut) {
      window.electronAPI.registerSnipShortcut(snipAccelerator);
    }
  }, [snipAccelerator]);
  const [providersLoaded, setProvidersLoaded] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  // E6-F08: a Settings section chosen outside the panel (command
  // palette) — handed to SettingsPanel as its initial section, then
  // cleared so a later plain "Go to Settings" keeps the panel's own
  // current section.
  const [settingsSection, setSettingsSection] = useState(null);
  // E6-F13: approvals awaiting a decision, mirrored into a ref so the
  // shell's global shortcut handler can act on the latest one without
  // re-registering on every message change. Entries are appended in
  // arrival order and removed as App answers them.
  const pendingApprovalsRef = useRef([]);
  const [localModels, setLocalModels] = useState([]);

  // Dashboard State (real data, not hardcoded)
  const [activeTaskIds, setActiveTaskIds] = useState(new Set());
  // RUX-C-14: null until the first PLUGIN_LIST_RESPONSE lands —
  // the Dashboard must not render an initial 0 as a real count.
  const [pluginCount, setPluginCount] = useState(null);
  // RUX-C-14: provider health starts as {} which the Dashboard
  // cannot tell from an answered-empty snapshot; this flag can.
  const [providerHealthLoaded, setProviderHealthLoaded] = useState(false);
  const [memoryMb, setMemoryMb] = useState(null);

  const params = new URLSearchParams(window.location.search);
  const isFloating = params.get('floating') === 'true';
  const initialSession = params.get('session');

  // Wave 1 slice 4: the startup gate (the owner's first-launch flow —
  // loading until the engine is healthy, THEN the setup guide, never
  // a half-live UI). The gate is active only in the Electron shell:
  // main's engine-state IPC (preload's getEngineState) is what can
  // report a definitive startup failure, and its presence is the
  // signal this window is a real shell window (the floating chat
  // window and non-Electron render contexts keep the old behaviour).
  const [engineStartState, setEngineStartState] = useState(null); // null | 'starting' | 'started' | 'failed'
  const [gatePassed, setGatePassed] = useState(false);
  const startupGateActive = !isFloating
    && typeof window !== 'undefined'
    && !!(window.electronAPI && typeof window.electronAPI.getEngineState === 'function');
  // Ready = the exact signals the onboarding overlay's mount already
  // waits for (App.jsx's onboarding gate below): socket connected,
  // engine config arrived, provider list arrived. Once true it is
  // LATCHED — a later mid-session drop must never bring the gate back.
  const engineReady = isConnected && engineConfig !== null && providersLoaded;
  useEffect(() => {
    if (engineReady) setGatePassed(true);
  }, [engineReady]);
  useEffect(() => {
    if (!startupGateActive) return undefined;
    let cancelled = false;
    const apply = (s) => {
      if (s && typeof s.state === 'string') setEngineStartState(s.state);
    };
    window.electronAPI.getEngineState()
      .then((s) => { if (!cancelled) apply(s); })
      .catch(() => {});
    let unsub = null;
    if (typeof window.electronAPI.onEngineState === 'function') {
      unsub = window.electronAPI.onEngineState(apply);
    }
    return () => {
      cancelled = true;
      if (typeof unsub === 'function') unsub();
    };
  }, [startupGateActive]);
  const handleGateRetry = useCallback(() => {
    // Back to STARTING immediately; main's own broadcast confirms the
    // outcome when the retried start settles.
    setEngineStartState('starting');
    if (window.electronAPI && typeof window.electronAPI.retryEngineStart === 'function') {
      Promise.resolve(window.electronAPI.retryEngineStart()).catch(() => {});
    }
  }, []);
  const handleGateOpenLog = useCallback(() => {
    if (window.electronAPI && typeof window.electronAPI.openEngineLog === 'function') {
      Promise.resolve(window.electronAPI.openEngineLog()).catch(() => {});
    }
  }, []);

  // Epic 8, E8-1.1a: the active session's rewind manifest — the LAST
  // task's recorded points (label, checkpoint, files written so far),
  // fetched via REWIND_MANIFEST_GET when a session is shown and when
  // a task starts or reaches a terminal state. Null = no points to
  // surface (the step menus stay hidden).
  const [rewindManifest, setRewindManifest] = useState(null);
  const requestRewindManifest = useCallback((sessionId) => {
    if (!sessionId || !wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
    wsRef.current.send(JSON.stringify({
      type: 'REWIND_MANIFEST_GET_REQUEST',
      schema_version: 1,
      request_id: `rewind_${Date.now()}`,
      payload: { session_id: sessionId },
    }));
  }, [wsRef]);
  // Wave 2 slice 4 (RUX-A-17): requestRewindManifest is OPEN-guarded,
  // so a session shown while the engine was down never got its
  // manifest — and without one the rewind/fork step menus stay hidden
  // for that session even after the engine returns. Re-request it for
  // the active session on every reconnect.
  useEffect(() => {
    if (reconnectCount > 0 && activeSessionRef.current) {
      requestRewindManifest(activeSessionRef.current);
    }
    // activeSessionRef is a stable ref owned by the session store
    // (created below); it cannot be listed here without a TDZ error.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reconnectCount, requestRewindManifest]);
  // Epic 8, E8-1.1b: rewind execution state. rewindConfirm holds the
  // point awaiting confirmation ({entry, filesNotReverted}) for the
  // RewindConfirmModal; rewindUndo holds the staged rewind's undo
  // window ({sessionId, expiresAt}) for the toast in the chat window.
  // The engine prunes on its own timer at expiry — the local timeout
  // only clears the display and refetches the (now pruned) manifest.
  const [rewindConfirm, setRewindConfirm] = useState(null);
  const [rewindUndo, setRewindUndo] = useState(null);
  const rewindUndoTimerRef = useRef(null);
  const clearRewindUndo = useCallback(() => {
    if (rewindUndoTimerRef.current) {
      clearTimeout(rewindUndoTimerRef.current);
      rewindUndoTimerRef.current = null;
    }
    setRewindUndo(null);
  }, []);
  const handleRewindRequest = useCallback((entry) => {
    // Files written after the chosen point, as the engine computes
    // them for the response (this is the modal's pre-confirmation
    // copy): the write EVENTS recorded after the point — the only
    // record that sees a file written before the point and AGAIN
    // after it (N01) — unioned with the trail's final cumulative
    // list minus the point's own, which covers points recorded
    // before write events existed.
    if (!rewindManifest || !entry) return;
    const entries = Array.isArray(rewindManifest.entries) ? rewindManifest.entries : [];
    const finalEntry = entries[entries.length - 1];
    const atPoint = new Set(Array.isArray(entry.files_written) ? entry.files_written : []);
    const seen = new Set();
    const filesNotReverted = [];
    const addPath = (path) => {
      if (!seen.has(path)) {
        seen.add(path);
        filesNotReverted.push(path);
      }
    };
    entries
      .filter((e) => e.step_index > entry.step_index)
      .forEach((e) => (Array.isArray(e.write_events) ? e.write_events : []).forEach(addPath));
    if (finalEntry) {
      (finalEntry.files_written || []).filter((path) => !atPoint.has(path)).forEach(addPath);
    }
    setRewindConfirm({ entry, filesNotReverted });
  }, [rewindManifest]);
  const handleRewindConfirm = useCallback(() => {
    if (!rewindConfirm || !rewindManifest) return;
    // Wave 2 slice 2 (RUX-A-03): the rewind goes out via safeSend —
    // never discarded. While 'sent', behaviour is unchanged (the
    // modal closes; TASK_REWIND_RESPONSE does the rest). While
    // 'queued', the modal must NOT close as if the rewind had
    // completed: it stays open in its queued state (the modal says
    // the rewind will be sent on reconnect), and the response
    // handler closes it when the flushed request actually lands.
    const disposition = safeSend('TASK_REWIND_REQUEST', {
      session_id: rewindManifest.session_id,
      task_id: rewindManifest.task_id,
      step_index: rewindConfirm.entry.step_index,
      checkpoint_id: rewindConfirm.entry.checkpoint_id,
    });
    if (disposition === 'queued') {
      setRewindConfirm(prev => (prev ? { ...prev, queued: true } : prev));
    } else {
      setRewindConfirm(null);
    }
  }, [rewindConfirm, rewindManifest, safeSend]);
  const handleRewindUndoClick = useCallback(() => {
    if (!rewindUndo) return;
    // Wave 2 slice 2 (RUX-A-03): the undo goes out via safeSend. A
    // click inside the toast's window used to be discarded outright
    // when the socket was down, and the window then expired — the
    // undo opportunity was gone for good. Queued, it flushes on
    // reconnect; the toast flags that state instead of pretending
    // the restore already happened.
    const disposition = safeSend('TASK_REWIND_UNDO_REQUEST', {
      session_id: rewindUndo.sessionId,
    });
    if (disposition === 'queued') {
      setRewindUndo(prev => (prev ? { ...prev, queued: true } : prev));
    }
  }, [rewindUndo, safeSend]);
  const handleRewindUndoExpire = useCallback(() => {
    // The window closed (toast countdown or the local timer): the
    // engine has pruned; drop the display and refetch the manifest.
    const sessionId = rewindUndo?.sessionId || activeSessionRef.current;
    clearRewindUndo();
    if (sessionId) requestRewindManifest(sessionId);
    // activeSessionRef: stable store-owned ref, unlistable here
    // (TDZ — the store is created below). See the reconnect effect.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rewindUndo, clearRewindUndo, requestRewindManifest]);
  // Epic 8, E8-1.2: fork the active session at a step boundary. The
  // step menu hands over the boundary it resolved (a manifest
  // step_index for the last task's steps, a message_id for older
  // ones); the engine copies the history — and, for a manifest
  // point, the checkpoint state — into a new session, and the
  // SESSION_FORK_RESPONSE handler below lands the user in it.
  const handleForkRequest = useCallback(({ message_id, step_index } = {}) => {
    const sessionId = activeSessionRef.current;
    if (!sessionId) return;
    // Wave 2 slice 2 (RUX-A-04): via safeSend — a fork clicked while
    // the socket is down is queued and lands on reconnect (the
    // SESSION_FORK_RESPONSE handler switches into the fork whenever
    // it arrives), instead of silently doing nothing at all.
    safeSend('SESSION_FORK_REQUEST', {
      session_id: sessionId,
      message_id: message_id ?? null,
      step_index: step_index ?? null,
      new_title: null,
    });
    // activeSessionRef: stable store-owned ref, unlistable here
    // (TDZ — the store is created below). See the reconnect effect.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [safeSend]);
  // Wave 5, Annex C row 5 (session-store seam): the session list /
  // selection state machine lives in hooks/useSessionStore.js. It is
  // created here — after the rewind state its switch action resets
  // exists — and its names are destructured back so the rest of the
  // shell (handlers, effects, render) reads as before.
  const {
    sessions, setSessions, sessionsRef,
    planModeBySession, setPlanModeBySession,
    scratchpadBySession, setScratchpadBySession,
    pendingThinkingRef,
    sessionSearchQuery, setSessionSearchQuery,
    sessionSearchResults, setSessionSearchResults,
    sessionSearchQueryRef,
    editingSessionId,
    editingSessionTitle, setEditingSessionTitle,
    activeSession, setActiveSession, activeSessionRef,
    workspaces, setWorkspaces,
    sessionListView,
    sessionSelectMode, setSessionSelectMode,
    selectedSessionIds, setSelectedSessionIds,
    moveMenuFor, setMoveMenuFor,
    moveStatus, setMoveStatus, moveBatchRef,
    pendingSessionSwitchRef, pendingSessionSwitchIdRef,
    sessionSwitchPendingId, setSessionSwitchPendingId,
    sessionCacheRef, activeTaskSessionRef,
    sendSessionListRequest, requestWorkspaceList,
    activeWorkspaceRow, workspaceLabelFor,
    handleSessionSwitch, handleNewChat, handleDeleteSession,
    handleStartRenameSession, handleCancelRenameSession,
    handleCommitRenameSession,
    handleSessionListViewChange, toggleSessionSelected,
    startSessionMoves,
  } = useSessionStore({
    wsRef, safeSend, initialSession, pendingApprovalsRef,
    setChatMessages, setRewindManifest, setRewindConfirm,
    clearRewindUndo, rewindUndo,
  });

  const [activeTab, setActiveTab] = useState('Chat'); // 'Chat', 'Dashboard', 'Artifacts', 'Projects', 'Settings'
  const [initialInput, setInitialInput] = useState('');
  const [isSidebarCollapsed, setIsSidebarCollapsed] = useState(false);
  const [slashCommands, setSlashCommands] = useState([]);
  const [importStatus, setImportStatus] = useState(null);
  const [exportStatus, setExportStatus] = useState(null);
  // Epic 8, E8-2.1: Artifacts canvas state (auto-derived, per-session, view-only)
  const [isCanvasOpen, setIsCanvasOpen] = useState(false);
  const [canvasArtifacts, setCanvasArtifacts] = useState([]);
  const [selectedArtifactId, setSelectedArtifactId] = useState(null);
  const [activeArtifactContent, setActiveArtifactContent] = useState(null);
  // Epic 8, E8-2.2: latest dev-server probe result for the canvas
  // Preview tab (DEV_SERVER_DETECT_RESPONSE payload, or null).
  const [devServerInfo, setDevServerInfo] = useState(null);

  // D3-19: the artifact-content loading lifecycle behind
  // ArtifactsCanvas's isLoading prop. A load starts when an
  // ARTIFACT_GET_REQUEST goes out for the selected artifact and ends
  // when the ARTIFACT_GET_RESPONSE lands, when the selection/session
  // context changes underneath it, or when the engine never
  // answers — the no-answer window mirrors the D3-03
  // LIST_NO_ANSWER_MS rule (MediaLibrary / GlobalArtifactsView,
  // after Onboarding's SAVE_NO_ANSWER_MS), so the loading state can
  // never stick for a whole visit.
  const [artifactContentLoading, setArtifactContentLoading] = useState(false);
  const artifactLoadTimerRef = useRef(null);
  const artifactLoadTargetRef = useRef(null);
  const markArtifactLoadStarted = useCallback((artifactId) => {
    artifactLoadTargetRef.current = artifactId || null;
    if (artifactLoadTimerRef.current) clearTimeout(artifactLoadTimerRef.current);
    setArtifactContentLoading(true);
    artifactLoadTimerRef.current = setTimeout(() => {
      artifactLoadTimerRef.current = null;
      // Only the load this timer was armed for may expire itself; a
      // newer selection re-arms (and re-targets) instead.
      if (artifactLoadTargetRef.current === (artifactId || null)) {
        artifactLoadTargetRef.current = null;
        setArtifactContentLoading(false);
      }
    }, ARTIFACT_LOAD_NO_ANSWER_MS);
  }, []);
  const markArtifactLoadFinished = useCallback(() => {
    if (artifactLoadTimerRef.current) {
      clearTimeout(artifactLoadTimerRef.current);
      artifactLoadTimerRef.current = null;
    }
    artifactLoadTargetRef.current = null;
    setArtifactContentLoading(false);
  }, []);
  const importFileInputRef = useRef(null);
  // Epic 8, E8-A8.1a: cost ledger surfaces. costTasks is the
  // popover's per-task/per-model shape, built from ledger rows
  // (USAGE_LEDGER_QUERY_RESPONSE) and kept live by TASK_COST_UPDATE
  // pushes; the header total is their sum, so it always reconciles
  // with what the popover shows.
  const [costTasks, setCostTasks] = useState([]);
  const [lastCostTaskId, setLastCostTaskId] = useState(null);
  const sessionCostUsd = costTasks.reduce((sum, t) => sum + (t.cost_usd || 0), 0);
  const runningCostTaskIds = isThinking && lastCostTaskId ? [lastCostTaskId] : [];
  // Epic 8, E8-A8.1b: context breakdown and daily budget alerting.
  const [contextBreakdown, setContextBreakdown] = useState(null);
  const [dailyBudget, setDailyBudget] = useState(null);
  const [budgetAlert, setBudgetAlert] = useState(null);

  // Wave 5, Annex C row 5 (voice bridge): the voice state-machine
  // glue lives in hooks/useVoiceBridge.js — voice session state,
  // RTC signalling, dictation and read-aloud. The names are
  // destructured back so the message handler, the disconnect
  // effect and the render read as before.
  const {
    voiceState, voiceAudioChunk, voicePlaybackCut,
    voiceRtcAnswer, voiceRtcRemoteCandidate,
    dictationState, readAloudState,
    voiceStateRef, voiceChunkSeqRef, voiceRtcSeqRef,
    readAloudAudioCtxRef, readAloudSourcesRef, readAloudPlayHeadRef,
    applyVoiceState, setVoiceAudioChunk, setVoicePlaybackCut,
    setVoiceRtcAnswer, setVoiceRtcRemoteCandidate,
    setDictationState, setReadAloudState,
    commitVoiceAssistant, stopAppReadAloud,
    handleVoiceStart, handleVoiceStop, handleVoiceDismissError,
    handleVoiceRtcOffer, handleVoiceRtcIceCandidate,
    handleVoicePlaybackProgress,
    handleDictationStart, handleDictationChunk, handleDictationStop,
    handleReadAloudStart, handleReadAloudStop,
  } = useVoiceBridge({ wsRef, activeSessionRef, activeSession, setChatMessages });

  // Takes the session explicitly (or reads the ref) because the
  // engine-message handler is registered once and must not close
  // over render-scoped state — the requestRewindManifest pattern.
  const requestCostBreakdown = useCallback((sessionId) => {
    const target = sessionId || activeSessionRef.current;
    if (target && wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'USAGE_LEDGER_QUERY_REQUEST',
        schema_version: 1,
        request_id: `session_cost_${target}_${Date.now()}`,
        payload: { session_id: target }
      }));
    }
  }, [wsRef, activeSessionRef]);

  const requestContextBreakdown = useCallback((sessionId, model) => {
    const target = sessionId || activeSessionRef.current;
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'CONTEXT_BREAKDOWN_REQUEST',
        schema_version: 1,
        request_id: `context_breakdown_${target || 'none'}_${Date.now()}`,
        payload: {
          session_id: target || '',
          model: model || null,
        }
      }));
    }
  }, [wsRef, activeSessionRef]);

  // Epic 8, E8-6.1: dedicated-profile agent browser session state.
  // Keyed by CHAT session id (the desktop's keying model). The
  // engine's browser sessions have their own UUIDs — learned from
  // its events/responses into browserSessionIdsRef — so control
  // messages carry the chat id in chat_session_id (the engine
  // resolves the binding) AND the exact browser session id once
  // known. (Before review finding M04 was fixed, this sent the
  // chat id AS session_id, which never matched a browser session
  // on the engine, and inbound state was keyed by the browser
  // UUID, which never matched the chat lookup — control was a
  // dead path in both directions.)
  const [browserSessions, setBrowserSessions] = useState({});
  const browserSessionIdsRef = useRef({});
  const handleBrowserControl = useCallback((action) => {
    // Wave 2 slice 2 (RUX-A-06): via safeSend — pause/resume/stop for
    // the agent's browser session is the user's emergency control;
    // the old early return made it silently do nothing exactly when
    // the connection was already in trouble. Queued, the control
    // flushes on reconnect and the BROWSER_TASK_PAUSE_RESPONSE
    // handler updates the card whenever it lands.
    const chatSessionId = activeSessionRef.current;
    const payload = {
      chat_session_id: chatSessionId,
      action: action,
    };
    const browserSessionId = chatSessionId ? browserSessionIdsRef.current[chatSessionId] : null;
    if (browserSessionId) payload.session_id = browserSessionId;
    safeSend('BROWSER_TASK_PAUSE_REQUEST', payload);
  }, [safeSend, activeSessionRef]);
  
  // S3-M7: purge the legacy localStorage copy of the transfer state
  // left behind by older builds, so the plaintext chat history does
  // not linger in web storage after the move to main-process memory.
  useEffect(() => {
    try {
      localStorage.removeItem('floating_transfer_state');
    } catch (e) {}
  }, []);

  useEffect(() => {
    if (!isFloating) return undefined;
    let cancelled = false;
    (async () => {
      const parsed = await readFloatingTransferState();
      if (cancelled || !parsed) return;
      if (parsed.messages) setChatMessages(parsed.messages);
      if (parsed.input) setInitialInput(parsed.input);
      // We intentionally don't clear it: the state lives in the main
      // process, so refreshing the floating window re-reads it and
      // keeps its context. Main clears it when the floating window
      // closes for real.
    })();
    return () => { cancelled = true; };
  }, [isFloating]);

  // Sync edits back to the main-process transfer state if messages
  // change in the floating window (read-modify-write, preserving
  // the draft input the floating window was handed).
  useEffect(() => {
    if (!isFloating || chatMessages.length === 0) return undefined;
    let cancelled = false;
    (async () => {
      const current = (await readFloatingTransferState()) || {};
      if (cancelled) return;
      await writeFloatingTransferState({ ...current, messages: chatMessages });
    })();
    return () => { cancelled = true; };
  }, [isFloating, chatMessages]);

  const graphRef = useRef();


  // Shell subscriptions that shared the handler's registration
  // effect: chat detach/attach notifications and the Dashboard
  // memory poll. They ride separate channels from the engine
  // socket, so they keep their own mount effect.
  useEffect(() => {
    const unsubscribers = [];
    if (window.electronAPI && window.electronAPI.onChatDetached) {
      unsubscribers.push(window.electronAPI.onChatDetached(() => setIsChatDetached(true)));
    }

    if (window.electronAPI && window.electronAPI.onChatAttached) {
      unsubscribers.push(window.electronAPI.onChatAttached(async (attachedState) => {
        setIsChatDetached(false);
        // S3-M7: main hands the final transfer snapshot over WITH
        // this event (its store is already cleared at that point);
        // fall back to reading the store when no snapshot rides
        // along (e.g. a bridge that does not forward it).
        const parsed = (attachedState && typeof attachedState === 'object')
          ? attachedState
          : await readFloatingTransferState();
        if (parsed && parsed.messages) setChatMessages(parsed.messages);
      }));
    }

    // Poll Electron process memory usage every 5s for Dashboard
    const memoryPoll = setInterval(async () => {
      if (window.electronAPI?.getMemoryUsage) {
        try {
          const mb = await window.electronAPI.getMemoryUsage();
          setMemoryMb(mb);
        } catch (e) {
          // ignore
        }
      }
    }, 5000);
    // Initial fetch
    if (window.electronAPI?.getMemoryUsage) {
      window.electronAPI.getMemoryUsage().then(mb => setMemoryMb(mb)).catch(() => {});
    }

    // Phase 9, B3: removed the 30 random "ambient" nodes that simulated a vast
    // network. The background graph is decorative branding, not live data —
    // it must not fabricate the appearance of a populated knowledge graph.
    return () => {
      clearInterval(memoryPoll);
      unsubscribers.forEach(unsub => {
        if (typeof unsub === 'function') unsub();
      });
    };
  }, []);

  // Wave 5, Annex C row 5 (message-router seam): the once-registered
  // engine-message dispatch lives in hooks/useEngineMessages.js. It
  // is handed the refs, setters and stable callbacks it writes
  // through — the refs discipline (D1-A1) is unchanged, and the two
  // D3-19 callbacks report the artifact-content load events only the
  // handler can see.
  useEngineMessages({
    wsRef, isFloating, initialSession,
    activeSessionRef, activeTaskSessionRef, browserSessionIdsRef,
    chatMessagesRef, isThinkingRef, moveBatchRef, pendingApprovalsRef,
    pendingSessionSwitchIdRef, pendingSessionSwitchRef, pendingThinkingRef,
    readAloudAudioCtxRef, readAloudPlayHeadRef, readAloudSourcesRef,
    rewindUndoTimerRef, sessionCacheRef, sessionSearchQueryRef, sessionsRef,
    voiceChunkSeqRef, voiceRtcSeqRef, voiceStateRef,
    setActiveArtifactContent, setActiveSession, setActiveTaskIds,
    setBrowserSessions, setBudgetAlert, setCanvasArtifacts, setChatMessages,
    setContextBreakdown, setCostTasks, setDailyBudget, setDevServerInfo,
    setDictationState, setEngineConfig, setExportStatus, setImportStatus,
    setIsThinking, setLastCostTaskId, setLocalModels, setMoveStatus,
    setPlanModeBySession, setPluginCount, setProviderHealth,
    setProviderHealthLoaded, setProviders, setProvidersLoaded,
    setReadAloudState, setRewindConfirm, setRewindManifest, setRewindUndo,
    setScratchpadBySession, setSelectedArtifactId, setSelectedSessionIds,
    setSessionSearchResults, setSessionSelectMode, setSessionSwitchPendingId,
    setSessions, setSlashCommands, setStatus, setVoiceAudioChunk,
    setVoicePlaybackCut, setVoiceRtcAnswer, setVoiceRtcRemoteCandidate,
    setWorkspaces,
    applyVoiceState, clearRewindUndo, commitVoiceAssistant,
    handleRewindUndoExpire, handleSessionSwitch, requestContextBreakdown,
    requestCostBreakdown, requestRewindManifest, requestWorkspaceList,
    sendSessionListRequest, stopAppReadAloud,
    onArtifactLoadStart: markArtifactLoadStarted,
    onArtifactLoadEnd: markArtifactLoadFinished,
  });

  // Phase 10.3 C1: connection transitions come from the facade's
  // isConnected (driven by its _ws_status listener and getWsStatus), which
  // replaces the _ws_status branch of App's old direct subscription. On
  // connect, (re-)request the lists the UI needs; on disconnect, stop any
  // spinner and surface the drop in chat.
  const wasConnectedRef = useRef(false);
  useEffect(() => {
    if (isConnected && !wasConnectedRef.current) {
      setStatus('Online');
      if (wsRef.current) {
        sendSessionListRequest();
        requestWorkspaceList();
        wsRef.current.send(JSON.stringify({ type: 'CONFIG_GET_REQUEST', schema_version: 1, request_id: Date.now().toString(), payload: {} }));
        wsRef.current.send(JSON.stringify({ type: 'PROVIDER_LIST_REQUEST', schema_version: 1, request_id: Date.now().toString() }));
        wsRef.current.send(JSON.stringify({ type: 'LOCAL_MODEL_LIST_REQUEST', schema_version: 1, request_id: Date.now().toString() }));
        wsRef.current.send(JSON.stringify({ type: 'PLUGIN_LIST_REQUEST', schema_version: 1, request_id: Date.now().toString(), payload: {} }));
        wsRef.current.send(JSON.stringify({ type: 'SLASH_COMMAND_LIST_REQUEST', schema_version: 1, request_id: Date.now().toString(), payload: {} }));
      }
    } else if (!isConnected && wasConnectedRef.current) {
      setStatus('Offline / Engine Disconnected');
      // If the engine crashes or drops connection, forcefully terminate any pending tasks so the UI doesn't hang spinning forever.
      setIsThinking(false);
      // D1-A5 note: undecided approvals are deliberately NOT
      // cleared here. A disconnect does not make an approval dead —
      // the shipped RUX-A-05 contract queues an offline decision
      // through safeSend instead of dropping it. Approvals are
      // cleared where their task is known to have ended:
      // TASK_COMPLETED / TASK_FAILED / TASK_CANCELLED, session
      // create, and session switch.
      setChatMessages(prev => {
        let newMessages = [...prev];
        const lastAgentIdx = newMessages.findLastIndex(m => m.role === 'agent' && !m.isFinal);
        if (lastAgentIdx !== -1) {
          const last = newMessages[lastAgentIdx];
          newMessages[lastAgentIdx] = {
            ...last,
            content: last.content ? `${last.content}\n\n**Error**: Connection lost (Engine crashed).` : "**Error**: Connection lost (Engine crashed).",
            isFinal: true,
            hasError: true
          };
          return newMessages;
        }
        return prev;
      });
      // Lane C grouped S4: a drop DURING a voice session, dictation
      // or read-aloud left each surface looking live over a dead
      // engine session (starting one while offline was already
      // honest). The engine ends its voice pipelines on disconnect,
      // so flip each live voice state to its error presentation —
      // the same finalise-don't-fake treatment as the pending
      // message above. Nothing auto-resumes on reconnect; the user
      // restarts, and the error states say so.
      const voice = voiceStateRef.current;
      if (voice && (voice.status === 'starting' || voice.status === 'active')) {
        setVoiceRtcAnswer(null);
        setVoiceRtcRemoteCandidate(null);
        applyVoiceState({
          ...voice,
          status: 'error',
          error: 'Voice session ended — the engine connection was lost. Start voice again once it reconnects.',
          webrtcConfig: null,
        });
      }
      if (dictationState && (dictationState.status === 'starting' || dictationState.status === 'active')) {
        setDictationState({
          ...dictationState,
          status: 'error',
          error: 'Dictation stopped — the engine connection was lost.',
        });
      }
      if (readAloudState && (readAloudState.status === 'starting' || readAloudState.status === 'playing')) {
        stopAppReadAloud();
        setReadAloudState({
          ...readAloudState,
          status: 'error',
          error: 'Read aloud stopped — the engine connection was lost.',
        });
      }
    }
    wasConnectedRef.current = isConnected;
  }, [isConnected, wsRef, sendSessionListRequest, requestWorkspaceList, dictationState, readAloudState, applyVoiceState, stopAppReadAloud, voiceStateRef, setDictationState, setReadAloudState, setVoiceRtcAnswer, setVoiceRtcRemoteCandidate]);


  // Epic 8, E8-2.1: sync artifacts when active session changes
  useEffect(() => {
    // Epic 8, E8-2.2: a probe result belongs to the previous
    // session's project — never carry it across sessions.
    setDevServerInfo(null);
    if (activeSession && wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'ARTIFACT_LIST_REQUEST',
        schema_version: 1,
        request_id: `session_artifacts_${activeSession}_${Date.now()}`,
        payload: { session_id: activeSession, global: false }
      }));
      const savedId = localStorage.getItem(`aether_canvas_selected_${activeSession}`);
      setSelectedArtifactId(savedId || null);
      if (savedId) {
        wsRef.current.send(JSON.stringify({
          type: 'ARTIFACT_GET_REQUEST',
          schema_version: 1,
          request_id: Date.now().toString(),
          payload: { artifact_id: savedId }
        }));
        // D3-19: the restored selection's content is now in flight.
        markArtifactLoadStarted(savedId);
      } else {
        setActiveArtifactContent(null);
        // D3-19: nothing is being fetched for this session.
        markArtifactLoadFinished();
      }
    } else if (!activeSession) {
      setCanvasArtifacts([]);
      setSelectedArtifactId(null);
      setActiveArtifactContent(null);
      // D3-19: no session, no load.
      markArtifactLoadFinished();
    }
  }, [activeSession, isConnected, wsRef, markArtifactLoadStarted, markArtifactLoadFinished]);

  // Epic 8, E8-A8.1a/b: sync the cost and context surfaces when the session changes
  useEffect(() => {
    setCostTasks([]);
    setLastCostTaskId(null);
    requestCostBreakdown();
    requestContextBreakdown();
  }, [activeSession, isConnected, requestCostBreakdown, requestContextBreakdown]);

  // Wave 2 slice 2 (RUX-A-08): the canvas/artifact open sends below
  // go through safeSend. Opening the canvas or selecting an artifact
  // while the socket was down used to discard the ARTIFACT_GET /
  // DEV_SERVER_DETECT request — the canvas opened blank with no
  // loading or error state and stayed that way. Queued, the content
  // request flushes on reconnect and the ARTIFACT_GET_RESPONSE /
  // DEV_SERVER_DETECT_RESPONSE handlers fill the canvas whenever
  // they land; the selection itself is local state and stays honest.
  const handleToggleCanvas = useCallback(() => {
    // D1-A11: the side effects (selecting the first artifact,
    // persisting the selection, requesting it) ran INSIDE the
    // state updater; updaters must be pure and StrictMode
    // double-invokes them — the artifact request went out twice
    // per toggle. Compute the next value from current state, set
    // it, then act.
    const next = !isCanvasOpen;
    setIsCanvasOpen(next);
    if (next && canvasArtifacts.length > 0 && !selectedArtifactId) {
      const firstId = canvasArtifacts[0].id;
      setSelectedArtifactId(firstId);
      if (activeSession) {
        localStorage.setItem(`aether_canvas_selected_${activeSession}`, firstId);
      }
      safeSend('ARTIFACT_GET_REQUEST', { artifact_id: firstId });
      // D3-19: the first artifact's content is now in flight.
      markArtifactLoadStarted(firstId);
    }
  }, [isCanvasOpen, canvasArtifacts, selectedArtifactId, activeSession, safeSend, markArtifactLoadStarted]);

  const handleSelectArtifact = useCallback((artifactId) => {
    setSelectedArtifactId(artifactId);
    if (activeSession) {
      localStorage.setItem(`aether_canvas_selected_${activeSession}`, artifactId);
    }
    safeSend('ARTIFACT_GET_REQUEST', { artifact_id: artifactId });
    // D3-19: the newly selected artifact's content is now in flight.
    markArtifactLoadStarted(artifactId);
  }, [activeSession, safeSend, markArtifactLoadStarted]);

  // Epic 8, E8-2.2: ask the engine to probe for a dev server in the
  // artifact's project directory; the answer lands in devServerInfo
  // via DEV_SERVER_DETECT_RESPONSE.
  const handleDetectDevServer = useCallback(({ workspace_root = null, project_path = null } = {}) => {
    safeSend('DEV_SERVER_DETECT_REQUEST', { workspace_root, project_path });
  }, [safeSend]);

  const handleOpenArtifact = useCallback((pathOrTitle) => {
    setIsCanvasOpen(true);
    const cleanName = pathOrTitle ? pathOrTitle.split(/[/\\]/).pop() : '';
    const match = canvasArtifacts.find(a =>
      a.file_path === pathOrTitle ||
      a.title === pathOrTitle ||
      a.title === cleanName
    );
    if (match) {
      handleSelectArtifact(match.id);
    } else {
      safeSend('ARTIFACT_LIST_REQUEST', { session_id: activeSession, global: false });
    }
  }, [canvasArtifacts, handleSelectArtifact, activeSession, safeSend]);

  const handleSendMessage = (text, attachments = []) => {
    // E8-1.1b: sending a message while the rewind undo toast is up
    // dismisses it and commits the rewind — the engine does the
    // pruning when this START_TASK arrives; here we only drop the
    // display state.
    if (rewindUndo && rewindUndo.sessionId === activeSessionRef.current) {
      clearRewindUndo();
    }
    // One stable id for this message, shared by the rendered bubble and
    // the stored row (the engine saves it under this id), so a later
    // edit-and-resend addresses the exact message — never a content
    // lookalike. Falls back to a timestamp+random id where randomUUID
    // is unavailable.
    const messageId = (window.crypto && typeof window.crypto.randomUUID === 'function')
      ? window.crypto.randomUUID()
      : `msg-${Date.now()}-${Math.random().toString(36).slice(2, 12)}`;
    // Epic 1, E1-07: sending while a task is running steers that task
    // instead of replacing it — the bubble is flagged queued until the
    // engine answers STEERING_APPLIED, and no new agent bubble starts.
    const steering = isThinking;
    // Epic 1, E1-09 UI: plan mode rides START_TASK only (a steer is
    // mid-task guidance, not a new task). Read the per-session choice.
    const planMode = !!planModeBySession[activeSessionRef.current || '__pending'];
    setChatMessages(prev => steering
      ? [...prev, { id: messageId, role: 'user', content: text, attachments, isFinal: true, queued: true, steering: true }]
      : [
          ...prev,
          { id: messageId, role: 'user', content: text, attachments, isFinal: true },
          { role: 'agent', content: '', thoughts: [], isFinal: false }
        ]);
    setIsThinking(true);
    const outboundPacket = JSON.stringify({
      type: steering ? 'STEER_TASK' : 'START_TASK',
      schema_version: 1,
      request_id: messageId,
      payload: {
        prompt: text,
        session_id: activeSession,
        message_id: messageId,
        attachments: attachments || [],
        ...(steering ? {} : { plan_mode: planMode })
      }
    });
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(outboundPacket);
    } else if (steering) {
      // Offline steer (E1-F07): keep the bubble queued rather than
      // failing the running task's bubble — and hand the packet to
      // the facade anyway. Its send() queues while the socket is
      // down and flushes on reconnect, so the steer actually reaches
      // the engine once it is back; clearing the queued flag is then
      // STEERING_APPLIED's job, as the bubble promises. Dropping the
      // packet here (the old behaviour) lost the message silently:
      // reconnect had nothing to deliver.
      if (wsRef.current) {
        wsRef.current.send(outboundPacket);
      }
    } else {
      setTimeout(() => {
        setIsThinking(false);
        setChatMessages(prev => {
          let newMessages = [...prev];
          const lastAgentIdx = newMessages.findLastIndex(m => m.role === 'agent' && !m.isFinal);
          if (lastAgentIdx !== -1) {
            newMessages[lastAgentIdx] = { ...newMessages[lastAgentIdx], content: "**Error**: Offline mode: Backend not connected.", isFinal: true, hasError: true };
            return newMessages;
          }
          return [...newMessages, { role: 'agent', content: "**Error**: Offline mode: Backend not connected.", isFinal: true, hasError: true }];
        });
      }, 1000);
    }
  };


  // N08: exact-message scrolling for "jump to conversation". The
  // target is remembered and the scroll fires when the message
  // actually renders — a session switch plus the SESSION_GET
  // round-trip are async, so the old fixed 150 ms DOM lookup raced
  // the render and usually found nothing. The target is scoped to
  // its session so a stale jump can never scroll a different chat.
  const pendingScrollMessageRef = useRef(null);

  const scrollToMessageById = useCallback((messageId) => {
    if (!messageId) return false;
    const el = document.querySelector(`[data-message-id="${messageId}"]`);
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'center' });
      return true;
    }
    return false;
  }, []);

  useEffect(() => {
    const pending = pendingScrollMessageRef.current;
    if (!pending) return;
    if (pending.sessionId && pending.sessionId !== activeSession) return;
    if (scrollToMessageById(pending.messageId)) {
      pendingScrollMessageRef.current = null;
    }
  }, [chatMessages, activeTab, activeSession, scrollToMessageById]);

  const queueMessageScroll = useCallback((sessionId, messageId) => {
    if (!messageId) return;
    pendingScrollMessageRef.current = { sessionId, messageId };
    scrollToMessageById(messageId);
  }, [scrollToMessageById]);

  // Epic 8, E8-2.3: navigate from Global Artifacts view into session with canvas open
  const handleNavigateToArtifactSession = useCallback((artifact) => {
    if (!artifact) return;
    if (artifact.session_id && artifact.session_id !== activeSession) {
      handleSessionSwitch(artifact.session_id);
    }
    if (artifact.session_id && artifact.id) {
      localStorage.setItem(`aether_canvas_selected_${artifact.session_id}`, artifact.id);
    }
    setSelectedArtifactId(artifact.id);
    setIsCanvasOpen(true);
    if (artifact.id) {
      // RUX-A-08: via safeSend, like handleSelectArtifact above.
      safeSend('ARTIFACT_GET_REQUEST', { artifact_id: artifact.id });
      // D3-19: the navigated-to artifact's content is now in flight.
      markArtifactLoadStarted(artifact.id);
    }
    setActiveTab('Chat');

    if (artifact.message_id) {
      queueMessageScroll(artifact.session_id || activeSession, artifact.message_id);
    }
  }, [activeSession, safeSend, queueMessageScroll, markArtifactLoadStarted]); // eslint-disable-line react-hooks/exhaustive-deps

  // N08: media "jump to conversation" — a separate path from artifact
  // navigation. Media ids are NOT artifact ids: no canvas selection
  // and no ARTIFACT_GET_REQUEST, just the owning session and the
  // exact message that produced the item (recorded at registration).
  // (Plain function, not useCallback: it closes over the unmemoized
  // handleSessionSwitch, like the other session handlers here.)
  const handleNavigateToMediaSession = (item) => {
    if (!item) return;
    if (item.session_id && item.session_id !== activeSession) {
      handleSessionSwitch(item.session_id);
    }
    setActiveTab('Chat');
    if (item.message_id) {
      queueMessageScroll(item.session_id || activeSession, item.message_id);
    }
  };


  // Epic 6, E6-02: completing or skipping onboarding persists the
  // flag engine-side so the overlay appears exactly once.
  //
  // E6-F02: the send goes through the SocketContext facade's
  // sendMessage, which QUEUES while the socket is down and flushes
  // on reconnect — the old raw-socket send was guarded by an OPEN
  // check, so a Finish/Skip pressed while disconnected was dropped
  // entirely and the engine's still-false flag resurrected the
  // wizard on the next config reply. The send is issued at most
  // once per renderer session: the facade queue drains each entry
  // exactly once, the local flag set below hides the wizard, and
  // the only way onDone can fire again is a stale engine config
  // reply briefly resurrecting the wizard before the update reply
  // lands — that must not duplicate the request. A restarted
  // renderer resets the guard, which is correct: it re-reads the
  // engine flag, and the wizard legitimately returns (and can be
  // finished again) only if the flag never landed. Completion
  // also hands the user into Chat, wherever they finished from.
  const handleOnboardingDone = useCallback(() => {
    setEngineConfig(prev => (prev ? { ...prev, onboarding_completed: true } : prev));
    if (!onboardingCompletionSentRef.current) {
      onboardingCompletionSentRef.current = true;
      sendMessage('CONFIG_UPDATE_REQUEST', {
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: { config: { onboarding_completed: true } },
      });
    }
    setActiveTab('Chat');
  }, [sendMessage]);

  // Epic 6, E6-03: palette items — only entries that really act.
  const paletteItems = useMemo(() => {
    const items = [
      { id: 'act-new-chat', kind: 'action', label: 'New chat', run: () => { handleNewChat(); setActiveTab('Chat'); } },
      { id: 'act-tab-chat', kind: 'action', label: 'Go to Chat', run: () => setActiveTab('Chat') },
      { id: 'act-tab-dashboard', kind: 'action', label: 'Go to Dashboard', run: () => setActiveTab('Dashboard') },
      { id: 'act-tab-artifacts', kind: 'action', label: 'Go to Artifacts', run: () => setActiveTab('Artifacts') },
      { id: 'act-tab-projects', kind: 'action', label: 'Go to Projects', run: () => setActiveTab('Projects') },
      { id: 'act-tab-settings', kind: 'action', label: 'Go to Settings', run: () => setActiveTab('Settings') },
      { id: 'act-shortcuts', kind: 'action', label: 'Keyboard shortcuts', run: () => setShortcutsOpen(true) },
    ];
    // E6-F08: individual Settings sections are palette actions too —
    // the section list mirrors SettingsPanel's own menu inventory
    // (ids + labels), and running one jumps straight there:
    // Settings tab + that section as the panel's initial menu.
    SETTINGS_SECTIONS.forEach(({ id, label }) => {
      items.push({
        id: `settings-${id}`, kind: 'settings', label: `Settings: ${label}`, hint: 'Settings section',
        run: () => { setSettingsSection(id); setActiveTab('Settings'); },
      });
    });
    (sessions || []).forEach(sess => {
      items.push({
        id: `sess-${sess.id}`, kind: 'session',
        label: sess.title || 'Untitled chat',
        // Q10: palette session entries carry their workspace label,
        // like the search results in the sidebar list.
        hint: workspaceLabelFor(sess.workspace),
        run: () => { handleSessionSwitch(sess.id); setActiveTab('Chat'); },
      });
    });
    (Array.isArray(providers) ? providers : [])
      .filter(p => p.has_key && Array.isArray(p.models))
      .forEach(p => {
        p.models.forEach(m => {
          items.push({
            id: `model-${p.name}-${m}`, kind: 'model', label: m, hint: p.name,
            // Wave 2 slice 1 (REF): a palette model switch is a user
            // action — it goes through safeSend, so a switch picked
            // while the engine is unreachable is queued (and shown by
            // the pending indicator) instead of being silently
            // discarded by the readyState guard this used to carry.
            run: () => { safeSend('MODEL_SET_DEFAULT', { provider: p.name, model: m }); },
          });
        });
      });
    return items;
  }, [sessions, providers, wsRef, workspaces, safeSend]); // eslint-disable-line react-hooks/exhaustive-deps

  // Epic 6, E6-03: the wired shortcut set (the dialog lists exactly
  // these): Ctrl+K palette, Ctrl+N new chat, Ctrl+, Settings.
  // E6-F09: Escape closes the shortcuts dialog, as the dialog itself
  // advertises (the palette already handles its own Escape).
  // E6-F13: Ctrl+. cancels the running task (the Stop action);
  // Ctrl+Enter approves and Ctrl+Shift+Enter rejects the most recent
  // pending approval. The approval shortcuts never fire while a text
  // field has focus — approving must be deliberate, never a
  // keystroke meant for the composer or a rename box.
  useEffect(() => {
    if (isFloating) return undefined;
    const onKey = (e) => {
      const mod = e.ctrlKey || e.metaKey;
      if (e.key === 'Escape' && shortcutsOpen) {
        e.preventDefault();
        setShortcutsOpen(false);
      } else if (mod && (e.key === 'k' || e.key === 'K')) {
        e.preventDefault();
        setPaletteOpen(prev => !prev);
      } else if (mod && (e.key === 'n' || e.key === 'N')) {
        e.preventDefault();
        handleNewChat();
        setActiveTab('Chat');
      } else if (mod && e.key === ',') {
        e.preventDefault();
        setActiveTab('Settings');
      } else if (mod && e.key === '.') {
        e.preventDefault();
        handleStopTask();
      } else if (mod && e.key === 'Enter') {
        const target = e.target;
        const typing = target && (
          target.tagName === 'INPUT' || target.tagName === 'TEXTAREA'
          || target.tagName === 'SELECT' || target.isContentEditable);
        const pending = pendingApprovalsRef.current[pendingApprovalsRef.current.length - 1];
        if (!typing && pending) {
          e.preventDefault();
          const approved = !e.shiftKey;
          if (pending.kind === 'tool') {
            handleToolApproval(pending.id, approved, pending.approval_key);
          } else if (pending.kind === 'vault') {
            handleVaultFillApproval(pending.id, approved);
          } else {
            handlePluginApproval(pending.id, approved);
          }
        }
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [isFloating, shortcutsOpen]); // eslint-disable-line react-hooks/exhaustive-deps

  // Wave 2 slice 2 (RUX-A-07): delete / export / import / move below
  // all go out via safeSend. Each used to no-op silently while the
  // socket was down (move returned before even closing its menu).
  // None of them mutate local state optimistically — the session
  // list, the export download, the import status and the move tally
  // are all driven by the engine's responses — so queueing is the
  // whole fix: the action lands on reconnect and its response drives
  // the UI then, with the pending indicator covering the wait.

  const handleExportSession = (format) => {
    if (!activeSession) return;
    safeSend('SESSION_EXPORT_REQUEST', { session_id: activeSession, format });
  };

  const handleTriggerImport = () => {
    if (importFileInputRef.current) {
      importFileInputRef.current.value = '';
      importFileInputRef.current.click();
    }
  };

  const handleImportFileChange = (e) => {
    const file = e.target.files && e.target.files[0];
    if (!file) return;
    const isZip = file.name.endsWith('.zip');
    const reader = new FileReader();
    if (isZip) {
      reader.onload = (event) => {
        const raw = event.target.result;
        const b64 = typeof raw === 'string' && raw.includes(',') ? raw.split(',')[1] : raw;
        // RUX-A-07: via safeSend (see handleDeleteSession above).
        safeSend('SESSION_IMPORT_REQUEST', {
          format: 'zip',
          content: b64,
          file_path: file.path || null
        });
      };
      reader.readAsDataURL(file);
    } else {
      reader.onload = (event) => {
        // RUX-A-07: via safeSend (see handleDeleteSession above).
        safeSend('SESSION_IMPORT_REQUEST', {
          format: 'json',
          content: event.target.result,
          file_path: file.path || null
        });
      };
      reader.readAsText(file);
    }
  };

  // Wave 2 slice 2 (RUX-A-05): an approval decision is marked on its
  // chat card when it could only be queued (see handleToolApproval).
  // The card stays in place — disabled, with the decision shown as
  // queued — instead of vanishing (sent) or silently staying live
  // while its decision was thrown away (the old discard).
  const markApprovalDecisionQueued = (requestId, key, approved) => {
    setChatMessages(prev => prev.map(m =>
      (m.id === requestId || (key && m.approval_key === key))
        ? { ...m, decisionQueued: true, decisionApproved: approved }
        : m));
  };

  const handleToolApproval = (requestId, approved, approvalKey, scope, editedArgs, browserEnvelope) => {
    const key = approvalKey || requestId;
    // Wave 2 slice 2 (RUX-A-05): the decision goes out via safeSend —
    // never discarded. The envelope request_id stays the approval's
    // own id (the engine correlates on payload.approval_key, with the
    // request_id as its fallback). The pendingApprovalsRef entry is
    // removed only AFTER the decision is actually sent or queued:
    // the old order removed it before a readyState guard that then
    // threw the send away, which killed the Ctrl+Enter shortcut path
    // for that approval while its card stayed on screen undecided.
    const disposition = safeSend(
      approved ? 'TOOL_APPROVAL_GRANTED' : 'TOOL_APPROVAL_REJECTED',
      {
        approval_key: key,
        approved: approved,
        // Epic 2, E2-09: a scoped grant ("file" / "folder") rides the
        // GRANTED event; a plain approve sends no scope (= "once").
        ...(approved && scope ? { scope } : {}),
        // Epic 8, E8-1.3: approval-time argument edits ride the
        // GRANTED event as the full replacement argument set; the
        // engine re-runs the full policy pipeline on them.
        ...(approved && editedArgs ? { edited_args: editedArgs } : {}),
        // Epic 8, E8-6.2: a per-task envelope grant made at a
        // browser confirmation rides the GRANTED event; the engine
        // scopes it to the confirmed action's category + site.
        ...(approved && browserEnvelope ? { browser_envelope: browserEnvelope } : {})
      },
      requestId,
    );
    // E6-F13: decided now — no longer shortcut-actionable.
    pendingApprovalsRef.current = pendingApprovalsRef.current.filter(
      (entry) => !(entry.kind === 'tool' && entry.id === requestId));
    if (disposition === 'queued') {
      // The card stays, showing the decision as queued; thinking
      // resumes when the flushed decision actually starts the tool.
      markApprovalDecisionQueued(requestId, key, approved);
    } else {
      // Remove the approval card from chat
      setChatMessages(prev => prev.filter(m => m.id !== requestId && m.approval_key !== key));
      if (approved) setIsThinking(true);
    }
  };

  // Wave 2 slice 1 (REF): the chat-window (HUD) model switch already
  // queued via the facade; it now goes through safeSend like the
  // Settings and palette paths, so all three model-switch routes share
  // one send discipline and one pending state.
  const handleChangeModel = (provider, model) => {
    safeSend('MODEL_SET_DEFAULT', { provider, model });
  };

  const handlePluginApproval = (requestId, approved) => {
    // Wave 2 slice 2 (RUX-A-05): via safeSend, like handleToolApproval
    // above — including the ref ordering. The requestId argument is
    // load-bearing here: the engine pops the pending plugin install
    // by the decision's envelope request_id, so it must be the
    // PLUGIN_APPROVAL_REQUEST's own id, not a minted one.
    const disposition = safeSend(
      approved ? 'PLUGIN_APPROVAL_GRANTED' : 'PLUGIN_APPROVAL_REJECTED',
      {},
      requestId,
    );
    // E6-F13: decided now — no longer shortcut-actionable.
    pendingApprovalsRef.current = pendingApprovalsRef.current.filter(
      (entry) => !(entry.kind === 'plugin' && entry.id === requestId));
    if (disposition === 'queued') {
      markApprovalDecisionQueued(requestId, null, approved);
    } else {
      setChatMessages(prev => prev.filter(m => m.id !== requestId));
      if (approved) setIsThinking(true);
    }
  };

  // F2 (post-Wave-6 linkage audit): the owner's answer to a vault
  // ask-every-time fill prompt. Per contracts.py the desktop answers
  // with VAULT_FILL_APPROVAL_RESPONSE {"request_id", "approved"} —
  // the engine's handler reads BOTH from the payload, so the payload
  // carries them; the envelope id repeats the correlation id, as
  // with plugin approvals. Via safeSend like the other approval
  // decisions: queued (never dropped) while offline, with the card
  // marked queued; the engine tolerates a stale id silently (the
  // fill already failed closed on its timeout), so a late or queued
  // answer can never fill anything on its own.
  const handleVaultFillApproval = (requestId, approved) => {
    const disposition = safeSend(
      'VAULT_FILL_APPROVAL_RESPONSE',
      { request_id: requestId, approved },
      requestId,
    );
    // E6-F13: decided now — no longer shortcut-actionable.
    pendingApprovalsRef.current = pendingApprovalsRef.current.filter(
      (entry) => !(entry.kind === 'vault' && entry.id === requestId));
    if (disposition === 'queued') {
      markApprovalDecisionQueued(requestId, null, approved);
    } else {
      setChatMessages(prev => prev.filter(m => m.id !== requestId));
      if (approved) setIsThinking(true);
    }
  };

  const handleReattachChat = async () => {
    // S3-M7: read the floating window's edits from the main-process
    // store BEFORE closing it — main clears the store the moment the
    // floating window closes (the 'chat-attached' event that follows
    // carries the same snapshot and restores it again, idempotently).
    const parsed = await readFloatingTransferState();
    if (parsed && parsed.messages) setChatMessages(parsed.messages);
    if (window.electronAPI && window.electronAPI.closeFloatingChat) {
      window.electronAPI.closeFloatingChat();
    }
    setIsChatDetached(false);
    setActiveTab('Chat');
  };

  const handleStopTask = () => {
    // D1-A7 (Pass B verdict scope): like every other user action
    // this goes through safeSend, and the disposition is honoured.
    // 'sent' — the cancel reached the engine, so the task UI stands
    // down. 'queued' — the connection is down and the cancel sits in
    // the offline queue, where (CANCEL_TASK being destructive) it is
    // held for reconfirmation on reconnect rather than flushing
    // verbatim; the task is still running, so isThinking stays set
    // and the eventual TASK_CANCELLED (or the disconnect transition)
    // is what clears it — the UI must not claim "stopped" for a task
    // that is still running.
    const disposition = safeSend('CANCEL_TASK', {});
    if (disposition === 'sent') {
      setIsThinking(false);
    }
  };

  // Epic 1 UI batch: the truncate machinery shared by edit-and-resend
  // and regenerate (E1-08). sendSessionTruncate tells the engine to
  // wipe a user message + everything after it and returns the
  // occurrence of that message among identical user messages;
  // pruneMessagesFrom cuts the local list at the same row, resolved
  // defensively (rendered index, then shared id, then the
  // occurrence-aware content match) so the wrong branch is never cut.
  const sendSessionTruncate = (msg, index, list) => {
    const content = typeof msg.content === 'string' ? msg.content : '';
    let occurrence = 0;
    if (Number.isInteger(index) && index > 0) {
      occurrence = (list || [])
        .slice(0, index)
        .filter(m => m.role === 'user' && m.content === content)
        .length;
    }
    // Sent through the socket facade unconditionally: the facade
    // queues while offline and flushes in order on reconnect, so the
    // truncate is never silently dropped (a readyState check threw it
    // away whenever the socket was mid-reconnect). The occurrence
    // tells the engine WHICH identical message was addressed when the
    // id isn't a stored row id.
    const sessionId = activeSessionRef.current;
    if (sessionId && wsRef.current && wsRef.current.send) {
      wsRef.current.send(JSON.stringify({
        type: 'SESSION_TRUNCATE_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: {
          session_id: sessionId,
          message_id: msg.id || null,
          content,
          role: 'user',
          occurrence
        }
      }));
    }
    return occurrence;
  };

  const pruneMessagesFrom = (prev, msg, index, occurrence) => {
    const content = typeof msg.content === 'string' ? msg.content : '';
    let idx = -1;
    if (Number.isInteger(index) && prev[index]
        && prev[index].role === 'user' && prev[index].content === content) {
      idx = index;
    }
    if (idx === -1 && msg.id) {
      idx = prev.findIndex(m => m.id === msg.id);
    }
    if (idx === -1) {
      let seen = -1;
      idx = prev.findIndex(m => {
        if (m.role === 'user' && m.content === content) {
          seen += 1;
          return seen === occurrence;
        }
        return false;
      });
    }
    return idx === -1 ? prev : prev.slice(0, idx);
  };

  const handleEditMessage = (msg, index) => {
    // Only user messages are editable, and only with real text content.
    if (!msg || msg.role !== 'user') return;

    // 1. Cancel any running task first so ghost progress doesn't stream in
    if (isThinking) {
      handleStopTask();
    }

    // 2+3. Truncate the stored session at this message and prune the
    // local chat from it onward; the composer refill happens in the
    // chat window (it owns the input state).
    const occurrence = sendSessionTruncate(msg, index, chatMessages);
    setChatMessages(prev => pruneMessagesFrom(prev, msg, index, occurrence));
  };

  // Epic 1 UI batch (E1-08): regenerate the last exchange. Same
  // truncate as edit-and-resend, but the prompt is resent immediately
  // as a fresh task — nothing goes into the composer, and the send
  // path adds exactly one new user bubble (the normal one).
  const handleRegenerate = () => {
    if (isThinking) return;
    const lastUserIdx = chatMessages.findLastIndex(m => m.role === 'user');
    if (lastUserIdx === -1) return;
    const userMsg = chatMessages[lastUserIdx];
    const prompt = typeof userMsg.content === 'string' ? userMsg.content : '';
    if (!prompt) return;
    const occurrence = sendSessionTruncate(userMsg, lastUserIdx, chatMessages);
    setChatMessages(prev => pruneMessagesFrom(prev, userMsg, lastUserIdx, occurrence));
    handleSendMessage(prompt);
  };

  // Epic 1, E1-09 UI: plan mode is a per-session choice.
  const planModeKey = activeSession || '__pending';
  const planMode = !!planModeBySession[planModeKey];
  const handleTogglePlanMode = () => {
    setPlanModeBySession(prev => ({ ...prev, [planModeKey]: !prev[planModeKey] }));
  };


  // The move-target list shared by the per-row menu and the bulk
  // bar: every configured workspace plus "No workspace". A session's
  // current filing is disabled — moving there would be a no-op the
  // UI should not offer. For the bulk menu (currentWorkspace ===
  // undefined) nothing is disabled: a mixed selection may hold any
  // binding.
  const renderMoveTargets = (currentWorkspace, onPick) => (
    <>
      {workspaces.map(w => (
        <button
          key={w.path}
          disabled={currentWorkspace === w.path}
          onClick={() => onPick(w.path)}
          style={{
            background: 'var(--bg-overlay)',
            border: '1px solid var(--border-default)',
            color: currentWorkspace === w.path ? 'var(--text-muted)' : 'var(--text-primary)',
            borderRadius: '6px',
            padding: '5px 10px',
            cursor: currentWorkspace === w.path ? 'default' : 'pointer',
            fontSize: '12px',
            textAlign: 'left',
          }}
        >
          {w.name}
        </button>
      ))}
      <button
        disabled={currentWorkspace === null}
        onClick={() => onPick(null)}
        style={{
          background: 'var(--bg-overlay)',
          border: '1px solid var(--border-default)',
          color: currentWorkspace === null ? 'var(--text-muted)' : 'var(--text-primary)',
          borderRadius: '6px',
          padding: '5px 10px',
          cursor: currentWorkspace === null ? 'default' : 'pointer',
          fontSize: '12px',
          textAlign: 'left',
        }}
      >
        No workspace
      </button>
    </>
  );


  // Wave 1 slice 4: until the engine is ready, the gate REPLACES the
  // shell. Every hook above keeps running, so engine data keeps
  // arriving behind the gate and the ready latch flips the moment the
  // signals land; the onboarding overlay below then mounts on top of
  // the shell exactly as before for a first run.
  const startupPhase = deriveStartupPhase({
    passed: gatePassed,
    ready: engineReady,
    engineState: engineStartState,
  });
  if (startupGateActive && startupPhase !== 'ready') {
    return (
      <StartupGate
        phase={startupPhase}
        onRetry={handleGateRetry}
        onOpenLog={handleGateOpenLog}
      />
    );
  }

  return (
    <div style={{ width: '100vw', height: '100vh', display: 'flex', overflow: 'hidden', backgroundColor: isFloating ? 'transparent' : 'var(--bg-base)', flexDirection: 'column' }}>
      {/* Wave 6 Slice 5: the glass refraction filter — one hidden
          defs block for the whole app. Every glass surface's
          backdrop-filter references #aether-glass-refraction; the
          displacement bends the backdrop at the edges (the owner's
          "proper light bending around the corner"). Visual only:
          no props, no state. */}
      <svg aria-hidden="true" width="0" height="0" style={{ position: 'absolute' }}>
        <defs>
          <filter id="aether-glass-refraction">
            <feTurbulence type="fractalNoise" baseFrequency="0.008 0.012" numOctaves="2" seed="7" result="n" />
            <feDisplacementMap in="SourceGraphic" in2="n" scale="12" />
          </filter>
        </defs>
      </svg>
      
      {!isFloating && (
        <TitleBar
          isChatDetached={isChatDetached}
          onReattachChat={handleReattachChat}
          isThinking={isThinking}
          onStopTask={handleStopTask}
        />
      )}

      {/* Wave 2 slice 1: app-level pending indicator — renders only
          while safeSend-queued user actions wait in the offline queue. */}
      <PendingQueueIndicator />

      {/* Wave 2 slice 4: reconfirmation prompt for queued destructive
          actions — renders only while the provider holds any. */}
      <HeldActionsPrompt />
      
      <div style={{ flex: 1, position: 'relative', display: 'flex', zIndex: 10 }}>
        {/* Toggle Button for Sidebar */}
        {!isFloating && (
          <button
            onClick={() => setIsSidebarCollapsed(!isSidebarCollapsed)}
            aria-label="Toggle sidebar"
            style={{
              position: 'absolute',
              top: '22px',
              left: '24px',
              zIndex: 100,
              background: 'var(--bg-raised)',
              border: '1px solid var(--border-default)',
              color: 'var(--text-secondary)',
              cursor: 'pointer',
              padding: '6px',
              borderRadius: '8px',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              transition: 'transform 160ms cubic-bezier(0.23, 1, 0.32, 1), background 160ms ease-out'
            }}
            onMouseDown={(e) => e.currentTarget.style.transform = 'scale(0.95)'}
            onMouseUp={(e) => e.currentTarget.style.transform = 'scale(1)'}
            onMouseLeave={(e) => {
              e.currentTarget.style.transform = 'scale(1)';
              e.currentTarget.style.background = 'var(--bg-raised)';
            }}
            onMouseEnter={(e) => e.currentTarget.style.background = 'var(--bg-hover)'}
          >
            <Menu size={20} />
          </button>
        )}

        {/* Sidebar Command Palette (Glassmorphism) */}
        {!isFloating && (
          <div className="glass-panel" style={{
            width: '320px',
            height: '100%',
            display: 'flex',
            flexDirection: 'column',
            position: 'absolute',
            left: 0,
            top: 0,
            padding: '24px 0',
            zIndex: 20,
            transform: isSidebarCollapsed ? 'translateX(-100%)' : 'translateX(0)',
            transition: 'transform 250ms cubic-bezier(0.23, 1, 0.32, 1)'
          }}>
            <div style={{ padding: '0 24px', paddingLeft: '72px', marginBottom: '32px', display: 'flex', alignItems: 'center', gap: '12px' }}>
              <div>
                <h2 style={{ fontSize: '18px', margin: 0, color: 'var(--text-primary)' }}>AETHER-OS</h2>
                <div style={{ fontSize: '12px', color: 'var(--text-muted)', textTransform: 'uppercase', letterSpacing: '1px' }}>{status}</div>
              </div>
            </div>

            <nav style={{ display: 'flex', flexDirection: 'column', gap: '8px', padding: '0 12px' }}>
              <NavItem icon={<MessageSquare size={18} />} label="Chat" activeTab={activeTab} setActiveTab={setActiveTab} />
              <NavItem icon={<LayoutDashboard size={18} />} label="Dashboard" activeTab={activeTab} setActiveTab={setActiveTab} />
              <NavItem icon={<Layers size={18} />} label="Artifacts" activeTab={activeTab} setActiveTab={setActiveTab} />
              <NavItem icon={<Folder size={18} />} label="Projects" activeTab={activeTab} setActiveTab={setActiveTab} />
              <NavItem icon={<Settings size={18} />} label="Settings" activeTab={activeTab} setActiveTab={setActiveTab} />
            </nav>

            {/* Sessions Section */}
            <div style={{ flex: 1, marginTop: '32px', display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
              <div style={{ padding: '0 24px', marginBottom: '16px', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                <h3 style={{ fontSize: '12px', textTransform: 'uppercase', color: 'var(--text-muted)', letterSpacing: '1px', margin: 0 }}>Chats</h3>
                <div style={{ display: 'flex', gap: '6px' }}>
                  <button 
                    onClick={handleTriggerImport}
                    title="Import Session"
                    aria-label="Import Session"
                    style={{
                      background: 'var(--bg-overlay)',
                      border: 'none',
                      color: 'var(--text-secondary)',
                      borderRadius: 'var(--radius-sm)',
                      padding: '4px 8px',
                      display: 'flex',
                      alignItems: 'center',
                      gap: '4px',
                      cursor: 'pointer',
                      fontSize: '11px'
                    }}>
                    <Upload size={12} /> Import
                  </button>
                <button 
                  onClick={handleNewChat}
                  style={{
                  background: 'var(--bg-overlay)',
                  border: 'none',
                  color: 'var(--text-secondary)',
                  borderRadius: 'var(--radius-sm)',
                  padding: '4px 8px',
                  display: 'flex',
                  alignItems: 'center',
                  gap: '4px',
                  cursor: 'pointer',
                  fontSize: '11px'
                }}>
                  <Plus size={12} /> New
                </button>
              </div>
              </div>
              <input
                type="file"
                ref={importFileInputRef}
                style={{ display: 'none' }}
                accept=".json,.zip"
                onChange={handleImportFileChange}
              />
              {importStatus?.type === 'error' && (
                <div style={{ margin: '0 24px 12px', padding: '8px 10px', background: 'rgba(201, 42, 42, 0.15)', border: '1px solid rgba(201, 42, 42, 0.3)', borderRadius: 'var(--radius-sm)', color: 'var(--danger)', fontSize: '11px', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                  <span>{importStatus.message}</span>
                  <button onClick={() => setImportStatus(null)} style={{ background: 'none', border: 'none', color: 'var(--danger)', cursor: 'pointer', padding: 0 }}><X size={12} /></button>
                </div>
              )}
              {/* D1-A3: export outcomes render beside import's — the
                  export status state used to be write-only, so a
                  refused export looked exactly like a successful one
                  (no file, no message). */}
              {exportStatus && (
                <div data-testid="session-export-status" style={{ margin: '0 24px 12px', padding: '8px 10px', background: exportStatus.type === 'error' ? 'rgba(201, 42, 42, 0.15)' : 'rgba(76, 195, 138, 0.12)', border: `1px solid ${exportStatus.type === 'error' ? 'rgba(201, 42, 42, 0.3)' : 'rgba(76, 195, 138, 0.3)'}`, borderRadius: 'var(--radius-sm)', color: exportStatus.type === 'error' ? 'var(--danger)' : 'var(--success)', fontSize: '11px', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                  <span>{exportStatus.message}</span>
                  <button onClick={() => setExportStatus(null)} style={{ background: 'none', border: 'none', color: 'inherit', cursor: 'pointer', padding: 0 }}><X size={12} /></button>
                </div>
              )}
              
              {/* Epic 1 UI batch (E1-08): session search. A non-empty
                  query swaps the list for engine search results. */}
              <div style={{ padding: '0 24px', marginBottom: '12px' }}>
                <input
                  type="text"
                  className="aether-search-input"
                  placeholder="Search chats..."
                  aria-label="Search chats"
                  value={sessionSearchQuery}
                  onChange={(e) => setSessionSearchQuery(e.target.value)}
                  style={{
                    width: '100%',
                    boxSizing: 'border-box',
                    background: 'var(--bg-raised)',
                    border: '1px solid var(--border-default)',
                    borderRadius: 'var(--radius-md)',
                    padding: '7px 10px',
                    color: 'var(--text-primary)',
                    fontSize: '12px'
                  }}
                />
              </div>

              {/* Per-workspace sessions (Q5): the list files itself
                  under the active workspace; "All sessions" and
                  "No workspace" are one click away. The chips (and
                  the Select toggle for bulk moves, Q6) only exist
                  when at least one workspace is configured — with
                  none, the list is the plain unfiltered list it
                  always was. Filtering governs the LIST only; the
                  open session is never switched or closed by it. */}
              {workspaces.length > 0 && (
                <div style={{ display: 'flex', gap: '6px', padding: '0 24px', marginBottom: '12px', alignItems: 'center', flexWrap: 'wrap' }}>
                  {[
                    { view: 'workspace', label: activeWorkspaceRow ? activeWorkspaceRow.name : 'Workspace' },
                    { view: 'all', label: 'All sessions' },
                    { view: 'unbound', label: 'No workspace' },
                  ].map(chip => (
                    <button
                      key={chip.view}
                      aria-pressed={sessionListView === chip.view}
                      onClick={() => handleSessionListViewChange(chip.view)}
                      style={{
                        background: sessionListView === chip.view ? 'var(--bg-overlay)' : 'var(--bg-raised)',
                        border: `1px solid ${sessionListView === chip.view ? 'var(--accent)' : 'var(--border-default)'}`,
                        color: sessionListView === chip.view ? 'var(--text-primary)' : 'var(--text-secondary)',
                        borderRadius: '999px',
                        padding: '4px 10px',
                        cursor: 'pointer',
                        fontSize: '11px',
                      }}
                    >
                      {chip.label}
                    </button>
                  ))}
                  <button
                    aria-label="Select sessions"
                    aria-pressed={sessionSelectMode}
                    onClick={() => {
                      setSessionSelectMode(prev => !prev);
                      setSelectedSessionIds([]);
                      setMoveMenuFor(null);
                    }}
                    style={{
                      marginLeft: 'auto',
                      background: sessionSelectMode ? 'var(--bg-overlay)' : 'var(--bg-raised)',
                      border: `1px solid ${sessionSelectMode ? 'var(--accent)' : 'var(--border-default)'}`,
                      color: sessionSelectMode ? 'var(--text-primary)' : 'var(--text-secondary)',
                      borderRadius: '999px',
                      padding: '4px 10px',
                      cursor: 'pointer',
                      fontSize: '11px',
                    }}
                  >
                    Select
                  </button>
                </div>
              )}

              {/* Bulk move bar (Q6): appears in select mode once the
                  selection is non-empty. One "Move to…" applies the
                  single-move contract per selected session. */}
              {sessionSelectMode && selectedSessionIds.length > 0 && (
                <div data-testid="session-bulk-bar" style={{ margin: '0 24px 12px', padding: '8px 10px', background: 'var(--bg-overlay)', border: '1px solid var(--border-strong)', borderRadius: 'var(--radius-md)', display: 'flex', flexDirection: 'column', gap: '8px' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                    <span style={{ fontSize: '12px', color: 'var(--text-primary)' }}>{selectedSessionIds.length} selected</span>
                    <button
                      onClick={() => setMoveMenuFor(moveMenuFor === '__bulk__' ? null : '__bulk__')}
                      style={{ background: 'var(--bg-hover)', border: '1px solid var(--border-strong)', color: 'var(--text-primary)', borderRadius: 'var(--radius-sm)', padding: '4px 10px', cursor: 'pointer', fontSize: '12px' }}
                    >
                      Move to…
                    </button>
                    <button
                      onClick={() => setSelectedSessionIds([])}
                      style={{ background: 'none', border: 'none', color: 'var(--text-muted)', cursor: 'pointer', fontSize: '12px', padding: '4px' }}
                    >
                      Clear
                    </button>
                  </div>
                  {moveMenuFor === '__bulk__' && (
                    <div data-testid="session-bulk-move-menu" style={{ display: 'flex', flexDirection: 'column', gap: '6px' }}>
                      {renderMoveTargets(undefined, (target) => startSessionMoves(selectedSessionIds, target))}
                    </div>
                  )}
                </div>
              )}

              {/* Move outcome (Q6): refusals and bulk tallies surface
                  here instead of failing silently. */}
              {moveStatus && (
                <div data-testid="session-move-status" style={{ margin: '0 24px 12px', padding: '8px 10px', background: moveStatus.type === 'error' ? 'rgba(201, 42, 42, 0.15)' : 'rgba(76, 195, 138, 0.12)', border: `1px solid ${moveStatus.type === 'error' ? 'rgba(201, 42, 42, 0.3)' : 'rgba(76, 195, 138, 0.3)'}`, borderRadius: 'var(--radius-sm)', color: moveStatus.type === 'error' ? 'var(--danger)' : 'var(--success)', fontSize: '11px', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                  <span>{moveStatus.message}</span>
                  <button onClick={() => setMoveStatus(null)} style={{ background: 'none', border: 'none', color: 'inherit', cursor: 'pointer', padding: 0 }}><X size={12} /></button>
                </div>
              )}

              <div className="chat-scroll" style={{ padding: '0 12px', flex: 1, overflowY: 'auto' }}>
                {sessionSearchResults !== null ? (
                  sessionSearchResults.length === 0 ? (
                    <div style={{ color: 'var(--text-muted)', fontSize: '13px', padding: '12px 14px' }}>No chats match your search.</div>
                  ) : sessionSearchResults.map(r => (
                    <div
                      key={r.id}
                      className={`session-item ${activeSession === r.id ? 'active' : ''}`}
                      onClick={() => handleSessionSwitch(r.id)}
                      style={{
                        display: 'flex',
                        alignItems: 'flex-start',
                        gap: '8px'
                      }}
                    >
                      <MessageSquare size={16} style={{ opacity: activeSession === r.id ? 1 : 0.5, flexShrink: 0, marginTop: '2px' }} />
                      <div style={{ overflow: 'hidden' }}>
                        <div style={{ fontSize: '13px', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{r.title || `Session ${r.id}`}</div>
                        {r.snippet && (
                          <div style={{ fontSize: '12px', color: 'var(--text-muted)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{r.snippet}</div>
                        )}
                        {/* Q10: search is global, so every result
                            names the workspace it is filed under. */}
                        <div style={{ fontSize: '11px', color: 'var(--text-muted)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{workspaceLabelFor(r.workspace)}</div>
                      </div>
                    </div>
                  ))
                ) : sessions.map(s => (
                  <React.Fragment key={s.id}>
                  <div
                    className={`session-item ${activeSession === s.id ? 'active' : ''}`}
                    onClick={() => {
                      // In select mode a row click picks the row
                      // for a bulk move instead of switching to it.
                      if (sessionSelectMode) toggleSessionSelected(s.id);
                      else handleSessionSwitch(s.id);
                    }}
                    onDoubleClick={() => { if (!sessionSelectMode) handleStartRenameSession(s); }}
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'space-between'
                    }}
                  >
                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px', overflow: 'hidden', flex: 1 }}>
                      {sessionSelectMode && (
                        <input
                          type="checkbox"
                          aria-label={`Select session ${s.title || s.name || s.id}`}
                          checked={selectedSessionIds.includes(s.id)}
                          onChange={() => toggleSessionSelected(s.id)}
                          onClick={(e) => e.stopPropagation()}
                          style={{ flexShrink: 0, accentColor: 'var(--accent)' }}
                        />
                      )}
                      <MessageSquare size={16} style={{ opacity: activeSession === s.id ? 1 : 0.5, flexShrink: 0 }} />
                      {editingSessionId === s.id ? (
                        <input
                          type="text"
                          aria-label="Session title"
                          autoFocus
                          value={editingSessionTitle}
                          onChange={(e) => setEditingSessionTitle(e.target.value)}
                          onClick={(e) => e.stopPropagation()}
                          onDoubleClick={(e) => e.stopPropagation()}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter') {
                              e.preventDefault();
                              handleCommitRenameSession(s.id);
                            } else if (e.key === 'Escape') {
                              e.preventDefault();
                              handleCancelRenameSession();
                            }
                          }}
                          onBlur={() => handleCommitRenameSession(s.id)}
                          style={{
                            flex: 1,
                            minWidth: 0,
                            background: 'var(--bg-base)',
                            border: '1px solid var(--accent)',
                            borderRadius: 'var(--radius-sm)',
                            padding: '3px 6px',
                            color: 'var(--text-primary)',
                            fontSize: '13px',
                            outline: 'none'
                          }}
                        />
                      ) : (
                        <span style={{ fontSize: '13px', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{s.title || s.name || `Session ${s.id}`}</span>
                      )}
                    </div>
                    {!sessionSelectMode && (
                    <div style={{ display: 'flex', alignItems: 'center', flexShrink: 0 }}>
                      <button
                        className="session-move-btn"
                        onClick={(e) => { e.stopPropagation(); setMoveMenuFor(moveMenuFor === s.id ? null : s.id); }}
                        title="Move Session"
                        aria-label="Move session"
                      >
                        <FolderInput size={13} />
                      </button>
                      <button
                        className="session-rename-btn"
                        onClick={(e) => handleStartRenameSession(s, e)}
                        title="Rename Session"
                        aria-label="Rename session"
                      >
                        <Pencil size={13} />
                      </button>
                      <button
                        className="session-delete-btn"
                        onClick={(e) => handleDeleteSession(s.id, e)}
                        title="Delete Session"
                      >
                        <Trash2 size={14} />
                      </button>
                    </div>
                    )}
                  </div>
                  {moveMenuFor === s.id && (
                    <div
                      data-testid={`session-move-menu-${s.id}`}
                      style={{ display: 'flex', flexDirection: 'column', gap: '6px', padding: '8px', margin: '0 2px 6px', background: 'var(--bg-raised)', border: '1px solid var(--border-default)', borderRadius: 'var(--radius-md)' }}
                    >
                      <div style={{ fontSize: '11px', color: 'var(--text-muted)' }}>Move to…</div>
                      {renderMoveTargets(s.workspace ?? null, (target) => startSessionMoves([s.id], target))}
                    </div>
                  )}
                  </React.Fragment>
                ))}
              </div>
            </div>

          </div>
        )}

      {activeTab === 'Chat' && !isChatDetached && (
        <DraggableChatWindow 
          messages={chatMessages} 
          sessionId={activeSession}
          sessionSwitchPending={!!sessionSwitchPendingId && sessionSwitchPendingId === activeSession}
          onExportSession={handleExportSession}
          onSendMessage={handleSendMessage} 
          onStopTask={handleStopTask}
          onApproveTool={handleToolApproval}
          onApprovePlugin={handlePluginApproval}
          onVaultFillApproval={handleVaultFillApproval}
          status={status}
          isThinking={isThinking}
          providers={providers}
          onChangeModel={handleChangeModel}
          onEditMessage={handleEditMessage}
          onRegenerate={handleRegenerate}
          planMode={planMode}
          onTogglePlanMode={handleTogglePlanMode}
          scratchpad={scratchpadBySession[activeSession || '__pending'] || null}
          rewindManifest={rewindManifest}
          onRewindRequest={handleRewindRequest}
          onForkRequest={handleForkRequest}
          rewindUndo={rewindUndo && rewindUndo.sessionId === activeSession ? rewindUndo : null}
          onRewindUndo={handleRewindUndoClick}
          onRewindUndoExpire={handleRewindUndoExpire}
          onDetach={async (state) => {
            // S3-M7: hand the history + draft to main-process memory
            // and await it BEFORE opening the floating window, so the
            // floating window can never load a stale/absent state.
            await writeFloatingTransferState({
              messages: chatMessages,
              input: state?.input || ''
            });
            setIsChatDetached(true);
            window.electronAPI?.openFloatingChat(activeSession);
          }}
          isFloating={isFloating}
          initialInput={initialInput}
          slashCommands={slashCommands}
          isSidebarCollapsed={isSidebarCollapsed}
          artifactsCount={canvasArtifacts.length}
          isCanvasOpen={isCanvasOpen}
          onToggleCanvas={handleToggleCanvas}
          onOpenArtifact={handleOpenArtifact}
          sessionCostUsd={sessionCostUsd}
          costTasks={costTasks}
          runningCostTaskIds={runningCostTaskIds}
          onRequestCostBreakdown={requestCostBreakdown}
          contextBreakdown={contextBreakdown}
          onRequestContextBreakdown={requestContextBreakdown}
          budgetAlert={budgetAlert}
          browserSession={browserSessions[activeSession] || null}
          onBrowserControl={handleBrowserControl}
          voiceState={voiceState}
          voiceAudioChunk={voiceAudioChunk}
          voicePlaybackCut={voicePlaybackCut}
          onVoiceStart={handleVoiceStart}
          onVoiceStop={handleVoiceStop}
          onVoiceDismissError={handleVoiceDismissError}
          voiceRtcAnswer={voiceRtcAnswer}
          voiceRtcRemoteCandidate={voiceRtcRemoteCandidate}
          onVoiceRtcOffer={handleVoiceRtcOffer}
          onVoiceRtcIceCandidate={handleVoiceRtcIceCandidate}
          onVoicePlaybackProgress={handleVoicePlaybackProgress}
          dictationState={dictationState}
          readAloudState={readAloudState}
          onDictationStart={handleDictationStart}
          onDictationChunk={handleDictationChunk}
          onDictationStop={handleDictationStop}
          onReadAloudStart={handleReadAloudStart}
          onReadAloudStop={handleReadAloudStop}
        />
      )}

      {rewindConfirm && (
        <RewindConfirmModal
          entry={rewindConfirm.entry}
          filesNotReverted={rewindConfirm.filesNotReverted}
          onConfirm={handleRewindConfirm}
          onCancel={() => setRewindConfirm(null)}
          queued={!!rewindConfirm.queued}
        />
      )}

      {activeTab === 'Chat' && !isChatDetached && isCanvasOpen && (
        <ArtifactsCanvas
          isOpen={isCanvasOpen}
          onClose={() => setIsCanvasOpen(false)}
          artifacts={canvasArtifacts}
          selectedArtifactId={selectedArtifactId}
          onSelectArtifact={handleSelectArtifact}
          activeArtifactContent={activeArtifactContent}
          isLoading={artifactContentLoading}
          devServerInfo={devServerInfo}
          onDetectDevServer={handleDetectDevServer}
        />
      )}

      {activeTab !== 'Chat' && (
        <div style={{
          position: 'absolute',
          top: '40px',
          left: isSidebarCollapsed ? '80px' : '360px',
          right: '40px',
          bottom: '40px',
          zIndex: 40,
          display: 'flex',
          justifyContent: 'center',
          alignItems: 'center',
          transition: 'left 250ms cubic-bezier(0.23, 1, 0.32, 1)'
        }}>
          <div className="ios-glass" style={{ width: '100%', height: '100%', padding: '32px', display: 'flex', flexDirection: 'column' }}>
            {activeTab === 'Settings' && (
               <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden', position: 'relative' }}>
                 <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '24px' }}>
                   <h1 style={{ color: '#fff', fontSize: '28px', margin: 0 }}>Settings</h1>
                   <button 
                     onClick={() => setActiveTab('Chat')}
                     className="ios-close-btn"
                     aria-label="Close Settings"
                   >
                     <X size={20} />
                   </button>
                 </div>
                 <SettingsPanel
                   wsRef={wsRef}
                   safeSend={safeSend}
                   pendingMessages={pendingMessages}
                   reconnectCount={reconnectCount}
                   providers={providers}
                   providersLoaded={providersLoaded}
                   isConnected={isConnected}
                   providerHealth={providerHealth}
                   localModels={localModels}
                   activeSession={activeSession}
                   dailyBudget={dailyBudget}
                   budgetAlert={budgetAlert}
                   initialMenu={settingsSection}
                   onInitialMenuConsumed={() => setSettingsSection(null)}
                 />
               </div>
            )}
            {activeTab === 'Dashboard' && <Dashboard status={status} activeTasks={activeTaskIds.size} pluginCount={pluginCount} memoryMb={memoryMb} providerHealth={providerHealth} providerHealthLoaded={providerHealthLoaded} wsRef={wsRef} reconnectCount={reconnectCount} onStartWorkflow={() => setActiveTab('Chat')} />}
            {activeTab === 'Artifacts' && (
              <GlobalArtifactsView
                wsRef={wsRef}
                safeSend={safeSend}
                pendingMessages={pendingMessages}
                reconnectCount={reconnectCount}
                isConnected={isConnected}
                sessions={sessions}
                onNavigateToSession={handleNavigateToArtifactSession}
                onNavigateToMedia={handleNavigateToMediaSession}
              />
            )}
            {activeTab === 'Projects' && <Projects wsRef={wsRef} isConnected={isConnected} reconnectCount={reconnectCount} />}
            {activeTab !== 'Settings' && activeTab !== 'Dashboard' && activeTab !== 'Artifacts' && activeTab !== 'Projects' && (
               <div style={{ display: 'flex', flex: 1, alignItems: 'center', justifyContent: 'center', color: 'var(--text-muted)' }}>
                 <h2>{activeTab} view coming soon.</h2>
               </div>
            )}
          </div>
        </div>
      )}

      {/* E6-F01: shell-level overlays. Onboarding, the command palette and
          shortcuts help mount here — independent of the active tab — so
          they render on the default Chat tab exactly as on every other
          tab. Each keeps its own visibility condition unchanged, and all
          three roots are position:fixed, so the mount point does not
          change their placement. */}
      {!isFloating && engineConfig && !engineConfig.onboarding_completed && providersLoaded
        && !(Array.isArray(providers) && providers.some(p => p.has_key)) && (
        <Onboarding providers={providers} onDone={handleOnboardingDone} />
      )}
      {!isFloating && (
        <CommandPalette open={paletteOpen} items={paletteItems} onClose={() => setPaletteOpen(false)} isConnected={isConnected} />
      )}
      {!isFloating && shortcutsOpen && (
        <div data-testid="shortcuts-dialog" style={{ position: 'fixed', inset: 0, background: 'rgba(5, 5, 6, 0.55)', backdropFilter: 'blur(6px)', WebkitBackdropFilter: 'blur(6px)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 205 }} onClick={() => setShortcutsOpen(false)}>
          <div style={{ background: 'rgba(16, 16, 18, 0.66)', backdropFilter: 'blur(16px) saturate(140%) url(#aether-glass-refraction)', WebkitBackdropFilter: 'blur(16px) saturate(140%) url(#aether-glass-refraction)', border: '1px solid var(--glass-edge)', borderRadius: 'var(--radius-lg)', boxShadow: 'inset 0 1px 0 var(--glass-specular), 0 16px 40px rgba(0, 0, 0, 0.45)', padding: '24px', width: '380px', maxWidth: '92vw', color: 'var(--text-primary)' }} onClick={(e) => e.stopPropagation()}>
            <h3 style={{ margin: '0 0 14px 0' }}>Keyboard shortcuts</h3>
            {[
              ['Ctrl+K', 'Command palette'],
              ['Ctrl+N', 'New chat'],
              ['Ctrl+,', 'Open Settings'],
              ['Ctrl+.', 'Cancel current task'],
              ['Ctrl+Enter', 'Approve pending request'],
              ['Ctrl+Shift+Enter', 'Reject pending request'],
              ['Alt+Space', 'Show / hide Aether-OS (global)'],
              [resolveSnipHotkey(engineConfig?.snip_hotkey).accelerator, 'Snip & ask (global)'],
              ['Esc', 'Close palette / dialogs'],
            ].map(([keys, desc]) => (
              <div key={keys} style={{ display: 'flex', justifyContent: 'space-between', padding: '7px 0', borderBottom: '1px solid var(--border-default)', fontSize: '14px' }}>
                <span style={{ fontFamily: 'var(--font-mono)', color: 'var(--accent)' }}>{keys}</span>
                <span style={{ color: 'var(--text-secondary)' }}>{desc}</span>
              </div>
            ))}
            <button onClick={() => setShortcutsOpen(false)} style={{ marginTop: '16px', background: 'var(--accent)', color: 'var(--accent-on)', border: 'none', padding: '8px 18px', borderRadius: 'var(--radius-md)', cursor: 'pointer', fontWeight: 600 }}>Done</button>
          </div>
        </div>
      )}

      {/* 3D Knowledge Graph Background */}
      {!isFloating && (
        <div style={{ position: 'absolute', top: 0, left: 0, right: 0, bottom: 0, zIndex: 1 }}>
          <React.Suspense fallback={<div style={{ position: 'absolute', inset: 0, background: '#05050f' }} />}>
          <ForceGraph3D
            ref={graphRef}
            graphData={initGraphData}
            backgroundColor="#05050f"
            nodeColor={node => {
              if (node.group === 1) return '#ec4899'; // AETHER-OS root
              if (node.group === 2) return '#60a5fa'; // Core brain nodes
              if (node.group === 3) return '#F5B942'; // Logs — accent hex inlined (canvas colours cannot read CSS vars)
              return '#312e81'; // Ambient
            }}
            nodeRelSize={6}
            linkOpacity={0.3}
            linkWidth={1.5}
            linkColor={() => '#4338ca'}
            enableNodeDrag={false}
            showNavInfo={false}
          />
          </React.Suspense>
        </div>
      )}

    </div>
    </div>
  );
}

function NavItem({ icon, label, activeTab, setActiveTab }) {
  const activate = () => setActiveTab(label);
  return (
    <div
      role="button"
      tabIndex={0}
      onClick={activate}
      onKeyDown={(e) => {
        // E6-F06: keyboard activation does exactly what a click does.
        // (role="button" also opts the item into the global
        // :focus-visible ring in index.css.)
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          activate();
        }
      }}
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: '12px',
        padding: '10px 14px',
        borderRadius: 'var(--radius-md)',
        color: activeTab === label ? 'var(--text-primary)' : 'var(--text-secondary)',
        background: activeTab === label ? 'var(--bg-overlay)' : 'transparent',
        boxShadow: activeTab === label ? 'inset 3px 0 0 var(--accent)' : 'none',
        cursor: 'pointer',
        transition: 'all var(--motion-base)',
        fontSize: '14px',
        fontWeight: activeTab === label ? '500' : '400',
      }}
      onMouseEnter={(e) => {
        if (activeTab !== label) {
          e.currentTarget.style.background = 'var(--bg-hover)';
          e.currentTarget.style.color = 'var(--text-primary)';
        }
      }}
      onMouseLeave={(e) => {
        if (activeTab !== label) {
          e.currentTarget.style.background = 'transparent';
          e.currentTarget.style.color = 'var(--text-secondary)';
        }
      }}
    >
      {icon}
      {label}
    </div>
  );
}

function TitleBar({ isChatDetached, onReattachChat, isThinking, onStopTask }) {
  const handleMinimize = () => window.electronAPI?.minimizeWindow();
  const handleMaximize = () => window.electronAPI?.maximizeWindow();
  const handleClose = () => window.electronAPI?.closeWindow();

  return (
    <div style={{
      height: '36px',
      background: 'transparent',
      WebkitAppRegion: 'drag', // Electron specific for dragging
      display: 'flex',
      justifyContent: 'flex-end',
      alignItems: 'center',
      zIndex: 100,
      position: 'absolute',
      top: 0,
      left: 0,
      right: 0,
      padding: '0 8px 0 16px'
    }}>
      <div style={{ WebkitAppRegion: 'no-drag', display: 'flex', alignItems: 'center', height: '100%', gap: '6px' }}>
        {/* Stop Task button shown whenever a task is active */}
        {isThinking && (
          <button 
            onClick={onStopTask}
            className="ios-stop-btn"
            title="Stop current task"
            aria-label="Stop current task"
          >
            <Square size={9} fill="currentColor" />
          </button>
        )}

        {/* Re-attach button with running task indicator */}
        {isChatDetached && (
          <button 
            onClick={onReattachChat}
            className={`ios-reattach-btn ${isThinking ? 'running' : ''}`}
            title={isThinking ? "Task is running • Click to re-attach to Main Window" : "Re-attach to Main Window"}
            aria-label="Re-attach to Main Window"
          >
            <LogIn size={13} />
            {isThinking && <span className="ios-running-badge" />}
          </button>
        )}
        <div style={{ display: 'flex', height: '100%' }}>
          <button onClick={handleMinimize} className="window-btn" title="Minimize" aria-label="Minimize window">
            <Minus size={14} />
          </button>
          <button onClick={handleMaximize} className="window-btn" title="Maximize" aria-label="Maximize window">
            <Square size={12} />
          </button>
          <button onClick={handleClose} className="window-btn close-btn" title="Close" aria-label="Close window">
            <X size={14} />
          </button>
        </div>
      </div>
    </div>
  );
}

export default function App() {
  return (
    <SocketProvider>
      <AppContent />
    </SocketProvider>
  );
}
