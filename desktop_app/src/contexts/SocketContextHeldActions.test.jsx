import React, { useEffect } from 'react';
import { render, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import { SocketProvider, useAetherSocket, DESTRUCTIVE_MESSAGE_TYPES } from './SocketContext';

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

// Wave 2 slice 4 (audit Lane A, QUEUE-class caution): queued
// destructive actions must NOT flush verbatim on reconnect. The
// provider diverts them to a held set; only the user's explicit
// confirm releases them (in original order), discard drops them
// permanently, and ignoring them leaves them held across any number
// of further reconnects. Live destructive actions — performed while
// connected — are never held.
describe('SocketContext held destructive actions (Wave 2 slice 4)', () => {
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

  const emitStatus = (status) => {
    act(() => {
      [...listeners].forEach((cb) => cb(JSON.stringify({ type: '_ws_status', status })));
    });
  };

  const sentTypes = () => electronAPI.sendEngineMessage.mock.calls.map(([raw]) => JSON.parse(raw).type);

  function renderProvider() {
    let socket = null;
    render(
      <SocketProvider>
        <Probe onSocket={(s) => { socket = s; }} />
      </SocketProvider>
    );
    return () => socket;
  }

  it('names the destructive set once: reset, memory clear (both), provider remove, cancel task', () => {
    expect([...DESTRUCTIVE_MESSAGE_TYPES].sort()).toEqual([
      'CANCEL_TASK',
      'CONFIG_RESET_REQUEST',
      'MEMORY_EPISODES_CLEAR_REQUEST',
      'MEMORY_GRAPH_CLEAR_REQUEST',
      'PROVIDER_REMOVE_REQUEST',
    ]);
  });

  it('flushes normal queued messages on reconnect but holds destructive ones; confirm releases them in original order', async () => {
    const getSocket = renderProvider();
    await flushPromises();
    const socket = getSocket();
    expect(socket.isConnected).toBe(false);

    act(() => {
      socket.sendMessage('SESSION_LIST_REQUEST', { schema_version: 1, request_id: 'n1' });
      socket.sendMessage('PROVIDER_REMOVE_REQUEST', { schema_version: 1, request_id: 'd1', payload: { provider: 'gemini' } });
      socket.safeSend('MODEL_SET_DEFAULT', { provider: 'gemini', model: 'gemini-b' });
      socket.sendMessage('CONFIG_RESET_REQUEST', { schema_version: 1, request_id: 'd2', payload: {} });
    });
    expect(electronAPI.sendEngineMessage).not.toHaveBeenCalled();
    expect(getSocket().heldMessages).toHaveLength(0);

    emitStatus('connected');
    await flushPromises();

    // Non-destructive traffic flushed, in order; destructive did not move.
    expect(sentTypes()).toEqual(['SESSION_LIST_REQUEST', 'MODEL_SET_DEFAULT']);
    expect(getSocket().pendingCount).toBe(0);
    expect(getSocket().heldMessages.map((m) => m.type)).toEqual([
      'PROVIDER_REMOVE_REQUEST',
      'CONFIG_RESET_REQUEST',
    ]);
    expect(getSocket().heldMessages[0].payload).toEqual({ provider: 'gemini' });
    expect(getSocket().heldCount).toBe(2);

    act(() => { getSocket().confirmHeld(); });
    expect(sentTypes()).toEqual([
      'SESSION_LIST_REQUEST',
      'MODEL_SET_DEFAULT',
      'PROVIDER_REMOVE_REQUEST',
      'CONFIG_RESET_REQUEST',
    ]);
    expect(getSocket().heldMessages).toHaveLength(0);
  });

  it('discard drops a held action permanently — it never transmits, even after another disconnect/reconnect cycle', async () => {
    const getSocket = renderProvider();
    await flushPromises();

    act(() => {
      getSocket().sendMessage('CANCEL_TASK', { schema_version: 1, request_id: 'd1', payload: {} });
    });
    emitStatus('connected');
    await flushPromises();
    expect(sentTypes()).toEqual([]);
    expect(getSocket().heldMessages.map((m) => m.type)).toEqual(['CANCEL_TASK']);

    act(() => { getSocket().discardHeld(); });
    expect(getSocket().heldMessages).toHaveLength(0);

    emitStatus('disconnected');
    emitStatus('connected');
    await flushPromises();
    expect(sentTypes()).toEqual([]);
  });

  it('supports per-message verdicts: discard one held action, confirm the other', async () => {
    const getSocket = renderProvider();
    await flushPromises();

    act(() => {
      getSocket().sendMessage('MEMORY_EPISODES_CLEAR_REQUEST', { schema_version: 1, request_id: 'd1', payload: {} });
      getSocket().sendMessage('PROVIDER_REMOVE_REQUEST', { schema_version: 1, request_id: 'd2', payload: { provider: 'openai' } });
    });
    emitStatus('connected');
    await flushPromises();
    expect(getSocket().heldMessages.map((m) => m.type)).toEqual([
      'MEMORY_EPISODES_CLEAR_REQUEST',
      'PROVIDER_REMOVE_REQUEST',
    ]);

    act(() => { getSocket().discardHeld(0); });
    expect(getSocket().heldMessages.map((m) => m.type)).toEqual(['PROVIDER_REMOVE_REQUEST']);
    expect(sentTypes()).toEqual([]);

    act(() => { getSocket().confirmHeld(0); });
    expect(sentTypes()).toEqual(['PROVIDER_REMOVE_REQUEST']);
    expect(getSocket().heldMessages).toHaveLength(0);
  });

  it('ignored held actions stay held across further reconnects (never auto-send)', async () => {
    const getSocket = renderProvider();
    await flushPromises();

    act(() => {
      getSocket().safeSend('CONFIG_RESET_REQUEST', {});
    });
    emitStatus('connected');
    await flushPromises();
    expect(getSocket().heldMessages).toHaveLength(1);

    emitStatus('disconnected');
    emitStatus('connected');
    await flushPromises();
    expect(sentTypes()).toEqual([]);
    expect(getSocket().heldMessages).toHaveLength(1);
  });

  it('a confirm given while disconnected again re-queues at the front and is held again on the next reconnect', async () => {
    const getSocket = renderProvider();
    await flushPromises();

    act(() => {
      getSocket().sendMessage('PROVIDER_REMOVE_REQUEST', { schema_version: 1, request_id: 'd1', payload: { provider: 'gemini' } });
    });
    emitStatus('connected');
    await flushPromises();
    expect(getSocket().heldMessages).toHaveLength(1);

    emitStatus('disconnected');
    await flushPromises();
    act(() => { getSocket().confirmHeld(); });
    // Not transmitted (no live socket) and not lost: back in the queue.
    expect(sentTypes()).toEqual([]);
    expect(getSocket().heldMessages).toHaveLength(0);
    expect(getSocket().pendingCount).toBe(1);

    emitStatus('connected');
    await flushPromises();
    // Held again rather than flushed.
    expect(sentTypes()).toEqual([]);
    expect(getSocket().heldMessages.map((m) => m.type)).toEqual(['PROVIDER_REMOVE_REQUEST']);

    act(() => { getSocket().confirmHeld(); });
    expect(sentTypes()).toEqual(['PROVIDER_REMOVE_REQUEST']);
  });

  it('a destructive action performed while connected sends immediately and is never held', async () => {
    ({ api: electronAPI, listeners } = makeElectronAPI({ connected: true }));
    window.electronAPI = electronAPI;
    const getSocket = renderProvider();
    await flushPromises();
    expect(getSocket().isConnected).toBe(true);

    let result;
    act(() => {
      result = getSocket().safeSend('CONFIG_RESET_REQUEST', {});
    });
    expect(result).toBe('sent');
    expect(sentTypes()).toEqual(['CONFIG_RESET_REQUEST']);
    expect(getSocket().heldMessages).toHaveLength(0);
  });

  it('reconnectCount counts recoveries only, never the initial connect', async () => {
    const getSocket = renderProvider();
    await flushPromises();
    expect(getSocket().reconnectCount).toBe(0);

    emitStatus('connected');
    await flushPromises();
    expect(getSocket().reconnectCount).toBe(1);

    emitStatus('disconnected');
    await flushPromises();
    expect(getSocket().reconnectCount).toBe(1);

    emitStatus('connected');
    await flushPromises();
    expect(getSocket().reconnectCount).toBe(2);
  });

  it('reconnectCount stays 0 when the provider mounts already connected', async () => {
    ({ api: electronAPI, listeners } = makeElectronAPI({ connected: true }));
    window.electronAPI = electronAPI;
    const getSocket = renderProvider();
    await flushPromises();
    expect(getSocket().isConnected).toBe(true);
    expect(getSocket().reconnectCount).toBe(0);
  });
});
