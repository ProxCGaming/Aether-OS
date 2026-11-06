import React, { useEffect } from 'react';
import { render, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import { SocketProvider, useAetherSocket } from './SocketContext';

// E10-06 / E10-07 — the WS message-type trace belongs at the
// transport, not inside per-subscriber wrappers or only on the
// connected send path:
//   - one incoming packet must produce ONE 'in' trace line however
//     many subscribers receive it (it used to produce one per
//     subscriber, spending the shared append-ui-log rate cap N
//     times and making the trace's counts wrong by a factor of N);
//   - a message queued while disconnected must produce its 'out'
//     trace when it actually crosses on reconnect (queued flushes
//     used to bypass the trace entirely — exactly the traffic a
//     post-mortem of an outage goes looking for).

global.WebSocket = { OPEN: 1, CLOSED: 3, CONNECTING: 0, CLOSING: 2 };

function makeElectronAPI({ connected = false } = {}) {
  const listeners = [];
  const api = {
    sendEngineMessage: jest.fn(),
    appendUiLog: jest.fn(),
    onEngineMessage: jest.fn((cb) => {
      listeners.push(cb);
      return () => {
        const idx = listeners.indexOf(cb);
        if (idx !== -1) listeners.splice(idx, 1);
      };
    }),
    getWsStatus: jest.fn(() => Promise.resolve(connected)),
  };
  return { api, listeners };
}

function Probe({ onSocket }) {
  const socket = useAetherSocket();
  useEffect(() => { onSocket(socket); }, [socket, onSocket]);
  return null;
}

async function flushPromises() {
  await act(async () => {});
}

function traceEntries(api, message) {
  return api.appendUiLog.mock.calls.filter(([entry]) => entry && entry.message === message);
}

describe('SocketContext message-type tracing (E10-06, E10-07)', () => {
  let electronAPI;
  let listeners;

  beforeEach(() => {
    ({ api: electronAPI, listeners } = makeElectronAPI({ connected: false }));
    window.electronAPI = electronAPI;
  });

  afterEach(() => {
    delete window.electronAPI;
    jest.restoreAllMocks();
  });

  const emitEngineMessage = (raw) => {
    act(() => {
      [...listeners].forEach((cb) => cb(raw));
    });
  };

  it('traces an incoming packet once per packet, not once per subscriber (E10-06)', async () => {
    let socket = null;
    render(
      <SocketProvider>
        <Probe onSocket={(s) => { socket = s; }} />
      </SocketProvider>
    );
    await flushPromises();

    const handlerA = jest.fn();
    const handlerB = jest.fn();
    act(() => {
      socket.wsRef.current.addEventListener('message', handlerA);
      socket.wsRef.current.addEventListener('message', handlerB);
    });

    const packet = JSON.stringify({
      type: 'MODEL_DEFAULT_CHANGED',
      request_id: 'req-1',
      event_id: 'evt-1',
      ts: 1700000000.5,
      payload: {},
    });
    emitEngineMessage(packet);

    // Both subscribers still receive the packet…
    expect(handlerA).toHaveBeenCalledTimes(1);
    expect(handlerB).toHaveBeenCalledTimes(1);
    // …but the trace records the packet exactly once.
    expect(traceEntries(electronAPI, 'ws message in: MODEL_DEFAULT_CHANGED')).toHaveLength(1);
  });

  it('traces a queued message when the reconnect flush actually sends it (E10-07)', async () => {
    let socket = null;
    render(
      <SocketProvider>
        <Probe onSocket={(s) => { socket = s; }} />
      </SocketProvider>
    );
    await flushPromises();

    // Disconnected: sendMessage queues the message. Nothing has
    // crossed the transport, so there is no 'out' trace yet.
    act(() => {
      socket.sendMessage('MODEL_SET_DEFAULT', { payload: { provider: 'openai', model: 'gpt-x' } });
    });
    expect(electronAPI.sendEngineMessage).not.toHaveBeenCalled();
    expect(traceEntries(electronAPI, 'ws message out: MODEL_SET_DEFAULT')).toHaveLength(0);

    // The engine reconnects: the queue flushes through the
    // transport, and the flush is traced like any other send.
    emitEngineMessage(JSON.stringify({ type: '_ws_status', status: 'connected' }));
    expect(electronAPI.sendEngineMessage).toHaveBeenCalledTimes(1);
    expect(traceEntries(electronAPI, 'ws message out: MODEL_SET_DEFAULT')).toHaveLength(1);
  });

  it('traces a held message when the user confirms its release (E10-07)', async () => {
    let socket = null;
    render(
      <SocketProvider>
        <Probe onSocket={(s) => { socket = s; }} />
      </SocketProvider>
    );
    await flushPromises();

    // Queue a destructive action while offline; on reconnect it is
    // HELD (never auto-flushed), and only the user's confirmation
    // sends it — that send must be traced too.
    act(() => {
      socket.sendMessage('PROVIDER_REMOVE_REQUEST', { payload: { provider: 'openai' } });
    });
    emitEngineMessage(JSON.stringify({ type: '_ws_status', status: 'connected' }));
    expect(electronAPI.sendEngineMessage).not.toHaveBeenCalled();

    act(() => {
      socket.confirmHeld();
    });
    expect(electronAPI.sendEngineMessage).toHaveBeenCalledTimes(1);
    expect(traceEntries(electronAPI, 'ws message out: PROVIDER_REMOVE_REQUEST')).toHaveLength(1);
  });
});
