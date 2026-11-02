import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import SettingsPanel from '../SettingsPanel';

// E6-F04 (renderer side): a successful CONFIG_RESET_RESPONSE used to
// only flip the "Settings were reset" message — the panel kept
// displaying the pre-reset values (Disable Fallbacks still checked,
// the cached reasoning effort still in force) until it was remounted.
// The CONFIG_GET / CONFIG_UPDATE handler in the same file applies the
// returned config to the controls; the reset reply must be applied
// the same way.

jest.mock('../contexts/SocketContext', () => ({
  useAetherSocket: () => ({
    wsRef: { current: { addEventListener: jest.fn(), removeEventListener: jest.fn() } },
    sendMessage: jest.fn(),
  }),
}));

function makeWsRef() {
  let messageHandler = null;
  const wsRef = {
    current: {
      readyState: 1,
      send: jest.fn(),
      addEventListener: jest.fn((event, handler) => { messageHandler = handler; }),
      removeEventListener: jest.fn(),
    },
  };
  return { wsRef, getHandler: () => messageHandler };
}

const PRE_RESET_CONFIG = {
  disable_fallbacks: true,
  fallback_chain: ['openai'],
  reasoning_effort: 'high',
  snip_hotkey: 'CommandOrControl+Shift+S',
  vad_threshold: 0.5,
  silence_duration_ms: 700,
  auxiliary_models: {},
};

// The fresh UserConfig defaults the engine saves and returns on reset.
const RESET_CONFIG = {
  disable_fallbacks: false,
  fallback_chain: [],
  reasoning_effort: 'standard',
  snip_hotkey: 'CommandOrControl+Shift+S',
  vad_threshold: 0.5,
  silence_duration_ms: 700,
  auxiliary_models: {},
};

function findInAncestors(startEl, selector) {
  let el = startEl;
  while (el && !el.querySelector(selector)) el = el.parentElement;
  return el ? el.querySelector(selector) : null;
}

function fallbackCheckbox() {
  return findInAncestors(
    screen.getByText('Disable Fallback System'), 'input[type="checkbox"]');
}

function fallbackChainInput() {
  return findInAncestors(
    screen.getByText('Fallback Provider Chain (Optional)'), 'input[type="text"]');
}

describe('SettingsPanel reset applies the returned defaults (E6-F04)', () => {
  beforeEach(() => {
    localStorage.clear();
    // The audit's renderer state: reasoning effort cached at "high".
    localStorage.setItem('reasoning_effort', 'high');
  });

  afterEach(() => {
    localStorage.clear();
  });

  it('live controls follow the reset values, not just the Done mark', () => {
    const { wsRef, getHandler } = makeWsRef();
    render(<SettingsPanel wsRef={wsRef} providers={[]} localModels={[]} />);
    fireEvent.click(screen.getByText('Advanced'));

    // The engine reports the pre-reset values on panel open.
    act(() => {
      getHandler()({
        data: JSON.stringify({
          type: 'CONFIG_GET_RESPONSE',
          payload: { config: PRE_RESET_CONFIG },
        }),
      });
    });
    expect(fallbackCheckbox()).toBeChecked();

    // Two-step reset, confirmed.
    fireEvent.click(screen.getByRole('button', { name: 'Reset all settings' }));
    fireEvent.click(screen.getByText('Click again to confirm reset'));
    expect(
      wsRef.current.send.mock.calls.map(([raw]) => JSON.parse(raw).type),
    ).toContain('CONFIG_RESET_REQUEST');

    act(() => {
      getHandler()({
        data: JSON.stringify({
          type: 'CONFIG_RESET_RESPONSE',
          payload: { success: true, config: RESET_CONFIG },
        }),
      });
    });

    expect(screen.getByText('Settings were reset to defaults.')).toBeInTheDocument();
    // The displayed values are the reset values now.
    expect(fallbackCheckbox()).not.toBeChecked();
    expect(fallbackChainInput().value).toBe('');
    expect(localStorage.getItem('reasoning_effort')).toBe('standard');
  });
});
