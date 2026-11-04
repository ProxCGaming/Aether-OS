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

// Wave 2 slice 1: safeSend — the shared send path for user actions.
// The guarantee under test: a user action is never silently dropped.
// Connected it transmits immediately and reports 'sent'; disconnected
// it lands in the facade's offline queue, reports 'queued', and the
// pending state (pendingCount / pendingMessages) reflects the real
// queue until the reconnect flush drains it, in order.
describe('SocketContext safeSend (Wave 2 slice 1)', () => {
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

  const sentPayloads = () => electronAPI.sendEngineMessage.mock.calls.map(([raw]) => JSON.parse(raw));

  it('transmits immediately and returns "sent" while connected, with the standard envelope', async () => {
    ({ api: electronAPI, listeners } = makeElectronAPI({ connected: true }));
    window.electronAPI = electronAPI;
    let socket = null;
    render(
      <SocketProvider>
        <Probe onSocket={(s) => { socket = s; }} />
      </SocketProvider>
    );
    await flushPromises();
    expect(socket.isConnected).toBe(true);

    let result;
    act(() => {
      result = socket.safeSend('MODEL_SET_DEFAULT', { provider: 'gemini', model: 'gemini-b' });
    });

    expect(result).toBe('sent');
    expect(electronAPI.sendEngineMessage).toHaveBeenCalledTimes(1);
    const parsed = sentPayloads()[0];
    expect(parsed.type).toBe('MODEL_SET_DEFAULT');
    expect(parsed.schema_version).toBe(1);
    expect(typeof parsed.request_id).toBe('string');
    expect(parsed.payload).toEqual({ provider: 'gemini', model: 'gemini-b' });
    // Nothing is pending after an immediate send.
    expect(socket.pendingCount).toBe(0);
    expect(socket.pendingMessages).toEqual([]);
  });

  it('queues while disconnected and returns "queued" — the action is not dropped', async () => {
    let socket = null;
    render(
      <SocketProvider>
        <Probe onSocket={(s) => { socket = s; }} />
      </SocketProvider>
    );
    await flushPromises();
    expect(socket.isConnected).toBe(false);

    let result;
    act(() => {
      result = socket.safeSend('MODEL_SET_DEFAULT', { provider: 'gemini', model: 'gemini-b' });
    });

    expect(result).toBe('queued');
    expect(electronAPI.sendEngineMessage).not.toHaveBeenCalled();
    expect(socket.pendingCount).toBe(1);
    expect(socket.pendingMessages).toEqual([
      { type: 'MODEL_SET_DEFAULT', payload: { provider: 'gemini', model: 'gemini-b' } },
    ]);
  });

  it('pending state tracks every queue entry point (safeSend, sendMessage, raw facade send) in order', async () => {
    let socket = null;
    render(
      <SocketProvider>
        <Probe onSocket={(s) => { socket = s; }} />
      </SocketProvider>
    );
    await flushPromises();

    act(() => {
      socket.safeSend('MODEL_SET_DEFAULT', { provider: 'gemini', model: 'gemini-b' });
      socket.sendMessage('PROVIDER_LIST', {});
      socket.wsRef.current.send(JSON.stringify({ type: 'RAW_ENVELOPE', payload: { n: 3 } }));
    });

    expect(socket.pendingCount).toBe(3);
    expect(socket.pendingMessages.map((m) => m.type)).toEqual([
      'MODEL_SET_DEFAULT',
      'PROVIDER_LIST',
      'RAW_ENVELOPE',
    ]);
    expect(socket.pendingMessages[2].payload).toEqual({ n: 3 });
  });

  it('flush on reconnect drains the queue in order and pending state returns to zero', async () => {
    let socket = null;
    render(
      <SocketProvider>
        <Probe onSocket={(s) => { socket = s; }} />
      </SocketProvider>
    );
    await flushPromises();

    act(() => {
      socket.safeSend('FIRST_ACTION', { n: 1 });
      socket.safeSend('SECOND_ACTION', { n: 2 });
    });
    expect(socket.pendingCount).toBe(2);

    emitEngineMessage(JSON.stringify({ type: '_ws_status', status: 'connected' }));
    await flushPromises();

    expect(electronAPI.sendEngineMessage).toHaveBeenCalledTimes(2);
    expect(sentPayloads().map((p) => p.type)).toEqual(['FIRST_ACTION', 'SECOND_ACTION']);
    expect(socket.pendingCount).toBe(0);
    expect(socket.pendingMessages).toEqual([]);
  });

  it('a queued model switch is delivered exactly once, payload intact, after reconnect', async () => {
    let socket = null;
    render(
      <SocketProvider>
        <Probe onSocket={(s) => { socket = s; }} />
      </SocketProvider>
    );
    await flushPromises();

    act(() => {
      socket.safeSend('MODEL_SET_DEFAULT', { provider: 'openai', model: 'gpt-a' });
    });
    emitEngineMessage(JSON.stringify({ type: '_ws_status', status: 'connected' }));
    await flushPromises();

    const delivered = sentPayloads().filter((p) => p.type === 'MODEL_SET_DEFAULT');
    expect(delivered).toHaveLength(1);
    expect(delivered[0].payload).toEqual({ provider: 'openai', model: 'gpt-a' });
    expect(socket.pendingCount).toBe(0);
  });
});
