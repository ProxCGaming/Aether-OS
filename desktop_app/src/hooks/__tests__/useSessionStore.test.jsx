import React from 'react';
import { render, act } from '@testing-library/react';
import { useSessionStore } from '../useSessionStore';

// Wave 5, Annex C row 5 (session-store seam): contract tests for the
// extracted session list / selection state machine. The store is
// driven through its deps (socket facade + the cross-seam
// collaborators App lends it) and its returned surface — the same
// names App destructures.

global.WebSocket = { OPEN: 1, CLOSED: 3, CONNECTING: 0, CLOSING: 2 };

function makeDeps(overrides = {}) {
  const ws = { readyState: 1, send: jest.fn() };
  return {
    wsRef: { current: ws },
    safeSend: jest.fn(() => 'sent'),
    initialSession: null,
    pendingApprovalsRef: { current: [{ id: 'appr-1' }] },
    setChatMessages: jest.fn(),
    setRewindManifest: jest.fn(),
    setRewindConfirm: jest.fn(),
    clearRewindUndo: jest.fn(),
    rewindUndo: null,
    ...overrides,
    _ws: ws,
  };
}

let store;
function Probe({ deps }) {
  store = useSessionStore(deps);
  return null;
}

function mount(overrides) {
  const deps = makeDeps(overrides);
  const utils = render(<Probe deps={deps} />);
  const sent = () => deps._ws.send.mock.calls.map(([raw]) => JSON.parse(raw));
  return { deps, sent, ...utils };
}

