import React, { useEffect } from 'react';
import { render, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import { SocketProvider, useAetherSocket } from './SocketContext';

// jsdom has no WebSocket; the socket layer only needs the constants.
global.WebSocket = { OPEN: 1, CLOSED: 3, CONNECTING: 0, CLOSING: 2 };

function makeElectronAPI({ connected = false } = {}) {
  const listeners = [];
  const api = {
    sendEngineMessage: jest.fn(),
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

// Captures the socket object from inside an effect (after mount).
function Probe({ onSocket }) {
  const socket = useAetherSocket();
  useEffect(() => { onSocket(socket); }, [socket, onSocket]);
  return null;
}

// Captures the socket object during render (before any effect has run).
function CaptureRender({ onRender }) {
  const socket = useAetherSocket();
  onRender(socket);
  return null;
}

async function flushPromises() {
  await act(async () => {});
}

describe('SocketContext (Phase 10.3 C1: the single engine-message facade)', () => {
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

  it('exposes a usable wsRef from the very first render', () => {
    // AppContent mounts at the same time as the provider, and its mount
    // effect runs BEFORE the provider's effect (child effects first), so
    // the facade must exist at render time, not only after the effect.
    // Snapshot the ref's VALUE during render: reading the live ref after
    // render() returns would see the effect's assignment and hide the race.
    let firstRenderWs = 'unset';
    render(
      <SocketProvider>
        <CaptureRender onRender={(s) => { firstRenderWs = s.wsRef.current; }} />
      </SocketProvider>
    );
    expect(firstRenderWs).not.toBeNull();
    expect(typeof firstRenderWs.send).toBe('function');
    expect(typeof firstRenderWs.addEventListener).toBe('function');
    expect(typeof firstRenderWs.removeEventListener).toBe('function');
  });

  it('registers exactly one onEngineMessage subscription for its own status listener', () => {
    render(
      <SocketProvider>
        <div />
      </SocketProvider>
    );
    expect(electronAPI.onEngineMessage).toHaveBeenCalledTimes(1);
  });

  it('queues messages while offline and flushes them in order on reconnect', async () => {
    let socket = null;
    render(
      <SocketProvider>
        <Probe onSocket={(s) => { socket = s; }} />
      </SocketProvider>
    );
    await flushPromises();
    expect(socket.isConnected).toBe(false);

    act(() => {
      socket.sendMessage('FIRST', { n: 1 });
      socket.sendMessage('SECOND', { n: 2 });
    });
    // Nothing leaves the renderer while the engine is unreachable.
    expect(electronAPI.sendEngineMessage).not.toHaveBeenCalled();

    emitEngineMessage(JSON.stringify({ type: '_ws_status', status: 'connected' }));
    await flushPromises();

    expect(socket.isConnected).toBe(true);
    expect(electronAPI.sendEngineMessage).toHaveBeenCalledTimes(2);
    expect(electronAPI.sendEngineMessage).toHaveBeenNthCalledWith(1, JSON.stringify({ type: 'FIRST', n: 1 }));
    expect(electronAPI.sendEngineMessage).toHaveBeenNthCalledWith(2, JSON.stringify({ type: 'SECOND', n: 2 }));
  });

  it('delivers an identical packet only once within the dedup window, then again after it', async () => {
    let now = 10000;
    jest.spyOn(Date, 'now').mockImplementation(() => now);

    let socket = null;
    render(
      <SocketProvider>
        <Probe onSocket={(s) => { socket = s; }} />
      </SocketProvider>
    );
    await flushPromises();

    const handler = jest.fn();
    act(() => {
      socket.wsRef.current.addEventListener('message', handler);
    });
    // One addEventListener call adds exactly one underlying subscription.
    expect(electronAPI.onEngineMessage).toHaveBeenCalledTimes(2); // status listener + this one

    const packet = JSON.stringify({ type: 'TASK_PROGRESS', payload: { text_delta: 'hello' } });
    const other = JSON.stringify({ type: 'TASK_PROGRESS', payload: { text_delta: 'world' } });

    emitEngineMessage(packet);
    emitEngineMessage(packet); // identical duplicate in the same tick
    expect(handler).toHaveBeenCalledTimes(1);
    expect(handler).toHaveBeenCalledWith({ data: packet });

    emitEngineMessage(other);
    expect(handler).toHaveBeenCalledTimes(2);

    // The window is measured against the last DELIVERED packet: a packet
    // that differs from its predecessor is new, even inside the window.
    now += 149;
    emitEngineMessage(packet);
    expect(handler).toHaveBeenCalledTimes(3);
    emitEngineMessage(packet); // identical to the packet just delivered
    expect(handler).toHaveBeenCalledTimes(3);

    now += 150; // 150ms since the last delivery of this packet: new again
    emitEngineMessage(packet);
    expect(handler).toHaveBeenCalledTimes(4);
  });

  it('stops delivering to a handler after removeEventListener', async () => {
    let socket = null;
    render(
      <SocketProvider>
        <Probe onSocket={(s) => { socket = s; }} />
      </SocketProvider>
    );
    await flushPromises();

    const handler = jest.fn();
    act(() => {
      socket.wsRef.current.addEventListener('message', handler);
    });
    emitEngineMessage(JSON.stringify({ type: 'hello', payload: {} }));
    expect(handler).toHaveBeenCalledTimes(1);

    act(() => {
      socket.wsRef.current.removeEventListener('message', handler);
    });
    // The underlying IPC subscription is gone too (only the status one left).
    expect(electronAPI.onEngineMessage).toHaveBeenCalledTimes(2);
    expect(listeners).toHaveLength(1);

    emitEngineMessage(JSON.stringify({ type: 'hello', payload: {} }));
    expect(handler).toHaveBeenCalledTimes(1);
  });

  it('reports CLOSED readyState while offline and OPEN once connected', async () => {
    let socket = null;
    render(
      <SocketProvider>
        <Probe onSocket={(s) => { socket = s; }} />
      </SocketProvider>
    );
    await flushPromises();
    expect(socket.wsRef.current.readyState).toBe(WebSocket.CLOSED);

    emitEngineMessage(JSON.stringify({ type: '_ws_status', status: 'connected' }));
    await flushPromises();
    expect(socket.wsRef.current.readyState).toBe(WebSocket.OPEN);

    emitEngineMessage(JSON.stringify({ type: '_ws_status', status: 'disconnected' }));
    await flushPromises();
    expect(socket.wsRef.current.readyState).toBe(WebSocket.CLOSED);
  });
});
