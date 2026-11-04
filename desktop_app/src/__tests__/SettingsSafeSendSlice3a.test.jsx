import React from 'react';
import { render, screen, fireEvent, act, within } from '@testing-library/react';
import '@testing-library/jest-dom';
import { SocketProvider, useAetherSocket } from '../contexts/SocketContext';
import SettingsPanel from '../SettingsPanel';
import UsageSettingsPanel from '../components/Settings/UsageSettingsPanel';

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

// Wires the panels to the REAL SocketContext exactly the way App
// does: the facade wsRef plus the safeSend / pendingMessages props.
function SettingsHarness({ captureRef }) {
  const socket = useAetherSocket();
  captureRef.current = socket;
  return (
    <SettingsPanel
      wsRef={socket.wsRef}
      safeSend={socket.safeSend}
      pendingMessages={socket.pendingMessages}
      providers={[]}
      localModels={[]}
    />
  );
}

function UsageHarness({ captureRef }) {
  const socket = useAetherSocket();
  captureRef.current = socket;
  return (
    <UsageSettingsPanel
      wsRef={socket.wsRef}
      safeSend={socket.safeSend}
      pendingMessages={socket.pendingMessages}
      initialTotals={{ by_model: {}, by_agent: {}, total_cost_usd: 0, input_tokens: 0, output_tokens: 0 }}
    />
  );
}

async function flushPromises() {
  await act(async () => {});
}

