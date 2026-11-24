/** Wave 3, D2-04…D2-08 / D2-20 / D2-21 — SettingsPanel mechanics.
 *
 * - D2-04: fossil state/fetches for the extracted panels are gone:
 *   opening Memory/MCP/Plugins/Agents no longer makes SettingsPanel
 *   re-request data the panels fetch themselves (its live fetches —
 *   tools, config — stay).
 * - D2-05: a failed CONFIG_GET is an error state on the
 *   config-gated tabs, with a retry — not "Loading…" forever.
 * - D2-06: the optimistic selectors (approval mode, reasoning
 *   effort, auxiliary model) roll back and surface the refusal;
 *   the Advanced tab renders its own config error and rolls its
 *   optimistic checkbox back.
 * - D2-07: a diagnostics run has the codebase's 35 s no-answer
 *   rule, and a failure-shaped reply is an error, not a silent
 *   "Status Unknown".
 * - D2-08: Voice sliders commit on release, not per drag tick.
 * - D2-20: an armed settings reset disarms itself.
 * - D2-21: the snip hotkey applies only when changed, and a
 *   missing registration API is a failure — never a silent save.
 */
import React from 'react';
import { render, screen, fireEvent, act, within } from '@testing-library/react';
import '@testing-library/jest-dom';
import SettingsPanel from '../SettingsPanel';

const mockContextSendMessage = jest.fn();
jest.mock('../contexts/SocketContext', () => ({
  useAetherSocket: () => ({
    wsRef: { current: { addEventListener: jest.fn(), removeEventListener: jest.fn() } },
    sendMessage: mockContextSendMessage,
  }),
}));

function makeWsRef() {
  // The real socket delivers every frame to EVERY listener
  // (SettingsPanel and the panels it renders — SpeechSettingsPanel
  // on the Models tab — each subscribe), so the harness keeps them
  // all and feeds them all.
  let messageHandlers = [];
  const wsRef = {
    current: {
      readyState: 1,
      send: jest.fn(),
      addEventListener: jest.fn((event, handler) => { messageHandlers.push(handler); }),
      removeEventListener: jest.fn((event, handler) => {
        messageHandlers = messageHandlers.filter((h) => h !== handler);
      }),
    },
  };
  const feed = (type, payload) => {
    act(() => {
      messageHandlers.forEach((handler) => {
        handler({ data: JSON.stringify({ type, payload }) });
      });
    });
  };
  const sent = () => wsRef.current.send.mock.calls.map(([raw]) => JSON.parse(raw));
  const sentTypes = () => sent().map((m) => m.type);
  return { wsRef, feed, sent, sentTypes };
}

const PROVIDERS = [
  { name: 'openai', display_name: 'OpenAI', has_key: true, is_default: true, default_model: 'gpt-4o', models: ['gpt-4o'] },
];

beforeEach(() => {
  localStorage.clear();
  mockContextSendMessage.mockClear();
  delete window.electronAPI;
});

afterEach(() => {
  delete window.electronAPI;
  jest.useRealTimers();
});

describe('D2-04 — duplicate fetches for extracted panels are gone', () => {
  test('Memory menu: SettingsPanel sends no memory requests (MemoryManagement self-fetches)', () => {
    const { wsRef, sentTypes } = makeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={[]} />);
    fireEvent.click(screen.getByText('Memory & Storage'));
    expect(sentTypes()).not.toContain('MEMORY_EPISODES_REQUEST');
    expect(sentTypes()).not.toContain('MEMORY_GRAPH_REQUEST');
    // The panel itself still fetches through the context seam.
    const contextTypes = mockContextSendMessage.mock.calls.map((c) => c[0]);
    expect(contextTypes).toContain('MEMORY_EPISODES_REQUEST');
  });

  test('Agents menu: no AGENT_LIST from SettingsPanel; its live fetches stay', () => {
    const { wsRef, sentTypes } = makeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={[]} />);
    fireEvent.click(screen.getByText('Agents'));
    expect(sentTypes()).not.toContain('AGENT_LIST_REQUEST');
    expect(sentTypes()).toContain('TOOL_LIST_REQUEST');
    expect(sentTypes()).toContain('CONFIG_GET_REQUEST');
  });

  test('MCP and Plugins menus: no list requests from SettingsPanel', () => {
    const { wsRef, sentTypes } = makeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={[]} />);
    fireEvent.click(screen.getByText('MCP Servers'));
    expect(sentTypes()).not.toContain('MCP_SERVER_LIST_REQUEST');
    fireEvent.click(screen.getByText('Plugins'));
    expect(sentTypes()).not.toContain('PLUGIN_LIST_REQUEST');
  });
});

