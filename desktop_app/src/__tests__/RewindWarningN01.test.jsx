import React from 'react';
import { render, screen, fireEvent, within, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import App from '../App';

// Stub 3D force graph for jsdom (same as ForkSession.test.jsx).
jest.mock('react-force-graph-3d', () => ({
  __esModule: true,
  default: () => <div data-testid="force-graph-stub" />,
}));

// react-markdown is ESM-only; stub it for Jest (same as the sibling
// DraggableChatWindow*.test.jsx files).
jest.mock('react-markdown', () => ({
  __esModule: true,
  default: ({ children }) => <div data-testid="markdown-stub">{children}</div>,
}));

// jsdom has no scrollIntoView; the chat window calls it on new messages.
Element.prototype.scrollIntoView = jest.fn();

// jsdom has no WebSocket; the socket layer only needs the constants.
global.WebSocket = { OPEN: 1, CLOSED: 3, CONNECTING: 0, CLOSING: 2 };

// Epic 8, N01: the RewindConfirmModal's file list is computed by App
// from the manifest entries. A file written before the chosen point
// and AGAIN after it must appear — its current content postdates the
// point and will NOT be reverted. The old computation subtracted the
// point's cumulative files_written list from the final one, which
// cancels exactly those repeat writes out of the warning.
describe('App rewind warning with repeated writes (N01)', () => {
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

  const sentPackets = () =>
    electronAPI.sendEngineMessage.mock.calls.map(([raw]) => JSON.parse(raw));

  // Land in session sess-1 with a two-message history, then surface
  // a manifest whose trail writes notes/out.txt at step 1, writes it
  // AGAIN at step 2 (a repeat: the cumulative list never changes),
  // and finishes at step 3.
  const connectAppWithRepeatTrail = async () => {
    render(<App />);
    await flushPromises();
    emitEngineMessage({ type: '_ws_status', status: 'connected' });
    await flushPromises();
    emitEngineMessage({
      type: 'SESSION_LIST_RESPONSE',
      payload: { sessions: [{ id: 'sess-1', title: 'Chat' }] },
    });
    await flushPromises();
    const getReq = sentPackets().find((p) => p.type === 'SESSION_GET_REQUEST');
    expect(getReq).toBeDefined();
    emitEngineMessage({
      type: 'SESSION_GET_RESPONSE',
      request_id: getReq.request_id,
      payload: {
        session: {
          id: 'sess-1',
          title: 'Chat',
          messages: [
            { id: 'u1', role: 'user', content: 'write the notes', thoughts: [] },
            { id: 'a1', role: 'assistant', content: 'Done.', thoughts: [] },
          ],
        },
      },
    });
    await flushPromises();
    emitEngineMessage({
      type: 'REWIND_MANIFEST_GET_RESPONSE',
      payload: {
        session_id: 'sess-1',
        task_id: 'task-1',
        entries: [
          { step_index: 0, checkpoint_id: 'cp0', label: 'Before this task began', files_written: [], write_events: [], timestamp: 100, cut_message_id: null },
          { step_index: 1, checkpoint_id: 'cp1', label: 'After planner', files_written: ['notes/out.txt'], write_events: ['notes/out.txt'], timestamp: 101, cut_message_id: 'u1' },
          { step_index: 2, checkpoint_id: 'cp2', label: 'After coder', files_written: ['notes/out.txt'], write_events: ['notes/out.txt'], timestamp: 102, cut_message_id: 'a1' },
          { step_index: 3, checkpoint_id: 'cp3', label: 'Task complete', files_written: ['notes/out.txt'], write_events: [], timestamp: 103, cut_message_id: 'a1' },
        ],
      },
    });
    await flushPromises();
  };

  const openPickerAndRewindTo = (itemIndex) => {
    fireEvent.click(screen.getByTestId('step-picker-btn'));
    const items = screen.getAllByTestId('step-picker-item');
    fireEvent.click(within(items[itemIndex]).getByTestId('step-picker-rewind'));
  };

  it('lists a file written again after the chosen point as will NOT be reverted', async () => {
    await connectAppWithRepeatTrail();
    // Rewind to step 1 — between the two writes of notes/out.txt.
    openPickerAndRewindTo(1);

    const modal = screen.getByTestId('rewind-confirm-modal');
    const list = within(modal).getByTestId('rewind-files-not-reverted');
    expect(list).toHaveTextContent('notes/out.txt');
    expect(modal).toHaveTextContent('will NOT be reverted');
  });

  it('does not warn about the repeat write when rewinding to after it', async () => {
    await connectAppWithRepeatTrail();
    // Rewind to step 2 — the repeat write is AT this point, and
    // nothing was written after it.
    openPickerAndRewindTo(2);

    const modal = screen.getByTestId('rewind-confirm-modal');
    expect(within(modal).queryByTestId('rewind-files-not-reverted')).not.toBeInTheDocument();
    expect(modal).toHaveTextContent('No files were written after this point.');
  });
});
