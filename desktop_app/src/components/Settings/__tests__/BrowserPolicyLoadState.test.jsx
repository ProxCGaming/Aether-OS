/** Wave 3 slice 1 (RUX-C-02): the Browser Agent policy surface must
 * never render the hard-coded PRESET as the live policy. Until the
 * GET reply lands it shows a load state — loading, offline, or
 * error — with no policy controls on screen at all. */
import React from 'react';
import { render, screen, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import BrowserPolicyPanel from '../BrowserPolicyPanel';
import { useAetherSocket } from '../../../contexts/SocketContext';

jest.mock('../../../contexts/SocketContext', () => ({
  useAetherSocket: jest.fn(),
}));

function makeHarness({ isConnected = true } = {}) {
  let messageListeners = [];
  const wsRef = {
    current: {
      readyState: 1, // WebSocket.OPEN
      send: jest.fn(),
      addEventListener: jest.fn((event, cb) => {
        if (event === 'message') messageListeners.push(cb);
      }),
      removeEventListener: jest.fn((event, cb) => {
        if (event === 'message') messageListeners = messageListeners.filter((l) => l !== cb);
      }),
    },
  };
  useAetherSocket.mockReturnValue({ wsRef, sendMessage: null, isConnected });
  const simulateMessage = (data) => {
    act(() => {
      messageListeners.forEach((cb) => cb({ data: JSON.stringify(data) }));
    });
  };
  return { wsRef, simulateMessage };
}

describe('BrowserPolicyPanel load states (RUX-C-02)', () => {
  test('before the GET reply: loading state, and NO preset values rendered as live policy', () => {
    const { wsRef } = makeHarness();
    render(<BrowserPolicyPanel wsRef={wsRef} />);
    const row = screen.getByTestId('browser-policy-load-state');
    expect(row).toHaveAttribute('data-load-display', 'loading');
    expect(row).toHaveTextContent(/Loading your saved browser policy/);
    // The preset's controls must not be on screen at all.
    expect(screen.queryByTestId('browser-policy-enabled')).not.toBeInTheDocument();
    expect(screen.queryByTestId('browser-policy-mode-payments')).not.toBeInTheDocument();
  });

  test('while disconnected: the offline state shows instead of the policy', () => {
    const { wsRef } = makeHarness({ isConnected: false });
    render(<BrowserPolicyPanel wsRef={wsRef} />);
    const row = screen.getByTestId('browser-policy-load-state');
    expect(row).toHaveAttribute('data-load-display', 'offline');
    expect(row).toHaveTextContent(/Waiting for the engine/);
    expect(screen.queryByTestId('browser-policy-enabled')).not.toBeInTheDocument();
  });

  test('an answered policy renders the ENGINE values, not the preset', () => {
    const { wsRef, simulateMessage } = makeHarness();
    render(<BrowserPolicyPanel wsRef={wsRef} />);
    simulateMessage({
      type: 'BROWSER_POLICY_GET_RESPONSE',
      payload: {
        enabled: false,
        vault_autofill: true,
        envelopes_enabled: false,
        categories: {
          payments: 'allow_freely',
          sends_publishes: 'always_confirm',
          deletes: 'always_confirm',
          account_changes: 'always_confirm',
        },
      },
    });
    expect(screen.queryByTestId('browser-policy-load-state')).not.toBeInTheDocument();
    // Every value differs from the preset somewhere: preset is
    // enabled=true / envelopes=true / autofill=false / payments
    // always_confirm — the reply must be what renders.
    expect(screen.getByTestId('browser-policy-enabled')).not.toBeChecked();
    expect(screen.getByTestId('browser-policy-envelopes')).not.toBeChecked();
    expect(screen.getByTestId('browser-policy-vault-autofill')).toBeChecked();
    expect(screen.getByTestId('browser-policy-mode-payments')).toHaveValue('allow_freely');
  });

  test('a failed GET is an error state, never the preset', () => {
    const { wsRef, simulateMessage } = makeHarness();
    render(<BrowserPolicyPanel wsRef={wsRef} />);
    simulateMessage({
      type: 'BROWSER_POLICY_GET_RESPONSE',
      payload: { success: false, error: 'Policy store unavailable' },
    });
    const row = screen.getByTestId('browser-policy-load-state');
    expect(row).toHaveAttribute('data-load-display', 'error');
    expect(screen.getByText('Policy store unavailable')).toBeInTheDocument();
    expect(screen.queryByTestId('browser-policy-enabled')).not.toBeInTheDocument();
  });
});
