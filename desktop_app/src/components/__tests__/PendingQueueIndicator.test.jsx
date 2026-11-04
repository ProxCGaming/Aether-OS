import React, { useEffect } from 'react';
import { render, screen, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import { SocketProvider, useAetherSocket } from '../../contexts/SocketContext';
import PendingQueueIndicator from '../PendingQueueIndicator';

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

// Wave 2 slice 1: the app-level pending indicator is driven by the
// real SocketContext queue — it renders only while the queue is
// non-empty, counts what is actually waiting, and disappears when
// the queue flushes on reconnect.
describe('PendingQueueIndicator (Wave 2 slice 1)', () => {
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

  function renderIndicator(onSocket) {
    return render(
      <SocketProvider>
        <Probe onSocket={onSocket} />
        <PendingQueueIndicator />
      </SocketProvider>
    );
  }

  it('renders nothing while the queue is empty', async () => {
    renderIndicator(() => {});
    await flushPromises();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('shows the queued count in plain language while actions wait', async () => {
    let socket = null;
    renderIndicator((s) => { socket = s; });
    await flushPromises();

    act(() => {
      socket.safeSend('MODEL_SET_DEFAULT', { provider: 'gemini', model: 'gemini-b' });
      socket.safeSend('CONFIG_UPDATE_REQUEST', { config: { theme: 'dark' } });
    });

    expect(screen.getByRole('status')).toHaveTextContent(
      '2 actions queued — will send when the engine reconnects'
    );
  });

  it('uses the singular for a single queued action', async () => {
    let socket = null;
    renderIndicator((s) => { socket = s; });
    await flushPromises();

    act(() => {
      socket.safeSend('MODEL_SET_DEFAULT', { provider: 'gemini', model: 'gemini-b' });
    });

    expect(screen.getByRole('status')).toHaveTextContent(
      '1 action queued — will send when the engine reconnects'
    );
  });

  it('disappears after the queue flushes on reconnect', async () => {
    let socket = null;
    renderIndicator((s) => { socket = s; });
    await flushPromises();

    act(() => {
      socket.safeSend('MODEL_SET_DEFAULT', { provider: 'gemini', model: 'gemini-b' });
    });
    expect(screen.getByRole('status')).toBeInTheDocument();

    emitEngineMessage(JSON.stringify({ type: '_ws_status', status: 'connected' }));
    await flushPromises();

    expect(screen.queryByRole('status')).not.toBeInTheDocument();
    expect(electronAPI.sendEngineMessage).toHaveBeenCalledTimes(1);
  });
});