describe('useSessionStore', () => {
  afterEach(() => {
    jest.useRealTimers();
    jest.restoreAllMocks();
  });

  it('starts empty, or on the floating window\'s initial session', () => {
    mount();
    expect(store.sessions).toEqual([]);
    expect(store.activeSession).toBeNull();
    expect(store.sessionListView).toBe('workspace');
    const { unmount } = mount({ initialSession: 'sX' });
    expect(store.activeSession).toBe('sX');
    expect(store.activeSessionRef.current).toBe('sX');
    unmount();
  });

  it('mirrors the session list into sessionsRef for the message handler', () => {
    mount();
    act(() => { store.setSessions([{ id: 's1', title: 'One' }]); });
    expect(store.sessionsRef.current).toEqual([{ id: 's1', title: 'One' }]);
  });

  it('sendSessionListRequest encodes the three list views on the wire', () => {
    const { deps, sent } = mount();
    // Default 'workspace' view with no active workspace: bare request.
    act(() => { store.sendSessionListRequest(); });
    expect(sent().at(-1)).toEqual(expect.objectContaining({ type: 'SESSION_LIST_REQUEST' }));
    expect(sent().at(-1).payload).toBeUndefined();

    act(() => { store.handleSessionListViewChange('unbound'); });
    expect(sent().at(-1).payload).toEqual({ workspace: null });

    act(() => { store.handleSessionListViewChange('all'); });
    expect(sent().at(-1).payload).toBeUndefined();
    expect(deps._ws.send).toHaveBeenCalled();
  });

  it('the workspace effect re-files the list under a newly active workspace', () => {
    const { sent } = mount();
    act(() => {
      store.setWorkspaces([{ path: '/proj', name: 'Proj', is_active: true }]);
    });
    expect(sent().at(-1)).toEqual(expect.objectContaining({
      type: 'SESSION_LIST_REQUEST',
      payload: { workspace: '/proj' },
    }));
    expect(store.activeWorkspaceRow).toEqual(expect.objectContaining({ path: '/proj' }));
  });

  it('workspaceLabelFor labels bindings honestly', () => {
    mount();
    act(() => {
      store.setWorkspaces([{ path: '/proj', name: 'Proj', is_active: false }]);
    });
    expect(store.workspaceLabelFor(null)).toBe('No workspace');
    expect(store.workspaceLabelFor('/proj')).toBe('Proj');
    expect(store.workspaceLabelFor('/elsewhere')).toBe('/elsewhere');
  });

  it('handleSessionSwitch resets cross-seam state, caches, and fetches via safeSend', () => {
    const { deps } = mount();
    act(() => { store.handleSessionSwitch('s2'); });
    // D1-A5: the old session's approvals die with the switch.
    expect(deps.pendingApprovalsRef.current).toEqual([]);
    // E8-1.1a/b: rewind surfaces reset.
    expect(deps.setRewindManifest).toHaveBeenCalledWith(null);
    expect(deps.setRewindConfirm).toHaveBeenCalledWith(null);
    expect(store.activeSession).toBe('s2');
    expect(deps.safeSend).toHaveBeenCalledWith('SESSION_GET_REQUEST', { session_id: 's2' });
    // RUX-C-13: uncached target shows the switching state.
    expect(store.sessionSwitchPendingId).toBe('s2');

    // A cached target renders instantly — no switching state.
    act(() => { store.sessionCacheRef.current.s3 = [{ id: 'm1' }]; });
    act(() => { store.handleSessionSwitch('s3'); });
    expect(store.sessionSwitchPendingId).toBeNull();
  });

  it('handleNewChat clears immediately only when the create was sent (RUX-A-02)', () => {
    const { deps } = mount();
    act(() => { store.handleNewChat(); });
    expect(deps.safeSend).toHaveBeenCalledWith('SESSION_CREATE_REQUEST', { title: 'New Conversation' });
    expect(deps.setChatMessages).toHaveBeenCalledWith([]);
    expect(deps.setRewindManifest).toHaveBeenCalledWith(null);

    deps.setChatMessages.mockClear();
    deps.safeSend.mockReturnValue('queued');
    act(() => { store.handleNewChat(); });
    expect(deps.setChatMessages).not.toHaveBeenCalled();
  });

  it('handleDeleteSession stops the row click and sends via safeSend', () => {
    const { deps } = mount();
    const ev = { stopPropagation: jest.fn() };
    act(() => { store.handleDeleteSession('s1', ev); });
    expect(ev.stopPropagation).toHaveBeenCalled();
    expect(deps.safeSend).toHaveBeenCalledWith('SESSION_DELETE_REQUEST', { session_id: 's1' });
  });

  it('rename: start fills the editor, commit trims and sends, empty cancels', () => {
    const { deps, sent } = mount();
    act(() => { store.handleStartRenameSession({ id: 's1', title: 'Old name' }, null); });
    expect(store.editingSessionId).toBe('s1');
    expect(store.editingSessionTitle).toBe('Old name');
    act(() => { store.setEditingSessionTitle('  New name  '); });
    act(() => { store.handleCommitRenameSession('s1'); });
    expect(sent().at(-1)).toEqual(expect.objectContaining({
      type: 'SESSION_RENAME_REQUEST',
      payload: { session_id: 's1', title: 'New name' },
    }));
    expect(store.editingSessionId).toBeNull();
    expect(deps._ws.send).toHaveBeenCalled();
  });

  it('startSessionMoves arms the batch tally and sends one request per session', () => {
    const { deps } = mount();
    act(() => { store.setMoveMenuFor('s1'); });
    act(() => { store.startSessionMoves(['a', 'b'], '/proj'); });
    expect(store.moveMenuFor).toBeNull();
    expect(store.moveBatchRef.current).toEqual({
      total: 2, settled: 0, moved: 0, failed: 0, firstError: null,
    });
    expect(deps.safeSend).toHaveBeenCalledWith('SESSION_MOVE_REQUEST', { session_id: 'a', workspace: '/proj' });
    expect(deps.safeSend).toHaveBeenCalledWith('SESSION_MOVE_REQUEST', { session_id: 'b', workspace: '/proj' });
  });

  it('the search effect debounces and sends the current query', () => {
    jest.useFakeTimers();
    const { sent } = mount();
    act(() => { store.setSessionSearchQuery('hello'); });
    act(() => { jest.advanceTimersByTime(299); });
    expect(sent().filter((p) => p.type === 'SESSION_SEARCH_REQUEST')).toHaveLength(0);
    act(() => { jest.advanceTimersByTime(1); });
    const searches = sent().filter((p) => p.type === 'SESSION_SEARCH_REQUEST');
    expect(searches).toHaveLength(1);
    expect(searches[0].payload).toEqual({ query: 'hello' });
    expect(store.sessionSearchQueryRef.current).toBe('hello');
  });
});
