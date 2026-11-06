import { useState, useEffect, useRef, useCallback } from 'react';

// Wave 5, Annex C row 5 (session-store seam): the session list /
// selection state machine, extracted from App.jsx. The store owns
// the session list and its search / rename / move machinery, the
// per-session plan-mode and scratchpad maps, the active-session
// switch cache with its pending-switch refs, and the list-request
// builders — plus the two effects that are purely about this state
// (the active-workspace re-file and the debounced search).
//
// App hands it the socket facade pieces and the few cross-seam
// collaborators the switch / new-chat actions touch: switching
// sessions resets the rewind surfaces (E8-1.1a/b), which stay with
// App's rewind block, and clears the previous session's pending
// approvals (D1-A5), whose ref App owns. Everything the
// once-registered message handler needs from here reaches it
// through the returned refs and setters, unchanged.

export function useSessionStore(deps) {
  const {
    wsRef, safeSend, initialSession, pendingApprovalsRef,
    setChatMessages, setRewindManifest, setRewindConfirm,
    clearRewindUndo, rewindUndo,
  } = deps;

  const [sessions, setSessions] = useState([]);
  // D1-A1/A2: mirror of the session list for the once-registered
  // message handler — SESSION_DELETE_RESPONSE computes the
  // post-delete list and the follow-up switch from this ref,
  // outside any state updater.
  const sessionsRef = useRef([]);
  useEffect(() => { sessionsRef.current = sessions; }, [sessions]);
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
  // Per-workspace sessions (grill Q5/Q6/Q10): the engine's workspace
  // rows (WORKSPACE_LIST), which the session list filters by; the
  // list view — 'workspace' (the active root), 'all', or 'unbound';
  // and the move machinery (per-row menu + multi-select bulk loop).
  // The refs mirror what the once-registered message handler and
  // the list-request helper read.
  const [workspaces, setWorkspaces] = useState([]);
  const [sessionListView, setSessionListView] = useState('workspace');
  const sessionListViewRef = useRef('workspace');
  const activeWorkspacePathRef = useRef(null);
  const [sessionSelectMode, setSessionSelectMode] = useState(false);
  const [selectedSessionIds, setSelectedSessionIds] = useState([]);
  const [moveMenuFor, setMoveMenuFor] = useState(null);
  const [moveStatus, setMoveStatus] = useState(null);
  const moveBatchRef = useRef(null);

  const pendingSessionSwitchRef = useRef(null);
  // Wave 2 slice 2 (RUX-A-01): a session switch now goes out via
  // safeSend, whose request_id is minted inside the facade — the
  // switch can no longer register its own id in pendingSessionSwitchRef
  // ahead of the send. The target session id is registered here
  // instead, and the SESSION_GET_RESPONSE handler honours a reply
  // whose session matches it (a reply for any other session is still
  // ignored, so echoed broadcasts cannot hijack the view).
  const pendingSessionSwitchIdRef = useRef(null);
  // RUX-C-13: the reactive half of the pending switch — the id of
  // a session whose history has been requested but not yet
  // answered AND which has no cached copy to show. While it is
  // set, the chat pane renders a switching/loading state instead
  // of "No active session": an uncached session in flight is not
  // an empty session. Cleared by the honoured SESSION_GET reply.
  const [sessionSwitchPendingId, setSessionSwitchPendingId] = useState(null);
  const sessionCacheRef = useRef({});
  const activeTaskSessionRef = useRef(null);

  // Per-workspace sessions: the one place SESSION_LIST_REQUEST is
  // built, so every refresh honours the current list view. The
  // payload encodes the store's three filter states — no payload at
  // all for "all" (exactly the pre-slice wire shape), a path for
  // the active workspace, explicit null for unbound only.
  const sendSessionListRequest = useCallback(() => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
    const msg = { type: 'SESSION_LIST_REQUEST', schema_version: 1, request_id: Date.now().toString() };
    const view = sessionListViewRef.current;
    if (view === 'unbound') {
      msg.payload = { workspace: null };
    } else if (view === 'workspace' && activeWorkspacePathRef.current) {
      msg.payload = { workspace: activeWorkspacePathRef.current };
    }
    wsRef.current.send(JSON.stringify(msg));
  }, [wsRef]);
  const requestWorkspaceList = useCallback(() => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
    wsRef.current.send(JSON.stringify({
      type: 'WORKSPACE_LIST_REQUEST',
      schema_version: 1,
      request_id: Date.now().toString(),
      payload: {},
    }));
  }, [wsRef]);
  // Derived workspace lookups, shared by the palette items, the
  // search-result labels, and the sidebar filter chips. A binding
  // is a root path; its label is the workspace's display name, or
  // "No workspace" for an unbound (null) binding.
  const activeWorkspaceRow = workspaces.find(w => w.is_active) || null;
  const workspaceLabelFor = (path) => {
    if (!path) return 'No workspace';
    const row = workspaces.find(w => w.path === path);
    return row ? row.name : path;
  };

  // Per-workspace sessions (Q5): when the ACTIVE workspace changes,
  // the session list re-files itself under the new workspace — the
  // view returns to 'workspace' and the list is re-requested with
  // the new filter. Only a path change acts: count-only refreshes
  // of the rows leave the user's chosen view (All / No workspace)
  // alone. The open session is untouched either way — this effect
  // only ever re-requests the LIST.
  useEffect(() => {
    const active = workspaces.find(w => w.is_active) || null;
    const nextPath = active ? active.path : null;
    if (nextPath === activeWorkspacePathRef.current) return;
    activeWorkspacePathRef.current = nextPath;
    sessionListViewRef.current = 'workspace';
    setSessionListView('workspace');
    sendSessionListRequest();
  }, [workspaces, sendSessionListRequest]);

  const handleSessionSwitch = (id) => {
    // D1-A5: the previous session's undecided approvals are not
    // actionable from the session being switched to.
    pendingApprovalsRef.current = [];
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
    // Wave 2 slice 2 (RUX-A-01): the fetch goes out via safeSend —
    // a session clicked while the socket is down is queued, not
    // discarded, so the full history replaces the cached (or empty)
    // list as soon as the connection returns, instead of the cached
    // list being presented as that session forever. safeSend mints
    // the request_id inside the facade, so the pending switch is
    // registered by target session id (see pendingSessionSwitchIdRef).
    pendingSessionSwitchRef.current = null;
    pendingSessionSwitchIdRef.current = id;
    // RUX-C-13: a cached copy renders instantly and needs no
    // loading state (the fetch still refreshes it); an uncached
    // target shows the switching state until the reply lands.
    setSessionSwitchPendingId(sessionCacheRef.current[id] ? null : id);
    safeSend('SESSION_GET_REQUEST', { session_id: id });
  };

  const handleNewChat = () => {
    setRewindManifest(null); // E8-1.1a: a fresh chat has no points yet
    // Wave 2 slice 2 (RUX-A-02): the create goes out via safeSend —
    // never discarded. While 'sent', the chat clears immediately for
    // instant feel, exactly as before. While 'queued', the chat is
    // NOT cleared and the current session stays active: clearing
    // into a fake-empty view while activeSession still names the old
    // session was the worst silent failure in the app — the user's
    // next message silently landed in the session they believed
    // they had left. The queued create flushes on reconnect and the
    // SESSION_CREATE_RESPONSE handler performs the switch and the
    // clear when the new session actually exists; the app-level
    // pending indicator shows the action is waiting meanwhile.
    const disposition = safeSend('SESSION_CREATE_REQUEST', { title: 'New Conversation' });
    if (disposition === 'sent') {
      setChatMessages([]); // Clear immediately for instant feel
    }
  };

  const handleDeleteSession = (id, e) => {
    e.stopPropagation(); // prevent triggering handleSessionSwitch
    safeSend('SESSION_DELETE_REQUEST', { session_id: id });
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

  // Per-workspace sessions (Q5/Q6): list-view switching, selection,
  // and moves. The view lives in a ref as well as state because the
  // list-request helper and the message handler read it outside
  // React's render cycle. (The workspace lookups themselves —
  // activeWorkspaceRow / workspaceLabelFor — live above, beside the
  // request helpers; the palette memo reads them from the return.)
  const handleSessionListViewChange = (view) => {
    sessionListViewRef.current = view;
    setSessionListView(view);
    setMoveMenuFor(null);
    sendSessionListRequest();
  };

  const toggleSessionSelected = (sessionId) => {
    setSelectedSessionIds(prev => (prev.includes(sessionId)
      ? prev.filter(id => id !== sessionId)
      : [...prev, sessionId]));
  };

  // One SESSION_MOVE_REQUEST per session: the engine has no bulk
  // pair, so bulk move is this renderer loop over the single-move
  // contract. Outcomes tally into moveBatchRef and settle in the
  // SESSION_MOVE_RESPONSE branch of the message handler.
  const startSessionMoves = (sessionIds, targetWorkspace) => {
    if (!sessionIds.length) return;
    // RUX-A-07: via safeSend (see handleDeleteSession above) — the
    // menu closes and the batch tally is armed even while offline;
    // the moves flush in order on reconnect and the
    // SESSION_MOVE_RESPONSE tally settles when the replies land.
    setMoveMenuFor(null);
    setMoveStatus(null);
    moveBatchRef.current = { total: sessionIds.length, settled: 0, moved: 0, failed: 0, firstError: null };
    sessionIds.forEach((sid) => {
      safeSend('SESSION_MOVE_REQUEST', { session_id: sid, workspace: targetWorkspace });
    });
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

  return {
    sessions, setSessions, sessionsRef,
    planModeBySession, setPlanModeBySession,
    scratchpadBySession, setScratchpadBySession,
    pendingThinkingRef,
    sessionSearchQuery, setSessionSearchQuery,
    sessionSearchResults, setSessionSearchResults,
    sessionSearchQueryRef,
    editingSessionId, setEditingSessionId,
    editingSessionTitle, setEditingSessionTitle,
    activeSession, setActiveSession, activeSessionRef,
    workspaces, setWorkspaces,
    sessionListView, sessionListViewRef,
    activeWorkspacePathRef,
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
  };
}
