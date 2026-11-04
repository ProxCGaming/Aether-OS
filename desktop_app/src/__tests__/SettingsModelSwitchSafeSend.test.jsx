import React from 'react';
import { render, screen, fireEvent, act, within } from '@testing-library/react';
import '@testing-library/jest-dom';
import { SocketProvider, useAetherSocket } from '../contexts/SocketContext';
import SettingsPanel from '../SettingsPanel';

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

// Wires SettingsPanel to the REAL SocketContext exactly the way App
// does: the facade wsRef plus the safeSend / pendingMessages props.
function Harness({ providers, captureRef }) {
  const socket = useAetherSocket();
  captureRef.current = socket;
  return (
    <SettingsPanel
      wsRef={socket.wsRef}
      safeSend={socket.safeSend}
      pendingMessages={socket.pendingMessages}
      providers={providers}
      localModels={[]}
    />
  );
}

async function flushPromises() {
  await act(async () => {});
}

const PROVIDERS = [
  {
    name: 'gemini', display_name: 'Gemini', has_key: true,
    is_default: true, default_model: 'gemini-a',
    models: ['gemini-a', 'gemini-b'],
  },
  {
    name: 'openai', display_name: 'OpenAI', has_key: true,
    is_default: false, default_model: 'gpt-a',
    models: ['gpt-a'],
  },
];

// Wave 2 slice 1, finding REF: the Settings Global Default switch.
// Before the fix, picking a model while the socket was down hit a
// readyState guard and the MODEL_SET_DEFAULT was silently discarded —
// the controlled dropdown snapped back and the switch "didn't stick".
// Now the pick is queued through safeSend, the control shows its
// pending affordance, and the switch flushes on reconnect.
describe('Settings Global Default model switch (REF, Wave 2 slice 1)', () => {
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

  const sentOfType = (type) => electronAPI.sendEngineMessage.mock.calls
    .map(([raw]) => { try { return JSON.parse(raw); } catch (e) { return null; } })
    .filter((p) => p && p.type === type);

  const queuedSwitches = (captureRef) => captureRef.current.pendingMessages
    .filter((m) => m.type === 'MODEL_SET_DEFAULT');

  function pickGlobalDefaultModel(modelLabel) {
    fireEvent.click(screen.getByText('Models & Tasks'));
    const section = within(screen.getByText('Global Default Model').closest('div'));
    fireEvent.click(section.getByText('gemini-a'));
    fireEvent.click(screen.getByRole('button', { name: modelLabel }));
  }

  it('queues the switch while the engine is unreachable, shows the pending affordance, and flushes on reconnect', async () => {
    const captureRef = { current: null };
    render(
      <SocketProvider>
        <Harness providers={PROVIDERS} captureRef={captureRef} />
      </SocketProvider>
    );
    await flushPromises();
    expect(captureRef.current.isConnected).toBe(false);

    pickGlobalDefaultModel('gemini-b');

    // Not transmitted, and NOT silently dropped either: exactly one
    // MODEL_SET_DEFAULT is waiting in the facade queue.
    expect(sentOfType('MODEL_SET_DEFAULT')).toHaveLength(0);
    expect(queuedSwitches(captureRef)).toEqual([
      { type: 'MODEL_SET_DEFAULT', payload: { provider: 'gemini', model: 'gemini-b' } },
    ]);
    // The control explains itself while the switch is pending.
    expect(screen.getByText(/Model switch to gemini-b queued — will apply when the engine reconnects/)).toBeInTheDocument();

    emitEngineMessage(JSON.stringify({ type: '_ws_status', status: 'connected' }));
    await flushPromises();

    const delivered = sentOfType('MODEL_SET_DEFAULT');
    expect(delivered).toHaveLength(1);
    expect(delivered[0].payload).toEqual({ provider: 'gemini', model: 'gemini-b' });
    expect(queuedSwitches(captureRef)).toHaveLength(0);
    expect(screen.queryByText(/queued — will apply when the engine reconnects/)).not.toBeInTheDocument();
  });

  it('sends immediately while connected, with no pending state', async () => {
    ({ api: electronAPI, listeners } = makeElectronAPI({ connected: true }));
    window.electronAPI = electronAPI;
    const captureRef = { current: null };
    render(
      <SocketProvider>
        <Harness providers={PROVIDERS} captureRef={captureRef} />
      </SocketProvider>
    );
    await flushPromises();
    expect(captureRef.current.isConnected).toBe(true);

    pickGlobalDefaultModel('gemini-b');

    const delivered = sentOfType('MODEL_SET_DEFAULT');
    expect(delivered).toHaveLength(1);
    expect(delivered[0].schema_version).toBe(1);
    expect(delivered[0].payload).toEqual({ provider: 'gemini', model: 'gemini-b' });
    expect(queuedSwitches(captureRef)).toHaveLength(0);
    expect(screen.queryByText(/queued — will apply when the engine reconnects/)).not.toBeInTheDocument();
  });
});
