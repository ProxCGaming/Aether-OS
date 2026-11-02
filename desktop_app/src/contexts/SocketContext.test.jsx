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

  // S3-M9: dedup is keyed on the packet's own identity, never on raw
  // content alone. Engine packets (aether_common/contracts.py Event)
  // carry a top-level request_id/event_id (mirrored pair, per TASK —
  // every chunk of a stream shares it) and a per-instance ts stamped
  // at construction. A genuine duplicate (the StrictMode leaked
  // subscription redelivering the SAME serialized instance) therefore
  // repeats id + ts + bytes exactly, while two legitimate packets —
  // even with byte-identical payloads — differ in id or ts.
  it('suppresses a true duplicate delivery of the same packet identity within the window, then delivers it again after it', async () => {
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

    const packet = JSON.stringify({
      type: 'TASK_PROGRESS',
      request_id: 'req-1',
      event_id: 'req-1',
      ts: 1700000000.5,
      payload: { task_id: 'task-1', text_delta: 'hello' },
    });

    emitEngineMessage(packet);
    emitEngineMessage(packet); // same instance delivered twice (StrictMode leak)
    expect(handler).toHaveBeenCalledTimes(1);
    expect(handler).toHaveBeenCalledWith({ data: packet });

    // An interleaved distinct packet does not reset the suppression:
    // identity is remembered per packet, not just "same as the last one".
    const other = JSON.stringify({
      type: 'TASK_PROGRESS',
      request_id: 'req-2',
      event_id: 'req-2',
      ts: 1700000000.6,
      payload: { task_id: 'task-2', text_delta: 'world' },
    });
    emitEngineMessage(other);
    expect(handler).toHaveBeenCalledTimes(2);

    now += 149; // still inside the retention window: still a duplicate
    emitEngineMessage(packet);
    expect(handler).toHaveBeenCalledTimes(2);

    now += 150; // window expired since this identity was last delivered
    emitEngineMessage(packet);
    expect(handler).toHaveBeenCalledTimes(3);
  });

  it('delivers two identical payloads with different request_ids (distinct packet identities)', async () => {
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

    // Same type, same ts, byte-identical payload — only the packet
    // identity differs. Content-identity dedup dropped one of these;
    // identity-based dedup must deliver BOTH.
    const payload = { task_id: 'task-1', text_delta: 'hello', full_text: 'hello' };
    const first = JSON.stringify({ type: 'TASK_PROGRESS', request_id: 'req-a', event_id: 'req-a', ts: 1700000000.5, payload });
    const second = JSON.stringify({ type: 'TASK_PROGRESS', request_id: 'req-b', event_id: 'req-b', ts: 1700000000.5, payload });

    emitEngineMessage(first);
    emitEngineMessage(second);
    expect(handler).toHaveBeenCalledTimes(2);
    expect(handler).toHaveBeenNthCalledWith(1, { data: first });
    expect(handler).toHaveBeenNthCalledWith(2, { data: second });
  });

  it('delivers successive stream chunks that share a request_id but have distinct timestamps', async () => {
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

    // Real engine streams reuse ONE request_id for every chunk of a
    // task (fast_chat.py / executor.py), and repeated tokens make
    // payloads repeat exactly. Only ts separates the instances, so
    // keying on request_id alone would eat the stream.
    const payload = { task_id: 'task-1', text_delta: ' the', turn: 0, node: 'assistant' };
    const chunk1 = JSON.stringify({ type: 'TASK_PROGRESS', request_id: 'req-1', event_id: 'req-1', ts: 1700000000.001, payload });
    const chunk2 = JSON.stringify({ type: 'TASK_PROGRESS', request_id: 'req-1', event_id: 'req-1', ts: 1700000000.002, payload });

    emitEngineMessage(chunk1);
    emitEngineMessage(chunk2);
    expect(handler).toHaveBeenCalledTimes(2);
  });

  it('delivers anonymous packets with identical content (no identity field, no content dedup fallback)', async () => {
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

    // No request_id/event_id and no ts: there is nothing that could
    // distinguish a duplicate delivery from a distinct packet, so the
    // facade must not guess from content — both are delivered.
    const packet = JSON.stringify({ type: 'TASK_PROGRESS', payload: { text_delta: 'hello' } });
    emitEngineMessage(packet);
    emitEngineMessage(packet);
    expect(handler).toHaveBeenCalledTimes(2);
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
