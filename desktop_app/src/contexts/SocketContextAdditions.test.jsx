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

function Probe({ onSocket }) {
  const socket = useAetherSocket();
  useEffect(() => { onSocket(socket); }, [socket, onSocket]);
  return null;
}

async function flushPromises() {
  await act(async () => {});
}

// Phase 10.3 D2 — SocketContext additions. C1's SocketContext.test.jsx
// already covers: facade usable from first render, exactly 1 status
// subscription, offline queue + flush on '_ws_status' reconnect, dedup,
// removeEventListener, readyState. This file covers only what C1 left
// untested: the hook's guard, the direct wsRef.send path, the
// getWsStatus-at-mount flush path, and non-status packets.
describe('SocketContext additions (Phase 10.3 D2)', () => {
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

  it('useAetherSocket throws outside a SocketProvider', () => {
    function Outside() {
      useAetherSocket();
      return null;
    }
    // React logs the thrown error; silence the console noise for this case.
    const spy = jest.spyOn(console, 'error').mockImplementation(() => {});
    expect(() => render(<Outside />)).toThrow(
      'useAetherSocket must be used within a SocketProvider'
    );
    spy.mockRestore();
  });

  it('sendMessage sends immediately while connected instead of queueing', async () => {
    let socket = null;
    render(
      <SocketProvider>
        <Probe onSocket={(s) => { socket = s; }} />
      </SocketProvider>
    );
    await flushPromises();
    emitEngineMessage(JSON.stringify({ type: '_ws_status', status: 'connected' }));
    await flushPromises();
    expect(socket.isConnected).toBe(true);

    act(() => {
      socket.sendMessage('PING', { n: 1 });
    });

    expect(electronAPI.sendEngineMessage).toHaveBeenCalledTimes(1);
    expect(electronAPI.sendEngineMessage).toHaveBeenCalledWith(
      JSON.stringify({ type: 'PING', n: 1 })
    );
  });

  it('wsRef.send queues while offline and flushes on reconnect', async () => {
    let socket = null;
    render(
      <SocketProvider>
        <Probe onSocket={(s) => { socket = s; }} />
      </SocketProvider>
    );
    await flushPromises();

    act(() => {
      socket.wsRef.current.send('raw-packet');
    });
    expect(electronAPI.sendEngineMessage).not.toHaveBeenCalled();

    emitEngineMessage(JSON.stringify({ type: '_ws_status', status: 'connected' }));
    await flushPromises();

    expect(electronAPI.sendEngineMessage).toHaveBeenCalledTimes(1);
    expect(electronAPI.sendEngineMessage).toHaveBeenCalledWith('raw-packet');
  });

  it('messages queued before getWsStatus resolves flush when it reports connected', async () => {
    let resolveStatus;
    electronAPI.getWsStatus = jest.fn(
      () => new Promise((resolve) => { resolveStatus = resolve; })
    );
    let socket = null;
    render(
      <SocketProvider>
        <Probe onSocket={(s) => { socket = s; }} />
      </SocketProvider>
    );
    await flushPromises();
    expect(socket.isConnected).toBe(false);

    act(() => {
      socket.sendMessage('QUEUED_EARLY', {});
    });
    expect(electronAPI.sendEngineMessage).not.toHaveBeenCalled();

    await act(async () => { resolveStatus(true); });
    await flushPromises();

    expect(socket.isConnected).toBe(true);
    expect(electronAPI.sendEngineMessage).toHaveBeenCalledTimes(1);
    expect(electronAPI.sendEngineMessage).toHaveBeenCalledWith(
      JSON.stringify({ type: 'QUEUED_EARLY' })
    );
  });

  it('a non-status engine message does not change connection state', async () => {
    let socket = null;
    render(
      <SocketProvider>
        <Probe onSocket={(s) => { socket = s; }} />
      </SocketProvider>
    );
    await flushPromises();
    expect(socket.isConnected).toBe(false);

    emitEngineMessage(JSON.stringify({ type: 'hello', payload: { active_model: 'gpt-4' } }));
    await flushPromises();
    expect(socket.isConnected).toBe(false);

    emitEngineMessage(JSON.stringify({ type: '_ws_status', status: 'connected' }));
    await flushPromises();
    expect(socket.isConnected).toBe(true);

    emitEngineMessage(JSON.stringify({ type: 'TASK_PROGRESS', payload: { text_delta: 'hi' } }));
    await flushPromises();
    expect(socket.isConnected).toBe(true);
  });

  it('disconnect after connect flips isConnected back to false', async () => {
    let socket = null;
    render(
      <SocketProvider>
        <Probe onSocket={(s) => { socket = s; }} />
      </SocketProvider>
    );
    await flushPromises();
    emitEngineMessage(JSON.stringify({ type: '_ws_status', status: 'connected' }));
    await flushPromises();
    expect(socket.isConnected).toBe(true);

    emitEngineMessage(JSON.stringify({ type: '_ws_status', status: 'disconnected' }));
    await flushPromises();
    expect(socket.isConnected).toBe(false);
    expect(socket.wsRef.current.readyState).toBe(WebSocket.CLOSED);
  });
});
