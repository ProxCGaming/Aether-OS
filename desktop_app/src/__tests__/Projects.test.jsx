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

  it('sends WORKSPACE_REMOVE_REQUEST when removing a workspace', () => {
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
    fireEvent.click(screen.getByTitle('Remove workspace'));

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