describe('D2-05 — config load failure is an error state with a retry', () => {
  test('Models tab: a failed CONFIG_GET shows the error and Retry re-asks', () => {
    const { wsRef, feed, sentTypes } = makeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={[]} />);
    fireEvent.click(screen.getByText('Models & Tasks'));
    feed('CONFIG_GET_RESPONSE', { success: false, error: 'config store unavailable' });
    const row = screen.getByTestId('models-config-load-state');
    expect(row).toHaveAttribute('data-load-display', 'error');
    expect(row).toHaveTextContent(/config store unavailable/);

    wsRef.current.send.mockClear();
    fireEvent.click(screen.getByText('Retry'));
    expect(sentTypes()).toContain('CONFIG_GET_REQUEST');
    feed('CONFIG_GET_RESPONSE', { config: { reasoning_effort: 'standard' } });
    expect(screen.queryByTestId('models-config-load-state')).not.toBeInTheDocument();
  });

  test('Voice tab: a failed CONFIG_GET shows the error there too', () => {
    const { wsRef, feed } = makeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={[]} />);
    fireEvent.click(screen.getByText('Voice Tuning'));
    feed('CONFIG_GET_RESPONSE', { success: false, error: 'config store unavailable' });
    expect(screen.getByTestId('voice-config-load-state')).toHaveAttribute('data-load-display', 'error');
  });
});

describe('23 Nov 2026 — config-gated tabs opened first still get their config', () => {
  // Regression: Computer Use / Conversation / Capabilities were
  // missing from the per-menu CONFIG_GET list, so opening one of
  // them FIRST in a Settings visit stranded it on its load state
  // forever (Computer Use: "Loading your saved computer use
  // settings…"; Capabilities: "Waiting for the applied config").
  // The mount fetch + the extended per-menu list both cover it.
  test('Computer Use opened first: CONFIG_GET is sent and the tab renders when the answer lands', () => {
    const { wsRef, feed, sentTypes } = makeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={[]} />);
    fireEvent.click(screen.getByText('Computer Use'));
    expect(sentTypes()).toContain('CONFIG_GET_REQUEST');
    expect(screen.getByTestId('computer-use-config-load-state')).toBeInTheDocument();
    feed('CONFIG_GET_RESPONSE', { config: {} });
    expect(screen.queryByTestId('computer-use-config-load-state')).not.toBeInTheDocument();
    expect(screen.getByTestId('computer-use-enabled-toggle')).toBeInTheDocument();
  });

  test('Computer Use opened first: a failed CONFIG_GET is an error with a retry, not endless loading', () => {
    const { wsRef, feed, sentTypes } = makeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={[]} />);
    fireEvent.click(screen.getByText('Computer Use'));
    feed('CONFIG_GET_RESPONSE', { success: false, error: 'config store unavailable' });
    const row = screen.getByTestId('computer-use-config-load-state');
    expect(row).toHaveAttribute('data-load-display', 'error');
    expect(row).toHaveTextContent(/config store unavailable/);
    wsRef.current.send.mockClear();
    fireEvent.click(screen.getByText('Retry'));
    expect(sentTypes()).toContain('CONFIG_GET_REQUEST');
  });

  test('Capabilities opened first: the applied config lands and the feature-state rows leave the waiting state', () => {
    const { wsRef, feed, sentTypes } = makeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={[]} />);
    fireEvent.click(screen.getByText('Capabilities'));
    expect(sentTypes()).toContain('CONFIG_GET_REQUEST');
    feed('CONFIG_GET_RESPONSE', { config: { memory_backend: 'native', model_routing: 'off' } });
    expect(screen.getByTestId('feature-state-memory')).toHaveTextContent(/Configured backend: native/);
    expect(screen.getByTestId('feature-state-model_routing')).toHaveTextContent(/Off: specialists run/);
  });
});

