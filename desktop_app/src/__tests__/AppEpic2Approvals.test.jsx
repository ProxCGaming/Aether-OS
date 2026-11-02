import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
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

const DIFF = [
  '--- a/src/app.py',
  '+++ b/src/app.py',
  '@@ -1,3 +1,3 @@',
  ' context line',
  '-old line',
  '+new line',
].join('\n');

// Epic 2 UI batch at the App level: TOOL_APPROVAL_REQUEST previews are
// threaded into the pending approval (diff renders in the card), and a
// scoped approve rides TOOL_APPROVAL_GRANTED as payload.scope, while a
// plain approve sends no scope at all.
describe('App Epic 2 approvals (preview + scope)', () => {
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

  const emitEditApproval = (overrides = {}) => {
    emitEngineMessage({
      type: 'TOOL_APPROVAL_REQUEST',
      request_id: 'req-edit-1',
      payload: {
        tool_name: 'edit_file',
        tool_args: { path: '/home/user/proj/app.py' },
        approval_key: 'key-edit-1',
        preview: { diff: DIFF },
        ...overrides,
      },
    });
  };

  it('threads the diff preview into the approval card', async () => {
    await connect();
    emitEditApproval();
    expect(screen.getByTestId('approval-diff-preview')).toBeInTheDocument();
    expect(screen.getByText('+new line')).toBeInTheDocument();
    expect(screen.getByText('-old line')).toBeInTheDocument();
  });

  it('"Approve + remember folder" sends TOOL_APPROVAL_GRANTED with scope "folder"', async () => {
    await connect();
    emitEditApproval();
    fireEvent.click(screen.getByRole('button', { name: 'Approve + remember folder' }));
    const granted = packetsOfType('TOOL_APPROVAL_GRANTED');
    expect(granted).toHaveLength(1);
    expect(granted[0].payload.approval_key).toBe('key-edit-1');
    expect(granted[0].payload.approved).toBe(true);
    expect(granted[0].payload.scope).toBe('folder');
  });

  it('"Approve + remember file" sends scope "file"', async () => {
    await connect();
    emitEditApproval();
    fireEvent.click(screen.getByRole('button', { name: 'Approve + remember file' }));
    const granted = packetsOfType('TOOL_APPROVAL_GRANTED');
    expect(granted).toHaveLength(1);
    expect(granted[0].payload.scope).toBe('file');
  });

  it('plain approve sends no scope; preview.error shows the note', async () => {
    await connect();
    emitEditApproval({ preview: { error: 'file was deleted before preview' } });
    expect(screen.getByTestId('approval-preview-error')).toBeInTheDocument();
    expect(screen.getByText(/file was deleted before preview/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Approve Execution' }));
    const granted = packetsOfType('TOOL_APPROVAL_GRANTED');
    expect(granted).toHaveLength(1);
    expect(granted[0].payload).not.toHaveProperty('scope');
  });

  it('an approval without a path shows no scope buttons; reject sends REJECTED without scope', async () => {
    await connect();
    emitEngineMessage({
      type: 'TOOL_APPROVAL_REQUEST',
      request_id: 'req-shell-1',
      payload: {
        tool_name: 'execute_shell',
        tool_args: { command: 'ls -la' },
        approval_key: 'key-shell-1',
        preview: null,
      },
    });
    expect(screen.queryByRole('button', { name: 'Approve + remember file' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Approve + remember folder' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Reject Request' }));
    const rejected = packetsOfType('TOOL_APPROVAL_REJECTED');
    expect(rejected).toHaveLength(1);
    expect(rejected[0].payload.approval_key).toBe('key-shell-1');
    expect(rejected[0].payload).not.toHaveProperty('scope');
  });
});
