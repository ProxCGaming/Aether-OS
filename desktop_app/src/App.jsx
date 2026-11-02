import React, { useState, useEffect, useRef, useCallback, useMemo } from 'react';
import { SocketProvider, useAetherSocket } from './contexts/SocketContext';
// Epic 9: the 3D graph (three.js + force-graph, ~1 MB) is the ambient
// background, not the app shell — load it lazily so first paint and
// interactivity don't wait on it. The Suspense fallback paints the
// same background colour, so the graph simply fades in when ready.
const ForceGraph3D = React.lazy(() => import('react-force-graph-3d'));
import { Send, Settings, CheckCircle2, Circle, Bot, User, Trash2, X, Plus, TerminalSquare, MessageSquare, LayoutDashboard, Folder, BarChart2, Minus, Square, LogIn, Menu, Pencil, Upload, Layers } from 'lucide-react';
import DraggableChatWindow from './DraggableChatWindow';
import SettingsPanel from './SettingsPanel';
import Dashboard from './Dashboard';
import Projects from './Projects';
import Onboarding from './components/Onboarding';
import CommandPalette from './components/CommandPalette';
import ArtifactsCanvas from './components/ArtifactsCanvas';
import GlobalArtifactsView from './components/GlobalArtifactsView';
import { groupEntriesByTask } from './components/CostHeaderPopover';
import RewindConfirmModal from './components/RewindConfirmModal';
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

// Epic 1, E1-16: THINKING deltas accumulate into a message's thoughts
// array — consecutive deltas from the same node merge into one entry,
// preserving arrival order. Reasoning lives ONLY in thoughts; it is
// never written into a message's content.
function appendThoughtDelta(thoughts, node, delta) {
  const next = [...(thoughts || [])];
  if (next.length > 0 && next[next.length - 1].node === node && typeof next[next.length - 1].text === 'string') {
    next[next.length - 1] = { ...next[next.length - 1], text: next[next.length - 1].text + delta };
  } else {
    next.push({ node, text: delta });
  }
  return next;
}

// Append buffered thought entries onto existing thoughts, merging the
// seam entry when both sides end/start with the same node.
function mergeThoughtEntries(base, extra) {
  let merged = [...(base || [])];
  for (const entry of extra || []) {
    if (entry && typeof entry.text === 'string') {
      merged = appendThoughtDelta(merged, entry.node, entry.text);
    }
  }
  return merged;
}

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

