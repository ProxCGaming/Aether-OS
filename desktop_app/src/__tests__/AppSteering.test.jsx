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

// Epic 1, E1-07: sending while a task runs must steer it, not replace
// it. These tests drive the real App + chat window through the
// SocketContext facade: the second send goes out as STEER_TASK, its
// bubble carries the queued marker, and STEERING_APPLIED clears it.
describe('App steering (Epic 1, E1-07)', () => {
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

  const sendChatText = (text) => {
    const textarea = screen.getByPlaceholderText('Message Aether...');
    fireEvent.change(textarea, { target: { value: text } });
    fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: false });
  };

  it('sending while thinking emits STEER_TASK, shows Queued, and STEERING_APPLIED clears it', async () => {
    render(<App />);
    await flushPromises();
    emitEngineMessage({ type: '_ws_status', status: 'connected' });
    await flushPromises();

    // First message: a normal task start.
    sendChatText('first task');
    const startPackets = sentPackets().filter((p) => p.type === 'START_TASK');
    expect(startPackets).toHaveLength(1);
    expect(startPackets[0].payload.prompt).toBe('first task');
    expect(screen.getByText('first task')).toBeInTheDocument();

    // Second message while the task is running: steering, not a restart.
    sendChatText('steer this please');
    const steerPackets = sentPackets().filter((p) => p.type === 'STEER_TASK');
    expect(steerPackets).toHaveLength(1);
    expect(steerPackets[0].payload.prompt).toBe('steer this please');
    expect(sentPackets().filter((p) => p.type === 'START_TASK')).toHaveLength(1);

    // The steer bubble is visibly queued.
    expect(screen.getByText('steer this please')).toBeInTheDocument();
    expect(screen.getByTestId('queued-indicator')).toBeInTheDocument();
    expect(screen.getByText('Queued')).toBeInTheDocument();

    // The engine applies it: the marker clears for that message id.
    emitEngineMessage({
      type: 'STEERING_APPLIED',
      payload: {
        message_id: steerPackets[0].payload.message_id,
        content: 'steer this please',
      },
    });
    expect(screen.queryByTestId('queued-indicator')).not.toBeInTheDocument();
    expect(screen.queryByText('Queued')).not.toBeInTheDocument();
    // The bubble itself stays — applied, not deleted.
    expect(screen.getByText('steer this please')).toBeInTheDocument();
  });

  it('a normal (not thinking) send never uses STEER_TASK', async () => {
    render(<App />);
    await flushPromises();
    emitEngineMessage({ type: '_ws_status', status: 'connected' });
    await flushPromises();

    sendChatText('ordinary message');
    expect(sentPackets().filter((p) => p.type === 'STEER_TASK')).toHaveLength(0);
    expect(sentPackets().filter((p) => p.type === 'START_TASK')).toHaveLength(1);
    expect(screen.queryByTestId('queued-indicator')).not.toBeInTheDocument();
  });
});