describe('D2-06 — optimistic selectors roll back on refusal', () => {
  test('approval mode: refusal restores the confirmed mode and names the error', () => {
    const { wsRef, feed } = makeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={[]} />);
    fireEvent.click(screen.getByText('Tools'));
    feed('TOOL_LIST_RESPONSE', { tools: [], approval_mode: 'ask' });

    fireEvent.click(screen.getByText('Ask for approval'));
    fireEvent.click(screen.getByText('Auto-approve writes'));
    expect(screen.getByText('Auto-approve writes')).toBeInTheDocument();

    feed('APPROVAL_MODE_SET_RESPONSE', {
      success: false, mode: 'ask', error: 'Approval mode could not be saved: disk full',
    });
    expect(screen.getByText('Ask for approval')).toBeInTheDocument();
    expect(screen.queryByText('Auto-approve writes')).not.toBeInTheDocument();
    expect(screen.getByText(/Approval mode could not be saved: disk full/)).toBeInTheDocument();
  });

  test('reasoning effort: refusal restores the value and localStorage', () => {
    const { wsRef, feed } = makeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={PROVIDERS} />);
    fireEvent.click(screen.getByText('Models & Tasks'));
    feed('CONFIG_GET_RESPONSE', { config: { reasoning_effort: 'standard', auxiliary_models: {} } });

    fireEvent.click(screen.getByText('Standard'));
    fireEvent.click(screen.getByText('Deep Thinking'));
    expect(localStorage.getItem('reasoning_effort')).toBe('high');

    feed('REASONING_EFFORT_SET_RESPONSE', { success: false, error: 'effort refused' });
    expect(screen.getByText('Standard')).toBeInTheDocument();
    expect(localStorage.getItem('reasoning_effort')).toBe('standard');
    expect(screen.getByText(/effort refused/)).toBeInTheDocument();
  });

  test('auxiliary model: refusal restores the confirmed override and names the error', () => {
    const { wsRef, feed } = makeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={PROVIDERS} />);
    fireEvent.click(screen.getByText('Models & Tasks'));
    feed('CONFIG_GET_RESPONSE', { config: { reasoning_effort: 'standard', auxiliary_models: {} } });

    fireEvent.click(screen.getByText('Use main model'));
    // The open menu is the only .ios-glass list; the global-default
    // dropdown's trigger also reads "gpt-4o", so pick inside it.
    fireEvent.click(within(document.querySelector('.ios-glass')).getByText('gpt-4o'));
    expect(screen.queryByText(/auto · use main model/)).not.toBeInTheDocument();

    feed('AUXILIARY_MODEL_SET_RESPONSE', { success: false, error: 'aux refused' });
    expect(screen.getByText(/auto · use main model/)).toBeInTheDocument();
    expect(screen.getByText(/aux refused/)).toBeInTheDocument();
  });

  test('Advanced tab: a refused config update renders in-tab and rolls the checkbox back', () => {
    const { wsRef, feed } = makeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={[]} />);
    fireEvent.click(screen.getByText('Advanced'));
    feed('CONFIG_GET_RESPONSE', { config: { disable_fallbacks: false, fallback_chain: [] } });

    const checkbox = screen.getByText('Disable Fallback System')
      .closest('div').parentElement.parentElement.querySelector('input[type="checkbox"]');
    fireEvent.click(checkbox);
    expect(checkbox).toBeChecked();

    feed('CONFIG_UPDATE_RESPONSE', { success: false, error: 'strict field refused' });
    expect(checkbox).not.toBeChecked();
    expect(screen.getByText(/strict field refused/)).toBeInTheDocument();
  });
});

