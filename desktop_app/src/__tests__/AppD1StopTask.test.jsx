import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import App from '../App';

// Wave 3, D1-A7 (Pass B: DOWNGRADED S3 -> S4). The facade's send()
// returns a disposition: 'sent' when the message actually left, and
// 'queued' when the connection is down and the message went into the
// offline queue (where a CANCEL_TASK, being destructive, is held for
// reconfirmation on reconnect — it never flushes verbatim). The old
// handleStopTask ignored that return value and unconditionally ran
// setIsThinking(false): the UI claimed "stopped" for a task that was
// still running. These tests drive App at the facade boundary (the
// AppSteeringOffline mocking pattern) so the disposition is under
// test control while App's own state stays live.

jest.mock('react-force-graph-3d', () => ({
  __esModule: true,
  default: () => <div data-testid="force-graph-stub" />,
}));

jest.mock('react-markdown', () => ({
  __esModule: true,
  default: ({ children }) => <div data-testid="markdown-stub">{children}</div>,
}));

jest.mock('../contexts/SocketContext', () => {
  const listeners = new Set();
  const fake = {
    sent: [],
    get readyState() {
      return 1; // WebSocket.OPEN
    },
    send(data) {
      this.sent.push(data);
      return 'sent';
    },
    addEventListener(event, handler) {
      if (event === 'message') listeners.add(handler);
    },
    removeEventListener(event, handler) {
      listeners.delete(handler);
    },
    emit(obj) {
      const data = JSON.stringify(obj);
      [...listeners].forEach((handler) => handler({ data }));
    },
    reset() {
      this.sent = [];
      listeners.clear();
    },
  };
  const wsRef = { current: fake };
  const sendMessage = jest.fn();
  const safeSend = jest.fn(() => 'sent');
  return {
    SocketProvider: ({ children }) => <>{children}</>,
    useAetherSocket: () => ({
      wsRef,
      isConnected: true,
      sendMessage,
      safeSend,
      pendingMessages: [],
      reconnectCount: 0,
    }),
    __fakeSocket: fake,
    __safeSend: safeSend,
  };
});

const { __fakeSocket, __safeSend } = jest.requireMock('../contexts/SocketContext');

Element.prototype.scrollIntoView = jest.fn();
global.WebSocket = { OPEN: 1, CLOSED: 3, CONNECTING: 0, CLOSING: 2 };

describe('D1-A7 handleStopTask honours the send disposition', () => {
  beforeEach(() => {
    __fakeSocket.reset();
    __safeSend.mockReset();
    __safeSend.mockReturnValue('sent');
    window.electronAPI = {
      getMemoryUsage: jest.fn(() => Promise.resolve(128)),
      onChatDetached: jest.fn(() => jest.fn()),
      onChatAttached: jest.fn(() => jest.fn()),
    };
  });

  afterEach(() => {
    delete window.electronAPI;
  });

  const emit = (obj) => {
    act(() => {
      __fakeSocket.emit(obj);
    });
  };

  const startTask = async () => {
    render(<App />);
    // Land in s1.
    emit({
      type: 'SESSION_LIST_RESPONSE',
      payload: { sessions: [{ id: 's1', title: 'First Chat' }] },
    });
    const getReq = __safeSend.mock.calls.find(([type]) => type === 'SESSION_GET_REQUEST');
    expect(getReq).toBeDefined();
    emit({
      type: 'SESSION_GET_RESPONSE',
      request_id: 'get-1',
      payload: { session: { id: 's1', title: 'First Chat', messages: [] } },
    });
    // Send a message: a task is now running locally.
    const textarea = screen.getByPlaceholderText('Message Aether...');
    fireEvent.change(textarea, { target: { value: 'long task' } });
    fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: false });
    expect(screen.getByRole('button', { name: 'Stop generation' })).toBeInTheDocument();
  };

  it('a cancel that was only QUEUED does not claim the task stopped', async () => {
    await startTask();
    __safeSend.mockReturnValue('queued');
    fireEvent.click(screen.getByRole('button', { name: 'Stop generation' }));
    // The cancel went out through safeSend as a user action...
    expect(__safeSend).toHaveBeenCalledWith('CANCEL_TASK', {});
    // ...but it has not reached the engine, so the task is still
    // presented as running: the Stop control stays.
    expect(screen.getByRole('button', { name: 'Stop generation' })).toBeInTheDocument();
  });

  it('a cancel that was SENT stops the task UI as before', async () => {
    await startTask();
    __safeSend.mockReturnValue('sent');
    fireEvent.click(screen.getByRole('button', { name: 'Stop generation' }));
    expect(__safeSend).toHaveBeenCalledWith('CANCEL_TASK', {});
    expect(screen.queryByRole('button', { name: 'Stop generation' })).not.toBeInTheDocument();
  });
});
