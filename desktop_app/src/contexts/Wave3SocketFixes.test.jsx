import React, { useEffect } from 'react';
import { render, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import { SocketProvider, useAetherSocket } from './SocketContext';
import { packetIdentityKey } from '../packetIdentity';

// Wave 3 (D3 chunk) — SocketContext:
//   D3-10 — safeSend must mint a request_id that names exactly one
//           action. Date.now() alone collides for every action sent
//           inside the same millisecond, and request_id is the
//           correlation id the engine answers against.
//   D3-11 — the dedup identity must not embed the entire packet
//           (MB-sized base64 packets were retained whole, per
//           subscriber), and pruning must not depend on another
//           packet arriving: entries expire on a timer too. The key
//           is pinned directly — packetIdentityKey is exported for
//           exactly this (precedent: destructiveActionLabel).

global.WebSocket = { OPEN: 1, CLOSED: 3, CONNECTING: 0, CLOSING: 2 };

function makeElectronAPI({ connected = true } = {}) {
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

describe('D3-10 — safeSend request ids are unique within the same millisecond', () => {
  let electronAPI;

  beforeEach(() => {
    ({ api: electronAPI } = makeElectronAPI({ connected: true }));
    window.electronAPI = electronAPI;
    jest.spyOn(Date, 'now').mockReturnValue(1700000000000);
  });

  afterEach(() => {
    delete window.electronAPI;
    jest.restoreAllMocks();
  });

  it('three safeSends in one millisecond carry three different request_ids', async () => {
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
      socket.safeSend('THIRD_ACTION', { n: 3 });
    });

    const ids = electronAPI.sendEngineMessage.mock.calls
      .map(([raw]) => JSON.parse(raw).request_id);
    expect(ids).toHaveLength(3);
    expect(new Set(ids).size).toBe(3);
    ids.forEach((id) => expect(typeof id).toBe('string'));
  });

  it('a caller-supplied correlation id is still passed through untouched', async () => {
    let socket = null;
    render(
      <SocketProvider>
        <Probe onSocket={(s) => { socket = s; }} />
      </SocketProvider>
    );
    await flushPromises();

    act(() => {
      socket.safeSend('PLUGIN_APPROVAL_DECISION', { ok: true }, 'install-42');
    });
    const parsed = JSON.parse(electronAPI.sendEngineMessage.mock.calls[0][0]);
    expect(parsed.request_id).toBe('install-42');
  });
});

describe('D3-11 — the dedup identity is bounded and content-sensitive', () => {
  const bigPacket = (data) => JSON.stringify({
    event_id: 'evt-1',
    ts: 4242,
    type: 'MEDIA_RECORDED',
    payload: { data },
  });

  it('keys a multi-MB packet with a short bounded string, not the packet itself', () => {
    const key = packetIdentityKey(bigPacket('x'.repeat(2 * 1024 * 1024)));
    expect(typeof key).toBe('string');
    expect(key.length).toBeLessThan(200);
  });

  it('is stable for identical bytes and distinct for different content under the same id/ts', () => {
    const a = bigPacket('a'.repeat(5000));
    const b = bigPacket('b'.repeat(5000));
    expect(packetIdentityKey(a)).toBe(packetIdentityKey(a));
    expect(packetIdentityKey(a)).not.toBe(packetIdentityKey(b));
  });

  it('returns null for packets with no identity to key on', () => {
    expect(packetIdentityKey(JSON.stringify({ type: 'UNKEYED' }))).toBeNull();
    expect(packetIdentityKey('not json')).toBeNull();
  });
});

describe('D3-11 — dedup behaviour guards (identity semantics unchanged)', () => {
  let electronAPI;
  let listeners;

  beforeEach(() => {
    ({ api: electronAPI, listeners } = makeElectronAPI({ connected: true }));
    window.electronAPI = electronAPI;
  });

  afterEach(() => {
    delete window.electronAPI;
    jest.restoreAllMocks();
    jest.useRealTimers();
  });

  const emit = (raw) => {
    act(() => { [...listeners].forEach((cb) => cb(raw)); });
  };

  async function subscribe() {
    let socket = null;
    render(
      <SocketProvider>
        <Probe onSocket={(s) => { socket = s; }} />
      </SocketProvider>
    );
    await flushPromises();
    const handler = jest.fn();
    act(() => { socket.wsRef.current.addEventListener('message', handler); });
    return handler;
  }

  const packet = (data) => JSON.stringify({
    event_id: 'evt-1', ts: 4242, type: 'MEDIA_RECORDED', payload: { data },
  });

  it('suppresses a byte-identical redelivery but delivers same-id/ts packets whose content differs', async () => {
    const handler = await subscribe();
    const p = packet('x'.repeat(5000));
    emit(p);
    emit(p);
    expect(handler).toHaveBeenCalledTimes(1);
    emit(packet('y'.repeat(5000)));
    expect(handler).toHaveBeenCalledTimes(2);
  });

  it('delivers the same packet again once the window has passed in a quiet socket', async () => {
    jest.useFakeTimers();
    const handler = await subscribe();
    const p = packet('z'.repeat(5000));
    emit(p);
    expect(handler).toHaveBeenCalledTimes(1);
    // No further packets arrive; expiry must not depend on one.
    act(() => { jest.advanceTimersByTime(1000); });
    emit(p);
    expect(handler).toHaveBeenCalledTimes(2);
  });
});