describe('D2-07 — diagnostics run: timeout and failure shape', () => {
  test('a run with no answer resolves after 35 s with an error', () => {
    jest.useFakeTimers();
    const { wsRef } = makeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={[]} />);
    fireEvent.click(screen.getByText('Capabilities'));
    fireEvent.click(screen.getByText('Run Diagnostics Now'));
    expect(screen.getByText('Testing...')).toBeInTheDocument();
    act(() => { jest.advanceTimersByTime(35000); });
    expect(screen.getByText('Run Diagnostics Now')).toBeInTheDocument();
    expect(screen.getByTestId('diagnostics-run-error')).toHaveTextContent(/No answer from the engine in 35s/);
  });

  test('a response without a summary is a failure surface, not a silent Status Unknown', () => {
    const { wsRef, feed } = makeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={[]} />);
    fireEvent.click(screen.getByText('Capabilities'));
    fireEvent.click(screen.getByText('Run Diagnostics Now'));
    feed('ENVIRONMENT_DIAGNOSTIC_RESPONSE', {});
    expect(screen.getByText('Run Diagnostics Now')).toBeInTheDocument();
    expect(screen.getByTestId('diagnostics-run-error')).toHaveTextContent(/no result/i);
  });

  test('a healthy response still lands as before', () => {
    const { wsRef, feed } = makeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={[]} />);
    fireEvent.click(screen.getByText('Capabilities'));
    fireEvent.click(screen.getByText('Run Diagnostics Now'));
    feed('ENVIRONMENT_DIAGNOSTIC_RESPONSE', {
      summary: { failed_count: 0 },
      history: [{ timestamp: 1700000000, target_name: 'engine', status: 'OK', details: '' }],
    });
    expect(screen.getByText('System Healthy')).toBeInTheDocument();
    expect(screen.queryByTestId('diagnostics-run-error')).not.toBeInTheDocument();
  });
});

describe('D2-08 — Voice sliders commit on release', () => {
  const renderVoice = () => {
    const harness = makeWsRef();
    render(<SettingsPanel wsRef={harness.wsRef} providers={[]} />);
    fireEvent.click(screen.getByText('Voice Tuning'));
    harness.feed('CONFIG_GET_RESPONSE', {
      config: { vad_threshold: 0.5, silence_duration_ms: 700 },
    });
    harness.wsRef.current.send.mockClear();
    return harness;
  };

  test('dragging the VAD slider sends nothing until release, then one update', () => {
    const { sent } = renderVoice();
    const slider = screen.getByTestId('vad-threshold-slider');
    fireEvent.change(slider, { target: { value: '0.35' } });
    fireEvent.change(slider, { target: { value: '0.4' } });
    expect(screen.getByTestId('vad-threshold-value')).toHaveTextContent('0.40');
    expect(sent().filter((m) => m.type === 'CONFIG_UPDATE_REQUEST')).toHaveLength(0);
    fireEvent.pointerUp(slider);
    const updates = sent().filter(
      (m) => m.type === 'CONFIG_UPDATE_REQUEST' && m.payload?.config?.vad_threshold !== undefined,
    );
    expect(updates).toHaveLength(1);
    expect(updates[0].payload.config.vad_threshold).toBe(0.4);
  });

  test('the silence slider commits on release too', () => {
    const { sent } = renderVoice();
    const slider = screen.getByTestId('silence-duration-slider');
    fireEvent.change(slider, { target: { value: '450' } });
    expect(sent().filter((m) => m.type === 'CONFIG_UPDATE_REQUEST')).toHaveLength(0);
    fireEvent.pointerUp(slider);
    const updates = sent().filter(
      (m) => m.type === 'CONFIG_UPDATE_REQUEST' && m.payload?.config?.silence_duration_ms !== undefined,
    );
    expect(updates).toHaveLength(1);
    expect(updates[0].payload.config.silence_duration_ms).toBe(450);
  });
});