// Wave 2 slice 3a: the Settings-side user actions from Lane A —
// RUX-A-10 (tool policy), A-11 (grant revoke), A-12 (snip hotkey
// persist), A-14 (skill toggle), A-15 (voice sliders), A-20 (daily
// budget) and A-22 (speech dropdowns). Before the fix, each either
// hit a readyState guard and was silently discarded while the
// engine was unreachable, or queued with a controlled display that
// snapped back and ghost-applied on reconnect. Now every one goes
// through safeSend / the facade queue AND shows its pending state
// from the real queue until the engine's reply lands.
describe('Settings safeSend conversions (Wave 2 slice 3a)', () => {
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

  const emitEngineMessage = (raw) => {
    act(() => {
      [...listeners].forEach((cb) => cb(raw));
    });
  };

  const feed = (type, payload) => emitEngineMessage(JSON.stringify({ type, payload }));

  const sentOfType = (type) => electronAPI.sendEngineMessage.mock.calls
    .map(([raw]) => { try { return JSON.parse(raw); } catch (e) { return null; } })
    .filter((p) => p && p.type === type);

  const queuedOfType = (type) => captureRef.current.pendingMessages
    .filter((m) => m.type === type);

  const connect = async () => {
    emitEngineMessage(JSON.stringify({ type: '_ws_status', status: 'connected' }));
    await flushPromises();
  };

  function renderSettings() {
    render(
      <SocketProvider>
        <SettingsHarness captureRef={captureRef} />
      </SocketProvider>
    );
  }

  const TOOL_AT_DEFAULT = {
    function: { name: 'read_file', description: 'Read a file.' },
    risk: 'read_only',
    always_allow_permitted: true,
    effective_policy: 'allow',
    policy_is_default: true,
    policy: 'Require Approval',
  };

  it('A-10: an offline tool-policy pick queues, names the pending policy, and applies on the reply', async () => {
    renderSettings();
    await flushPromises();
    expect(captureRef.current.isConnected).toBe(false);

    fireEvent.click(screen.getByText('Tools'));
    feed('TOOL_LIST_RESPONSE', { tools: [TOOL_AT_DEFAULT], approval_mode: 'ask' });
    feed('APPROVAL_GRANTS_RESPONSE', { grants: [] });

    fireEvent.click(screen.getByRole('button', { name: 'Always Allow' }));
    fireEvent.click(screen.getByText('Deny'));

    // Not transmitted, not dropped: exactly one policy change waits
    // in the real queue, and the control says so, naming the policy.
    expect(sentOfType('TOOL_POLICY_SET_REQUEST')).toHaveLength(0);
    expect(queuedOfType('TOOL_POLICY_SET_REQUEST')).toEqual([
      { type: 'TOOL_POLICY_SET_REQUEST', payload: { tool_name: 'read_file', policy: 'Deny' } },
    ]);
    expect(screen.getByText(/Policy change to Deny queued — will apply when the engine reconnects/)).toBeInTheDocument();

    await connect();
    const delivered = sentOfType('TOOL_POLICY_SET_REQUEST');
    expect(delivered).toHaveLength(1);
    expect(delivered[0].payload).toEqual({ tool_name: 'read_file', policy: 'Deny' });
    expect(queuedOfType('TOOL_POLICY_SET_REQUEST')).toHaveLength(0);
    expect(screen.queryByText(/Policy change to Deny queued/)).not.toBeInTheDocument();

    // The reply drives the control to the new policy.
    feed('TOOL_LIST_RESPONSE', {
      tools: [{ ...TOOL_AT_DEFAULT, policy_is_default: false, policy: 'Deny' }],
      approval_mode: 'ask',
    });
    expect(screen.getByRole('button', { name: 'Deny' })).toBeInTheDocument();
  });

  it('A-11: an offline grant revoke queues and the grant stays truthfully listed until the reply', async () => {
    renderSettings();
    await flushPromises();

    fireEvent.click(screen.getByText('Tools'));
    feed('TOOL_LIST_RESPONSE', { tools: [], approval_mode: 'ask' });
    feed('APPROVAL_GRANTS_RESPONSE', {
      grants: [
        { id: 'g1', prefix: '/home/user/proj', ceiling: 'write', tool_name: 'edit_file' },
        { id: 'g2', prefix: '/home/user/notes.txt', ceiling: 'read_only', tool_name: null },
      ],
    });

    fireEvent.click(screen.getAllByRole('button', { name: 'Revoke' })[0]);

    expect(sentOfType('APPROVAL_GRANT_REVOKE_REQUEST')).toHaveLength(0);
    expect(queuedOfType('APPROVAL_GRANT_REVOKE_REQUEST')).toEqual([
      { type: 'APPROVAL_GRANT_REVOKE_REQUEST', payload: { id: 'g1' } },
    ]);
    // The grant is still in force, so it stays listed — but its
    // button now shows the queued state instead of pretending.
    expect(screen.getByText('/home/user/proj')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Revoke queued' })).toBeDisabled();
    expect(screen.getByText(/will apply when the engine reconnects/)).toBeInTheDocument();

    await connect();
    expect(sentOfType('APPROVAL_GRANT_REVOKE_REQUEST')).toHaveLength(1);

    feed('APPROVAL_GRANT_REVOKE_RESPONSE', {
      id: 'g1',
      revoked: true,
      grants: [{ id: 'g2', prefix: '/home/user/notes.txt', ceiling: 'read_only', tool_name: null }],
    });
    expect(screen.queryByText('/home/user/proj')).not.toBeInTheDocument();
    expect(screen.getByText('/home/user/notes.txt')).toBeInTheDocument();
  });

  it('A-14: an offline skill toggle queues with no false state, and the refreshed list confirms it', async () => {
    renderSettings();
    await flushPromises();

    fireEvent.click(screen.getByText('Skills'));
    feed('SKILL_LIST_RESPONSE', {
      skills: [{ name: 'alpha', description: 'A skill', source: 'local', slash_commands: ['/alpha'], required_tools: [], enabled: true }],
    });

    fireEvent.click(screen.getByText('Disable skill'));

    expect(sentOfType('SKILL_TOGGLE_REQUEST')).toHaveLength(0);
    expect(queuedOfType('SKILL_TOGGLE_REQUEST')).toEqual([
      { type: 'SKILL_TOGGLE_REQUEST', payload: { name: 'alpha', enabled: false } },
    ]);
    // No fake state: the skill still presents as enabled (the button
    // still offers Disable, now disabled itself) and the queued note
    // carries the honesty.
    expect(screen.getByRole('button', { name: 'Disable skill' })).toBeDisabled();
    expect(screen.getByText(/Disable queued — will apply when the engine reconnects/)).toBeInTheDocument();

    await connect();
    expect(sentOfType('SKILL_TOGGLE_REQUEST')).toHaveLength(1);

    feed('SKILL_TOGGLE_RESPONSE', {
      success: true,
      skills: [{ name: 'alpha', description: 'A skill', source: 'local', slash_commands: ['/alpha'], required_tools: [], enabled: false }],
    });
    expect(screen.getByRole('button', { name: 'Enable skill' })).not.toBeDisabled();
    expect(screen.queryByText(/Disable queued/)).not.toBeInTheDocument();
  });

  it('A-15: an offline VAD slider drag shows the intent and names the queued change', async () => {
    renderSettings();
    await flushPromises();

    fireEvent.click(screen.getByText('Voice Tuning'));
    const slider = screen.getByTestId('vad-threshold-slider');
    fireEvent.change(slider, { target: { value: '0.7' } });

    // The slider keeps the user's intent on display (E6-F15
    // optimistic pattern) and the edit is in the real queue — not
    // discarded as before.
    expect(screen.getByTestId('vad-threshold-value')).toHaveTextContent('0.70');
    const queued = queuedOfType('CONFIG_UPDATE_REQUEST')
      .filter((m) => m.payload && m.payload.config && 'vad_threshold' in m.payload.config);
    expect(queued).toEqual([
      { type: 'CONFIG_UPDATE_REQUEST', payload: { config: { vad_threshold: 0.7 } } },
    ]);
    expect(screen.getByTestId('vad-threshold-queued')).toHaveTextContent(
      /VAD threshold change to 0\.70 queued — will apply when the engine reconnects/,
    );

    await connect();
    const delivered = sentOfType('CONFIG_UPDATE_REQUEST')
      .filter((p) => p.payload && p.payload.config && 'vad_threshold' in p.payload.config);
    expect(delivered).toHaveLength(1);
    expect(screen.queryByTestId('vad-threshold-queued')).not.toBeInTheDocument();
  });

  it('A-12: an offline snip-hotkey change registers locally and queues its engine persist', async () => {
    renderSettings();
    await flushPromises();

    fireEvent.click(screen.getByText('Advanced'));
    const input = screen.getByTestId('snip-hotkey-input');
    fireEvent.change(input, { target: { value: 'Ctrl+Alt+X' } });
    await act(async () => {
      fireEvent.blur(input);
    });
    await flushPromises();

    // Registration succeeded in the main process, and the persist
    // is waiting in the real queue instead of being discarded.
    expect(electronAPI.registerSnipShortcut).toHaveBeenCalledWith('Ctrl+Alt+X');
    expect(queuedOfType('CONFIG_UPDATE_REQUEST')).toEqual([
      { type: 'CONFIG_UPDATE_REQUEST', payload: { config: { snip_hotkey: 'Ctrl+Alt+X' } } },
    ]);
    expect(screen.getByTestId('snip-hotkey-queued')).toHaveTextContent(
      /Snip hotkey change to Ctrl\+Alt\+X queued — will be saved when the engine reconnects/,
    );

    await connect();
    expect(sentOfType('CONFIG_UPDATE_REQUEST')).toHaveLength(1);
    expect(screen.queryByTestId('snip-hotkey-queued')).not.toBeInTheDocument();
  });

  it('A-22: an offline speech pick stays on the current value, names the queued change, and applies on the config reply — no ghost', async () => {
    renderSettings();
    await flushPromises();

    fireEvent.click(screen.getByText('Models & Tasks'));
    feed('CONFIG_GET_RESPONSE', {
      config: { speech_stt_model: 'medium', transcription_stt_model: 'base', speech_tts_voice: 'bm_george' },
    });

    const wrapper = screen.getByTestId('speech-stt-select');
    expect(within(wrapper).getByText('medium')).toBeInTheDocument();
    fireEvent.click(wrapper.querySelector('button'));
    fireEvent.click(within(wrapper).getByText('large-v3'));

    // The send queued through the facade; the controlled select
    // still shows the value actually in force, and the queued hint
    // names the pending one.
    expect(queuedOfType('CONFIG_UPDATE_REQUEST')).toEqual([
      { type: 'CONFIG_UPDATE_REQUEST', payload: { config: { speech_stt_model: 'large-v3' } } },
    ]);
    expect(within(wrapper).getByText('medium')).toBeInTheDocument();
    expect(screen.getByTestId('speech-stt-queued')).toHaveTextContent(
      /Speech-to-text change to large-v3 queued — will apply when the engine reconnects/,
    );

    await connect();
    expect(sentOfType('CONFIG_UPDATE_REQUEST')).toHaveLength(1);
    // Flushed but not yet confirmed: the hint clears with the queue,
    // and the select STILL shows the in-force value — the change is
    // no longer a ghost because it was announced while pending, and
    // it only displays once the config reply lands.
    expect(screen.queryByTestId('speech-stt-queued')).not.toBeInTheDocument();
    expect(within(wrapper).getByText('medium')).toBeInTheDocument();

    feed('CONFIG_UPDATE_RESPONSE', {
      success: true,
      config: { speech_stt_model: 'large-v3', transcription_stt_model: 'base', speech_tts_voice: 'bm_george' },
    });
    expect(within(wrapper).getByText('large-v3')).toBeInTheDocument();
  });

  it('A-20: an offline budget save queues with no false Saved state, and the response confirms it', async () => {
    render(
      <SocketProvider>
        <UsageHarness captureRef={captureRef} />
      </SocketProvider>
    );
    await flushPromises();
    expect(captureRef.current.isConnected).toBe(false);

    fireEvent.change(screen.getByTestId('daily-budget-input'), { target: { value: '15.50' } });
    fireEvent.click(screen.getByTestId('daily-budget-save-btn'));

    expect(sentOfType('DAILY_BUDGET_SET_REQUEST')).toHaveLength(0);
    expect(queuedOfType('DAILY_BUDGET_SET_REQUEST')).toEqual([
      { type: 'DAILY_BUDGET_SET_REQUEST', payload: { daily_budget_usd: 15.5 } },
    ]);
    // No false confirmation: the button does not claim Saved, it is
    // inert against stacking, and the note names the pending change.
    expect(screen.queryByText('Saved')).not.toBeInTheDocument();
    expect(screen.getByTestId('daily-budget-save-btn')).toBeDisabled();
    expect(screen.getByTestId('daily-budget-queued')).toHaveTextContent(
      /Daily budget change to \$15\.5 queued — will apply when the engine reconnects/,
    );

    await connect();
    expect(sentOfType('DAILY_BUDGET_SET_REQUEST')).toHaveLength(1);
    expect(screen.queryByTestId('daily-budget-queued')).not.toBeInTheDocument();

    feed('DAILY_BUDGET_SET_RESPONSE', { daily_budget_usd: 15.5 });
    expect(screen.getByTestId('daily-budget-save-btn')).toHaveTextContent('Saved');
  });
});
