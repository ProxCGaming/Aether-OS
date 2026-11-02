import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import App from '../App';
import { __fakeSocket } from '../contexts/SocketContext';

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

// The socket facade is replaced by a fake implementing its documented,
// separately unit-tested contract (src/contexts/SocketContext.test.jsx:
// "queues messages while offline and flushes them in order on
// reconnect"): send() delivers immediately while the socket is open
// and queues while it is closed; reopening flushes the queue in
// order. The drop is driven at the facade boundary because that is
// where readyState lives — App's own disconnect transition (which
// also ends the thinking state) is a different, already-tested path.
jest.mock('../contexts/SocketContext', () => {
  const listeners = new Set();
  const fake = {
    open: true,
    sent: [],
    queued: [],
    get readyState() {
      return this.open ? 1 : 3; // WebSocket.OPEN / WebSocket.CLOSED
    },
    send(data) {
      if (this.open) {
        this.sent.push(data);
      } else {
        this.queued.push(data);
      }
    },
    addEventListener(event, handler) {
      if (event === 'message') listeners.add(handler);
    },
    removeEventListener(event, handler) {
      listeners.delete(handler);
    },
    setOpen(open) {
      this.open = open;
      if (open) {
        const flushed = this.queued.splice(0, this.queued.length);
        flushed.forEach((data) => this.sent.push(data));
      }
    },
    emit(obj) {
      const data = JSON.stringify(obj);
      [...listeners].forEach((handler) => handler({ data }));
    },
    reset() {
      this.open = true;
      this.sent = [];
      this.queued = [];
      listeners.clear();
    },
  };
  const wsRef = { current: fake };
  const sendMessage = (type, payload = {}) => {
    fake.send(JSON.stringify({ type, ...payload }));
  };
  return {
    SocketProvider: ({ children }) => <>{children}</>,
    useAetherSocket: () => ({ wsRef, isConnected: true, sendMessage }),
    __fakeSocket: fake,
  };
});

// Astra E1-F07: a steer sent while the socket was down was labelled
// Queued in the UI but the send path never handed it to the facade —
// the disconnected steering branch in handleSendMessage was a
// comment-only no-op — so the facade queue (which flushes on
// reconnect) never held it, the engine never saw it, and
// STEERING_APPLIED could never clear the marker. The steer must go
// through the facade like every other packet: queued while closed,
// delivered on reconnect.
describe('App steering while the socket is down (Astra E1-F07)', () => {
  let electronAPI;

  beforeEach(() => {
    __fakeSocket.reset();
    electronAPI = {
      sendEngineMessage: jest.fn(),
      onEngineMessage: jest.fn(() => () => {}),
      getWsStatus: jest.fn(() => Promise.resolve(true)),
      getMemoryUsage: jest.fn(() => Promise.resolve(128)),
      onChatDetached: jest.fn(() => jest.fn()),
      onChatAttached: jest.fn(() => jest.fn()),
    };
    window.electronAPI = electronAPI;
  });

  afterEach(() => {
    delete window.electronAPI;
  });

  const flushPromises = async () => {
    await act(async () => {});
  };

  const sentPackets = () =>
    __fakeSocket.sent.map((raw) => JSON.parse(raw));

  const queuedPackets = () =>
    __fakeSocket.queued.map((raw) => JSON.parse(raw));

  const sendChatText = (text) => {
    const textarea = screen.getByPlaceholderText('Message Aether...');
    fireEvent.change(textarea, { target: { value: text } });
    fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: false });
  };

  it('an offline steer enters the facade queue and is delivered on reconnect', async () => {
    render(<App />);
    await flushPromises();

    // A task starts over the open socket and is still running.
    sendChatText('first task');
    expect(sentPackets().filter((p) => p.type === 'START_TASK')).toHaveLength(1);

    // The socket drops while the task runs.
    __fakeSocket.setOpen(false);

    // The user steers: the bubble shows Queued, nothing can be
    // delivered yet — but the packet must be IN the facade queue.
    sendChatText('offline steer');
    expect(screen.getByText('offline steer')).toBeInTheDocument();
    expect(screen.getByText('Queued')).toBeInTheDocument();
    expect(sentPackets().filter((p) => p.type === 'STEER_TASK')).toHaveLength(0);
    const queued = queuedPackets().filter((p) => p.type === 'STEER_TASK');
    expect(queued).toHaveLength(1);
    expect(queued[0].payload.prompt).toBe('offline steer');
    expect(queued[0].payload.message_id).toBeTruthy();

    // Reconnect flushes the queue: the STEER_TASK is delivered with
    // the same message id the bubble carries.
    __fakeSocket.setOpen(true);
    const delivered = sentPackets().filter((p) => p.type === 'STEER_TASK');
    expect(delivered).toHaveLength(1);
    expect(delivered[0].payload.message_id).toBe(queued[0].payload.message_id);

    // The engine applies it: the queued marker clears, the bubble stays.
    act(() => {
      __fakeSocket.emit({
        type: 'STEERING_APPLIED',
        payload: {
          message_id: delivered[0].payload.message_id,
          content: 'offline steer',
        },
      });
    });
    expect(screen.queryByText('Queued')).not.toBeInTheDocument();
    expect(screen.getByText('offline steer')).toBeInTheDocument();
  });
});
