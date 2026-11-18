import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import Projects from '../Projects';

function makeWsRef() {
  let messageHandler = null;
  const wsRef = {
    current: {
      readyState: 1,
      send: jest.fn(),
      addEventListener: jest.fn((event, handler) => { messageHandler = handler; }),
      removeEventListener: jest.fn(),
    },
  };
  return { wsRef, getHandler: () => messageHandler };
}

function sentTypes(wsRef) {
  return wsRef.current.send.mock.calls.map(([raw]) => JSON.parse(raw).type);
}

describe('Projects', () => {
  it('requests the workspace list on mount', () => {
    const { wsRef } = makeWsRef();
    render(<Projects wsRef={wsRef} />);
    expect(sentTypes(wsRef)).toContain('WORKSPACE_LIST_REQUEST');
  });

  it('renders workspaces from the engine response, with indexed badge', () => {
    const { wsRef, getHandler } = makeWsRef();
    render(<Projects wsRef={wsRef} />);
    act(() => {
      getHandler()({
        data: JSON.stringify({
          type: 'WORKSPACE_LIST_RESPONSE',
          payload: {
            workspaces: [
              { path: '/home/user/proj', name: 'proj', is_indexed: true },
              { path: '/home/user/other', name: 'other', is_indexed: false },
            ],
          },
        }),
      });
    });
    expect(screen.getByText('/home/user/proj')).toBeInTheDocument();
    expect(screen.getByText('/home/user/other')).toBeInTheDocument();
    // Only the indexed workspace shows the badge.
    expect(screen.getByText('Indexed')).toBeInTheDocument();
  });

  it('sends WORKSPACE_ADD_REQUEST after the Electron folder picker', async () => {
    const { wsRef } = makeWsRef();
    window.electronAPI = { showOpenDialog: jest.fn().mockResolvedValue('/home/user/new') };
    render(<Projects wsRef={wsRef} />);

    await act(async () => {
      fireEvent.click(screen.getByText('Add Folder'));
    });

    const addCalls = wsRef.current.send.mock.calls
      .map(([raw]) => JSON.parse(raw))
      .filter((m) => m.type === 'WORKSPACE_ADD_REQUEST');
    expect(addCalls).toHaveLength(1);
    expect(addCalls[0].payload.path).toBe('/home/user/new');
    delete window.electronAPI;
  });

  it('sends WORKSPACE_REMOVE_REQUEST when removing a workspace (after the confirmation)', () => {
    const { wsRef, getHandler } = makeWsRef();
    render(<Projects wsRef={wsRef} />);
    act(() => {
      getHandler()({
        data: JSON.stringify({
          type: 'WORKSPACE_LIST_RESPONSE',
          payload: { workspaces: [{ path: '/home/user/proj', name: 'proj', is_indexed: false }] },
        }),
      });
    });
    // The remove button is the Trash2 icon button next to the workspace.
    // Per-workspace sessions (Q7): the click now arms a confirmation
    // first — nothing is sent until it is confirmed.
    fireEvent.click(screen.getByTitle('Remove workspace'));
    expect(sentTypes(wsRef)).not.toContain('WORKSPACE_REMOVE_REQUEST');
    fireEvent.click(screen.getByRole('button', { name: 'Remove' }));

    const removeCalls = wsRef.current.send.mock.calls
      .map(([raw]) => JSON.parse(raw))
      .filter((m) => m.type === 'WORKSPACE_REMOVE_REQUEST');
    expect(removeCalls).toHaveLength(1);
    expect(removeCalls[0].payload.path).toBe('/home/user/proj');
  });
});

