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
