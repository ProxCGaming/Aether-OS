import React from 'react';
import { render, screen, fireEvent, act, within } from '@testing-library/react';
import '@testing-library/jest-dom';
import App from '../App';

// The 3D force graph needs WebGL; replace with a stub for jsdom.
jest.mock('react-force-graph-3d', () => ({
  __esModule: true,
  default: () => <div data-testid="force-graph-stub" />,
}));

// react-markdown is ESM-only; stub it for Jest.
jest.mock('react-markdown', () => ({
  __esModule: true,
  default: ({ children }) => <div data-testid="markdown-stub">{children}</div>,
}));

// jsdom has no scrollIntoView; stub it.
Element.prototype.scrollIntoView = jest.fn();

// jsdom has no WebSocket; the socket layer only needs the constants.
global.WebSocket = { OPEN: 1, CLOSED: 3, CONNECTING: 0, CLOSING: 2 };

// Per-workspace sessions, renderer slice (grill Q5/Q6/Q10): the
// session list filters by the active workspace with "All sessions"
// and "No workspace" escapes; switching workspaces never loses the
// open session; sessions move via SESSION_MOVE (single + bulk loop);
// search stays global and labels each result with its workspace.
describe('App per-workspace session list', () => {
  let engineListeners;
  let electronAPI;

  beforeEach(() => {
    engineListeners = [];
    electronAPI = {
      sendEngineMessage: jest.fn(),
      onEngineMessage: jest.fn((cb) => {
        engineListeners.push(cb);
        return () => {
          const idx = engineListeners.indexOf(cb);
          if (idx !== -1) engineListeners.splice(idx, 1);
        };
      }),
      getWsStatus: jest.fn(() => Promise.resolve(false)),
      getMemoryUsage: jest.fn(() => Promise.resolve(128)),
      onChatDetached: jest.fn(() => jest.fn()),
      onChatAttached: jest.fn(() => jest.fn()),
    };
    window.electronAPI = electronAPI;
  });

  afterEach(() => {
    delete window.electronAPI;
  });

  const emitEngineMessage = (obj) => {
    const raw = typeof obj === 'string' ? obj : JSON.stringify(obj);
    act(() => {
      [...engineListeners].forEach((cb) => cb(raw));
    });
  };

  const flushPromises = async () => {
    await act(async () => {});
  };

  const connect = async () => {
    render(<App />);
    await flushPromises();
    emitEngineMessage({ type: '_ws_status', status: 'connected' });
    await flushPromises();
  };

  const sentPackets = () =>
    electronAPI.sendEngineMessage.mock.calls.map(([raw]) => JSON.parse(raw));

  const packetsOfType = (type) => sentPackets().filter((p) => p.type === type);

  const lastListRequest = () => {
    const reqs = packetsOfType('SESSION_LIST_REQUEST');
    return reqs[reqs.length - 1];
  };

  const WORKSPACES = [
    { path: '/ws/a', name: 'Alpha', is_indexed: false, exists: true, session_count: 1 },
    { path: '/ws/b', name: 'Beta', is_indexed: false, exists: true, session_count: 1 },
  ];

  const seedWorkspaces = (activePath) => {
    emitEngineMessage({
      type: 'WORKSPACE_LIST_RESPONSE',
      payload: {
        workspaces: WORKSPACES.map((w) => ({ ...w, is_active: w.path === activePath })),
        unbound_session_count: 1,
      },
    });
  };

  const seedFilteredList = (sessions) => {
    emitEngineMessage({ type: 'SESSION_LIST_RESPONSE', payload: { sessions } });
  };

  describe('list filtering (Q5)', () => {
    it('requests the active workspace sessions once the workspace list lands', async () => {
      await connect();
      seedWorkspaces('/ws/a');

      expect(lastListRequest().payload).toEqual({ workspace: '/ws/a' });

      seedFilteredList([{ id: 'sa', title: 'Alpha chat', workspace: '/ws/a' }]);
      expect(screen.getByText('Alpha chat')).toBeInTheDocument();
    });

    it('offers All sessions and No workspace escapes that re-request with the matching filter', async () => {
      await connect();
      seedWorkspaces('/ws/a');
      seedFilteredList([{ id: 'sa', title: 'Alpha chat', workspace: '/ws/a' }]);

      fireEvent.click(screen.getByRole('button', { name: 'All sessions' }));
      const allReq = lastListRequest();
      expect(allReq.payload?.workspace).toBeUndefined();

      fireEvent.click(screen.getByRole('button', { name: 'No workspace' }));
      expect(lastListRequest().payload).toEqual({ workspace: null });
    });

    it('re-filters when the active workspace changes', async () => {
      await connect();
      seedWorkspaces('/ws/a');
      seedFilteredList([{ id: 'sa', title: 'Alpha chat', workspace: '/ws/a' }]);

      // The user picks "All sessions", then sets Beta active in
      // Projects: the list returns to the (new) workspace view.
      fireEvent.click(screen.getByRole('button', { name: 'All sessions' }));
      seedWorkspaces('/ws/b');
      expect(lastListRequest().payload).toEqual({ workspace: '/ws/b' });

      seedFilteredList([{ id: 'sb', title: 'Beta chat', workspace: '/ws/b' }]);
      expect(screen.getByText('Beta chat')).toBeInTheDocument();
      expect(screen.queryByText('Alpha chat')).not.toBeInTheDocument();
    });

    it('shows no filter chips and lists everything when no workspace is configured', async () => {
      await connect();
      emitEngineMessage({
        type: 'WORKSPACE_LIST_RESPONSE',
        payload: { workspaces: [], unbound_session_count: 2 },
      });
      seedFilteredList([
        { id: 's1', title: 'Loose One', workspace: null },
        { id: 's2', title: 'Loose Two', workspace: null },
      ]);

      expect(screen.getByText('Loose One')).toBeInTheDocument();
      expect(screen.getByText('Loose Two')).toBeInTheDocument();
      expect(screen.queryByRole('button', { name: 'All sessions' })).not.toBeInTheDocument();
      expect(lastListRequest().payload?.workspace).toBeUndefined();
    });
  });

  describe('open-session preservation (Q5, owner words)', () => {
    it('switching workspaces never closes or replaces the open session', async () => {
      await connect();
      seedWorkspaces('/ws/a');
      // The filtered list lands; its first session auto-opens.
      seedFilteredList([{ id: 'sa', title: 'Alpha chat', workspace: '/ws/a' }]);
      const getsBefore = packetsOfType('SESSION_GET_REQUEST').length;
      expect(getsBefore).toBeGreaterThan(0);

      // Switch to Beta: the list re-files, but no session switch is
      // forced and no row claims to be the active one.
      seedWorkspaces('/ws/b');
      seedFilteredList([{ id: 'sb', title: 'Beta chat', workspace: '/ws/b' }]);
      expect(screen.getByText('Beta chat')).toBeInTheDocument();
      expect(packetsOfType('SESSION_GET_REQUEST')).toHaveLength(getsBefore);
      expect(document.querySelector('.session-item.active')).toBeNull();

      // Clicking a title is what switches the current session.
      fireEvent.click(screen.getByText('Beta chat'));
      const gets = packetsOfType('SESSION_GET_REQUEST');
      expect(gets).toHaveLength(getsBefore + 1);
      expect(gets[gets.length - 1].payload.session_id).toBe('sb');

      // Switching back shows the earlier session in its own
      // workspace list again, still the preserved one until clicked.
      seedWorkspaces('/ws/a');
      seedFilteredList([{ id: 'sa', title: 'Alpha chat', workspace: '/ws/a' }]);
      expect(screen.getByText('Alpha chat')).toBeInTheDocument();
    });
  });

  describe('moves (Q6)', () => {
    const openMoveMenu = async () => {
      await connect();
      seedWorkspaces('/ws/a');
      seedFilteredList([{ id: 'sa', title: 'Alpha chat', workspace: '/ws/a' }]);
      fireEvent.click(screen.getByRole('button', { name: 'Move session' }));
      return screen.getByTestId('session-move-menu-sa');
    };

    it('sends SESSION_MOVE_REQUEST for the chosen target and reconciles the list', async () => {
      const menu = await openMoveMenu();
      fireEvent.click(within(menu).getByRole('button', { name: 'Beta' }));

      const moves = packetsOfType('SESSION_MOVE_REQUEST');
      expect(moves).toHaveLength(1);
      expect(moves[0].payload).toEqual({ session_id: 'sa', workspace: '/ws/b' });

      emitEngineMessage({
        type: 'SESSION_MOVE_RESPONSE',
        request_id: moves[0].request_id,
        payload: { session_id: 'sa', workspace: '/ws/b', moved: true },
      });
      // The filtered list is re-requested and the moved row leaves it.
      expect(lastListRequest().payload).toEqual({ workspace: '/ws/a' });
      seedFilteredList([]);
      expect(screen.queryByText('Alpha chat')).not.toBeInTheDocument();
    });

    it('surfaces a refused move and keeps the row', async () => {
      const menu = await openMoveMenu();
      fireEvent.click(within(menu).getByRole('button', { name: 'Beta' }));
      const moves = packetsOfType('SESSION_MOVE_REQUEST');

      emitEngineMessage({
        type: 'SESSION_MOVE_RESPONSE',
        request_id: moves[0].request_id,
        payload: { session_id: 'sa', workspace: '/ws/a', moved: false, error: 'Target workspace is not configured' },
      });

      expect(screen.getByTestId('session-move-status')).toHaveTextContent('Target workspace is not configured');
      expect(screen.getByText('Alpha chat')).toBeInTheDocument();
    });

    it('bulk-moves the selection with one request per session and reports a partial failure honestly', async () => {
      await connect();
      seedWorkspaces('/ws/a');
      seedFilteredList([{ id: 'sa', title: 'Alpha chat', workspace: '/ws/a' }]);
      fireEvent.click(screen.getByRole('button', { name: 'All sessions' }));
      seedFilteredList([
        { id: 'sa', title: 'Alpha chat', workspace: '/ws/a' },
        { id: 'sc', title: 'Loose chat', workspace: null },
      ]);

      fireEvent.click(screen.getByRole('button', { name: 'Select sessions' }));
      fireEvent.click(screen.getByLabelText('Select session Alpha chat'));
      fireEvent.click(screen.getByLabelText('Select session Loose chat'));
      expect(screen.getByText('2 selected')).toBeInTheDocument();

      const bar = screen.getByTestId('session-bulk-bar');
      fireEvent.click(within(bar).getByRole('button', { name: 'Move to…' }));
      const menu = screen.getByTestId('session-bulk-move-menu');
      fireEvent.click(within(menu).getByRole('button', { name: 'Beta' }));

      const moves = packetsOfType('SESSION_MOVE_REQUEST');
      expect(moves).toHaveLength(2);
      expect(moves.map((m) => m.payload.session_id).sort()).toEqual(['sa', 'sc']);
      expect(moves.every((m) => m.payload.workspace === '/ws/b')).toBe(true);

      emitEngineMessage({
        type: 'SESSION_MOVE_RESPONSE',
        request_id: moves.find((m) => m.payload.session_id === 'sa').request_id,
        payload: { session_id: 'sa', workspace: '/ws/b', moved: true },
      });
      emitEngineMessage({
        type: 'SESSION_MOVE_RESPONSE',
        request_id: moves.find((m) => m.payload.session_id === 'sc').request_id,
        payload: { session_id: 'sc', workspace: null, moved: false, error: 'Session vanished mid-move' },
      });

      expect(screen.getByTestId('session-move-status')).toHaveTextContent('1 moved, 1 failed');
      expect(screen.getByTestId('session-move-status')).toHaveTextContent('Session vanished mid-move');
      // Selection mode ends once the batch settles.
      expect(screen.queryByLabelText('Select session Alpha chat')).not.toBeInTheDocument();
    });
  });

  describe('search + palette labels (Q10)', () => {
    it('searches globally (no workspace filter) and labels each result with its workspace', async () => {
      await connect();
      seedWorkspaces('/ws/a');
      seedFilteredList([{ id: 'sa', title: 'Alpha chat', workspace: '/ws/a' }]);

      const search = screen.getByLabelText('Search chats');
      fireEvent.change(search, { target: { value: 'hello' } });
      await act(async () => {
        await new Promise((r) => setTimeout(r, 400));
      });
      const searches = packetsOfType('SESSION_SEARCH_REQUEST');
      expect(searches).toHaveLength(1);
      expect(searches[0].payload).toEqual({ query: 'hello' });

      emitEngineMessage({
        type: 'SESSION_SEARCH_RESPONSE',
        payload: {
          query: 'hello',
          results: [
            { id: 's9', title: 'Found In Beta', snippet: 'hit', workspace: '/ws/b' },
            { id: 's10', title: 'Found Loose', snippet: 'hit', workspace: null },
          ],
        },
      });

      expect(screen.getByText('Found In Beta')).toBeInTheDocument();
      expect(screen.getByText('Found Loose')).toBeInTheDocument();
      // The Beta result carries its workspace's display name; the
      // unbound result is labelled "No workspace" (the filter chip of
      // the same name accounts for the second occurrence).
      expect(screen.getByText('Beta')).toBeInTheDocument();
      expect(screen.getAllByText('No workspace').length).toBeGreaterThanOrEqual(2);
    });

    it('labels palette session entries with their workspace', async () => {
      await connect();
      seedWorkspaces('/ws/a');
      seedFilteredList([{ id: 'sa', title: 'Alpha chat', workspace: '/ws/a' }]);

      fireEvent.keyDown(window, { key: 'k', ctrlKey: true });
      // The palette caps its list at 12, with sessions last — a
      // query is what brings the session entry into view.
      fireEvent.change(screen.getByLabelText('Search commands'), { target: { value: 'Alpha chat' } });
      const item = screen.getByTestId('palette-item-sess-sa');
      expect(within(item).getByText('Alpha')).toBeInTheDocument();
    });
  });
});