describe('Projects (Epic 6, E6-04)', () => {
  it('shows Active + missing states and can set a workspace active', () => {
    const { wsRef, getHandler } = makeWsRef();
    render(<Projects wsRef={wsRef} />);
    act(() => {
      getHandler()({
        data: JSON.stringify({
          type: 'WORKSPACE_LIST_RESPONSE',
          payload: {
            workspaces: [
              { path: '/srv/main', name: 'main', is_indexed: false, is_active: true, exists: true },
              { path: '/srv/gone', name: 'gone', is_indexed: false, is_active: false, exists: false },
              { path: '/srv/side', name: 'side', is_indexed: false, is_active: false, exists: true },
            ],
          },
        }),
      });
    });
    expect(screen.getByText('Active')).toBeInTheDocument();
    expect(screen.getByText('Folder missing')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Set active'));
    const sent = wsRef.current.send.mock.calls.map(([raw]) => JSON.parse(raw));
    const setActive = sent.find((m) => m.type === 'WORKSPACE_SET_ACTIVE_REQUEST');
    expect(setActive.payload.path).toBe('/srv/side');
  });
});

describe('Projects workspace badges (E6-F11)', () => {
  it('removing the active workspace marks its successor active without a refetch', () => {
    const { wsRef, getHandler } = makeWsRef();
    render(<Projects wsRef={wsRef} />);
    act(() => {
      getHandler()({
        data: JSON.stringify({
          type: 'WORKSPACE_LIST_RESPONSE',
          payload: {
            workspaces: [
              { path: '/home/user/a', name: 'a', is_indexed: false, is_active: true, exists: true },
              { path: '/home/user/b', name: 'b', is_indexed: false, is_active: false, exists: true },
            ],
          },
        }),
      });
    });
    expect(screen.getByText('Active')).toBeInTheDocument();

    wsRef.current.send.mockClear();
    act(() => {
      getHandler()({
        data: JSON.stringify({
          type: 'WORKSPACE_REMOVE_RESPONSE',
          payload: { success: true, path: '/home/user/a' },
        }),
      });
    });
    // The successor (now the first root) shows the Active badge…
    expect(screen.getByText('Active')).toBeInTheDocument();
    expect(screen.queryByText('/home/user/a')).not.toBeInTheDocument();
    // …derived from the reply, not from a LIST refetch race.
    expect(sentTypes(wsRef)).not.toContain('WORKSPACE_LIST_REQUEST');
  });

  it('an ADD reply carrying is_active/exists renders the badges immediately', () => {
    const { wsRef, getHandler } = makeWsRef();
    render(<Projects wsRef={wsRef} />);
    act(() => {
      getHandler()({
        data: JSON.stringify({
          type: 'WORKSPACE_ADD_RESPONSE',
          payload: {
            success: true,
            workspace: { path: '/home/user/new', name: 'new', is_indexed: false, is_active: true, exists: false },
          },
        }),
      });
    });
    expect(screen.getByText('Active')).toBeInTheDocument();
    expect(screen.getByText('Folder missing')).toBeInTheDocument();
  });
});

describe('Projects per-workspace session counts (renderer slice, Q7)', () => {
  const seedList = (getHandler, payload) => {
    act(() => {
      getHandler()({
        data: JSON.stringify({ type: 'WORKSPACE_LIST_RESPONSE', payload }),
      });
    });
  };

  it('shows each workspace session_count and the unbound count on a No workspace line', () => {
    const { wsRef, getHandler } = makeWsRef();
    render(<Projects wsRef={wsRef} />);
    seedList(getHandler, {
      workspaces: [
        { path: '/home/user/proj', name: 'proj', is_indexed: false, is_active: true, exists: true, session_count: 2 },
        { path: '/home/user/solo', name: 'solo', is_indexed: false, is_active: false, exists: true, session_count: 1 },
      ],
      unbound_session_count: 3,
    });

    expect(screen.getByText('2 sessions')).toBeInTheDocument();
    expect(screen.getByText('1 session')).toBeInTheDocument();
    expect(screen.getByText('No workspace')).toBeInTheDocument();
    expect(screen.getByText('3 unbound sessions')).toBeInTheDocument();
  });

  it('the remove confirmation names the unbinding with the count, before anything is sent', () => {
    const { wsRef, getHandler } = makeWsRef();
    render(<Projects wsRef={wsRef} />);
    seedList(getHandler, {
      workspaces: [
        { path: '/home/user/proj', name: 'proj', is_indexed: false, is_active: true, exists: true, session_count: 2 },
      ],
      unbound_session_count: 0,
    });

    wsRef.current.send.mockClear();
    fireEvent.click(screen.getByTitle('Remove workspace'));

    expect(
      screen.getByText('2 sessions will move to No workspace — nothing is deleted.')
    ).toBeInTheDocument();
    expect(sentTypes(wsRef)).not.toContain('WORKSPACE_REMOVE_REQUEST');

    // Cancel backs out with nothing sent and the row intact.
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(sentTypes(wsRef)).not.toContain('WORKSPACE_REMOVE_REQUEST');
    expect(screen.getByText('/home/user/proj')).toBeInTheDocument();
    expect(
      screen.queryByText('2 sessions will move to No workspace — nothing is deleted.')
    ).not.toBeInTheDocument();
  });

  it('a successful remove reply folds the unbound count into the No workspace line', () => {
    const { wsRef, getHandler } = makeWsRef();
    render(<Projects wsRef={wsRef} />);
    seedList(getHandler, {
      workspaces: [
        { path: '/home/user/proj', name: 'proj', is_indexed: false, is_active: true, exists: true, session_count: 2 },
      ],
      unbound_session_count: 1,
    });
    expect(screen.getByText('1 unbound session')).toBeInTheDocument();

    fireEvent.click(screen.getByTitle('Remove workspace'));
    fireEvent.click(screen.getByRole('button', { name: 'Remove' }));
    act(() => {
      getHandler()({
        data: JSON.stringify({
          type: 'WORKSPACE_REMOVE_RESPONSE',
          payload: { success: true, path: '/home/user/proj', unbound_sessions: 2 },
        }),
      });
    });

    expect(screen.queryByText('/home/user/proj')).not.toBeInTheDocument();
    expect(screen.getByText('3 unbound sessions')).toBeInTheDocument();
  });

  it('the remove confirmation does not invent a count the engine never reported (D2-22)', () => {
    const { wsRef, getHandler } = makeWsRef();
    render(<Projects wsRef={wsRef} />);
    // No session_count on this row: the engine has not reported one.
    seedList(getHandler, {
      workspaces: [
        { path: '/home/user/proj', name: 'proj', is_indexed: false, is_active: true, exists: true },
      ],
      unbound_session_count: 0,
    });

    fireEvent.click(screen.getByTitle('Remove workspace'));

    // The confirmation must not assert "0 sessions" as fact; it
    // still says what will happen, without the invented number.
    expect(screen.queryByText(/0 sessions will move/)).not.toBeInTheDocument();
    expect(
      screen.getByText(/will move to No workspace — nothing is deleted\./)
    ).toBeInTheDocument();
    expect(sentTypes(wsRef)).not.toContain('WORKSPACE_REMOVE_REQUEST');
  });
});

describe('Projects load states (RUX-C-08)', () => {
  it('shows a loading state, not "No workspaces added", before the list reply', () => {
    const { wsRef } = makeWsRef();
    render(<Projects wsRef={wsRef} />);
    const row = screen.getByTestId('projects-list-load-state');
    expect(row.dataset.loadDisplay).toBe('loading');
    expect(screen.queryByText(/No workspaces added/)).not.toBeInTheDocument();
  });

  it('shows the waiting-for-engine state while disconnected', () => {
    const { wsRef } = makeWsRef();
    render(<Projects wsRef={wsRef} isConnected={false} />);
    const row = screen.getByTestId('projects-list-load-state');
    expect(row.dataset.loadDisplay).toBe('offline');
    expect(row.textContent).toMatch(/Waiting for the engine/);
  });

  it('an answered-empty list renders the genuine empty state', () => {
    const { wsRef, getHandler } = makeWsRef();
    render(<Projects wsRef={wsRef} />);
    act(() => {
      getHandler()({
        data: JSON.stringify({
          type: 'WORKSPACE_LIST_RESPONSE',
          payload: { workspaces: [] },
        }),
      });
    });
    expect(screen.queryByTestId('projects-list-load-state')).not.toBeInTheDocument();
    expect(screen.getByText(/No workspaces added/)).toBeInTheDocument();
  });

  it('a refused remove is surfaced instead of silently ignored', () => {
    const { wsRef, getHandler } = makeWsRef();
    render(<Projects wsRef={wsRef} />);
    act(() => {
      getHandler()({
        data: JSON.stringify({
          type: 'WORKSPACE_LIST_RESPONSE',
          payload: { workspaces: [{ path: '/home/user/proj', name: 'proj', is_indexed: false }] },
        }),
      });
    });
    fireEvent.click(screen.getByTitle('Remove workspace'));
    fireEvent.click(screen.getByRole('button', { name: 'Remove' }));
    act(() => {
      getHandler()({
        data: JSON.stringify({
          type: 'WORKSPACE_REMOVE_RESPONSE',
          payload: { success: false, error: 'The folder is in use by a running task.' },
        }),
      });
    });
    expect(screen.getByTestId('projects-action-error').textContent).toMatch(/in use by a running task/);
    // The workspace is still listed — the refusal changed nothing.
    expect(screen.getByText('/home/user/proj')).toBeInTheDocument();
  });

  it('a refused add without an engine reason still names the failure', () => {
    const { wsRef, getHandler } = makeWsRef();
    render(<Projects wsRef={wsRef} />);
    act(() => {
      getHandler()({
        data: JSON.stringify({
          type: 'WORKSPACE_ADD_RESPONSE',
          payload: { success: false },
        }),
      });
    });
    expect(screen.getByTestId('projects-action-error').textContent).toMatch(/could not be added/);
  });

  it('Add Folder explains itself when the Electron picker API is absent (D2-23)', () => {
    const { wsRef } = makeWsRef();
    delete window.electronAPI;
    render(<Projects wsRef={wsRef} />);
    fireEvent.click(screen.getByText('Add Folder'));
    // Not a dead click: the panel names why nothing happened, and
    // no add request is sent.
    expect(screen.getByTestId('projects-action-error').textContent).toMatch(
      /folder picker is not available/i
    );
    expect(sentTypes(wsRef)).not.toContain('WORKSPACE_ADD_REQUEST');
  });
});

describe('Projects doorway (Full Furnish S-7)', () => {
  const seedList = (getHandler, workspaces, extra = {}) => {
    act(() => {
      getHandler()({
        data: JSON.stringify({
          type: 'WORKSPACE_LIST_RESPONSE',
          payload: { workspaces, ...extra },
        }),
      });
    });
  };
  const sentMessages = (wsRef) => wsRef.current.send.mock.calls.map(([raw]) => JSON.parse(raw));
  const twoRows = [
    { path: '/home/user/main', name: 'main', is_indexed: false, is_active: true, exists: true, session_count: 2 },
    { path: '/home/user/side', name: 'side', is_indexed: false, is_active: false, exists: true, session_count: 3 },
  ];

  it('the session count is a link: opening a non-active workspace sets it active, then jumps', () => {
    const { wsRef, getHandler } = makeWsRef();
    const onGoToChat = jest.fn();
    render(<Projects wsRef={wsRef} onGoToChat={onGoToChat} />);
    seedList(getHandler, twoRows);
    wsRef.current.send.mockClear();

    fireEvent.click(screen.getByText('3 sessions'));
    const setActive = sentMessages(wsRef).find((m) => m.type === 'WORKSPACE_SET_ACTIVE_REQUEST');
    expect(setActive.payload.path).toBe('/home/user/side');
    expect(onGoToChat).toHaveBeenCalledTimes(1);
  });

  it('opening the already-active workspace jumps without an engine round-trip', () => {
    const { wsRef, getHandler } = makeWsRef();
    const onGoToChat = jest.fn();
    render(<Projects wsRef={wsRef} onGoToChat={onGoToChat} />);
    seedList(getHandler, twoRows);
    wsRef.current.send.mockClear();

    fireEvent.click(screen.getByText('2 sessions'));
    expect(sentTypes(wsRef)).not.toContain('WORKSPACE_SET_ACTIVE_REQUEST');
    expect(onGoToChat).toHaveBeenCalledTimes(1);
  });

  it('New chat creates a session bound to that workspace and jumps when its own reply lands', () => {
    const { wsRef, getHandler } = makeWsRef();
    const onGoToChat = jest.fn();
    render(<Projects wsRef={wsRef} onGoToChat={onGoToChat} />);
    seedList(getHandler, twoRows);
    wsRef.current.send.mockClear();

    // Two "New chat" buttons (one per row); the second row is /side.
    fireEvent.click(screen.getAllByText('New chat')[1]);
    const creates = sentMessages(wsRef).filter((m) => m.type === 'SESSION_CREATE_REQUEST');
    expect(creates).toHaveLength(1);
    expect(creates[0].payload).toEqual({ title: 'New Conversation', workspace: '/home/user/side' });

    // A create reply for some other request must not navigate.
    act(() => {
      getHandler()({
        data: JSON.stringify({
          type: 'SESSION_CREATE_RESPONSE',
          request_id: 'someone_elses_create',
          payload: { session_id: 'sx', workspace: '/home/user/side' },
        }),
      });
    });
    expect(onGoToChat).not.toHaveBeenCalled();

    act(() => {
      getHandler()({
        data: JSON.stringify({
          type: 'SESSION_CREATE_RESPONSE',
          request_id: creates[0].request_id,
          payload: { session_id: 's9', workspace: '/home/user/side' },
        }),
      });
    });
    expect(onGoToChat).toHaveBeenCalledTimes(1);
  });

  it('a refused workspace-bound create surfaces the error and does not jump', () => {
    const { wsRef, getHandler } = makeWsRef();
    const onGoToChat = jest.fn();
    render(<Projects wsRef={wsRef} onGoToChat={onGoToChat} />);
    seedList(getHandler, twoRows);
    fireEvent.click(screen.getAllByText('New chat')[0]);
    const create = sentMessages(wsRef).find((m) => m.type === 'SESSION_CREATE_REQUEST');
    act(() => {
      getHandler()({
        data: JSON.stringify({
          type: 'SESSION_CREATE_RESPONSE',
          request_id: create.request_id,
          payload: { session_id: null, workspace: null, success: false, error: 'That folder is not a configured workspace.' },
        }),
      });
    });
    expect(screen.getByTestId('projects-action-error').textContent).toMatch(/not a configured workspace/);
    expect(onGoToChat).not.toHaveBeenCalled();
  });

  it('set-active offers the jump inline once it lands', () => {
    const { wsRef, getHandler } = makeWsRef();
    const onGoToChat = jest.fn();
    render(<Projects wsRef={wsRef} onGoToChat={onGoToChat} />);
    seedList(getHandler, twoRows);
    expect(screen.queryByText('Open its chats')).not.toBeInTheDocument();

    fireEvent.click(screen.getByText('Set active'));
    act(() => {
      getHandler()({
        data: JSON.stringify({
          type: 'WORKSPACE_SET_ACTIVE_RESPONSE',
          payload: { success: true },
        }),
      });
    });
    // The refetch the reply triggers re-files the rows as active.
    act(() => {
      getHandler()({
        data: JSON.stringify({
          type: 'WORKSPACE_LIST_RESPONSE',
          payload: {
            workspaces: [
              { ...twoRows[0], is_active: false },
              { ...twoRows[1], is_active: true },
            ],
          },
        }),
      });
    });
    fireEvent.click(screen.getByText('Open its chats'));
    expect(onGoToChat).toHaveBeenCalledTimes(1);
    // The offer is spent once used.
    expect(screen.queryByText('Open its chats')).not.toBeInTheDocument();
  });

  it('a set-active sent as part of Open chats does not leave a stale jump offer behind', () => {
    const { wsRef, getHandler } = makeWsRef();
    const onGoToChat = jest.fn();
    render(<Projects wsRef={wsRef} onGoToChat={onGoToChat} />);
    seedList(getHandler, twoRows);
    fireEvent.click(screen.getByText('3 sessions'));
    expect(onGoToChat).toHaveBeenCalledTimes(1);
    act(() => {
      getHandler()({
        data: JSON.stringify({
          type: 'WORKSPACE_SET_ACTIVE_RESPONSE',
          payload: { success: true },
        }),
      });
    });
    expect(screen.queryByText('Open its chats')).not.toBeInTheDocument();
  });

  it('the answered-empty list renders the shared EmptyState with its Add Folder action', () => {
    const { wsRef, getHandler } = makeWsRef();
    render(<Projects wsRef={wsRef} />);
    seedList(getHandler, []);
    expect(screen.getByTestId('projects-empty-state')).toBeInTheDocument();
    expect(screen.getByText('No workspaces added')).toBeInTheDocument();
    // Two Add Folder affordances now: the header button and the
    // empty state's single CTA — both wired to the same picker.
    expect(screen.getAllByText('Add Folder')).toHaveLength(2);
  });
});
