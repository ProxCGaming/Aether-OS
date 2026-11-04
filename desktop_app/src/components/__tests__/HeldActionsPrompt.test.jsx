import React from 'react';
import { render, screen, act, within } from '@testing-library/react';
import '@testing-library/jest-dom';
import { SocketProvider, useAetherSocket } from '../../contexts/SocketContext';
import HeldActionsPrompt from '../HeldActionsPrompt';

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

function Capture({ captureRef }) {
  captureRef.current = useAetherSocket();
  return null;
}

async function flushPromises() {
  await act(async () => {});
}

// Wave 2 slice 4: the reconfirmation prompt is the user's verdict
// point for held destructive actions. Driven by the REAL
// SocketProvider: queue destructive actions offline, reconnect, and
// the prompt — and only the prompt's buttons — decides their fate.
describe('HeldActionsPrompt (Wave 2 slice 4)', () => {
  let electronAPI;
  let listeners;
  let captureRef;

  beforeEach(() => {
    ({ api: electronAPI, listeners } = makeElectronAPI({ connected: false }));
    window.electronAPI = electronAPI;
    captureRef = { current: null };
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

  it('appears on reconnect with plain-language labels, and per-action Send/Discard decide each action', async () => {
    render(
      <SocketProvider>
        <Capture captureRef={captureRef} />
        <HeldActionsPrompt />
      </SocketProvider>
    );
    await flushPromises();
    expect(screen.queryByTestId('held-actions-prompt')).not.toBeInTheDocument();

    act(() => {
      captureRef.current.sendMessage('PROVIDER_REMOVE_REQUEST', {
        schema_version: 1, request_id: 'd1', payload: { provider: 'gemini' },
      });
      captureRef.current.safeSend('CONFIG_RESET_REQUEST', {});
    });
    // Still offline: nothing held yet, no prompt.
    expect(screen.queryByTestId('held-actions-prompt')).not.toBeInTheDocument();

    emitStatus('connected');
    await flushPromises();

    const prompt = screen.getByTestId('held-actions-prompt');
    expect(prompt).toBeInTheDocument();
    expect(sentTypes()).toEqual([]);
    const rows = within(prompt).getAllByTestId('held-action-row');
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent('Remove provider “gemini”');
    expect(rows[1]).toHaveTextContent('Reset all settings to defaults');

    // Send only the provider removal.
    act(() => { within(rows[0]).getByRole('button', { name: 'Send' }).click(); });
    expect(sentTypes()).toEqual(['PROVIDER_REMOVE_REQUEST']);
    expect(within(screen.getByTestId('held-actions-prompt')).getAllByTestId('held-action-row')).toHaveLength(1);

    // Discard the reset: prompt disappears, reset never transmits.
    const lastRow = within(screen.getByTestId('held-actions-prompt')).getAllByTestId('held-action-row')[0];
    act(() => { within(lastRow).getByRole('button', { name: 'Discard' }).click(); });
    expect(screen.queryByTestId('held-actions-prompt')).not.toBeInTheDocument();
    expect(sentTypes()).toEqual(['PROVIDER_REMOVE_REQUEST']);
  });

  it('Send all releases every held action in original order', async () => {
    render(
      <SocketProvider>
        <Capture captureRef={captureRef} />
        <HeldActionsPrompt />
      </SocketProvider>
    );
    await flushPromises();

    act(() => {
      captureRef.current.sendMessage('MEMORY_GRAPH_CLEAR_REQUEST', {
        schema_version: 1, request_id: 'd1', payload: {},
      });
      captureRef.current.sendMessage('CANCEL_TASK', {
        schema_version: 1, request_id: 'd2', payload: {},
      });
    });
    emitStatus('connected');
    await flushPromises();

    const prompt = screen.getByTestId('held-actions-prompt');
    expect(prompt).toHaveTextContent('Clear the whole memory graph');
    expect(prompt).toHaveTextContent('Cancel the running task');

    act(() => { within(prompt).getByRole('button', { name: 'Send all' }).click(); });
    expect(sentTypes()).toEqual(['MEMORY_GRAPH_CLEAR_REQUEST', 'CANCEL_TASK']);
    expect(screen.queryByTestId('held-actions-prompt')).not.toBeInTheDocument();
  });
});
