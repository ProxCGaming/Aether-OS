import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import App from '../App';

// Epic 6 minors, App-shell seams:
//  - E6-F08: the command palette must search/open individual Settings
//    sections, not just a generic "Go to Settings".
//  - E6-F09: the shortcuts dialog advertises "Esc — Close palette /
//    dialogs" but Escape never closed it.
//  - E6-F10: the Dashboard Active Tasks count ignored TASK_CREATED —
//    the only start event the engine actually emits (TASK_STARTED is
//    never sent), so real tasks never counted.
//  - E6-F13: no shortcuts existed for cancel-task or approval
//    approve/reject, though the actions themselves exist.
//  - E6-F14: an "About Aether" palette entry opens the About section
//    (the panel itself is covered in SettingsPanelEpic6Minors).
//  - E6-F16: the session search input suppressed the global focus ring.

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

describe('App Epic 6 minors', () => {
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
      openPath: jest.fn(() => Promise.resolve('')),
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

  const openPalette = () => fireEvent.keyDown(window, { key: 'k', ctrlKey: true });

  const activeTasksValue = () => {
    const label = screen.getByText('Active Tasks');
    const card = label.closest('div').parentElement;
    return card.children[1].textContent;
  };

  it('E6-F10: TASK_CREATED counts toward Active Tasks until the terminal event', async () => {
    await connect();
    fireEvent.click(screen.getByText('Dashboard'));
    expect(activeTasksValue()).toBe('0');

    emitEngineMessage({
      type: 'TASK_CREATED',
      request_id: 'req-task-1',
      payload: { task_id: 'task-1', prompt: 'do a thing', state: 'running' },
    });
    expect(activeTasksValue()).toBe('1');

    // A second concurrent task counts too; progress does not double-count.
    emitEngineMessage({
      type: 'TASK_CREATED',
      request_id: 'req-task-2',
      payload: { task_id: 'task-2', prompt: 'another', state: 'running' },
    });
    expect(activeTasksValue()).toBe('2');

    emitEngineMessage({
      type: 'TASK_COMPLETED',
      request_id: 'req-task-1',
      payload: { task_id: 'task-1', response: 'done' },
    });
    expect(activeTasksValue()).toBe('1');
  });

  it('E6-F09: Escape closes the shortcuts dialog it advertises closing', async () => {
    await connect();
    openPalette();
    fireEvent.change(screen.getByLabelText('Search commands'), { target: { value: 'Keyboard shortcuts' } });
    fireEvent.click(screen.getByTestId('palette-item-act-shortcuts'));
    expect(screen.getByTestId('shortcuts-dialog')).toBeInTheDocument();

    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.queryByTestId('shortcuts-dialog')).not.toBeInTheDocument();
  });

  it('E6-F08: the palette finds a Settings section by name and opens it', async () => {
    await connect();
    openPalette();
    fireEvent.change(screen.getByLabelText('Search commands'), { target: { value: 'Memory & Storage' } });
    const item = screen.getByTestId('palette-item-settings-Memory');
    expect(item).toBeInTheDocument();
    fireEvent.click(item);
    // App lands on Settings with the Memory section active (its panel
    // requests the episode list on mount).
    expect(screen.getAllByText('Memory & Storage').length).toBeGreaterThan(0);
    expect(packetsOfType('MEMORY_EPISODES_REQUEST').length).toBeGreaterThan(0);
  });

  it('E6-F14: the palette offers About Aether and opens the About section', async () => {
    await connect();
    openPalette();
    fireEvent.change(screen.getByLabelText('Search commands'), { target: { value: 'About' } });
    const item = screen.getByTestId('palette-item-settings-About');
    expect(item).toBeInTheDocument();
    fireEvent.click(item);
    expect(screen.getByText('About Aether-OS')).toBeInTheDocument();
  });

  it('E6-F13: Ctrl+. cancels the running task; the dialog documents it', async () => {
    await connect();
    fireEvent.keyDown(window, { key: '.', ctrlKey: true });
    expect(packetsOfType('CANCEL_TASK')).toHaveLength(1);

    openPalette();
    fireEvent.change(screen.getByLabelText('Search commands'), { target: { value: 'Keyboard shortcuts' } });
    fireEvent.click(screen.getByTestId('palette-item-act-shortcuts'));
    const dialog = screen.getByTestId('shortcuts-dialog');
    expect(dialog.textContent).toContain('Cancel current task');
    expect(dialog.textContent).toContain('Approve');
    expect(dialog.textContent).toContain('Reject');
  });

  it('E6-F13: Ctrl+Enter approves and Ctrl+Shift+Enter rejects the pending approval', async () => {
    await connect();
    emitEngineMessage({
      type: 'TOOL_APPROVAL_REQUEST',
      request_id: 'req-appr-1',
      payload: {
        tool_name: 'write_file',
        tool_args: { path: '/tmp/x.txt' },
        approval_key: 'key-appr-1',
      },
    });
    fireEvent.keyDown(window, { key: 'Enter', ctrlKey: true });
    const granted = packetsOfType('TOOL_APPROVAL_GRANTED');
    expect(granted).toHaveLength(1);
    expect(granted[0].payload.approval_key).toBe('key-appr-1');
    expect(granted[0].payload.approved).toBe(true);
    expect(granted[0].payload.scope).toBeUndefined();

    emitEngineMessage({
      type: 'TOOL_APPROVAL_REQUEST',
      request_id: 'req-appr-2',
      payload: {
        tool_name: 'write_file',
        tool_args: { path: '/tmp/y.txt' },
        approval_key: 'key-appr-2',
      },
    });
    fireEvent.keyDown(window, { key: 'Enter', ctrlKey: true, shiftKey: true });
    const rejected = packetsOfType('TOOL_APPROVAL_REJECTED');
    expect(rejected).toHaveLength(1);
    expect(rejected[0].payload.approval_key).toBe('key-appr-2');
  });

  it('E6-F13: the approval shortcuts never fire while typing in a text field', async () => {
    await connect();
    emitEngineMessage({
      type: 'TOOL_APPROVAL_REQUEST',
      request_id: 'req-appr-3',
      payload: {
        tool_name: 'write_file',
        tool_args: { path: '/tmp/z.txt' },
        approval_key: 'key-appr-3',
      },
    });
    const search = screen.getByLabelText('Search chats');
    search.focus();
    fireEvent.keyDown(search, { key: 'Enter', ctrlKey: true });
    expect(packetsOfType('TOOL_APPROVAL_GRANTED')).toHaveLength(0);
  });

  it('E6-F16: the session search input carries the shared focus-ring class', async () => {
    await connect();
    const search = screen.getByLabelText('Search chats');
    expect(search.classList.contains('aether-search-input')).toBe(true);
    expect(search.style.outline).toBe('');
  });
});
