import React from 'react';
import { render, screen, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import { SocketProvider, useAetherSocket } from '../contexts/SocketContext';
import SettingsPanel from '../SettingsPanel';
import UsageSettingsPanel from '../components/Settings/UsageSettingsPanel';
import GlobalArtifactsView from '../components/GlobalArtifactsView';

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
    registerSnipShortcut: jest.fn((hotkey) => Promise.resolve(hotkey)),
  };
  return { api, listeners };
}

// Harnesses wire each surface to the REAL SocketContext exactly the
// way App does after Wave 2 slice 4: facade wsRef, safeSend,
// pendingMessages — and the reconnectCount refetch signal.
function SettingsHarness() {
  const socket = useAetherSocket();
  return (
    <SettingsPanel
      wsRef={socket.wsRef}
      safeSend={socket.safeSend}
      pendingMessages={socket.pendingMessages}
      reconnectCount={socket.reconnectCount}
      providers={[]}
      isConnected={socket.isConnected}
      localModels={[]}
      initialMenu="Tools"
    />
  );
}

function UsageHarness() {
  const socket = useAetherSocket();
  return (
    <UsageSettingsPanel
      wsRef={socket.wsRef}
      safeSend={socket.safeSend}
      pendingMessages={socket.pendingMessages}
      reconnectCount={socket.reconnectCount}
      isConnected={socket.isConnected}
    />
  );
}

function ArtifactsHarness() {
  const socket = useAetherSocket();
  return (
    <GlobalArtifactsView
      wsRef={socket.wsRef}
      safeSend={socket.safeSend}
      pendingMessages={socket.pendingMessages}
      reconnectCount={socket.reconnectCount}
      sessions={[]}
    />
  );
}

async function flushPromises() {
  await act(async () => {});
}

// Wave 2 slice 4 (RUX-A-16 / RUX-A-17, mechanism 3): fetch-once
// effects whose sends are OPEN-guarded used to leave a surface
// frozen on whatever it rendered during a drop — permanently, until
// the user left and re-entered. Each surface now re-runs its fetch
// when the socket layer's reconnectCount changes.
describe('Refetch on reconnect (Wave 2 slice 4)', () => {
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

  const emitStatus = (status) => emitEngineMessage(JSON.stringify({ type: '_ws_status', status }));

  const sentTypes = () => electronAPI.sendEngineMessage.mock.calls.map(([raw]) => JSON.parse(raw).type);

  it('SettingsPanel: a section opened while offline refetches on reconnect and renders the reply (RUX-A-16)', async () => {
    render(
      <SocketProvider>
        <SettingsHarness />
      </SocketProvider>
    );
    await flushPromises();

    // Tools section is open, socket down: its OPEN-guarded fetch
    // never went out. (Wave 3 slice 1: the section now shows its
    // offline load state — the "No tools registered." this used to
    // assert was the RUX-C-06 false empty, rendered before any
    // answer existed.)
    expect(sentTypes()).not.toContain('TOOL_LIST_REQUEST');
    expect(screen.queryByText('No tools registered.')).not.toBeInTheDocument();
    expect(screen.getByTestId('tools-load-state')).toHaveAttribute('data-load-display', 'offline');

    emitStatus('connected');
    await flushPromises();

    // The fetch re-fired on reconnect.
    expect(sentTypes()).toContain('TOOL_LIST_REQUEST');
    expect(sentTypes()).toContain('APPROVAL_GRANTS_REQUEST');

    emitEngineMessage(JSON.stringify({
      type: 'TOOL_LIST_RESPONSE',
      payload: { tools: [{ function: { name: 'web_search' }, risk: 'read', effective_policy: 'allow' }] },
    }));
    await flushPromises();

    expect(screen.getByText('web_search')).toBeInTheDocument();
    expect(screen.queryByText('No tools registered.')).not.toBeInTheDocument();
  });

  it('UsageSettingsPanel: mounted while offline, it queries the ledger on reconnect and renders the totals (RUX-A-17)', async () => {
    render(
      <SocketProvider>
        <UsageHarness />
      </SocketProvider>
    );
    await flushPromises();
    expect(sentTypes()).not.toContain('USAGE_LEDGER_QUERY_REQUEST');

    emitStatus('connected');
    await flushPromises();
    expect(sentTypes()).toContain('USAGE_LEDGER_QUERY_REQUEST');

    emitEngineMessage(JSON.stringify({
      type: 'USAGE_LEDGER_QUERY_RESPONSE',
      payload: {
        totals: { total_cost_usd: 1.23, input_tokens: 10, output_tokens: 5, by_model: {}, by_agent: {} },
      },
    }));
    await flushPromises();
    expect(document.body.textContent).toContain('$1.23');
  });

  it('GlobalArtifactsView: mounted while offline, it re-queries on reconnect and renders the artifacts (RUX-A-17)', async () => {
    render(
      <SocketProvider>
        <ArtifactsHarness />
      </SocketProvider>
    );
    await flushPromises();
    expect(sentTypes()).not.toContain('ARTIFACT_LIST_REQUEST');

    emitStatus('connected');
    await flushPromises();
    expect(sentTypes()).toContain('ARTIFACT_LIST_REQUEST');

    emitEngineMessage(JSON.stringify({
      type: 'ARTIFACT_LIST_RESPONSE',
      payload: {
        global: true,
        artifacts: [{
          id: 'art-1',
          session_id: 'sess-1',
          kind: 'file',
          title: 'Quarterly Report.pdf',
          file_path: '/tmp/Quarterly Report.pdf',
          created_at: 1700000000000,
        }],
      },
    }));
    await flushPromises();
    expect(screen.getByText('Quarterly Report.pdf')).toBeInTheDocument();
  });
});