function AppContent() {
  // Phase 10.3 C1: the engine-message facade lives in SocketContext, not
  // here. AppContent consumes its wsRef/isConnected; it builds no mock of
  // its own and subscribes to nothing directly.
  const { wsRef, isConnected } = useAetherSocket();

  const [status, setStatus] = useState('Initializing');
  const [isThinking, setIsThinking] = useState(false);
  const [chatMessages, setChatMessages] = useState([]);
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
  const [providersLoaded, setProvidersLoaded] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const [localModels, setLocalModels] = useState([]);

  // Dashboard State (real data, not hardcoded)
  const [activeTaskIds, setActiveTaskIds] = useState(new Set());
  const [pluginCount, setPluginCount] = useState(0);
  const [memoryMb, setMemoryMb] = useState(null);

  const params = new URLSearchParams(window.location.search);
  const isFloating = params.get('floating') === 'true';
  const initialSession = params.get('session');

  const [sessions, setSessions] = useState([]);
  // Epic 1 UI batch: per-session plan mode (E1-09 UI). Keyed by session
  // id; the '__pending' key holds the choice made before the first
  // session exists and is adopted by the session when it is created.
  const [planModeBySession, setPlanModeBySession] = useState({});
  // Epic 1, E1-17: latest scratchpad per session ({kind, content,
  // agent}), keyed like planModeBySession ('__pending' before the
  // first session exists, adopted on SESSION_CREATE_RESPONSE).
  const [scratchpadBySession, setScratchpadBySession] = useState({});
  // Epic 1, E1-16: THINKING deltas that arrive before a streaming
  // agent bubble exists are buffered per session key here and flushed
  // into the bubble's thoughts when it appears (TASK_STARTED /
  // TASK_COMPLETED), so reasoning is never dropped.
  const pendingThinkingRef = useRef({});
  // Epic 1 UI batch (E1-08): session search + inline rename state.
  const [sessionSearchQuery, setSessionSearchQuery] = useState('');
  const [sessionSearchResults, setSessionSearchResults] = useState(null);
  const sessionSearchTimerRef = useRef(null);
  const sessionSearchQueryRef = useRef('');
  const [editingSessionId, setEditingSessionId] = useState(null);
  const [editingSessionTitle, setEditingSessionTitle] = useState('');
  const [activeSession, setActiveSession] = useState(initialSession || null);
  const activeSessionRef = useRef(initialSession || null);
  useEffect(() => { activeSessionRef.current = activeSession; }, [activeSession]);
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
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'TASK_REWIND_REQUEST',
        schema_version: 1,
        request_id: `task_rewind_${Date.now()}`,
        payload: {
          session_id: rewindManifest.session_id,
          task_id: rewindManifest.task_id,
          step_index: rewindConfirm.entry.step_index,
          checkpoint_id: rewindConfirm.entry.checkpoint_id,
        },
      }));
    }
    setRewindConfirm(null);
  }, [rewindConfirm, rewindManifest, wsRef]);
  const handleRewindUndoClick = useCallback(() => {
    if (!rewindUndo) return;
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'TASK_REWIND_UNDO_REQUEST',
        schema_version: 1,
        request_id: `task_rewind_undo_${Date.now()}`,
        payload: { session_id: rewindUndo.sessionId },
      }));
    }
  }, [rewindUndo, wsRef]);
  const handleRewindUndoExpire = useCallback(() => {
    // The window closed (toast countdown or the local timer): the
    // engine has pruned; drop the display and refetch the manifest.
    const sessionId = rewindUndo?.sessionId || activeSessionRef.current;
    clearRewindUndo();
    if (sessionId) requestRewindManifest(sessionId);
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
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'SESSION_FORK_REQUEST',
        schema_version: 1,
        request_id: `session_fork_${Date.now()}`,
        payload: {
          session_id: sessionId,
          message_id: message_id ?? null,
          step_index: step_index ?? null,
          new_title: null,
        },
      }));
    }
  }, [wsRef]);
  const [activeTab, setActiveTab] = useState('Chat'); // 'Chat', 'Dashboard', 'Artifacts', 'Projects', 'Settings'
  const [initialInput, setInitialInput] = useState('');
  const [isSidebarCollapsed, setIsSidebarCollapsed] = useState(false);
  const [slashCommands, setSlashCommands] = useState([]);
  const [importStatus, setImportStatus] = useState(null);
  const [_exportStatus, setExportStatus] = useState(null);
  // Epic 8, E8-2.1: Artifacts canvas state (auto-derived, per-session, view-only)
  const [isCanvasOpen, setIsCanvasOpen] = useState(false);
  const [canvasArtifacts, setCanvasArtifacts] = useState([]);
  const [selectedArtifactId, setSelectedArtifactId] = useState(null);
  const [activeArtifactContent, setActiveArtifactContent] = useState(null);
  // Epic 8, E8-2.2: latest dev-server probe result for the canvas
  // Preview tab (DEV_SERVER_DETECT_RESPONSE payload, or null).
  const [devServerInfo, setDevServerInfo] = useState(null);
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

  // Epic 8, E8-5.1a: voice mode. voiceState mirrors the engine
  // session — {status: 'starting'|'active'|'error', sessionId,
  // partialText, assistantText, announcementText, error,
  // webrtcConfig} — or null when
  // voice mode is off. Partial transcripts are provisional display
  // only; finals commit into chatMessages (the engine persists the
  // same turns to the session store). The ref mirror exists
  // because the engine-message handler is registered once and must
  // not close over render-scoped state.
  const [voiceState, setVoiceState] = useState(null);
  const [voiceAudioChunk, setVoiceAudioChunk] = useState(null);
  // E8-5.1b: bumped on every VOICE_BARGE_IN; the overlay cuts its
  // scheduled playback when this changes.
  const [voicePlaybackCut, setVoicePlaybackCut] = useState(0);
  // F-12b: RTC signalling for the live voice session. The engine's
  // answer and its ICE candidates are seq-numbered props for the
  // overlay (the voiceAudioChunk pattern); the overlay's offer and
  // local candidates go up through the callbacks below.
  const [voiceRtcAnswer, setVoiceRtcAnswer] = useState(null);
  const [voiceRtcRemoteCandidate, setVoiceRtcRemoteCandidate] = useState(null);
  const voiceStateRef = useRef(null);
  const voiceChunkSeqRef = useRef(0);
  const voiceRtcSeqRef = useRef(0);

  const applyVoiceState = useCallback((next) => {
    voiceStateRef.current = next;
    setVoiceState(next);
  }, []);

  const sendVoiceRequest = useCallback((type, payload) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type,
        schema_version: 1,
        request_id: `voice_${Date.now()}`,
        payload,
      }));
      return true;
    }
    return false;
  }, [wsRef]);

  const commitVoiceAssistant = useCallback((state) => {
    const text = (state && state.assistantText) || '';
    if (text.trim()) {
      setChatMessages(prev => [...prev, {
        id: `voice-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        role: 'agent', content: text, thoughts: [], isFinal: true,
      }]);
    }
  }, []);

  const handleVoiceStart = useCallback(() => {
    const current = voiceStateRef.current;
    if (current && current.status !== 'error') return;
    setVoiceRtcAnswer(null);
    setVoiceRtcRemoteCandidate(null);
    const sessionId = activeSessionRef.current;
    if (!sendVoiceRequest('VOICE_SESSION_START_REQUEST', { session_id: sessionId })) {
      applyVoiceState({
        status: 'error', sessionId, partialText: '', assistantText: '',
        announcementText: '',
        error: 'voice unavailable: the engine is not connected', webrtcConfig: null,
      });
      return;
    }
    applyVoiceState({
      status: 'starting', sessionId, partialText: '',
      assistantText: '', announcementText: '', error: null, webrtcConfig: null,
    });
  }, [applyVoiceState, sendVoiceRequest]);

  const handleVoiceStop = useCallback(() => {
    const current = voiceStateRef.current;
    if (!current) return;
    // Tear the UI down immediately (the overlay unmount stops the
    // mic); the engine stops its pipeline on the request — or on
    // disconnect if the request never arrives. The reply streamed
    // so far commits as a normal message.
    commitVoiceAssistant(current);
    sendVoiceRequest('VOICE_SESSION_STOP_REQUEST', {
      session_id: current.sessionId || activeSessionRef.current,
    });
    setVoiceRtcAnswer(null);
    setVoiceRtcRemoteCandidate(null);
    applyVoiceState(null);
  }, [applyVoiceState, commitVoiceAssistant, sendVoiceRequest]);

  const handleVoiceDismissError = useCallback(() => {
    setVoiceRtcAnswer(null);
    setVoiceRtcRemoteCandidate(null);
    applyVoiceState(null);
  }, [applyVoiceState]);

  // F-12b: the overlay offers (it holds the microphone); the
  // engine answers. A refused negotiation flips the session to the
  // error state — the overlay must never sit live with no media.
  const handleVoiceRtcOffer = useCallback((offer) => {
    const current = voiceStateRef.current;
    if (!current) return;
    sendVoiceRequest('VOICE_RTC_OFFER_REQUEST', {
      session_id: current.sessionId || activeSessionRef.current,
      sdp: offer,
    });
  }, [sendVoiceRequest]);

  const handleVoiceRtcIceCandidate = useCallback((candidate) => {
    const current = voiceStateRef.current;
    if (!current) return;
    sendVoiceRequest('VOICE_RTC_ICE_REQUEST', {
      session_id: current.sessionId || activeSessionRef.current,
      candidate,
    });
  }, [sendVoiceRequest]);

  // Epic 8, E8-5.3: Push-to-talk dictation + assistant message read-aloud
  const [dictationState, setDictationState] = useState(null);
  const [readAloudState, setReadAloudState] = useState(null);
  const readAloudAudioCtxRef = useRef(null);
  const readAloudSourcesRef = useRef(new Set());
  const readAloudPlayHeadRef = useRef(0);

  const handleDictationStart = useCallback(() => {
    const sessionId = activeSessionRef.current;
    if (!sendVoiceRequest('DICTATION_START_REQUEST', { session_id: sessionId })) {
      setDictationState({
        status: 'error',
        error: 'voice unavailable: the engine is not connected',
        text: '',
      });
      return;
    }
    setDictationState({ status: 'starting', error: null, text: '' });
  }, [sendVoiceRequest]);

  const handleDictationStop = useCallback(() => {
    const sessionId = activeSessionRef.current;
    sendVoiceRequest('DICTATION_STOP_REQUEST', { session_id: sessionId });
    setDictationState(prev => (prev ? { ...prev, status: 'idle' } : null));
  }, [sendVoiceRequest]);

  const stopAppReadAloud = useCallback(() => {
    readAloudSourcesRef.current.forEach((src) => {
      try { src.stop(); } catch {}
    });
    readAloudSourcesRef.current.clear();
    if (readAloudAudioCtxRef.current) {
      readAloudPlayHeadRef.current = readAloudAudioCtxRef.current.currentTime;
    } else {
      readAloudPlayHeadRef.current = 0;
    }
  }, []);

  const handleReadAloudStart = useCallback((messageId, text) => {
    stopAppReadAloud();
    if (!sendVoiceRequest('READ_ALOUD_START_REQUEST', { message_id: messageId, text })) {
      setReadAloudState({
        messageId,
        status: 'error',
        error: 'voice unavailable: the engine is not connected',
      });
      return;
    }
    setReadAloudState({ messageId, status: 'starting', error: null });
  }, [sendVoiceRequest, stopAppReadAloud]);

  const handleReadAloudStop = useCallback((messageId) => {
    stopAppReadAloud();
    sendVoiceRequest('READ_ALOUD_STOP_REQUEST', { message_id: messageId });
    setReadAloudState(null);
  }, [sendVoiceRequest, stopAppReadAloud]);

  // Leaving the voice session's chat ends voice mode without
  // committing the provisional bubbles here — the engine already
  // persisted the finals to that session's store.
  useEffect(() => {
    const current = voiceStateRef.current;
    if (current && current.sessionId && current.sessionId !== activeSession) {
      sendVoiceRequest('VOICE_SESSION_STOP_REQUEST', { session_id: current.sessionId });
      applyVoiceState(null);
    }
  }, [activeSession, applyVoiceState, sendVoiceRequest]);

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
  }, [wsRef]);

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
  }, [wsRef]);

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
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
    const chatSessionId = activeSessionRef.current;
    const payload = {
      chat_session_id: chatSessionId,
      action: action,
    };
    const browserSessionId = chatSessionId ? browserSessionIdsRef.current[chatSessionId] : null;
    if (browserSessionId) payload.session_id = browserSessionId;
    wsRef.current.send(JSON.stringify({
      type: 'BROWSER_TASK_PAUSE_REQUEST',
      schema_version: 1,
      request_id: `browser_control_${Date.now()}`,
      payload
    }));
  }, [wsRef]);
  
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
  const pendingSessionSwitchRef = useRef(null);
  const sessionCacheRef = useRef({});
  const activeTaskSessionRef = useRef(null);

  // Phase 10.3 C1: subscribe to engine messages through the single
  // SocketContext facade (wsRef). No mock wsRef, no direct onEngineMessage
  // subscription, no local dedup — all of that lives in the facade now.
  // Connection state (isConnected) also comes from the facade; see the
  // transition effect below.
  useEffect(() => {
    const handleMessage = (event) => {
      const data = JSON.parse(event.data);
        if (data.type === 'hello') {
          setStatus(`Online / ${data.payload.active_model || 'Agent'}`);
        } else if (data.type === 'START_TASK' || data.type === 'TASK_STARTED') {
          // Track active task for Dashboard
          if (data.payload?.task_id || data.request_id) {
            setActiveTaskIds(prev => new Set(prev).add(data.payload?.task_id || data.request_id));
          }
          activeTaskSessionRef.current = data.payload?.session_id || activeSessionRef.current;
          // E8-1.1a: a new task supersedes the session's manifest;
          // fetch now (points land as it runs) and again at the
          // terminal event, which carries the complete trail.
          requestRewindManifest(activeTaskSessionRef.current);
          setIsThinking(true);
          setChatMessages(prev => {
            // E1-16: flush any THINKING deltas buffered before this
            // bubble existed into its thoughts. Deltas that arrived
            // before the task (and its session id) was known sit
            // under '__pending', so fall back to that key.
            const thinkKey = data.payload?.session_id || activeSessionRef.current || '__pending';
            let buffered = pendingThinkingRef.current[thinkKey] || [];
            if (!buffered.length && thinkKey !== '__pending') {
              buffered = pendingThinkingRef.current.__pending || [];
              if (buffered.length) delete pendingThinkingRef.current.__pending;
            }
            const lastAgentIdx = prev.findLastIndex(m => m.role === 'agent' && !m.isFinal);
            if (lastAgentIdx === -1) {
              if (buffered.length) delete pendingThinkingRef.current[thinkKey];
              return [...prev, { role: 'agent', content: '', thoughts: buffered, isFinal: false }];
            }
            if (buffered.length) {
              delete pendingThinkingRef.current[thinkKey];
              const newMessages = [...prev];
              const last = newMessages[lastAgentIdx];
              newMessages[lastAgentIdx] = { ...last, thoughts: mergeThoughtEntries(last.thoughts || [], buffered) };
              return newMessages;
            }
            return prev;
          });
        } else if (data.type === 'STEERING_APPLIED') {
          // Epic 1, E1-07: the engine applied a queued steer to the
          // running task — clear the bubble's queued flag.
          const appliedId = data.payload?.message_id || data.request_id;
          if (appliedId) {
            const clearQueued = (list) => (list || []).map(m =>
              m.id === appliedId ? { ...m, queued: false, steering: false } : m);
            setChatMessages(prev => clearQueued(prev));
            Object.keys(sessionCacheRef.current).forEach((sid) => {
              sessionCacheRef.current[sid] = clearQueued(sessionCacheRef.current[sid]);
            });
          }
        } else if (data.type === 'THINKING') {
          // Epic 1, E1-16: model reasoning streams into the streaming
          // bubble's thoughts ONLY — never into content, never into
          // the persisted answer body. If no bubble is streaming yet,
          // the delta is buffered per session and flushed when the
          // bubble appears (TASK_STARTED) or finalises (TASK_COMPLETED).
          const thinkDelta = data.payload?.text_delta;
          if (typeof thinkDelta === 'string' && thinkDelta.length > 0) {
            const thinkNode = data.payload?.node || 'thinking';
            const targetSession = activeTaskSessionRef.current || activeSessionRef.current;
            const bufferKey = targetSession || '__pending';
            const applyThinking = (list) => {
              const idx = list.findLastIndex(m => m.role === 'agent' && !m.isFinal);
              if (idx === -1) return null;
              const newList = [...list];
              const last = newList[idx];
              newList[idx] = { ...last, thoughts: appendThoughtDelta(last.thoughts || [], thinkNode, thinkDelta) };
              return newList;
            };
            if (targetSession && targetSession !== activeSessionRef.current) {
              const cached = sessionCacheRef.current[targetSession] || [];
              const updated = applyThinking(cached);
              if (updated) {
                sessionCacheRef.current[targetSession] = updated;
              } else {
                pendingThinkingRef.current[bufferKey] = appendThoughtDelta(pendingThinkingRef.current[bufferKey] || [], thinkNode, thinkDelta);
              }
            } else {
              setChatMessages(prev => {
                const updated = applyThinking(prev);
                if (updated) return updated;
                pendingThinkingRef.current[bufferKey] = appendThoughtDelta(pendingThinkingRef.current[bufferKey] || [], thinkNode, thinkDelta);
                return prev;
              });
            }
          }
        } else if (data.type === 'SCRATCHPAD') {
          // Epic 1, E1-17: display-only scratchpad (diagram/sketch/
          // notes) from the engine. Keep only the latest per session;
          // the panel in DraggableChatWindow renders it.
          const padContent = data.payload?.content;
          if (typeof padContent === 'string' && padContent.length > 0) {
            const padSession = data.payload?.session_id || activeTaskSessionRef.current || activeSessionRef.current || '__pending';
            const padEntry = {
              kind: data.payload?.kind || 'text',
              content: padContent,
              agent: data.payload?.agent || null,
            };
            setScratchpadBySession(prev => ({ ...prev, [padSession]: padEntry }));
          }
        } else if (data.type === 'TASK_PROGRESS') {
          let delta = data.payload.text_delta;
          if (delta !== undefined && delta !== null && typeof delta !== 'string') {
            delta = JSON.stringify(delta, null, 2);
          }
          const node = data.payload.node;
          const isThought = node && node !== 'assistant' && node !== 'agent';
          
          // Filter out supervisor delegation strings that duplicate NODE_ACTIVITY
          if (isThought && delta && typeof delta === 'string' && delta.trim().startsWith('[Supervisor] -> Delegating')) {
            return;
          }
          
          if (delta) {
            const updateMessages = (prev) => {
              const lastAgentIdx = prev.findLastIndex(m => m.role === 'agent' && !m.isFinal);
              if (lastAgentIdx !== -1) {
                const newMessages = [...prev];
                const last = newMessages[lastAgentIdx];
                if (isThought) {
                  const currentThoughts = last.thoughts || [];
                  const updatedThoughts = [...currentThoughts];
                  if (updatedThoughts.length > 0 && updatedThoughts[updatedThoughts.length - 1].node === node) {
                    const currentText = updatedThoughts[updatedThoughts.length - 1].text || '';
                    if (delta.length > 10 && currentText.endsWith(delta)) {
                      return prev;
                    }
                    updatedThoughts[updatedThoughts.length - 1].text += delta;
                  } else {
                    updatedThoughts.push({ node, text: delta });
                  }
                  newMessages[lastAgentIdx] = { ...last, thoughts: updatedThoughts };
                } else {
                  newMessages[lastAgentIdx] = { ...last, content: (last.content || '') + delta };
                }
                return newMessages;
              } else {
                const veryLast = prev[prev.length - 1];
                if (veryLast && veryLast.role === 'agent' && veryLast.isFinal) {
                  // Drop ghost progress packets that arrive after a task is completed/cancelled
                  return prev;
                }
                if (isThought) {
                  return [...prev, { role: 'agent', content: '', thoughts: [{ node, text: delta }], isFinal: false }];
                } else {
                  return [...prev, { role: 'agent', content: delta, thoughts: [], isFinal: false }];
                }
              }
            };
            
            if (activeTaskSessionRef.current && activeTaskSessionRef.current !== activeSessionRef.current) {
              const cached = sessionCacheRef.current[activeTaskSessionRef.current] || [];
              sessionCacheRef.current[activeTaskSessionRef.current] = updateMessages(cached);
            } else {
              setChatMessages(updateMessages);
            }
          }
        } else if (data.type === 'NODE_ACTIVITY') {
          // Tool calls are exclusively rendered and managed via TOOL_ACTIVITY
          if (data.payload?.action === 'tool_request' || data.payload?.action === 'tool_result') {
            return;
          }
          const updateMessages = (prev) => {
            const lastAgentIdx = prev.findLastIndex(m => m.role === 'agent' && !m.isFinal);
            if (lastAgentIdx !== -1) {
              const newMessages = [...prev];
              const last = newMessages[lastAgentIdx];
              const currentThoughts = last.thoughts || [];
              const updatedThoughts = [...currentThoughts];
              
              if (data.payload.status !== 'pending') {
                const pendingIdx = updatedThoughts.findLastIndex(t => 
                  t.type === 'NODE_ACTIVITY' && 
                  t.payload?.from_node === data.payload.from_node && 
                  t.payload?.to_node === data.payload.to_node && 
                  t.payload?.status === 'pending'
                );
                if (pendingIdx !== -1) {
                  updatedThoughts[pendingIdx] = { 
                    ...updatedThoughts[pendingIdx], 
                    payload: { ...updatedThoughts[pendingIdx].payload, ...data.payload }
                  };
                  newMessages[lastAgentIdx] = { ...last, thoughts: updatedThoughts };
                  return newMessages;
                }
              }
              
              updatedThoughts.push({
                type: 'NODE_ACTIVITY',
                payload: data.payload,
                node: data.payload.from_node || data.payload.node
              });
              
              newMessages[lastAgentIdx] = { ...last, thoughts: updatedThoughts };
              return newMessages;
            }
            return prev;
          };
          
          if (activeTaskSessionRef.current && activeTaskSessionRef.current !== activeSessionRef.current) {
            const cached = sessionCacheRef.current[activeTaskSessionRef.current] || [];
            sessionCacheRef.current[activeTaskSessionRef.current] = updateMessages(cached);
          } else {
            setChatMessages(updateMessages);
          }
        } else if (data.type === 'TOOL_ACTIVITY') {
          const updateMessages = (prev) => {
            const lastAgentIdx = prev.findLastIndex(m => m.role === 'agent' && !m.isFinal);
            if (lastAgentIdx !== -1) {
              const newMessages = [...prev];
              const last = newMessages[lastAgentIdx];
              const currentThoughts = last.thoughts || [];
              const updatedThoughts = [...currentThoughts];
              
              if (data.payload.status !== 'pending') {
                const pendingIdx = updatedThoughts.findLastIndex(t => 
                  t.type === 'TOOL_ACTIVITY' && 
                  t.payload?.tool_name === data.payload.tool_name && 
                  t.payload?.status === 'pending'
                );
                if (pendingIdx !== -1) {
                  const prevArgs = updatedThoughts[pendingIdx].payload?.tool_args;
                  const newArgs = data.payload.tool_args;
                  const finalArgs = (newArgs && Object.keys(newArgs).length > 0) ? newArgs : (prevArgs || {});
                  
                  updatedThoughts[pendingIdx] = { 
                    ...updatedThoughts[pendingIdx], 
                    payload: {
                      ...updatedThoughts[pendingIdx].payload,
                      ...data.payload,
                      tool_args: finalArgs
                    }
                  };
                  newMessages[lastAgentIdx] = { ...last, thoughts: updatedThoughts };
                  return newMessages;
                }
              }
              
              updatedThoughts.push({
                type: 'TOOL_ACTIVITY',
                payload: data.payload,
                node: data.payload.node
              });
              
              newMessages[lastAgentIdx] = { ...last, thoughts: updatedThoughts };
              return newMessages;
            }
            return prev;
          };
          
          if (activeTaskSessionRef.current && activeTaskSessionRef.current !== activeSessionRef.current) {
            const cached = sessionCacheRef.current[activeTaskSessionRef.current] || [];
            sessionCacheRef.current[activeTaskSessionRef.current] = updateMessages(cached);
          } else {
            setChatMessages(updateMessages);
          }
        } else if (data.type === 'TASK_COMPLETED') {
          setIsThinking(false);
          // E8-1.1a: the finished task's manifest is complete now.
          requestRewindManifest(data.payload?.session_id || activeTaskSessionRef.current || activeSessionRef.current);
          // E8-A8.1a: reconcile the cost surfaces with the ledger
          // now the task's calls are all recorded (also covers the
          // fast-chat path, whose calls publish no live bus events).
          requestCostBreakdown(data.payload?.session_id || activeSessionRef.current);
          requestContextBreakdown(data.payload?.session_id || activeSessionRef.current);
          // Remove from active tasks for Dashboard
          if (data.payload?.task_id || data.request_id) {
            setActiveTaskIds(prev => { const next = new Set(prev); next.delete(data.payload?.task_id || data.request_id); return next; });
          }
          if (data.payload?.session_id && !activeSessionRef.current) {
            setActiveSession(data.payload.session_id);
            if (wsRef.current) {
              wsRef.current.send(JSON.stringify({ type: 'SESSION_LIST_REQUEST', schema_version: 1, request_id: Date.now().toString() }));
        wsRef.current.send(JSON.stringify({ type: 'CONFIG_GET_REQUEST', schema_version: 1, request_id: Date.now().toString(), payload: {} }));
            }
          }
          let result = data.payload.response || data.payload.result || data.payload.output;

          // E1-16: THINKING deltas still buffered (no streaming bubble
          // ever appeared) join the final message's thoughts here —
          // reasoning is preserved, and still kept out of content.
          const completionKey = activeTaskSessionRef.current || data.payload?.session_id || activeSessionRef.current || '__pending';
          const bufferedThoughts = pendingThinkingRef.current[completionKey] || [];
          if (bufferedThoughts.length) delete pendingThinkingRef.current[completionKey];

          const updateCompletedMessages = (prev) => {
            let newMessages = [...prev];
            const lastAgentIdx = newMessages.findLastIndex(m => m.role === 'agent' && !m.isFinal);
            
            if (lastAgentIdx !== -1) {
              const last = newMessages[lastAgentIdx];
              const finalContent = (result !== undefined && result !== null) ? (typeof result !== 'string' ? JSON.stringify(result, null, 2) : result) : last.content;
              const baseThoughts = (last.thoughts && last.thoughts.length > 0) ? last.thoughts : (data.payload.thoughts || []);
              const finalThoughts = bufferedThoughts.length ? mergeThoughtEntries(baseThoughts, bufferedThoughts) : baseThoughts;
              
              newMessages[lastAgentIdx] = { ...last, content: finalContent, thoughts: finalThoughts, isFinal: true };
              return newMessages.map((m, i) => 
                (m.role === 'agent' && !m.isFinal && i !== lastAgentIdx) ? { ...m, isFinal: true, hasError: true, content: "**Task Cancelled**: Superseded by new request." } : m
              );
            } else {
              const veryLast = newMessages[newMessages.length - 1];
              if (veryLast && veryLast.role === 'agent' && veryLast.isFinal) return newMessages;
              const finalContent = (result !== undefined && result !== null) ? (typeof result !== 'string' ? JSON.stringify(result, null, 2) : result) : '';
              const createdThoughts = bufferedThoughts.length ? mergeThoughtEntries(data.payload.thoughts || [], bufferedThoughts) : (data.payload.thoughts || []);
              return [...newMessages, { role: 'agent', content: finalContent, thoughts: createdThoughts, isFinal: true }];
            }
          };
          
          if (activeTaskSessionRef.current && activeTaskSessionRef.current !== activeSessionRef.current) {
            const cached = sessionCacheRef.current[activeTaskSessionRef.current] || [];
            sessionCacheRef.current[activeTaskSessionRef.current] = updateCompletedMessages(cached);
          } else {
            setChatMessages(updateCompletedMessages);
          }
        } else if (data.type === 'TASK_FAILED' || data.type === 'TASK_CANCELLED') {
          setIsThinking(false);
          // E8-1.1a: failed/cancelled tasks still recorded points.
          requestRewindManifest(data.payload?.session_id || activeTaskSessionRef.current || activeSessionRef.current);
          if (data.payload?.task_id || data.request_id) {
            setActiveTaskIds(prev => { const next = new Set(prev); next.delete(data.payload?.task_id || data.request_id); return next; });
          }
          
          const updateFailedMessages = (prev) => {
            let newMessages = [...prev];
            const lastAgentIdx = newMessages.findLastIndex(m => m.role === 'agent' && !m.isFinal);
            
            const errorText = `**${data.type === 'TASK_CANCELLED' ? 'Task Cancelled' : 'Task Failed'}**: ${data.payload.error || data.payload.message || 'Unknown error'}`;
            
            if (lastAgentIdx !== -1) {
              const last = newMessages[lastAgentIdx];
              newMessages[lastAgentIdx] = { 
                ...last, 
                content: last.content ? `${last.content}\n\n${errorText}` : errorText, 
                isFinal: true,
                hasError: true
              };
              return newMessages.map((m, i) => 
                (m.role === 'agent' && !m.isFinal && i !== lastAgentIdx) ? { ...m, isFinal: true, hasError: true, content: "**Task Cancelled**: Superseded by new request." } : m
              );
            } else {
              const veryLast = newMessages[newMessages.length - 1];
              if (veryLast && veryLast.role === 'agent' && veryLast.isFinal) return newMessages;
              return [...newMessages, { role: 'agent', content: errorText, isFinal: true, hasError: true }];
            }
          };
          
          if (activeTaskSessionRef.current && activeTaskSessionRef.current !== activeSessionRef.current) {
            const cached = sessionCacheRef.current[activeTaskSessionRef.current] || [];
            sessionCacheRef.current[activeTaskSessionRef.current] = updateFailedMessages(cached);
          } else {
            setChatMessages(updateFailedMessages);
          }
        } else if (data.type === 'SESSION_RENAME_RESPONSE') {
          // Epic 1 UI batch (E1-08): the engine confirms renames; only
          // a confirmed rename changes the local list.
          if (data.payload?.renamed && data.payload.session_id) {
            setSessions(prev => prev.map(s => s.id === data.payload.session_id ? { ...s, title: data.payload.title } : s));
          }
        } else if (data.type === 'SESSION_TITLE_UPDATED') {
          // Epic 1 UI batch (E1-08): the engine wrote a model title for
          // a session after a task completed — reflect it in the list.
          if (data.payload?.session_id && data.payload.title) {
            setSessions(prev => prev.map(s => s.id === data.payload.session_id ? { ...s, title: data.payload.title } : s));
          }
        } else if (data.type === 'SESSION_SEARCH_RESPONSE') {
          // Epic 1 UI batch (E1-08): ignore stale answers for a query
          // the user has already replaced or cleared.
          if (data.payload && data.payload.query === sessionSearchQueryRef.current) {
            setSessionSearchResults(Array.isArray(data.payload.results) ? data.payload.results : []);
          }
        } else if (data.type === 'SESSION_LIST_RESPONSE') {
          setSessions(data.payload.sessions || []);
          if (data.payload.sessions?.length > 0 && !activeSessionRef.current) {
            handleSessionSwitch(data.payload.sessions[0].id);
          } else if (isFloating && initialSession) {
            handleSessionSwitch(initialSession);
          }
        } else if (data.type === 'SLASH_COMMAND_LIST_RESPONSE') {
          setSlashCommands(data.payload.commands || []);
        } else if (data.type === 'WORKSPACE_ADD_RESPONSE' || data.type === 'WORKSPACE_REMOVE_RESPONSE') {
          if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
            wsRef.current.send(JSON.stringify({ type: 'SLASH_COMMAND_LIST_REQUEST', schema_version: 1, request_id: Date.now().toString(), payload: {} }));
            wsRef.current.send(JSON.stringify({ type: 'SKILL_LIST_REQUEST', schema_version: 1, request_id: Date.now().toString(), payload: {} }));
          }
        } else if (data.type === 'SESSION_GET_RESPONSE') {
          // Only replace chat messages if we explicitly requested a session switch and it matches exactly.
          if (data.request_id && data.request_id === pendingSessionSwitchRef.current) {
            pendingSessionSwitchRef.current = null; // Clear immediately to ignore any subsequent echoed broadcasts
            const sess = data.payload.session;
            setChatMessages(prev => {
              const backendMessages = (sess && sess.messages) ? sess.messages.map(m => {
                let parsedThoughts = m.thoughts;
                if (typeof parsedThoughts === 'string') {
                  try {
                    parsedThoughts = JSON.parse(parsedThoughts);
                  } catch (e) {
                    parsedThoughts = [];
                  }
                }
                return {
                  ...m,
                  role: m.role === 'assistant' ? 'agent' : m.role,
                  isFinal: true,
                  thoughts: Array.isArray(parsedThoughts) ? parsedThoughts : []
                };
              }) : [];
              
              // If we are currently running a task in this session, the DB won't have its incomplete messages yet.
              // We must merge it back from our cache or `prev` so the UI doesn't lose the stream.
              if (activeTaskSessionRef.current === activeSessionRef.current && isThinking) {
                 const activeMsg = prev.find(m => m.role === 'agent' && !m.isFinal);
                 if (activeMsg) {
                    return [...backendMessages, activeMsg];
                 }
              }
              return backendMessages;
            });
            // E8-1.1a: surface the switched-to session's points.
            requestRewindManifest(sess?.id || activeSessionRef.current);
          }
        } else if (data.type === 'REWIND_MANIFEST_GET_RESPONSE') {
          // E8-1.1a: only the active session's manifest is surfaced;
          // an empty trail clears it (the step menus stay hidden).
          if (data.payload?.session_id === activeSessionRef.current) {
            const entries = Array.isArray(data.payload.entries) ? data.payload.entries : [];
            setRewindManifest(entries.length ? {
              session_id: data.payload.session_id,
              task_id: data.payload.task_id,
              entries,
            } : null);
          }
        } else if (data.type === 'TASK_REWIND_RESPONSE') {
          // E8-1.1b: the engine rolled the session back. M12: render
          // the retained history the ENGINE returns in the response —
          // its own record of what the rewind kept. The old local
          // structural cut diverged at the final point (the engine
          // discards no stored messages there, but the local cut
          // removed the response anyway). The structural cut remains
          // only as a fallback for responses without the history.
          // Then arm the undo window.
          if (data.payload?.session_id === activeSessionRef.current) {
            if (data.payload.ok) {
              if (Array.isArray(data.payload.messages)) {
                setChatMessages(data.payload.messages.map(m => {
                  let parsedThoughts = m.thoughts;
                  if (typeof parsedThoughts === 'string') {
                    try {
                      parsedThoughts = JSON.parse(parsedThoughts);
                    } catch (e) {
                      parsedThoughts = [];
                    }
                  }
                  return {
                    ...m,
                    role: m.role === 'assistant' ? 'agent' : m.role,
                    isFinal: true,
                    thoughts: Array.isArray(parsedThoughts) ? parsedThoughts : []
                  };
                }));
              } else {
                const step = data.payload.rewound_to_step;
                setChatMessages(prev => {
                  let taskStart = -1;
                  prev.forEach((m, j) => {
                    if (m && m.role === 'user' && !m.steering) taskStart = j;
                  });
                  if (taskStart === -1) return prev;
                  return step === 0 ? prev.slice(0, taskStart) : prev.slice(0, taskStart + 1);
                });
              }
              requestRewindManifest(data.payload.session_id);
              const expiresAt = (data.payload.undo_expires_at || 0) * 1000;
              setRewindUndo({ sessionId: data.payload.session_id, expiresAt });
              if (rewindUndoTimerRef.current) clearTimeout(rewindUndoTimerRef.current);
              rewindUndoTimerRef.current = setTimeout(() => {
                handleRewindUndoExpire();
              }, Math.max(0, expiresAt - Date.now()));
            } else if (data.payload.error) {
              console.warn('Rewind refused:', data.payload.error);
            }
          }
        } else if (data.type === 'TASK_REWIND_UNDO_RESPONSE') {
          // E8-1.1b: the discarded steps are back in the store; the
          // rendered list is rebuilt from it via a session fetch
          // (the SESSION_GET_RESPONSE handler owns that rebuild and
          // only honours the request id registered here).
          if (data.payload?.ok && data.payload?.restored) {
            const sessionId = activeSessionRef.current;
            clearRewindUndo();
            if (sessionId && wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
              const reqId = `rewind_restore_${Date.now()}`;
              pendingSessionSwitchRef.current = reqId;
              wsRef.current.send(JSON.stringify({
                type: 'SESSION_GET_REQUEST',
                schema_version: 1,
                request_id: reqId,
                payload: { session_id: sessionId }
              }));
              requestRewindManifest(sessionId);
            }
          } else {
            clearRewindUndo();
          }
        } else if (data.type === 'BROWSER_TASK_PAUSE_RESPONSE') {
          // Epic 8, E8-6.1: dedicated-profile agent browser control response.
          // State keys by the owning CHAT session id (payload's
          // chat_session_id); the browser session id is remembered
          // for exact addressing of later control messages (M04).
          const payload = data.payload || {};
          const sid = payload.chat_session_id || activeSessionRef.current;
          if (sid && payload.session_id) {
            browserSessionIdsRef.current[sid] = payload.session_id;
          }
          if (sid) {
            setBrowserSessions(prev => ({
              ...prev,
              [sid]: {
                controlState: payload.control_state,
                currentUrl: payload.current_url || '',
                challenge: payload.challenge || null,
                browserSessionId: payload.session_id || null,
              }
            }));
          }
        } else if (data.type === 'BROWSER_CHALLENGE_DETECTED') {
          // Epic 8, E8-6.1: CAPTCHA / 2FA park notification — agent never solves challenges.
          // Same keying as the control response above (M04).
          const payload = data.payload || {};
          const sid = payload.chat_session_id || activeSessionRef.current;
          if (sid && payload.session_id) {
            browserSessionIdsRef.current[sid] = payload.session_id;
          }
          if (sid) {
            setBrowserSessions(prev => ({
              ...prev,
              [sid]: {
                controlState: 'waiting_user',
                currentUrl: payload.url || '',
                challenge: { challenge: payload.challenge, url: payload.url },
                browserSessionId: payload.session_id || null,
              }
            }));
          }
        } else if (data.type === 'FALLBACK_STARTED') {
          setChatMessages(prev => [...prev, { 
            id: Date.now().toString(),
            role: 'system', 
            content: `⚠️ Primary model unreachable. Falling back to alternative provider...`,
            isFinal: true
          }]);
        } else if (data.type === 'FALLBACK_COMPLETED') {
          setChatMessages(prev => [...prev, { 
            id: Date.now().toString(),
            role: 'system', 
            content: `✅ Fallback successful. Routing request through ${data.payload.provider || 'backup provider'}.`,
            isFinal: true
          }]);
        } else if (data.type === 'MCP_RESOURCE_READ_RESPONSE') {
          // Issue 006: a resource loaded into the current chat appears
          // live as a system message (the engine also saved it to the
          // session, so it survives a reload). Only for our session —
          // a resource loaded into another session is not ours to show.
          if (data.payload.success && data.payload.message
              && data.payload.session_id === activeSessionRef.current) {
            setChatMessages(prev => [...prev, {
              id: data.request_id || Date.now().toString(),
              role: 'system',
              content: data.payload.message,
              isFinal: true
            }]);
          }
        } else if (data.type === 'MCP_PROMPT_RUN_RESPONSE') {
          // Issue 007: a prompt run into the current chat appears live,
          // one message per prompt message, with the prompt's own role.
          // Only for our session.
          if (data.payload.success && Array.isArray(data.payload.messages)
              && data.payload.session_id === activeSessionRef.current) {
            setChatMessages(prev => [...prev, ...data.payload.messages.map((m, i) => ({
              id: `${data.request_id || Date.now().toString()}-${i}`,
              role: m.role,
              content: m.content,
              isFinal: true
            }))]);
          }
        } else if (data.type === 'TOOL_APPROVAL_REQUEST') {
          setIsThinking(false);
          const approvalMsg = { 
            id: data.request_id || Date.now().toString(),
            role: 'tool_approval', 
            tool_name: data.payload.tool_name,
            tool_args: data.payload.tool_args || data.payload.args || {},
            approval_key: data.payload.approval_key || data.request_id,
            // Epic 2: engine-built preview ({diff} for edit_file, or
            // {error} when no preview could be built, or null).
            preview: data.payload.preview || null,
            // Epic 8, E8-6.2: browser confirmation details (category
            // + the exact action named from the page) when this
            // approval is a browser confirmation; null otherwise.
            browser_confirmation: data.payload.browser_confirmation || null,
            isFinal: true
          };
          if (activeTaskSessionRef.current && activeTaskSessionRef.current !== activeSessionRef.current) {
            const cached = sessionCacheRef.current[activeTaskSessionRef.current] || [];
            sessionCacheRef.current[activeTaskSessionRef.current] = [...cached, approvalMsg];
          } else {
            setChatMessages(prev => [...prev, approvalMsg]);
          }
        } else if (data.type === 'PLUGIN_APPROVAL_REQUEST') {
          setIsThinking(false);
          setChatMessages(prev => [...prev, { 
            id: data.request_id || Date.now().toString(),
            role: 'plugin_approval', 
            plugin_name: data.payload.plugin_name,
            plugin_details: data.payload.plugin_details,
            isFinal: true
          }]);
        } else if (data.type === 'SESSION_CREATE_RESPONSE') {
          const newId = data.payload.session_id;
          setActiveSession(newId);
          // A plan-mode choice made before the session existed (the
          // '__pending' key) is adopted by the new session.
          setPlanModeBySession(prev => {
            if (!prev.__pending) return prev;
            const next = { ...prev, [newId]: prev.__pending };
            delete next.__pending;
            return next;
          });
          // Same adoption for a scratchpad that arrived pre-session.
          setScratchpadBySession(prev => {
            if (!prev.__pending) return prev;
            const next = { ...prev, [newId]: prev.__pending };
            delete next.__pending;
            return next;
          });
          if (!data.payload?.preserve_messages) {
            setChatMessages([]);
          }
          // Refresh list
          if (wsRef.current) {
            wsRef.current.send(JSON.stringify({ type: 'SESSION_LIST_REQUEST', schema_version: 1, request_id: Date.now().toString() }));
        wsRef.current.send(JSON.stringify({ type: 'CONFIG_GET_REQUEST', schema_version: 1, request_id: Date.now().toString(), payload: {} }));
          }
        } else if (data.type === 'SESSION_DELETE_RESPONSE') {
          const deletedId = data.payload.session_id;
          setSessions(prev => {
            const next = prev.filter(s => s.id !== deletedId);
            if (activeSession === deletedId) {
              if (next.length > 0) {
                handleSessionSwitch(next[0].id);
              } else {
                setActiveSession(null);
                setChatMessages([]);
              }
            }
            return next;
          });
        } else if (data.type === 'SESSION_EXPORT_RESPONSE') {
          if (data.payload?.error) {
            setExportStatus({ type: 'error', message: `Export failed: ${data.payload.error}` });
          } else {
            if (data.payload?.content) {
              const mime = data.payload.format === 'markdown' ? 'text/markdown;charset=utf-8' : 'application/json;charset=utf-8';
              const blob = new Blob([data.payload.content], { type: mime });
              const url = URL.createObjectURL(blob);
              const a = document.createElement('a');
              a.href = url;
              a.download = data.payload.filename || `session.${data.payload.format === 'markdown' ? 'md' : 'json'}`;
              document.body.appendChild(a);
              a.click();
              document.body.removeChild(a);
              URL.revokeObjectURL(url);
            }
            setExportStatus({
              type: 'success',
              message: `Exported ${data.payload?.filename || data.payload?.format || 'session'}`
            });
          }
        } else if (data.type === 'SESSION_FORK_RESPONSE') {
          // E8-1.2: the fork is a new session ("Fork of <title>")
          // holding the history up to the chosen step. Refresh the
          // list and land the user in it; the source is untouched.
          if (data.payload?.ok && data.payload?.new_session_id) {
            if (wsRef.current) {
              wsRef.current.send(JSON.stringify({
                type: 'SESSION_LIST_REQUEST',
                schema_version: 1,
                request_id: Date.now().toString()
              }));
            }
            handleSessionSwitch(data.payload.new_session_id);
          } else if (data.payload?.error) {
            console.warn('Fork refused:', data.payload.error);
          }
        } else if (data.type === 'SESSION_IMPORT_RESPONSE') {
          if (data.payload?.ok) {
            if (wsRef.current) {
              wsRef.current.send(JSON.stringify({
                type: 'SESSION_LIST_REQUEST',
                schema_version: 1,
                request_id: Date.now().toString()
              }));
            }
            if (data.payload.session_id) {
              handleSessionSwitch(data.payload.session_id);
            }
            setImportStatus({ type: 'success', message: `Restored session: ${data.payload.title}` });
          } else {
            setImportStatus({ type: 'error', message: data.payload?.error || 'Failed to import session' });
          }
        } else if (data.type === 'PROVIDER_LIST_RESPONSE') {
          // sends a list; an object payload is normalised via Object.values
          // and a missing one becomes []. Never store an object here — the
          // old `|| {}` fallback is what made MODEL_DEFAULT_CHANGED below
          // silently no-op (its Array.isArray guard bailed out).
          const list = data.payload.providers;
          setProviders(Array.isArray(list) ? list : (list && typeof list === 'object' ? Object.values(list) : []));
          setProvidersLoaded(true);
          // Epic 5, E5-05: per-provider health travels with the list.
          if (data.payload.health && typeof data.payload.health === 'object') setProviderHealth(data.payload.health);
        } else if (data.type === 'CONFIG_GET_RESPONSE' || data.type === 'CONFIG_UPDATE_RESPONSE' || data.type === 'CONFIG_RESET_RESPONSE') {
          if (data.payload && data.payload.config) setEngineConfig(data.payload.config);
        } else if (data.type === 'PROVIDER_REMOVE_RESPONSE' || data.type === 'PROVIDER_SAVE_RESPONSE') {
          if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
            wsRef.current.send(JSON.stringify({ type: 'PROVIDER_LIST_REQUEST', schema_version: 1, request_id: Date.now().toString() }));
          }
        } else if (data.type === 'MODEL_DEFAULT_CHANGED') {
          setProviders(prev => {
            if (!Array.isArray(prev)) return prev;
            return prev.map(p => {
              if (p.name === data.payload.provider) {
                return { ...p, is_default: true, default_model: data.payload.model };
              }
              return { ...p, is_default: false };
            });
          });
        } else if (data.type === 'LOCAL_MODEL_LIST_RESPONSE') {
          setLocalModels(data.payload.models || []);
        } else if (data.type === 'PLUGIN_LIST_RESPONSE') {
          const plugins = data.payload.plugins;
          const pluginArr = Array.isArray(plugins) ? plugins : Object.values(plugins || {});
          setPluginCount(pluginArr.filter(p => p.enabled !== false).length);
        } else if (data.type === 'ARTIFACT_LIST_RESPONSE') {
          // Epic 8, E8-2.3: ignore global requests meant for GlobalArtifactsView
          if (data.request_id && data.request_id.startsWith('global_artifacts_')) {
            return;
          }
          const list = data.payload?.artifacts || [];
          setCanvasArtifacts(list);
          const savedId = activeSession ? localStorage.getItem(`aether_canvas_selected_${activeSession}`) : null;
          if (savedId && list.some(a => a.id === savedId)) {
            setSelectedArtifactId(savedId);
            if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
              wsRef.current.send(JSON.stringify({
                type: 'ARTIFACT_GET_REQUEST',
                schema_version: 1,
                request_id: Date.now().toString(),
                payload: { artifact_id: savedId }
              }));
            }
          }
        } else if (data.type === 'ARTIFACT_RECORDED') {
          if (data.payload?.session_id === activeSession) {
            setCanvasArtifacts(prev => {
              const filtered = prev.filter(a => a.id !== data.payload.artifact_id);
              const artObj = {
                id: data.payload.artifact_id,
                session_id: data.payload.session_id,
                task_id: data.payload.task_id,
                kind: data.payload.kind,
                title: data.payload.title,
                file_path: data.payload.path,
                created_at: data.payload.created_at
              };
              return [artObj, ...filtered];
            });
            // Never auto-opens during task execution per PRD
          }
        } else if (data.type === 'ARTIFACT_GET_RESPONSE') {
          setActiveArtifactContent(data.payload);
        } else if (data.type === 'DEV_SERVER_DETECT_RESPONSE') {
          // Epic 8, E8-2.2: dev-server probe result for the Preview tab
          setDevServerInfo(data.payload || null);
        } else if (data.type === 'VOICE_SESSION_START_RESPONSE') {
          // Epic 8, E8-5.1a: ok -> the overlay opens and the mic
          // starts; refused -> the engine's honest reason shows and
          // no device is ever opened.
          const p = data.payload || {};
          const current = voiceStateRef.current;
          if (current) {
            if (p.ok) {
              applyVoiceState({
                ...current,
                status: 'active',
                sessionId: p.session_id || current.sessionId,
                webrtcConfig: p.webrtc_config || null,
                error: null,
              });
              if (p.session_id && p.session_id !== activeSessionRef.current) {
                setActiveSession(p.session_id);
              }
            } else {
              applyVoiceState({
                ...current,
                status: 'error',
                sessionId: p.session_id || current.sessionId,
                error: p.error || 'Voice is unavailable on this machine.',
                webrtcConfig: null,
              });
            }
          }
        } else if (data.type === 'VOICE_PARTIAL_TRANSCRIPT') {
          const p = data.payload || {};
          const current = voiceStateRef.current;
          if (current && current.status === 'active' && p.session_id === current.sessionId) {
            if (p.is_final) {
              // Commit in order: the previous reply if it was still
              // streaming, then this user turn.
              commitVoiceAssistant(current);
              if (p.text) {
                setChatMessages(prev => [...prev, {
                  id: `voice-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
                  role: 'user', content: p.text, isFinal: true,
                }]);
              }
              applyVoiceState({ ...current, partialText: '', assistantText: '', announcementText: '' });
            } else {
              applyVoiceState({ ...current, partialText: p.text || '' });
            }
          }
        } else if (data.type === 'VOICE_OUTPUT_CHUNK') {
          const p = data.payload || {};
          const current = voiceStateRef.current;
          if (current && current.status === 'active' && p.session_id === current.sessionId) {
            if (p.text_delta) {
              if (p.announcement_id) {
                // N10: announcement text is not a reply turn. It
                // is tracked apart from assistantText so it can
                // never be committed as a chat message — the
                // announced task's own message already stands in
                // chat — while the overlay still shows what is
                // being announced. Audio below plays as usual.
                applyVoiceState({
                  ...current,
                  announcementText: (current.announcementText || '') + p.text_delta,
                });
              } else {
                applyVoiceState({
                  ...current,
                  assistantText: (current.assistantText || '') + p.text_delta,
                });
              }
            }
            if (p.audio_chunk) {
              voiceChunkSeqRef.current += 1;
              setVoiceAudioChunk({ seq: voiceChunkSeqRef.current, data: p.audio_chunk });
            }
          }
        } else if (data.type === 'VOICE_BARGE_IN') {
          // E8-5.1b: the client honours the committed-words rule
          // by replacing the streaming bubble with exactly the
          // text that was actually heard, and cuts the overlay's
          // scheduled playback so the unheard tail never plays.
          setVoicePlaybackCut(c => c + 1);
          const p = data.payload || {};
          const current = voiceStateRef.current;
          if (current && p.session_id === current.sessionId) {
            const committed = p.committed_text || '';
            // N10: a barge-in that interrupted an announcement
            // (tagged with its announcement_id) commits nothing —
            // the heard prefix belongs to the task's own message,
            // which already stands in chat.
            if (committed.trim() && !p.announcement_id) {
              setChatMessages(prev => [...prev, {
                id: `voice-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
                role: 'agent', content: committed, thoughts: [], isFinal: true,
              }]);
            }
            applyVoiceState({ ...current, assistantText: '', announcementText: '' });
          }
        } else if (data.type === 'VOICE_SESSION_STOP_RESPONSE') {
          // A local stop already tore the UI down; this only lands
          // for a stop we did not initiate.
          if (voiceStateRef.current) applyVoiceState(null);
        } else if (data.type === 'VOICE_RTC_OFFER_RESPONSE') {
          // F-12b: the engine's answer goes down to the overlay's
          // peer connection; a refusal is the voice error state.
          const p = data.payload || {};
          const current = voiceStateRef.current;
          if (current && p.session_id === current.sessionId) {
            if (p.ok && p.answer) {
              voiceRtcSeqRef.current += 1;
              setVoiceRtcAnswer({ seq: voiceRtcSeqRef.current, answer: p.answer });
            } else {
              applyVoiceState({
                ...current,
                status: 'error',
                error: p.error || 'Voice connection failed.',
                webrtcConfig: null,
              });
            }
          }
        } else if (data.type === 'VOICE_RTC_ICE') {
          // F-12b: one of the engine's own ICE candidates (or the
          // null end-of-candidates marker) for the overlay's peer.
          const p = data.payload || {};
          const current = voiceStateRef.current;
          if (current && current.status === 'active' && p.session_id === current.sessionId) {
            voiceRtcSeqRef.current += 1;
            setVoiceRtcRemoteCandidate({
              seq: voiceRtcSeqRef.current,
              candidate: p.candidate ?? null,
            });
          }
        } else if (data.type === 'DICTATION_START_RESPONSE') {
          const p = data.payload || {};
          if (p.ok) {
            setDictationState({ status: 'active', error: null, text: '' });
          } else {
            setDictationState({
              status: 'error',
              error: p.error || 'Dictation unavailable',
              text: '',
            });
          }
        } else if (data.type === 'DICTATION_CHUNK_RESPONSE') {
          const p = data.payload || {};
          if (p.text !== undefined && p.text !== null) {
            setDictationState(prev => ({
              ...(prev || {}),
              text: p.text,
            }));
          }
        } else if (data.type === 'DICTATION_STOP_RESPONSE') {
          const p = data.payload || {};
          if (p.text !== undefined && p.text !== null) {
            setDictationState(prev => ({
              ...(prev || {}),
              status: 'idle',
              text: p.text,
            }));
          }
        } else if (data.type === 'READ_ALOUD_START_RESPONSE') {
          const p = data.payload || {};
          if (p.ok) {
            setReadAloudState({
              messageId: p.message_id,
              status: 'playing',
              error: null,
            });
          } else {
            setReadAloudState({
              messageId: p.message_id,
              status: 'error',
              error: p.error || 'TTS unavailable',
            });
          }
        } else if (data.type === 'READ_ALOUD_CHUNK') {
          const p = data.payload || {};
          if (p.audio_chunk) {
            try {
              const Ctx = window.AudioContext || window.webkitAudioContext;
              if (Ctx) {
                if (!readAloudAudioCtxRef.current) readAloudAudioCtxRef.current = new Ctx();
                const ctx = readAloudAudioCtxRef.current;
                const binary = window.atob(p.audio_chunk);
                const bytes = new Uint8Array(binary.length);
                for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
                const buffer = bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength);
                const decoded = ctx.decodeAudioData(buffer);
                if (decoded && typeof decoded.then === 'function') {
                  decoded.then((audioBuffer) => {
                    const source = ctx.createBufferSource();
                    source.buffer = audioBuffer;
                    source.connect(ctx.destination);
                    source.onended = () => { readAloudSourcesRef.current.delete(source); };
                    readAloudSourcesRef.current.add(source);
                    const startAt = Math.max(ctx.currentTime, readAloudPlayHeadRef.current);
                    source.start(startAt);
                    readAloudPlayHeadRef.current = startAt + (audioBuffer.duration || 0);
                  });
                }
              }
            } catch (err) {
              console.error('Failed to decode read-aloud chunk:', err);
            }
          }
          if (p.is_final) {
            // Finished playback
          }
        } else if (data.type === 'READ_ALOUD_STOP_RESPONSE') {
          stopAppReadAloud();
          setReadAloudState(null);
        } else if (data.type === 'USAGE_LEDGER_QUERY_RESPONSE') {
          // Epic 8, E8-A8.1a: rebuild the cost surfaces from the
          // ledger rows themselves, so the header total and the
          // popover always reconcile with the recorded calls.
          setCostTasks(groupEntriesByTask(data.payload?.entries || []));
        } else if (data.type === 'TASK_COST_UPDATE') {
          // Epic 8, E8-A8.1a: live tick. The engine read these
          // figures back from the ledger row it just recorded, so
          // merging them keeps the header in step with usage.db.
          const p = data.payload || {};
          if (p.session_id && p.session_id === activeSessionRef.current) {
            setLastCostTaskId(p.task_id);
            setCostTasks(prev => {
              const existing = prev.find(t => t.task_id === p.task_id);
              const base = existing || {
                task_id: p.task_id, cost_usd: 0, input_tokens: 0,
                output_tokens: 0, last_ts: 0, by_model: {},
              };
              const byModel = { ...base.by_model };
              const model = { ...(byModel[p.model] || {
                cost_usd: 0, input_tokens: 0, output_tokens: 0,
                calls: 0, is_local: false, unpriced: false,
              }) };
              model.cost_usd += p.delta_cost_usd || 0;
              model.input_tokens += p.input_tokens || 0;
              model.output_tokens += p.output_tokens || 0;
              model.calls += 1;
              if (p.is_local) model.is_local = true;
              if (p.pricing_status === 'unpriced') model.unpriced = true;
              byModel[p.model] = model;
              const updated = {
                ...base,
                cost_usd: typeof p.task_total_cost_usd === 'number'
                  ? p.task_total_cost_usd
                  : base.cost_usd + (p.delta_cost_usd || 0),
                input_tokens: base.input_tokens + (p.input_tokens || 0),
                output_tokens: base.output_tokens + (p.output_tokens || 0),
                last_ts: Date.now() / 1000,
                by_model: byModel,
              };
              return [updated, ...prev.filter(t => t.task_id !== p.task_id)];
            });
            requestContextBreakdown(p.session_id);
          }
        } else if (data.type === 'CONTEXT_BREAKDOWN_RESPONSE') {
          // Epic 8, E8-A8.1b: segmented context breakdown for header and settings
          if (data.payload) {
            setContextBreakdown(data.payload);
          }
        } else if (data.type === 'DAILY_BUDGET_SET_RESPONSE') {
          // Epic 8, E8-A8.1b: daily budget updated
          setDailyBudget(data.payload?.daily_budget_usd != null ? data.payload.daily_budget_usd : null);
        } else if (data.type === 'DAILY_BUDGET_EXCEEDED') {
          // Epic 8, E8-A8.1b: soft alert on daily budget (running task finishes uninterrupted)
          setBudgetAlert(data.payload);
        } else if (data.type === 'ERROR') {
          setIsThinking(false);
          setChatMessages(prev => {
            const errorText = `**Error:** ${data.payload.message || 'Unknown error'}`;
            const lastAgentIdx = prev.findLastIndex(m => m.role === 'agent' && !m.isFinal);
            if (lastAgentIdx !== -1) {
              const newMessages = [...prev];
              const last = newMessages[lastAgentIdx];
              newMessages[lastAgentIdx] = { 
                ...last, 
                content: last.content ? `${last.content}\n\n${errorText}` : errorText, 
                isFinal: true,
                hasError: true
              };
              return newMessages.map((m, i) => 
                (m.role === 'agent' && !m.isFinal && i !== lastAgentIdx) ? { ...m, isFinal: true, hasError: true, content: "**Task Cancelled**: Superseded by new request." } : m
              );
            } else {
              const veryLast = prev[prev.length - 1];
              if (veryLast && veryLast.role === 'agent' && veryLast.isFinal) return prev;
              return [...prev, { role: 'agent', content: errorText, isFinal: true, hasError: true }];
            }
          });
        }
    };

    // Single subscription, via the facade. Registered synchronously so the
    // effect cleanup (StrictMode double-mount included) always removes it —
    // the old async initConnection leaked the first subscription, which is
    // what the deleted dedup hack was papering over.
    if (wsRef.current) {
      wsRef.current.addEventListener('message', handleMessage);
    }

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
      if (wsRef.current) {
        wsRef.current.removeEventListener('message', handleMessage);
      }
      unsubscribers.forEach(unsub => {
        if (typeof unsub === 'function') unsub();
      });
    };
  }, []);

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
        wsRef.current.send(JSON.stringify({ type: 'SESSION_LIST_REQUEST', schema_version: 1, request_id: Date.now().toString() }));
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
    }
    wasConnectedRef.current = isConnected;
  }, [isConnected, wsRef]);

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
      } else {
        setActiveArtifactContent(null);
      }
    } else if (!activeSession) {
      setCanvasArtifacts([]);
      setSelectedArtifactId(null);
      setActiveArtifactContent(null);
    }
  }, [activeSession, isConnected, wsRef]);

  // Epic 8, E8-A8.1a/b: sync the cost and context surfaces when the session changes
  useEffect(() => {
    setCostTasks([]);
    setLastCostTaskId(null);
    requestCostBreakdown();
    requestContextBreakdown();
  }, [activeSession, isConnected, requestCostBreakdown, requestContextBreakdown]);

  const handleToggleCanvas = useCallback(() => {
    setIsCanvasOpen(prev => {
      const next = !prev;
      if (next && canvasArtifacts.length > 0 && !selectedArtifactId) {
        const firstId = canvasArtifacts[0].id;
        setSelectedArtifactId(firstId);
        if (activeSession) {
          localStorage.setItem(`aether_canvas_selected_${activeSession}`, firstId);
        }
        if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
          wsRef.current.send(JSON.stringify({
            type: 'ARTIFACT_GET_REQUEST',
            schema_version: 1,
            request_id: Date.now().toString(),
            payload: { artifact_id: firstId }
          }));
        }
      }
      return next;
    });
  }, [canvasArtifacts, selectedArtifactId, activeSession, wsRef]);

  const handleSelectArtifact = useCallback((artifactId) => {
    setSelectedArtifactId(artifactId);
    if (activeSession) {
      localStorage.setItem(`aether_canvas_selected_${activeSession}`, artifactId);
    }
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'ARTIFACT_GET_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: { artifact_id: artifactId }
      }));
    }
  }, [activeSession, wsRef]);

  // Epic 8, E8-2.2: ask the engine to probe for a dev server in the
  // artifact's project directory; the answer lands in devServerInfo
  // via DEV_SERVER_DETECT_RESPONSE.
  const handleDetectDevServer = useCallback(({ workspace_root = null, project_path = null } = {}) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'DEV_SERVER_DETECT_REQUEST',
        schema_version: 1,
        request_id: `dev_server_detect_${Date.now()}`,
        payload: { workspace_root, project_path }
      }));
    }
  }, [wsRef]);

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
    } else if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'ARTIFACT_LIST_REQUEST',
        schema_version: 1,
        request_id: `session_artifacts_${activeSession}_${Date.now()}`,
        payload: { session_id: activeSession, global: false }
      }));
    }
  }, [canvasArtifacts, handleSelectArtifact, activeSession, wsRef]);

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

  const handleSessionSwitch = (id) => {
    // E8-1.1a: never show one session's rewind points in another;
    // the fresh manifest is fetched on SESSION_GET_RESPONSE.
    setRewindManifest(null);
    // E8-1.1b: a staged rewind belongs to the session it rewound;
    // its toast (and confirm modal) must not follow the user into
    // another session. The engine-side window keeps running and
    // prunes on expiry.
    setRewindConfirm(null);
    if (rewindUndo && rewindUndo.sessionId !== id) clearRewindUndo();
    setChatMessages(prev => {
      if (activeSessionRef.current) {
        sessionCacheRef.current[activeSessionRef.current] = prev;
      }
      return sessionCacheRef.current[id] || [];
    });
    setActiveSession(id);
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      const reqId = `switch_${Date.now()}`;
      pendingSessionSwitchRef.current = reqId;
      wsRef.current.send(JSON.stringify({
        type: 'SESSION_GET_REQUEST',
        schema_version: 1,
        request_id: reqId,
        payload: { session_id: id }
      }));
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
    if (artifact.id && wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'ARTIFACT_GET_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: { artifact_id: artifact.id }
      }));
    }
    setActiveTab('Chat');

    if (artifact.message_id) {
      queueMessageScroll(artifact.session_id || activeSession, artifact.message_id);
    }
  }, [activeSession, wsRef, queueMessageScroll]);

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

  const handleNewChat = () => {
    setRewindManifest(null); // E8-1.1a: a fresh chat has no points yet
    setChatMessages([]); // Clear immediately for instant feel
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'SESSION_CREATE_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: { title: 'New Conversation' }
      }));
    }
  };

  // Epic 6, E6-02: completing or skipping onboarding persists the
  // flag engine-side so the overlay appears exactly once.
  const handleOnboardingDone = useCallback(() => {
    setEngineConfig(prev => (prev ? { ...prev, onboarding_completed: true } : prev));
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'CONFIG_UPDATE_REQUEST', schema_version: 1,
        request_id: Date.now().toString(),
        payload: { config: { onboarding_completed: true } },
      }));
    }
  }, [wsRef]);

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
    (sessions || []).forEach(sess => {
      items.push({
        id: `sess-${sess.id}`, kind: 'session',
        label: sess.title || 'Untitled chat',
        run: () => { handleSessionSwitch(sess.id); setActiveTab('Chat'); },
      });
    });
    (Array.isArray(providers) ? providers : [])
      .filter(p => p.has_key && Array.isArray(p.models))
      .forEach(p => {
        p.models.forEach(m => {
          items.push({
            id: `model-${p.name}-${m}`, kind: 'model', label: m, hint: p.name,
            run: () => {
              if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
                wsRef.current.send(JSON.stringify({
                  type: 'MODEL_SET_DEFAULT', schema_version: 1,
                  request_id: Date.now().toString(),
                  payload: { provider: p.name, model: m },
                }));
              }
            },
          });
        });
      });
    return items;
  }, [sessions, providers, wsRef]); // eslint-disable-line react-hooks/exhaustive-deps

  // Epic 6, E6-03: the wired shortcut set (the dialog lists exactly
  // these): Ctrl+K palette, Ctrl+N new chat, Ctrl+, Settings.
  useEffect(() => {
    if (isFloating) return undefined;
    const onKey = (e) => {
      const mod = e.ctrlKey || e.metaKey;
      if (mod && (e.key === 'k' || e.key === 'K')) {
        e.preventDefault();
        setPaletteOpen(prev => !prev);
      } else if (mod && (e.key === 'n' || e.key === 'N')) {
        e.preventDefault();
        handleNewChat();
        setActiveTab('Chat');
      } else if (mod && e.key === ',') {
        e.preventDefault();
        setActiveTab('Settings');
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [isFloating]); // eslint-disable-line react-hooks/exhaustive-deps

  const handleDeleteSession = (id, e) => {
    e.stopPropagation(); // prevent triggering handleSessionSwitch
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'SESSION_DELETE_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: { session_id: id }
      }));
    }
  };

  const handleExportSession = (format) => {
    if (!activeSession) return;
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: 'SESSION_EXPORT_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: { session_id: activeSession, format }
      }));
    }
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
        if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
          wsRef.current.send(JSON.stringify({
            type: 'SESSION_IMPORT_REQUEST',
            schema_version: 1,
            request_id: Date.now().toString(),
            payload: {
              format: 'zip',
              content: b64,
              file_path: file.path || null
            }
          }));
        }
      };
      reader.readAsDataURL(file);
    } else {
      reader.onload = (event) => {
        if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
          wsRef.current.send(JSON.stringify({
            type: 'SESSION_IMPORT_REQUEST',
            schema_version: 1,
            request_id: Date.now().toString(),
            payload: {
              format: 'json',
              content: event.target.result,
              file_path: file.path || null
            }
          }));
        }
      };
      reader.readAsText(file);
    }
  };

  const handleToolApproval = (requestId, approved, approvalKey, scope, editedArgs, browserEnvelope) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      const key = approvalKey || requestId;
      wsRef.current.send(JSON.stringify({
        type: approved ? 'TOOL_APPROVAL_GRANTED' : 'TOOL_APPROVAL_REJECTED',
        schema_version: 1,
        request_id: requestId,
        payload: {
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
        }
      }));
      // Remove the approval card from chat
      setChatMessages(prev => prev.filter(m => m.id !== requestId && m.approval_key !== key));
      if (approved) setIsThinking(true);
    }
  };

  const handleChangeModel = (provider, model) => {
    if (wsRef.current) {
      wsRef.current.send(JSON.stringify({
        type: 'MODEL_SET_DEFAULT',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: { provider, model }
      }));
    }
  };

  const handlePluginApproval = (requestId, approved) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        type: approved ? 'PLUGIN_APPROVAL_GRANTED' : 'PLUGIN_APPROVAL_REJECTED',
        schema_version: 1,
        request_id: requestId,
        payload: {}
      }));
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
    if (wsRef.current && wsRef.current.send) {
      wsRef.current.send(JSON.stringify({
        type: 'CANCEL_TASK',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: {}
      }));
    }
    setIsThinking(false);
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

  // Epic 1 UI batch (E1-08): session rename (inline edit in the list).
  const handleStartRenameSession = (session, e) => {
    if (e) e.stopPropagation();
    setEditingSessionId(session.id);
    setEditingSessionTitle(session.title || session.name || '');
  };

  const handleCancelRenameSession = () => {
    setEditingSessionId(null);
    setEditingSessionTitle('');
  };

  const handleCommitRenameSession = (sessionId) => {
    if (editingSessionId !== sessionId) return; // Escape-cancel already closed it
    const title = editingSessionTitle.trim();
    setEditingSessionId(null);
    setEditingSessionTitle('');
    if (!title) return; // an empty title cancels instead of renaming
    if (wsRef.current && wsRef.current.send) {
      wsRef.current.send(JSON.stringify({
        type: 'SESSION_RENAME_REQUEST',
        schema_version: 1,
        request_id: Date.now().toString(),
        payload: { session_id: sessionId, title }
      }));
    }
  };

  // Epic 1 UI batch (E1-08): debounced session search. An empty query
  // restores the normal list; results are only applied when they answer
  // the current query (see SESSION_SEARCH_RESPONSE above).
  useEffect(() => {
    sessionSearchQueryRef.current = sessionSearchQuery;
    if (sessionSearchTimerRef.current) clearTimeout(sessionSearchTimerRef.current);
    if (!sessionSearchQuery.trim()) {
      setSessionSearchResults(null);
      return;
    }
    sessionSearchTimerRef.current = setTimeout(() => {
      if (wsRef.current && wsRef.current.send) {
        wsRef.current.send(JSON.stringify({
          type: 'SESSION_SEARCH_REQUEST',
          schema_version: 1,
          request_id: Date.now().toString(),
          payload: { query: sessionSearchQuery }
        }));
      }
    }, 300);
    return () => {
      if (sessionSearchTimerRef.current) clearTimeout(sessionSearchTimerRef.current);
    };
  }, [sessionSearchQuery]);

  return (
    <div style={{ width: '100vw', height: '100vh', display: 'flex', overflow: 'hidden', backgroundColor: isFloating ? 'transparent' : '#05050f', flexDirection: 'column' }}>
      
      {!isFloating && (
        <TitleBar 
          isChatDetached={isChatDetached}
          onReattachChat={handleReattachChat}
          isThinking={isThinking}
          onStopTask={handleStopTask}
        />
      )}
      
      {/* Glowing Orbs for Glassmorphism pop */}
      {!isFloating && (
        <>
          <div style={{ position: 'absolute', top: '10%', left: '20%', width: '500px', height: '500px', background: 'radial-gradient(circle, rgba(124, 58, 237, 0.25) 0%, rgba(0,0,0,0) 70%)', filter: 'blur(80px)', zIndex: 2, pointerEvents: 'none', borderRadius: '50%', animation: 'float1 20s infinite ease-in-out' }}></div>
          <div style={{ position: 'absolute', bottom: '10%', right: '15%', width: '600px', height: '600px', background: 'radial-gradient(circle, rgba(236, 72, 153, 0.2) 0%, rgba(0,0,0,0) 70%)', filter: 'blur(100px)', zIndex: 2, pointerEvents: 'none', borderRadius: '50%', animation: 'float2 25s infinite ease-in-out reverse' }}></div>
          <div style={{ position: 'absolute', top: '40%', left: '50%', width: '400px', height: '400px', background: 'radial-gradient(circle, rgba(59, 130, 246, 0.2) 0%, rgba(0,0,0,0) 70%)', filter: 'blur(90px)', zIndex: 2, pointerEvents: 'none', borderRadius: '50%', transform: 'translate(-50%, -50%)', animation: 'float3 15s infinite ease-in-out' }}></div>
        </>
      )}

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
              background: 'rgba(255,255,255,0.05)',
              border: '1px solid rgba(255,255,255,0.1)',
              color: '#e2e8f0',
              cursor: 'pointer',
              padding: '6px',
              borderRadius: '8px',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              transition: 'transform 160ms cubic-bezier(0.23, 1, 0.32, 1), background 160ms ease-out',
              backdropFilter: 'blur(8px)'
            }}
            onMouseDown={(e) => e.currentTarget.style.transform = 'scale(0.95)'}
            onMouseUp={(e) => e.currentTarget.style.transform = 'scale(1)'}
            onMouseLeave={(e) => {
              e.currentTarget.style.transform = 'scale(1)';
              e.currentTarget.style.background = 'rgba(255,255,255,0.05)';
            }}
            onMouseEnter={(e) => e.currentTarget.style.background = 'rgba(255,255,255,0.1)'}
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
                <h2 style={{ fontSize: '18px', margin: 0, color: '#fff' }}>AETHER-OS</h2>
                <div style={{ fontSize: '12px', color: '#a78bfa', textTransform: 'uppercase', letterSpacing: '1px' }}>{status}</div>
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
                <h3 style={{ fontSize: '12px', textTransform: 'uppercase', color: '#9090a0', letterSpacing: '1px', margin: 0 }}>Chats</h3>
                <div style={{ display: 'flex', gap: '6px' }}>
                  <button 
                    onClick={handleTriggerImport}
                    title="Import Session"
                    aria-label="Import Session"
                    style={{ 
                      background: 'rgba(255,255,255,0.05)', 
                      border: 'none', 
                      color: '#fff',
                      borderRadius: '6px',
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
                  background: 'rgba(255,255,255,0.05)', 
                  border: 'none', 
                  color: '#fff',
                  borderRadius: '6px',
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
                <div style={{ margin: '0 24px 12px', padding: '8px 10px', background: 'rgba(239, 68, 68, 0.15)', border: '1px solid rgba(239, 68, 68, 0.3)', borderRadius: '6px', color: '#fca5a5', fontSize: '11px', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                  <span>{importStatus.message}</span>
                  <button onClick={() => setImportStatus(null)} style={{ background: 'none', border: 'none', color: '#fca5a5', cursor: 'pointer', padding: 0 }}><X size={12} /></button>
                </div>
              )}
              
              {/* Epic 1 UI batch (E1-08): session search. A non-empty
                  query swaps the list for engine search results. */}
              <div style={{ padding: '0 24px', marginBottom: '12px' }}>
                <input
                  type="text"
                  placeholder="Search chats..."
                  aria-label="Search chats"
                  value={sessionSearchQuery}
                  onChange={(e) => setSessionSearchQuery(e.target.value)}
                  style={{
                    width: '100%',
                    boxSizing: 'border-box',
                    background: 'rgba(255,255,255,0.05)',
                    border: '1px solid rgba(255,255,255,0.1)',
                    borderRadius: '8px',
                    padding: '7px 10px',
                    color: '#fff',
                    fontSize: '12px',
                    outline: 'none'
                  }}
                />
              </div>

              <div className="chat-scroll" style={{ padding: '0 12px', flex: 1, overflowY: 'auto' }}>
                {sessionSearchResults !== null ? (
                  sessionSearchResults.length === 0 ? (
                    <div style={{ color: '#9090a0', fontSize: '13px', padding: '12px 14px' }}>No chats match your search.</div>
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
                          <div style={{ fontSize: '12px', color: '#9090a0', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{r.snippet}</div>
                        )}
                      </div>
                    </div>
                  ))
                ) : sessions.map(s => (
                  <div
                    key={s.id}
                    className={`session-item ${activeSession === s.id ? 'active' : ''}`}
                    onClick={() => handleSessionSwitch(s.id)}
                    onDoubleClick={() => handleStartRenameSession(s)}
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'space-between'
                    }}
                  >
                    <div style={{ display: 'flex', alignItems: 'center', gap: '8px', overflow: 'hidden', flex: 1 }}>
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
                            background: 'rgba(0,0,0,0.3)',
                            border: '1px solid rgba(167, 139, 250, 0.5)',
                            borderRadius: '6px',
                            padding: '3px 6px',
                            color: '#fff',
                            fontSize: '13px',
                            outline: 'none'
                          }}
                        />
                      ) : (
                        <span style={{ fontSize: '13px', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{s.title || s.name || `Session ${s.id}`}</span>
                      )}
                    </div>
                    <div style={{ display: 'flex', alignItems: 'center', flexShrink: 0 }}>
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
                  </div>
                ))}
              </div>
            </div>

          </div>
        )}

      {activeTab === 'Chat' && !isChatDetached && (
        <DraggableChatWindow 
          messages={chatMessages} 
          sessionId={activeSession}
          onExportSession={handleExportSession}
          onSendMessage={handleSendMessage} 
          onStopTask={handleStopTask}
          onApproveTool={handleToolApproval}
          onApprovePlugin={handlePluginApproval}
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
          dictationState={dictationState}
          readAloudState={readAloudState}
          onDictationStart={handleDictationStart}
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
                   providers={providers}
                   providerHealth={providerHealth}
                   localModels={localModels}
                   activeSession={activeSession}
                   dailyBudget={dailyBudget}
                   budgetAlert={budgetAlert}
                 />
               </div>
            )}
            {activeTab === 'Dashboard' && <Dashboard status={status} activeTasks={activeTaskIds.size} pluginCount={pluginCount} memoryMb={memoryMb} providerHealth={providerHealth} wsRef={wsRef} onStartWorkflow={() => setActiveTab('Chat')} />}
            {activeTab === 'Artifacts' && (
              <GlobalArtifactsView
                wsRef={wsRef}
                sessions={sessions}
                onNavigateToSession={handleNavigateToArtifactSession}
                onNavigateToMedia={handleNavigateToMediaSession}
              />
            )}
            {activeTab === 'Projects' && <Projects wsRef={wsRef} />}
            {!isFloating && engineConfig && !engineConfig.onboarding_completed && providersLoaded
              && !(Array.isArray(providers) && providers.some(p => p.has_key)) && (
              <Onboarding providers={providers} onDone={handleOnboardingDone} />
            )}
            {!isFloating && (
              <CommandPalette open={paletteOpen} items={paletteItems} onClose={() => setPaletteOpen(false)} />
            )}
            {!isFloating && shortcutsOpen && (
              <div data-testid="shortcuts-dialog" style={{ position: 'fixed', inset: 0, background: 'rgba(5,5,10,0.6)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 205 }} onClick={() => setShortcutsOpen(false)}>
                <div style={{ background: 'rgba(20,20,28,0.98)', border: '1px solid rgba(255,255,255,0.12)', borderRadius: '16px', padding: '24px', width: '380px', maxWidth: '92vw', color: '#f0f0f5' }} onClick={(e) => e.stopPropagation()}>
                  <h3 style={{ margin: '0 0 14px 0' }}>Keyboard shortcuts</h3>
                  {[
                    ['Ctrl+K', 'Command palette'],
                    ['Ctrl+N', 'New chat'],
                    ['Ctrl+,', 'Open Settings'],
                    ['Alt+Space', 'Show / hide Aether-OS (global)'],
                    [engineConfig?.snip_hotkey || 'Ctrl+Shift+S', 'Snip & ask (global)'],
                    ['Esc', 'Close palette / dialogs'],
                  ].map(([keys, desc]) => (
                    <div key={keys} style={{ display: 'flex', justifyContent: 'space-between', padding: '7px 0', borderBottom: '1px solid rgba(255,255,255,0.06)', fontSize: '14px' }}>
                      <span style={{ fontFamily: 'monospace', color: '#a78bfa' }}>{keys}</span>
                      <span style={{ color: '#c9c9d4' }}>{desc}</span>
                    </div>
                  ))}
                  <button onClick={() => setShortcutsOpen(false)} style={{ marginTop: '16px', background: '#7c3aed', color: '#fff', border: 'none', padding: '8px 18px', borderRadius: '10px', cursor: 'pointer' }}>Done</button>
                </div>
              </div>
            )}
            {activeTab !== 'Settings' && activeTab !== 'Dashboard' && activeTab !== 'Artifacts' && activeTab !== 'Projects' && (
               <div style={{ display: 'flex', flex: 1, alignItems: 'center', justifyContent: 'center', color: '#9090a0' }}>
                 <h2>{activeTab} view coming soon.</h2>
               </div>
            )}
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
              if (node.group === 3) return '#a78bfa'; // Logs
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
  return (
    <div 
      onClick={() => setActiveTab(label)}
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: '12px',
        padding: '10px 14px',
        borderRadius: '10px',
        color: activeTab === label ? '#fff' : '#9090a0',
        background: activeTab === label ? 'rgba(255, 255, 255, 0.1)' : 'transparent',
        cursor: 'pointer',
        transition: 'all 0.2s',
        fontSize: '14px',
        fontWeight: activeTab === label ? '500' : '400',
      }}
      onMouseEnter={(e) => {
        if (activeTab !== label) {
          e.currentTarget.style.background = 'rgba(255, 255, 255, 0.05)';
          e.currentTarget.style.color = '#fff';
        }
      }}
      onMouseLeave={(e) => {
        if (activeTab !== label) {
          e.currentTarget.style.background = 'transparent';
          e.currentTarget.style.color = '#9090a0';
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