describe('D2-20 — an armed reset disarms itself', () => {
  test('the arm expires after 10 s without a confirming click', () => {
    jest.useFakeTimers();
    const { wsRef } = makeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={[]} />);
    fireEvent.click(screen.getByText('Advanced'));
    fireEvent.click(screen.getByRole('button', { name: 'Reset all settings' }));
    expect(screen.getByText('Click again to confirm reset')).toBeInTheDocument();
    act(() => { jest.advanceTimersByTime(10000); });
    expect(screen.getByRole('button', { name: 'Reset all settings' })).toBeInTheDocument();
  });

  test('leaving the tab disarms the reset', () => {
    const { wsRef } = makeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={[]} />);
    fireEvent.click(screen.getByText('Advanced'));
    fireEvent.click(screen.getByRole('button', { name: 'Reset all settings' }));
    expect(screen.getByText('Click again to confirm reset')).toBeInTheDocument();
    fireEvent.click(screen.getByText('About'));
    fireEvent.click(screen.getByText('Advanced'));
    expect(screen.getByRole('button', { name: 'Reset all settings' })).toBeInTheDocument();
  });
});

describe('D2-21 — snip hotkey: apply on change only; missing API is a failure', () => {
  const renderAdvanced = () => {
    const harness = makeWsRef();
    render(<SettingsPanel wsRef={harness.wsRef} providers={[]} />);
    fireEvent.click(screen.getByText('Advanced'));
    return harness;
  };

  test('blurring the field with an unchanged value registers and persists nothing', async () => {
    const registerSnipShortcut = jest.fn((s) => Promise.resolve({ ok: true, shortcut: s }));
    window.electronAPI = { registerSnipShortcut };
    const { sent } = renderAdvanced();
    const input = screen.getByTestId('snip-hotkey-input');
    await act(async () => { fireEvent.blur(input); });
    expect(registerSnipShortcut).not.toHaveBeenCalled();
    expect(sent().filter(
      (m) => m.type === 'CONFIG_UPDATE_REQUEST' && m.payload?.config?.snip_hotkey,
    )).toHaveLength(0);
  });

  test('without the registration API the new hotkey is NOT saved and the failure shows', async () => {
    window.electronAPI = {}; // shell without the preload bridge
    const { sent } = renderAdvanced();
    const input = screen.getByTestId('snip-hotkey-input');
    fireEvent.change(input, { target: { value: 'Ctrl+Alt+S' } });
    await act(async () => { fireEvent.blur(input); });
    expect(screen.getByTestId('snip-hotkey-error')).toHaveTextContent(/still your snip hotkey/);
    expect(input).toHaveValue('Control+S');
    expect(sent().filter(
      (m) => m.type === 'CONFIG_UPDATE_REQUEST' && m.payload?.config?.snip_hotkey,
    )).toHaveLength(0);
  });

  test('a confirmed registration still persists the new hotkey', async () => {
    const registerSnipShortcut = jest.fn((s) => Promise.resolve({ ok: true, shortcut: s }));
    window.electronAPI = { registerSnipShortcut };
    const { sent } = renderAdvanced();
    const input = screen.getByTestId('snip-hotkey-input');
    fireEvent.change(input, { target: { value: 'Ctrl+Alt+S' } });
    await act(async () => { fireEvent.blur(input); });
    expect(registerSnipShortcut).toHaveBeenCalledWith('Ctrl+Alt+S');
    const updates = sent().filter(
      (m) => m.type === 'CONFIG_UPDATE_REQUEST' && m.payload?.config?.snip_hotkey,
    );
    expect(updates).toHaveLength(1);
    expect(updates[0].payload.config.snip_hotkey).toBe('Ctrl+Alt+S');
  });
});
