import React, { useState, useEffect, useRef } from 'react';

import { Globe, Play, Pause, Maximize2, Minimize2, X, GripHorizontal, Bot, User, ShieldAlert, Check, ChevronDown, ExternalLink, LogIn, Square, Layers, Edit2, RotateCcw, Download, CircleDollarSign, Mic } from 'lucide-react';
import Dropdown from './Dropdown';
import UndoToast from './components/UndoToast';
import SnipOverlay from './components/SnipOverlay';
import CostHeaderPopover, { formatUsd } from './components/CostHeaderPopover';
import ContextBar, { CompactContextBar } from './components/ContextBar';
import VoiceOverlay from './components/VoiceOverlay';
import { sanitizeSvg } from './sanitizeSvg';
import { useSlowLoad } from './components/Settings/LoadStateRow';
import ChatComposer from './components/ChatComposer';
import ContextMenu from './components/ContextMenu';
import { buildContextMenuState } from './components/contextMenuState';
import ChatTranscript from './components/ChatTranscript';
import './chat.css';

import { parseApprovalEditedArgs, browserEnvelopeGrant } from './components/approvalLogic';
export {
  getNodeStyle,
  useSmoothScroll,
  PayloadDrawer,
  ApprovalPreview,
  approvalScopePath,
  getRewindPointForMessage,
  BrowserConfirmationControl,
  ToolActivityCard,
  NodeActivityCard,
  ThinkingAccordion,
} from './components/ChatCards';


// Epic 1, E1-17: display-only scratchpad panel. Mid-task the agent
// can sketch a diagram / SVG / notes here; the content arrives
// pre-sanitised from the engine (scripts and handlers stripped). The
// panel sits above the composer so it never interrupts the message
// stream; it collapses, and dismissing hides it until the next
// SCRATCHPAD event (App stores a fresh object per event, which the
// effect below treats as "reopen").
export function ScratchpadPanel({ scratchpad }) {
  const [isCollapsed, setIsCollapsed] = useState(false);
  const [isDismissed, setIsDismissed] = useState(false);
  const [mermaidSvg, setMermaidSvg] = useState('');
  const [mermaidError, setMermaidError] = useState('');

  const kind = scratchpad?.kind || 'text';
  const content = typeof scratchpad?.content === 'string' ? scratchpad.content : '';
  const agent = scratchpad?.agent || null;

  useEffect(() => {
    setIsDismissed(false);
    setIsCollapsed(false);
  }, [scratchpad]);

  // Mermaid sources render to inline SVG with the package loaded
  // lazily (importing this window never pays for it). Strict security
  // level plus the engine-side sanitisation keep it display-only.
  // Any failure falls back to showing the source with a note — the
  // panel must never take the chat down with it.
  useEffect(() => {
    if (!scratchpad || kind !== 'mermaid') {
      setMermaidSvg('');
      setMermaidError('');
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const mod = await import('mermaid');
        const mermaid = mod.default || mod;
        mermaid.initialize({ startOnLoad: false, securityLevel: 'strict' });
        const renderId = `aether-scratchpad-${Math.random().toString(36).slice(2, 10)}`;
        const result = await mermaid.render(renderId, content);
        if (!cancelled) {
          setMermaidSvg(result?.svg || '');
          setMermaidError('');
        }
      } catch (err) {
        if (!cancelled) {
          setMermaidSvg('');
          setMermaidError(err?.message || 'render failed');
        }
      }
    })();
    return () => { cancelled = true; };
  }, [scratchpad, kind, content]);

  if (!scratchpad || isDismissed) return null;

  return (
    <div
      className="scratchpad-panel"
      data-testid="scratchpad-panel"
      style={{
        margin: '12px 16px 0 16px',
        borderRadius: 'var(--radius-lg)',
        background: 'var(--bg-raised)',
        border: '1px solid var(--border-default)',
        overflow: 'hidden',
        flexShrink: 0
      }}
    >
      <div style={{
        display: 'flex',
        alignItems: 'center',
        gap: '8px',
        padding: '8px 12px',
        color: 'var(--text-primary)',
        fontSize: '12px',
        userSelect: 'none'
      }}>
        <Edit2 size={13} style={{ flexShrink: 0 }} />
        <span style={{ fontWeight: 600, letterSpacing: '0.4px' }}>Scratchpad</span>
        <span style={{
          fontSize: '10px',
          background: 'rgba(var(--accent-rgb), 0.16)',
          border: '1px solid rgba(var(--accent-rgb), 0.35)',
          padding: '2px 6px',
          borderRadius: '4px',
          fontWeight: 600,
          letterSpacing: '0.5px',
          textTransform: 'uppercase'
        }}>{kind}</span>
        {agent && <span style={{ fontSize: '11px', color: 'var(--text-muted)' }}>{agent}</span>}
        <span style={{ flex: 1 }} />
        <button
          type="button"
          onClick={() => setIsCollapsed(!isCollapsed)}
          aria-label={isCollapsed ? 'Expand scratchpad' : 'Collapse scratchpad'}
          title={isCollapsed ? 'Expand scratchpad' : 'Collapse scratchpad'}
          style={{ background: 'transparent', border: 'none', color: 'var(--text-secondary)', cursor: 'pointer', display: 'flex', alignItems: 'center', padding: '2px' }}
        >
          <ChevronDown size={14} style={{ transition: 'transform 0.3s ease', transform: isCollapsed ? 'rotate(0deg)' : 'rotate(180deg)' }} />
        </button>
        <button
          type="button"
          onClick={() => setIsDismissed(true)}
          aria-label="Dismiss scratchpad"
          title="Dismiss scratchpad"
          style={{ background: 'transparent', border: 'none', color: 'var(--text-secondary)', cursor: 'pointer', display: 'flex', alignItems: 'center', padding: '2px' }}
        >
          <X size={14} />
        </button>
      </div>
      {!isCollapsed && (
        <div
          className="chat-scroll"
          style={{
            maxHeight: '260px',
            overflowY: 'auto',
            overflowX: 'auto',
            padding: '10px 12px',
            borderTop: '1px solid var(--border-default)',
            background: 'var(--bg-base)'
          }}
        >
          {/* S3-M1 note is in sanitizeSvg.js: agent SVG is DOMPurify-sanitised before injection below. */}
          {kind === 'svg' && (
            <div className="scratchpad-svg" dangerouslySetInnerHTML={{ __html: sanitizeSvg(content) }} />
          )}
          {kind === 'mermaid' && (mermaidSvg ? (
            <div className="scratchpad-mermaid" dangerouslySetInnerHTML={{ __html: mermaidSvg }} />
          ) : mermaidError ? (
            <div>
              <div style={{ fontSize: '11px', color: 'var(--danger)', marginBottom: '6px' }}>
                Diagram could not be rendered — showing the source instead.
              </div>
              <pre className="scratchpad-source" style={{ margin: 0, whiteSpace: 'pre-wrap', fontSize: '11.5px', color: 'var(--text-secondary)' }}>{content}</pre>
            </div>
          ) : (
            <div style={{ fontSize: '11.5px', color: 'var(--text-muted)' }}>Rendering diagram…</div>
          ))}
          {kind !== 'svg' && kind !== 'mermaid' && (
            <pre className="scratchpad-text" style={{ margin: 0, whiteSpace: 'pre-wrap', fontSize: '12px', color: 'var(--text-primary)' }}>{content}</pre>
          )}
        </div>
      )}
    </div>
  );
}

