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

// Master build S2: two engine pushes that previously had NO handler
// in the renderer (zero hits in production src), so they landed
// nowhere — a task paused for approval left no trace in the
// transcript beyond its card, and a refused session truncate (the
// edit-and-resend / regenerate path) silently diverged from the
// locally pruned list. These tests pin the new branches in
// useEngineMessages.js.
describe('App S2 unheard pushes (TASK_INTERRUPTED, SESSION_TRUNCATE_RESPONSE)', () => {
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
    emitEngineMessage({ type: 'SESSION_CREATE_RESPONSE', payload: { session_id: 's1' } });
    await flushPromises();
  };

  const sentPackets = () =>
    electronAPI.sendEngineMessage.mock.calls.map(([raw]) => JSON.parse(raw));

  it('TASK_INTERRUPTED marks the pause in the transcript and keeps the approval card armed', async () => {
    await connect();
    emitEngineMessage({
      type: 'TOOL_APPROVAL_REQUEST',
      request_id: 'req-edit-1',
      payload: {
        tool_name: 'edit_file',
        tool_args: { path: '/home/user/proj/app.py' },
        approval_key: 'key-edit-1',
        session_id: 's1',
        preview: { diff: DIFF },
      },
    });
    expect(screen.getByTestId('approval-diff-preview')).toBeInTheDocument();

    emitEngineMessage({
      type: 'TASK_INTERRUPTED',
      request_id: 'key-edit-1',
      payload: {
        task_id: 't1',
        state: 'PENDING_APPROVAL',
        approval_key: 'key-edit-1',
        session_id: 's1',
      },
    });
    await flushPromises();

    // The pause is visible in the transcript...
    expect(screen.getByText(/Task paused — waiting for your approval/)).toBeInTheDocument();
    // ...and the approval card is NOT torn down: an interrupted task
    // is paused, not finished, so its approval must stay decidable.
    expect(screen.getByTestId('approval-diff-preview')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Approve Execution' }));
    const granted = sentPackets().filter((p) => p.type === 'TOOL_APPROVAL_GRANTED');
    expect(granted).toHaveLength(1);
    expect(granted[0].payload.approval_key).toBe('key-edit-1');
  });

  it('SESSION_TRUNCATE_RESPONSE with truncated:false surfaces the refused truncate', async () => {
    await connect();
    emitEngineMessage({
      type: 'SESSION_TRUNCATE_RESPONSE',
      payload: { session_id: 's1', truncated: false },
    });
    await flushPromises();
    expect(screen.getByText(/could not truncate this conversation/)).toBeInTheDocument();
  });

  it('SESSION_TRUNCATE_RESPONSE with truncated:true stays silent (the local prune already applied)', async () => {
    await connect();
    emitEngineMessage({
      type: 'SESSION_TRUNCATE_RESPONSE',
      payload: { session_id: 's1', truncated: true },
    });
    await flushPromises();
    expect(screen.queryByText(/could not truncate this conversation/)).not.toBeInTheDocument();
  });
});