// Epic 8, E8-6.1: browser task card for the dedicated agent browser
export function BrowserTaskCard({ session = {}, onControl = () => {} }) {
  const { controlState = 'agent', currentUrl = '', challenge = null } = session || {};

  const isStopped = controlState === 'stopped';
  const isPaused = controlState === 'paused';
  const isUserControl = controlState === 'user';
  const isWaitingUser = controlState === 'waiting_user';

  const challengeType = challenge?.challenge ? String(challenge.challenge).toLowerCase() : '';
  const challengeDisplay = challengeType === 'two_factor' ? 'Two-factor' : (challengeType || 'verification');

  return (
    <div
      data-testid="browser-task-card"
      style={{
        margin: '12px 16px 0 16px',
        borderRadius: 'var(--radius-lg)',
        border: '1px solid var(--border-default)',
        background: 'var(--bg-raised)',
        padding: '12px 16px',
        display: 'flex',
        flexDirection: 'column',
        gap: '10px',
        boxShadow: 'none',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '8px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', minWidth: 0 }}>
          <div style={{
            width: '28px',
            height: '28px',
            borderRadius: '8px',
            background: 'rgba(var(--accent-rgb), 0.15)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            color: 'var(--accent)',
            flexShrink: 0,
          }}>
            <Globe size={16} />
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', minWidth: 0 }}>
            <span style={{ fontSize: '13px', fontWeight: 600, color: 'var(--text-primary)' }}>
              Agent Browser (Dedicated Profile)
            </span>
            {currentUrl ? (
              <span
                style={{
                  fontSize: '11px',
                  color: 'var(--text-muted)', fontFamily: 'var(--font-mono)',
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  whiteSpace: 'nowrap',
                }}
                title={currentUrl}
              >
                {currentUrl}
              </span>
            ) : null}
          </div>
        </div>

        <div style={{
          fontSize: '11px',
          fontWeight: 500,
          padding: '2px 8px',
          borderRadius: '999px',
          background: isWaitingUser
            ? 'rgba(var(--accent-rgb), 0.15)'
            : isUserControl
            ? 'rgba(76, 195, 138, 0.15)'
            : isPaused
            ? 'rgba(var(--accent-rgb), 0.15)'
            : isStopped
            ? 'rgba(201, 42, 42, 0.15)'
            : 'rgba(var(--accent-rgb), 0.15)',
          color: isWaitingUser
            ? 'var(--accent)'
            : isUserControl
            ? 'var(--success)'
            : isPaused
            ? 'var(--accent)'
            : isStopped
            ? 'var(--danger)'
            : 'var(--accent)',
          border: `1px solid ${
            isWaitingUser
              ? 'rgba(var(--accent-rgb), 0.3)'
              : isUserControl
              ? 'rgba(76, 195, 138, 0.3)'
              : isPaused
              ? 'rgba(var(--accent-rgb), 0.3)'
              : isStopped
              ? 'rgba(201, 42, 42, 0.3)'
              : 'rgba(var(--accent-rgb), 0.3)'
          }`,
        }}>
          {isWaitingUser
            ? 'Parked'
            : isUserControl
            ? 'User in control'
            : isPaused
            ? 'Paused'
            : isStopped
            ? 'Stopped'
            : 'Active'}
        </div>
      </div>

      {isWaitingUser && (
        <div style={{
          background: 'rgba(var(--accent-rgb), 0.08)',
          border: '1px solid rgba(var(--accent-rgb), 0.25)',
          borderRadius: '8px',
          padding: '8px 12px',
          fontSize: '12px',
          color: 'var(--text-primary)',
          display: 'flex',
          alignItems: 'center',
          gap: '8px',
        }}>
          <ShieldAlert size={16} color="#F5B942" style={{ flexShrink: 0 }} />
          <span>
            Waiting for you: {challengeDisplay} challenge detected in browser window. The agent never solves challenges. Please complete it to continue.
          </span>
        </div>
      )}

      {isUserControl && (
        <div style={{
          background: 'rgba(76, 195, 138, 0.1)',
          border: '1px solid rgba(76, 195, 138, 0.25)',
          borderRadius: '8px',
          padding: '8px 12px',
          fontSize: '12px',
          color: 'var(--text-primary)',
        }}>
          You have control of the browser window. The agent is waiting for you to return control.
        </div>
      )}

      <div style={{ display: 'flex', gap: '8px', alignItems: 'center', flexWrap: 'wrap' }}>
        {isPaused ? (
          <button
            type="button"
            data-testid="browser-resume-btn"
            onClick={() => onControl('resume')}
            style={{
              background: 'var(--accent)',
              border: '1px solid var(--accent)',
              color: 'var(--accent-on)',
              borderRadius: '8px',
              padding: '5px 12px',
              fontSize: '12px',
              fontWeight: 500,
              cursor: 'pointer',
              display: 'flex',
              alignItems: 'center',
              gap: '6px',
            }}
          >
            <Play size={13} />
            <span>Resume</span>
          </button>
        ) : !isWaitingUser && !isUserControl ? (
          <button
            type="button"
            data-testid="browser-pause-btn"
            disabled={isStopped}
            onClick={() => onControl('pause')}
            style={{
              background: isStopped ? 'var(--bg-raised)' : 'rgba(var(--accent-rgb), 0.15)',
              border: isStopped ? '1px solid var(--border-default)' : '1px solid rgba(var(--accent-rgb), 0.35)',
              color: isStopped ? 'var(--text-muted)' : 'var(--accent)',
              borderRadius: '8px',
              padding: '5px 12px',
              fontSize: '12px',
              fontWeight: 500,
              cursor: isStopped ? 'not-allowed' : 'pointer',
              display: 'flex',
              alignItems: 'center',
              gap: '6px',
            }}
          >
            <Pause size={13} />
            <span>Pause</span>
          </button>
        ) : null}

        {isUserControl ? (
          <button
            type="button"
            data-testid="browser-return-control-btn"
            onClick={() => onControl('return_control')}
            style={{
              background: 'rgba(76, 195, 138, 0.15)',
              border: '1px solid rgba(76, 195, 138, 0.35)',
              color: 'var(--success)',
              borderRadius: '8px',
              padding: '5px 12px',
              fontSize: '12px',
              fontWeight: 500,
              cursor: 'pointer',
              display: 'flex',
              alignItems: 'center',
              gap: '6px',
            }}
          >
            <Bot size={13} />
            <span>Return Control</span>
          </button>
        ) : !isWaitingUser ? (
          <button
            type="button"
            data-testid="browser-take-control-btn"
            disabled={isStopped}
            onClick={() => onControl('take_control')}
            style={{
              background: isStopped ? 'var(--bg-raised)' : 'var(--bg-hover)',
              border: isStopped ? '1px solid var(--border-default)' : '1px solid var(--border-strong)',
              color: isStopped ? 'var(--text-muted)' : 'var(--text-primary)',
              borderRadius: '8px',
              padding: '5px 12px',
              fontSize: '12px',
              fontWeight: 500,
              cursor: isStopped ? 'not-allowed' : 'pointer',
              display: 'flex',
              alignItems: 'center',
              gap: '6px',
            }}
          >
            <User size={13} />
            <span>Take Control</span>
          </button>
        ) : null}

        {isWaitingUser && (
          <button
            type="button"
            data-testid="browser-resume-btn"
            onClick={() => onControl('resume')}
            style={{
              background: 'var(--accent)',
              border: '1px solid var(--accent)',
              color: 'var(--accent-on)',
              borderRadius: '8px',
              padding: '5px 12px',
              fontSize: '12px',
              fontWeight: 500,
              cursor: 'pointer',
              display: 'flex',
              alignItems: 'center',
              gap: '6px',
            }}
          >
            <Play size={13} />
            <span>Resume</span>
          </button>
        )}

        <button
          type="button"
          data-testid="browser-stop-btn"
          disabled={isStopped}
          onClick={() => onControl('stop')}
          style={{
            background: isStopped ? 'var(--bg-raised)' : 'rgba(201, 42, 42, 0.15)',
            border: isStopped ? '1px solid var(--border-default)' : '1px solid rgba(201, 42, 42, 0.35)',
            color: isStopped ? 'var(--text-muted)' : 'var(--danger)',
            borderRadius: '8px',
            padding: '5px 12px',
            fontSize: '12px',
            fontWeight: 500,
            cursor: isStopped ? 'not-allowed' : 'pointer',
            display: 'flex',
            alignItems: 'center',
            gap: '6px',
          }}
        >
          <Square size={13} />
          <span>Stop</span>
        </button>
      </div>
    </div>
  );
}


// D1-C6: how long the browser card may show an optimistic
// control state the engine has not confirmed before falling
// back to the last engine-confirmed state.
const BROWSER_CONTROL_CONFIRM_MS = 5000;

export default function DraggableChatWindow({ 
  messages, 
  onSendMessage, 
  onStopTask,
  status = 'Online',
  sessionSwitchPending = false,
  isThinking,
  onApproveTool,
  onApprovePlugin,
  onVaultFillApproval,
  providers = [],
  onChangeModel = () => {},
  onEditMessage,
  onRegenerate,
  planMode = false,
  scratchpad = null,
  onTogglePlanMode,
  onDetach,
  isFloating,
  initialInput = '',
  slashCommands = [],
  isSidebarCollapsed = false,
  onExportSession = () => {},
  rewindManifest = null,
  onRewindRequest = null,
  onForkRequest = null,
  rewindUndo = null,
  onRewindUndo = null,
  onRewindUndoExpire = null,
  artifactsCount = 0,
  isCanvasOpen = false,
  onToggleCanvas = () => {},
  onOpenArtifact = () => {},
  onTriggerSnip,
  initialAttachments = [],
  sessionCostUsd = 0,
  costTasks = [],
  runningCostTaskIds = [],
  onRequestCostBreakdown = () => {},
  contextBreakdown = null,
  onRequestContextBreakdown = () => {},
  budgetAlert = null,
  browserSession = null,
  onBrowserControl = null,
  voiceState = null,
  voiceAudioChunk = null,
  voicePlaybackCut = 0,
  onVoiceStart = () => {},
  onVoiceStop = () => {},
  onVoiceDismissError = () => {},
  voiceRtcAnswer = null,
  voiceRtcRemoteCandidate = null,
  onVoiceRtcOffer = () => {},
  onVoiceRtcIceCandidate = () => {},
  onVoicePlaybackProgress = () => {},
  dictationState = null,
  readAloudState = null,
  onDictationStart = null,
  onDictationStop = null,
  onReadAloudStart = null,
  onReadAloudStop = null
}) {
  const [input, setInput] = useState(initialInput);
  // RUX-C-16: the header dot is currently the only in-chat sign
  // that the engine is gone; the banner below the header states
  // it in words, where the user is typing. It derives from the
  // same status signal as the dot and clears itself on reconnect.
  const chatOffline = !(status || '').includes('Online');
  // RUX-C-13 mechanism 5: a session switch that outlasts the
  // shared slow interval gains the taking-longer line.
  const switchSlow = useSlowLoad(sessionSwitchPending);
  const [isMinimized, setIsMinimized] = useState(false);
  const [showExportMenu, setShowExportMenu] = useState(false);
  const exportMenuRef = useRef(null);
  const [showCostPopover, setShowCostPopover] = useState(false);
  const costMenuRef = useRef(null);
  const [showContextPopover, setShowContextPopover] = useState(false);
  const contextMenuRef = useRef(null);
  const [showStepPicker, setShowStepPicker] = useState(false);
  const stepPickerRef = useRef(null);
  // Epic 8, M12: the header step picker's data — the manifest's
  // ACTUAL steps when the engine recorded any; otherwise the
  // session's messages as fork boundaries (a forked or imported
  // session's inherited history carries no manifest).
  const stepPickerEntries = rewindManifest && Array.isArray(rewindManifest.entries)
    ? rewindManifest.entries : [];
  const stepPickerMessages = (messages || []).filter(
    (m) => m && (m.role === 'user' || m.role === 'agent' || m.role === 'assistant'));
  const [contextMenu, setContextMenu] = useState(null);
  const [toastMessage, setToastMessage] = useState('');  // Epic 8, E8-1.3: per-approval argument editor state for the
  // inline approval cards, keyed by message id: { text, error }.
  // Presence of an entry means that card's editor is open.
  const [approvalArgEdits, setApprovalArgEdits] = useState({});
  // Epic 8, E8-6.2: per-approval "allow for this task" envelope
  // opt-ins on browser confirmation cards, keyed by message id.
  const [browserEnvelopeGrants, setBrowserEnvelopeGrants] = useState({});
  // Epic 8, E8-1.1a: which message's step menu is open (index into
  // messages), or null. One menu at a time.
  const [openStepMenu, setOpenStepMenu] = useState(null);
  const [attachments, setAttachments] = useState(initialAttachments || []);
  const [isSnipOverlayOpen, setIsSnipOverlayOpen] = useState(false);
  const [isDictating, setIsDictating] = useState(false);
  const [dictationError, setDictationError] = useState(null);
  const [playingMessageId, setPlayingMessageId] = useState(null);
  const [readAloudError, setReadAloudError] = useState(null);

  const dictationStreamRef = useRef(null);
  const baseInputRef = useRef('');
  // D1-C11: whether a mic press is currently active (see
  // handleMicStart / handleMicEnd).
  const micPressActiveRef = useRef(false);
  // D1-C7: this component's old private read-aloud refs
  // (AudioContext / sources / play-head) are gone — its sources
  // set was never populated; playback lives in App.

  useEffect(() => {
    if (dictationState?.error) {
      setDictationError(dictationState.error);
      setIsDictating(false);
      if (dictationStreamRef.current) {
        try { dictationStreamRef.current.getTracks().forEach((t) => t.stop()); } catch {} 
        dictationStreamRef.current = null;
      }
    }
  }, [dictationState?.error]);

  useEffect(() => {
    if (dictationState?.status === 'active') {
      const startCapture = async () => {
        if (!navigator.mediaDevices || typeof navigator.mediaDevices.getUserMedia !== 'function') {
          setDictationError('Microphone is not available in this environment.');
          return;
        }
        try {
          const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
          dictationStreamRef.current = stream;
          setIsDictating(true);
        } catch (err) {
          setDictationError(`Microphone unavailable: ${err && err.message ? err.message : err}`);
        }
      };
      startCapture();
    }
  }, [dictationState?.status]);

  useEffect(() => {
    if (dictationState?.text !== undefined && dictationState?.text !== null) {
      const base = baseInputRef.current;
      setInput(base ? `${base} ${dictationState.text}` : dictationState.text);
    }
  }, [dictationState?.text]);

  useEffect(() => {
    if (readAloudState?.error) {
      setReadAloudError(readAloudState.error);
      setPlayingMessageId(null);
    } else if (readAloudState?.status === 'playing') {
      setPlayingMessageId(readAloudState.messageId);
    } else if (!readAloudState || readAloudState.status === 'idle') {
      setPlayingMessageId(null);
    }
  }, [readAloudState]);

  const handleMicStart = async (e) => {
    if (e) {
      if (typeof e.preventDefault === 'function') e.preventDefault();
      if (typeof e.stopPropagation === 'function') e.stopPropagation();
    }
    if (isDictating) return;
    micPressActiveRef.current = true;
    setDictationError(null);
    baseInputRef.current = input;

    if (onDictationStart) {
      onDictationStart();
      return;
    }

    if (!navigator.mediaDevices || typeof navigator.mediaDevices.getUserMedia !== 'function') {
      setDictationError('Microphone is not available in this environment.');
      return;
    }

    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      dictationStreamRef.current = stream;
      setIsDictating(true);
    } catch (err) {
      setDictationError(`Microphone unavailable: ${err && err.message ? err.message : err}`);
    }
  };

  const handleMicEnd = (e) => {
    if (e) {
      if (typeof e.preventDefault === 'function') e.preventDefault();
      if (typeof e.stopPropagation === 'function') e.stopPropagation();
    }
    // D1-C11: one press ends dictation once. The button binds
    // mouseUp AND mouseLeave AND touchEnd, so a press ending
    // with the pointer sliding off the button fired the stop
    // twice; the first end consumes the press and later ones
    // are no-ops (a bare mouseLeave that was never a press
    // included).
    if (!micPressActiveRef.current) return;
    micPressActiveRef.current = false;
    if (dictationStreamRef.current) {
      try {
        dictationStreamRef.current.getTracks().forEach((t) => t.stop());
      } catch {} 
      dictationStreamRef.current = null;
    }
    setIsDictating(false);
    if (onDictationStop) {
      onDictationStop();
    }
  };

  const handleMicKeyDown = (e) => {
    if (e.key === ' ' || e.key === 'Enter') {
      e.preventDefault();
      handleMicStart(e);
    }
  };

  const handleMicKeyUp = (e) => {
    if (e.key === ' ' || e.key === 'Enter') {
      e.preventDefault();
      handleMicEnd(e);
    }
  };

  // D1-C7: a local stopReadAloudPlayback used to sit here,
  // stopping a sources set that was never populated (chunks are
  // decoded and played in App) — an inert duplicate of App's
  // stopAppReadAloud. The real stop is App's, reached through
  // onReadAloudStop / onReadAloudStart in the toggle below.

  const handleToggleReadAloud = (msg, idx) => {
    const mid = msg.id || String(idx);
    setReadAloudError(null);

    if (playingMessageId === mid) {
      setPlayingMessageId(null);
      if (onReadAloudStop) onReadAloudStop(mid);
    } else {
      setPlayingMessageId(mid);
      if (onReadAloudStart) {
        onReadAloudStart(mid, msg.content || '');
      }
    }
  };

  // Epic 8, E8-6.1: agent-browser control and activity tracking
  const [localControlState, setLocalControlState] = useState(null);
  const lastBrowserStateRef = useRef(browserSession?.controlState);
  const lastBrowserChallengeRef = useRef(browserSession?.challenge);

  if (lastBrowserStateRef.current !== browserSession?.controlState ||
      lastBrowserChallengeRef.current !== browserSession?.challenge) {
    lastBrowserStateRef.current = browserSession?.controlState;
    lastBrowserChallengeRef.current = browserSession?.challenge;
    if (localControlState !== null) {
      setLocalControlState(null);
    }
  }

  const browserActivity = React.useMemo(() => {
    let latest = null;
    for (const m of messages || []) {
      for (const t of m.thoughts || []) {
        const payload = t.payload || {};
        const toolName = payload.tool_name || t.tool_name || '';
        if (typeof toolName === 'string' && toolName.startsWith('browser_')) {
          const url = payload.tool_args?.url || payload.args?.url || '';
          latest = { toolName, url };
        }
      }
    }
    return latest;
  }, [messages]);

  const activeBrowserSession = React.useMemo(() => {
    if (!browserActivity && !browserSession) return null;
    return {
      controlState: localControlState || browserSession?.controlState || 'agent',
      currentUrl: browserSession?.currentUrl || browserActivity?.url || '',
      challenge: browserSession?.challenge || null,
    };
  }, [browserActivity, browserSession, localControlState]);

  const handleBrowserControlClick = React.useCallback((action) => {
    const optimisticState = {
      pause: 'paused',
      resume: 'agent',
      take_control: 'user',
      return_control: 'agent',
      stop: 'stopped',
    }[action];
    // D1-C6: while offline the request is queued, not executed —
    // the card keeps showing the engine-confirmed state rather
    // than claiming a state that has not even been sent yet.
    if (optimisticState && !chatOffline) {
      setLocalControlState(optimisticState);
    }
    if (onBrowserControl) {
      onBrowserControl(action);
    }
  }, [onBrowserControl, chatOffline]);

  // D1-C6: an optimistic control state is a claim made ahead of
  // the engine. Bound it: if the engine's own state (the
  // browserSession prop) has not confirmed it within the
  // window, the card falls back to the last engine-confirmed
  // state instead of presenting the guess as settled fact
  // indefinitely — worst case, "Stopped" over a browser that is
  // still running. A confirmation clears localControlState
  // through the prop-change reset above, which also cancels
  // this timer via the cleanup.
  useEffect(() => {
    if (localControlState === null) return undefined;
    const timer = setTimeout(() => setLocalControlState(null), BROWSER_CONTROL_CONFIRM_MS);
    return () => clearTimeout(timer);
  }, [localControlState]);


  useEffect(() => {
    if (initialAttachments && initialAttachments.length > 0) {
      setAttachments(initialAttachments);
    }
  }, [initialAttachments]);

  useEffect(() => {
    if (window.electronAPI && typeof window.electronAPI.onTriggerSnip === 'function') {
      return window.electronAPI.onTriggerSnip(() => {
        setIsSnipOverlayOpen(true);
      });
    }
  }, []);

  const handleTriggerSnip = () => {
    if (onTriggerSnip) {
      onTriggerSnip();
    }
    setIsSnipOverlayOpen(true);
  };

  const handleSnipCaptured = (snip) => {
    setIsSnipOverlayOpen(false);
    setAttachments(prev => [...prev, snip]);
    // Lane C grouped S4: after capture the snip rides the normal
    // chat send path, whose only offline sign was the generic
    // "Queued" tag on the eventual bubble. Captured while the
    // engine is down, the snip gets its own one-line notice at the
    // moment of capture — the send itself already queues (Wave 2).
    if (chatOffline) {
      showToast('Snip captured — it will send when the engine reconnects.');
    }
  };

  const handleRemoveAttachment = (idxToRemove) => {
    setAttachments(prev => prev.filter((_, idx) => idx !== idxToRemove));
  };

  // Epic 8, E8-6.2: the envelope grant a browser confirmation
  // carries when its "allow for this task" box is checked.
  // Payments never offer one; the engine re-scopes the grant to
  // the confirmed action's category + site regardless.
  const browserEnvelopeGrantFor = (msg) => browserEnvelopeGrant(
    msg && msg.browser_confirmation,
    !!(msg && browserEnvelopeGrants[msg.id])
  );

  // N11: one grants map serves both approval surfaces — the
  // standalone card and the compact ToolActivityCard (via
  // ThinkingAccordion) read and write the same state.
  const handleEnvelopeGrantChange = (approvalId, checked) => {
    setBrowserEnvelopeGrants(prev => ({ ...prev, [approvalId]: checked }));
  };

  const approveToolWithEdits = (msg, scope) => {
    if (!onApproveTool) return;
    const envelope = browserEnvelopeGrantFor(msg);
    const approve = (s, edited) => {
      if (envelope) onApproveTool(msg.id, true, msg.approval_key, s, edited, envelope);
      else if (edited) onApproveTool(msg.id, true, msg.approval_key, s, edited);
      else if (s) onApproveTool(msg.id, true, msg.approval_key, s);
      else onApproveTool(msg.id, true, msg.approval_key);
    };
    const editState = approvalArgEdits[msg.id];
    if (!editState) {
      approve(scope, undefined);
      return;
    }
    const { editedArgs, error } = parseApprovalEditedArgs(editState.text, msg.tool_args || {});
    if (error) {
      setApprovalArgEdits(prev => ({ ...prev, [msg.id]: { ...editState, error } }));
      return;
    }
    approve(scope, editedArgs || undefined);
  };

  const toastTimeoutRef = useRef(null);
  const messagesEndRef = useRef(null);
  // D1-C5: whether the reader is at (or near) the bottom of the
  // feed. Auto-scroll follows new messages only while this
  // holds; a user who scrolled up to read history is left where
  // they are.
  const stickToBottomRef = useRef(true);
  const feedScrollRef = useRef(null);
  const handleFeedScroll = () => {
    const el = feedScrollRef.current;
    if (!el) return;
    stickToBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight <= 120;
  };
  const nodeRef = useRef(null);
  const textareaRef = useRef(null);
  const slashPopupRef = useRef(null);

  const [showSlashPopup, setShowSlashPopup] = useState(false);
  const [slashFilter, setSlashFilter] = useState('');
  const [slashIndex, setSlashIndex] = useState(0);
  const [filteredCommands, setFilteredCommands] = useState([]);

  const showToast = (msg) => {
    setToastMessage(msg);
    if (toastTimeoutRef.current) clearTimeout(toastTimeoutRef.current);
    toastTimeoutRef.current = setTimeout(() => {
      setToastMessage('');
    }, 2000);
  };

  useEffect(() => {
    const handleGlobalClick = (e) => {
      setContextMenu(null);
      setOpenStepMenu(null);
      // D1-C4: each popover's dismissal is INDEPENDENT. The old
      // brace nesting made the cost/context closes conditional
      // on the click also landing outside the export container,
      // and the step-picker close conditional on both of those —
      // so those popovers survived clicks that should have
      // dismissed them.
      if (exportMenuRef.current && !exportMenuRef.current.contains(e.target)) {
        setShowExportMenu(false);
      }
      if (costMenuRef.current && !costMenuRef.current.contains(e.target)) {
        setShowCostPopover(false);
      }
      if (contextMenuRef.current && !contextMenuRef.current.contains(e.target)) {
        setShowContextPopover(false);
      }
      if (stepPickerRef.current && !stepPickerRef.current.contains(e.target)) {
        setShowStepPicker(false);
      }
    };
    const handleGlobalKeyDown = (e) => {
      if (e.key === 'Escape') {
        setContextMenu(null);
        setShowExportMenu(false);
        setShowCostPopover(false);
        // D1-C4: Escape omitted these two popovers entirely.
        setShowContextPopover(false);
        setShowStepPicker(false);
      }
    };
    window.addEventListener('click', handleGlobalClick);
    window.addEventListener('scroll', handleGlobalClick, true);
    window.addEventListener('keydown', handleGlobalKeyDown);
    return () => {
      window.removeEventListener('click', handleGlobalClick);
      window.removeEventListener('scroll', handleGlobalClick, true);
      window.removeEventListener('keydown', handleGlobalKeyDown);
      if (toastTimeoutRef.current) clearTimeout(toastTimeoutRef.current);
    };
  }, []);

  useEffect(() => {
    if (showSlashPopup && slashPopupRef.current) {
      const selectedEl = slashPopupRef.current.children[slashIndex];
      if (selectedEl) {
        selectedEl.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
      }
    }
  }, [slashIndex, showSlashPopup]);

  const handleInputChange = (e) => {
    const val = e.target.value;
    setInput(val);
    
    if (val.startsWith('/')) {
      const parts = val.split(' ');
      if (parts.length === 1) {
        const filterStr = parts[0].toLowerCase();
        const filtered = slashCommands.filter(c => 
          c.command.toLowerCase().includes(filterStr) || 
          c.description.toLowerCase().includes(filterStr.substring(1))
        );
        setFilteredCommands(filtered);
        setSlashIndex(0);
        setShowSlashPopup(true);
        setSlashFilter(filterStr);
      } else {
        setShowSlashPopup(false);
      }
    } else {
      setShowSlashPopup(false);
    }
  };

  const handleInputKeyDown = (e) => {
    if (showSlashPopup && filteredCommands.length > 0) {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        setSlashIndex(prev => (prev < filteredCommands.length - 1 ? prev + 1 : 0));
        return;
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault();
        setSlashIndex(prev => (prev > 0 ? prev - 1 : filteredCommands.length - 1));
        return;
      }
      if (e.key === 'Enter' || e.key === 'Tab') {
        e.preventDefault();
        const selected = filteredCommands[slashIndex];
        if (selected) {
          setInput(selected.command + ' ');
        }
        setShowSlashPopup(false);
        return;
      }
      if (e.key === 'Escape') {
        setShowSlashPopup(false);
        return;
      }
    }
    
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit(e);
    }
  };


  const handleSlashSelect = (cmd) => {
    setInput(cmd.command + ' ');
    setShowSlashPopup(false);
    textareaRef.current?.focus();
  };

  const handleContextMenu = (e) => {
    const menu = buildContextMenuState(e, nodeRef.current);
    if (!menu) {
      setContextMenu(null);
      return;
    }
    e.preventDefault();
    setContextMenu(menu);
  };

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  };

  useEffect(() => {
    if (!isMinimized && stickToBottomRef.current) {
      scrollToBottom();
    }
  }, [messages, isThinking, isMinimized]);

  const handleSubmit = (e) => {
    e.preventDefault();
    // While a task runs, submitting steers it (Epic 1, E1-07) - App
    // decides START_TASK vs STEER_TASK; the input must not dead-end.
    if (input.trim() || attachments.length > 0) {
      // Sending your own message returns the feed to the bottom.
      stickToBottomRef.current = true;
      if (attachments.length > 0) {
        onSendMessage(input.trim(), attachments);
      } else {
        onSendMessage(input.trim());
      }
      setInput('');
      setAttachments([]);
    }
  };

  const handleMouseMove = (e) => {
    if (nodeRef.current) {
      const rect = nodeRef.current.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      nodeRef.current.style.setProperty('--mouse-x', `${x}px`);
      nodeRef.current.style.setProperty('--mouse-y', `${y}px`);
    }
  };

  return (
    <div 
      ref={nodeRef}
      className="ios-glass"
      onMouseMove={handleMouseMove}
      onContextMenu={handleContextMenu}
      style={isFloating ? {
        position: 'relative',
        margin: '0',
        width: '100vw',
        height: '100vh',
        display: 'flex',
        flexDirection: 'column',
        zIndex: 1000,
        boxShadow: 'inset 0 1px 0 var(--glass-specular), 0 16px 40px rgba(0, 0, 0, 0.45)',
        transition: 'all 0.3s cubic-bezier(0.16, 1, 0.3, 1)',
        opacity: isMinimized ? 0.8 : 1
      } : {
        position: 'absolute',
        top: '40px',
        left: isSidebarCollapsed ? '80px' : '360px',
        right: isCanvasOpen ? '530px' : '40px',
        bottom: '40px',
        width: 'auto',
        height: 'auto',
        display: 'flex',
        flexDirection: 'column',
        zIndex: 1000,
        transition: 'all 250ms cubic-bezier(0.23, 1, 0.32, 1)'
      }}
    >
      {/* Header */}
      <div 
        className="title-bar"
        style={{
          padding: '12px 16px',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          borderBottom: '1px solid rgba(255,255,255,0.08)',
          background: 'rgba(255,255,255,0.03)',
          WebkitAppRegion: isFloating ? 'drag' : undefined
        }}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flex: 1 }}>
          {isFloating && <GripHorizontal size={16} color="rgba(255,255,255,0.4)" />}
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flex: 1 }}>
              <div style={{ 
                width: '8px', 
                height: '8px', 
                borderRadius: '50%', 
                background: status.includes('Online') ? '#4CC38A' : '#C92A2A',
                boxShadow: status.includes('Online') ? '0 0 8px #4CC38A' : '0 0 8px #C92A2A',
                flexShrink: 0
              }}></div>
              
              <Dropdown 
                value={
                  (() => {
                    const activeProvider = (Array.isArray(providers) ? providers : []).find(p => p.is_default);
                    return activeProvider && activeProvider.default_model ? `${activeProvider.name}:::${activeProvider.default_model}` : '';
                  })()
                }
                onChange={(val) => {
                  const [provider, model] = val.split(':::');
                  if (provider && model) {
                    onChangeModel(provider, model);
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
                placeholder="Select Model..."
                style={{ maxWidth: '240px' }}
              />

            </div>
          </div>
          <div style={{ display: 'flex', gap: '12px', alignItems: 'center' }}>
            {/* Compact default-model context bar (Epic 8, E8-A8.1b) */}
            <CompactContextBar
              breakdown={contextBreakdown}
              onClick={() => {
                setShowContextPopover((prev) => !prev);
                if (!showContextPopover) onRequestContextBreakdown();
              }}
            />
            {/* Cost Ledger (Epic 8, E8-A8.1a) */}
            <div ref={costMenuRef} style={{ position: 'relative' }}>
              <button
                type="button"
                data-testid="cost-header-btn"
                onClick={() => {
                  setShowCostPopover(prev => !prev);
                  if (!showCostPopover) onRequestCostBreakdown();
                }}
                title="Session cost"
                aria-label="Session cost"
                style={{
                  background: showCostPopover ? 'rgba(var(--accent-rgb), 0.22)' : 'var(--bg-hover)',
                  border: showCostPopover ? '1px solid rgba(var(--accent-rgb), 0.5)' : '1px solid var(--border-default)',
                  color: '#fff',
                  cursor: 'pointer',
                  padding: '6px 10px',
                  borderRadius: '999px',
                  display: 'flex',
                  alignItems: 'center',
                  gap: '6px',
                  transition: 'all 0.2s',
                  fontSize: '12px',
                  fontWeight: 500
                }}
              >
                <CircleDollarSign size={14} />
                <span data-testid="cost-header-total">{formatUsd(sessionCostUsd)}</span>
              </button>
              {showCostPopover && (
                <CostHeaderPopover
                  tasks={costTasks}
                  totalCostUsd={sessionCostUsd}
                  runningTaskIds={runningCostTaskIds}
                  budgetAlert={budgetAlert}
                />
              )}
            </div>
            {/* Context Breakdown Popover (Epic 8, E8-A8.1b) */}
            <div ref={contextMenuRef} style={{ position: 'relative' }}>
              <button
                type="button"
                data-testid="context-header-btn"
                onClick={() => {
                  setShowContextPopover((prev) => !prev);
                  if (!showContextPopover) onRequestContextBreakdown();
                }}
                title="Context window breakdown"
                aria-label="Context window breakdown"
                style={{
                  background: showContextPopover ? 'rgba(var(--accent-rgb), 0.22)' : 'var(--bg-hover)',
                  border: showContextPopover ? '1px solid rgba(var(--accent-rgb), 0.5)' : '1px solid var(--border-default)',
                  color: '#fff',
                  cursor: 'pointer',
                  padding: '6px 10px',
                  borderRadius: '999px',
                  display: 'flex',
                  alignItems: 'center',
                  gap: '6px',
                  transition: 'all 0.2s',
                  fontSize: '12px',
                  fontWeight: 500
                }}
              >
                <Layers size={14} />
                <span>Context</span>
              </button>
              {showContextPopover && (
                <ContextBar breakdown={contextBreakdown} />
              )}
            </div>
            {/* Artifacts Canvas Toggle (Epic 8, E8-2.1) */}
            <button
              type="button"
              data-testid="artifacts-header-btn"
              onClick={() => onToggleCanvas && onToggleCanvas()}
              title="Artifacts"
              aria-label="Toggle Artifacts Canvas"
              style={{
                background: isCanvasOpen ? 'rgba(var(--accent-rgb), 0.22)' : 'var(--bg-hover)',
                border: isCanvasOpen ? '1px solid rgba(var(--accent-rgb), 0.5)' : '1px solid var(--border-default)',
                color: '#fff',
                cursor: 'pointer',
                padding: '6px 10px',
                borderRadius: '999px',
                display: 'flex',
                alignItems: 'center',
                gap: '6px',
                transition: 'all 0.2s',
                fontSize: '12px',
                fontWeight: 500
              }}
            >
              <Layers size={14} />
              <span>Artifacts</span>
              <span
                data-testid="artifacts-badge"
                style={{
                  background: isCanvasOpen ? 'var(--accent)' : 'var(--bg-hover)',
                  color: isCanvasOpen ? 'var(--accent-on)' : 'var(--text-secondary)',
                  borderRadius: '999px',
                  padding: '1px 6px',
                  fontSize: '10px',
                  fontWeight: 600,
                  minWidth: '16px',
                  textAlign: 'center'
                }}
              >
                {artifactsCount}
              </span>
            </button>
            {/* Voice mode (Epic 8, E8-5.1a) */}
            <button
              type="button"
              data-testid="voice-header-btn"
              onClick={() => {
                if (voiceState && (voiceState.status === 'active' || voiceState.status === 'starting')) onVoiceStop();
                else onVoiceStart();
              }}
              title="Voice mode"
              aria-label="Toggle voice mode"
              style={{
                background: voiceState && voiceState.status === 'active' ? 'rgba(var(--accent-rgb), 0.22)' : 'var(--bg-hover)',
                border: voiceState && voiceState.status === 'active' ? '1px solid rgba(var(--accent-rgb), 0.5)' : '1px solid var(--border-default)',
                color: '#fff',
                cursor: 'pointer',
                padding: '6px 10px',
                borderRadius: '999px',
                display: 'flex',
                alignItems: 'center',
                gap: '6px',
                transition: 'all 0.2s',
                fontSize: '12px',
                fontWeight: 500
              }}
            >
              <Mic size={14} />
              <span>Voice</span>
              {voiceState && voiceState.status === 'active' && (
                <span
                  data-testid="voice-live-dot"
                  style={{
                    width: '8px',
                    height: '8px',
                    borderRadius: '50%',
                    background: 'var(--danger)',
                    display: 'inline-block'
                  }}
                />
              )}
            </button>
            {/* Rewind / Fork step picker (Epic 8, E8-1.1a/1.2, M12):
                the session-header entry point. The per-message menus
                can only name the first/final point; this lists the
                manifest's ACTUAL steps so an intermediate point can
                be chosen exactly, and passes the chosen entry through
                unchanged. Without a manifest it offers message-
                boundary forks — fork from anywhere holds on inherited
                and imported histories too. */}
            <div ref={stepPickerRef} style={{ position: 'relative' }}>
              <button
                type="button"
                data-testid="step-picker-btn"
                onClick={() => setShowStepPicker(prev => !prev)}
                title="Rewind / Fork…"
                aria-label="Rewind or fork from a recorded step"
                style={{
                  background: showStepPicker ? 'rgba(var(--accent-rgb), 0.22)' : 'var(--bg-hover)',
                  border: showStepPicker ? '1px solid rgba(var(--accent-rgb), 0.5)' : '1px solid var(--border-default)',
                  color: '#fff',
                  cursor: 'pointer',
                  padding: '6px 10px',
                  borderRadius: '999px',
                  display: 'flex',
                  alignItems: 'center',
                  gap: '6px',
                  transition: 'all 0.2s',
                  fontSize: '12px',
                  fontWeight: 500
                }}
              >
                <RotateCcw size={14} />
                <span>Rewind / Fork…</span>
              </button>
              {showStepPicker && (
                <div
                  data-testid="step-picker"
                  style={{
                    position: 'absolute',
                    top: '100%',
                    right: 0,
                    marginTop: '6px',
                    background: 'rgba(16, 16, 18, 0.66)',
                    backdropFilter: 'blur(16px) saturate(140%) url(#aether-glass-refraction)', WebkitBackdropFilter: 'blur(16px) saturate(140%) url(#aether-glass-refraction)',
                    border: '1px solid var(--glass-edge)',
                    borderRadius: 'var(--radius-lg)',
                    padding: '6px',
                    minWidth: '340px',
                    maxHeight: '340px',
                    overflowY: 'auto',
                    zIndex: 1100,
                    boxShadow: 'inset 0 1px 0 var(--glass-specular), 0 16px 40px rgba(0, 0, 0, 0.45)'
                  }}
                >
                  {stepPickerEntries.length > 0 ? (
                    <>
                      <div style={{ padding: '4px 8px', fontSize: '11px', fontWeight: 600, color: 'var(--accent)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>
                        Steps in the current task
                      </div>
                      {stepPickerEntries.map((entry) => (
                        <div
                          key={entry.step_index}
                          data-testid="step-picker-item"
                          style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '6px 8px', borderRadius: '6px' }}
                        >
                          <div style={{ flex: 1, minWidth: 0 }}>
                            <div style={{ fontSize: '12px', color: 'var(--text-primary)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{entry.label}</div>
                            <div style={{ fontSize: '11px', color: 'var(--text-muted)' }}>
                              Step {entry.step_index} · Checkpoint {String(entry.checkpoint_id || '').slice(0, 8)}
                            </div>
                          </div>
                          <button
                            type="button"
                            data-testid="step-picker-rewind"
                            onClick={() => { setShowStepPicker(false); if (onRewindRequest) onRewindRequest(entry); }}
                            style={{ background: 'rgba(var(--accent-rgb), 0.2)', border: '1px solid rgba(var(--accent-rgb), 0.4)', color: 'var(--accent)', padding: '4px 8px', borderRadius: '6px', cursor: 'pointer', fontSize: '11px' }}
                          >
                            Rewind
                          </button>
                          <button
                            type="button"
                            data-testid="step-picker-fork"
                            onClick={() => { setShowStepPicker(false); if (onForkRequest) onForkRequest({ message_id: null, step_index: entry.step_index }); }}
                            style={{ background: 'var(--bg-hover)', border: '1px solid var(--border-strong)', color: 'var(--text-primary)', padding: '4px 8px', borderRadius: '6px', cursor: 'pointer', fontSize: '11px' }}
                          >
                            Fork
                          </button>
                        </div>
                      ))}
                    </>
                  ) : (
                    <>
                      <div style={{ padding: '4px 8px', fontSize: '11px', fontWeight: 600, color: 'var(--accent)', textTransform: 'uppercase', letterSpacing: '0.04em' }}>
                        Fork from a message
                      </div>
                      <div style={{ padding: '0 8px 4px', fontSize: '11px', color: 'var(--text-muted)' }}>
                        No recorded steps in this session — fork from any message instead.
                      </div>
                      {stepPickerMessages.map((m, j) => (
                        <div
                          key={m.id || j}
                          data-testid="step-picker-message-item"
                          style={{ display: 'flex', alignItems: 'center', gap: '8px', padding: '6px 8px', borderRadius: '6px' }}
                        >
                          <div style={{ flex: 1, minWidth: 0, fontSize: '12px', color: 'var(--text-primary)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
                            {typeof m.content === 'string' ? m.content.slice(0, 60) : ''}
                          </div>
                          <button
                            type="button"
                            data-testid="step-picker-fork-message"
                            onClick={() => { setShowStepPicker(false); if (onForkRequest) onForkRequest({ message_id: m.id || null, step_index: null }); }}
                            style={{ background: 'var(--bg-hover)', border: '1px solid var(--border-strong)', color: 'var(--text-primary)', padding: '4px 8px', borderRadius: '6px', cursor: 'pointer', fontSize: '11px' }}
                          >
                            Fork
                          </button>
                        </div>
                      ))}
                    </>
                  )}
                </div>
              )}
            </div>
            {/* Session Export menu */}
            <div ref={exportMenuRef} style={{ position: 'relative' }}>
              <button
                onClick={() => setShowExportMenu(prev => !prev)}
                title="Export Session"
                aria-label="Export Session"
                style={{
                  background: showExportMenu ? 'rgba(var(--accent-rgb), 0.22)' : 'var(--bg-hover)',
                  border: '1px solid rgba(255, 255, 255, 0.1)',
                  color: '#fff',
                  cursor: 'pointer',
                  padding: '6px',
                  borderRadius: '50%',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  transition: 'all 0.2s'
                }}
              >
                <Download size={14} />
              </button>
              {showExportMenu && (
                <div style={{
                  position: 'absolute',
                  top: '100%',
                  right: 0,
                  marginTop: '6px',
                  background: 'rgba(16, 16, 18, 0.66)',
                  backdropFilter: 'blur(16px) saturate(140%) url(#aether-glass-refraction)', WebkitBackdropFilter: 'blur(16px) saturate(140%) url(#aether-glass-refraction)',
                  border: '1px solid var(--glass-edge)',
                  borderRadius: 'var(--radius-lg)',
                  padding: '4px',
                  minWidth: '140px',
                  zIndex: 1100,
                  boxShadow: 'inset 0 1px 0 var(--glass-specular), 0 16px 40px rgba(0, 0, 0, 0.45)'
                }}>
                  <button
                    onClick={() => { setShowExportMenu(false); onExportSession('markdown'); }}
                    style={{ width: '100%', textAlign: 'left', background: 'transparent', border: 'none', color: 'var(--text-primary)', padding: '6px 10px', borderRadius: '4px', cursor: 'pointer', fontSize: '12px', display: 'flex', alignItems: 'center', gap: '6px' }}
                  >
                    Markdown (.md)
                  </button>
                  <button
                    onClick={() => { setShowExportMenu(false); onExportSession('json'); }}
                    style={{ width: '100%', textAlign: 'left', background: 'transparent', border: 'none', color: 'var(--text-primary)', padding: '6px 10px', borderRadius: '4px', cursor: 'pointer', fontSize: '12px', display: 'flex', alignItems: 'center', gap: '6px' }}
                  >
                    JSON (.json)
                  </button>
                  <button
                    onClick={() => { setShowExportMenu(false); onExportSession('zip'); }}
                    style={{ width: '100%', textAlign: 'left', background: 'transparent', border: 'none', color: 'var(--text-primary)', padding: '6px 10px', borderRadius: '4px', cursor: 'pointer', fontSize: '12px', display: 'flex', alignItems: 'center', gap: '6px' }}
                  >
                    ZIP (.zip)
                  </button>
                </div>
              )}
            </div>

            {/* Phase 10.3 C2: isMinimized used to be settable by nothing —
                the state existed, the control did not. This toggle wires
                it: minimise collapses the feed + input to the header bar,
                restore brings them back. Minimize2/Maximize2 were already
                imported and unused for this. */}
            {isMinimized ? (
              <button 
                onClick={() => setIsMinimized(false)}
                title="Restore chat"
                aria-label="Restore chat"
                style={{ background: 'var(--bg-hover)', border: '1px solid var(--border-default)', color: 'var(--text-primary)', cursor: 'pointer', padding: '6px', borderRadius: '50%', display: 'flex', alignItems: 'center', justifyContent: 'center', transition: 'all 0.2s' }}
              >
                <Maximize2 size={14} />
              </button>
            ) : (
              <button 
                onClick={() => setIsMinimized(true)}
                title="Minimise chat"
                aria-label="Minimise chat"
                style={{ background: 'var(--bg-hover)', border: '1px solid var(--border-default)', color: 'var(--text-primary)', cursor: 'pointer', padding: '6px', borderRadius: '50%', display: 'flex', alignItems: 'center', justifyContent: 'center', transition: 'all 0.2s' }}
              >
                <Minimize2 size={14} />
              </button>
            )}
            {isFloating ? (
              <>
                <button 
                  onClick={() => window.electronAPI && window.electronAPI.closeFloatingChat()}
                  title="Re-attach to Main Window"
                  style={{ WebkitAppRegion: 'no-drag', background: 'var(--bg-hover)', border: '1px solid var(--border-default)', color: 'var(--text-primary)', cursor: 'pointer', padding: '6px', borderRadius: '50%', display: 'flex', alignItems: 'center', justifyContent: 'center' }}
                >
                  <LogIn size={14} />
                </button>
                <button 
                  onClick={() => window.electronAPI && window.electronAPI.closeFloatingChat()}
                  title="Close Window"
                  style={{ WebkitAppRegion: 'no-drag', background: 'rgba(201, 42, 42, 0.2)', border: '1px solid rgba(201, 42, 42, 0.4)', color: 'var(--danger)', cursor: 'pointer', padding: '6px', borderRadius: '50%', display: 'flex', alignItems: 'center', justifyContent: 'center' }}
                >
                  <X size={14} />
                </button>
              </>
            ) : (
              <button 
                onClick={() => onDetach({ input })}
                title="Pop out to floating window"
                style={{ background: 'var(--bg-hover)', border: '1px solid var(--border-default)', color: 'var(--text-primary)', cursor: 'pointer', padding: '6px', borderRadius: '50%', display: 'flex', alignItems: 'center', justifyContent: 'center', transition: 'all 0.2s' }}
              >
                <ExternalLink size={14} />
              </button>
            )}
          </div>
        </div>

        {/* RUX-C-16: slim offline notice for the chat area. The
            PendingQueueIndicator counts queued ACTIONS; this states
            the connection FACT. It renders only while disconnected
            and removes itself on reconnect — nothing to dismiss. */}
        {!isMinimized && chatOffline && (
          <div
            data-testid="chat-offline-banner"
            style={{
              padding: '8px 16px',
              background: 'rgba(201, 42, 42, 0.08)',
              borderBottom: '1px solid rgba(201, 42, 42, 0.25)',
              color: 'var(--danger)',
              fontSize: '12.5px',
              textAlign: 'center'
            }}
          >
            Engine disconnected — you are viewing the last known state. Actions will queue until the connection returns.
          </div>
        )}

        {/* Chat Feed */}
        {!isMinimized && (
          <>
            <ChatTranscript
              messages={messages}
              isThinking={isThinking}
              sessionSwitchPending={sessionSwitchPending}
              chatOffline={chatOffline}
              switchSlow={switchSlow}
              voiceState={voiceState}
              feedScrollRef={feedScrollRef}
              onFeedScroll={handleFeedScroll}
              messagesEndRef={messagesEndRef}
              rewindManifest={rewindManifest}
              onRewindRequest={onRewindRequest}
              onForkRequest={onForkRequest}
              approvalArgEdits={approvalArgEdits}
              setApprovalArgEdits={setApprovalArgEdits}
              browserEnvelopeGrants={browserEnvelopeGrants}
              setBrowserEnvelopeGrants={setBrowserEnvelopeGrants}
              onEnvelopeGrantChange={handleEnvelopeGrantChange}
              onApproveTool={onApproveTool}
              onApprovePlugin={onApprovePlugin}
              onVaultFillApproval={onVaultFillApproval}
              approveToolWithEdits={approveToolWithEdits}
              onStopTask={onStopTask}
              onOpenArtifact={onOpenArtifact}
              onEditMessage={onEditMessage}
              setInput={setInput}
              textareaRef={textareaRef}
              openStepMenu={openStepMenu}
              setOpenStepMenu={setOpenStepMenu}
              playingMessageId={playingMessageId}
              readAloudError={readAloudError}
              onToggleReadAloud={handleToggleReadAloud}
              onRegenerate={onRegenerate}
            />

            {/* Agent browser task card (Epic 8, E8-6.1) */}
            {activeBrowserSession && (
              <BrowserTaskCard
                session={activeBrowserSession}
                onControl={handleBrowserControlClick}
              />
            )}

            {/* Scratchpad panel (Epic 1, E1-17): above the composer, below the message stream */}
            <ScratchpadPanel scratchpad={scratchpad} />

            {/* Input Area */}
            <ChatComposer
              input={input}
              attachments={attachments}
              isThinking={isThinking}
              planMode={planMode}
              isDictating={isDictating}
              dictationError={dictationError}
              showSlashPopup={showSlashPopup}
              filteredCommands={filteredCommands}
              slashIndex={slashIndex}
              slashPopupRef={slashPopupRef}
              textareaRef={textareaRef}
              onSubmit={handleSubmit}
              onInputChange={handleInputChange}
              onInputKeyDown={handleInputKeyDown}
              onTogglePlanMode={onTogglePlanMode}
              onTriggerSnip={handleTriggerSnip}
              onRemoveAttachment={handleRemoveAttachment}
              onSelectSlashCommand={handleSlashSelect}
              onMicStart={handleMicStart}
              onMicEnd={handleMicEnd}
              onMicKeyDown={handleMicKeyDown}
              onMicKeyUp={handleMicKeyUp}
              onStopTask={onStopTask}
              onDismissDictationError={() => setDictationError(null)}
            />
          </>
        )}

        {/* Epic 8, E8-1.1b: 5-second rewind undo window. Expiry is
            reported through onRewindUndoExpire so App can tell an
            expiry apart from an Undo click. */}
        {rewindUndo && (
          <UndoToast
            expiresAt={rewindUndo.expiresAt}
            onUndo={() => { if (onRewindUndo) onRewindUndo(); }}
            onExpire={() => { if (onRewindUndoExpire) onRewindUndoExpire(); }}
            queued={!!rewindUndo.queued}
          />
        )}

        {/* Copy Success Floating Toast */}
        {toastMessage && (
          <div className="ios-copy-toast">
            <Check size={14} color="#4CC38A" />
            <span>{toastMessage}</span>
          </div>
        )}

        {voiceState && (
          <VoiceOverlay
            open
            active={voiceState.status === 'active'}
            status={voiceState.status === 'active' ? ((voiceState.assistantText || voiceState.announcementText) ? 'speaking' : 'listening') : voiceState.status}
            partialText={voiceState.partialText || ''}
            assistantText={voiceState.assistantText || voiceState.announcementText || ''}
            error={voiceState.error || null}
            webrtcConfig={voiceState.webrtcConfig || null}
            audioChunk={voiceAudioChunk}
            playbackCut={voicePlaybackCut}
            onStop={onVoiceStop}
            onDismiss={onVoiceDismissError}
            onRtcOffer={onVoiceRtcOffer}
            rtcAnswer={voiceRtcAnswer}
            onRtcIceCandidate={onVoiceRtcIceCandidate}
            onPlaybackProgress={onVoicePlaybackProgress}
            rtcRemoteCandidate={voiceRtcRemoteCandidate}
          />
        )}

        <SnipOverlay isOpen={isSnipOverlayOpen} onCapture={handleSnipCaptured} onCancel={() => setIsSnipOverlayOpen(false)} />

        {/* Apple iOS Glass Context Menu */}
        <ContextMenu 
          menu={contextMenu} 
          onClose={() => setContextMenu(null)} 
          onShowToast={showToast}
        />
      </div>
  );
}
