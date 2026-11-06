import React from 'react';
import { render, screen, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import BrowserPolicyPanel from '../BrowserPolicyPanel';
import { useAetherSocket } from '../../../contexts/SocketContext';

jest.mock('../../../contexts/SocketContext', () => ({
  useAetherSocket: jest.fn(),
}));

// Wave 3, D2-03: the panel's own note is the spec — "Payments are
// always confirmed individually". The payments category must not
// offer a mode the engine will not honour: no "Allow freely" (and
// no envelope mode, which the gate coerces to a confirmation
// anyway). Other categories keep the full mode list.
function makeWsRef() {
  let messageListeners = [];
  const wsRef = {
    current: {
      readyState: 1,
      send: jest.fn(),
      addEventListener: jest.fn((event, cb) => {
        if (event === 'message') messageListeners.push(cb);
      }),
      removeEventListener: jest.fn((event, cb) => {
        if (event === 'message') messageListeners = messageListeners.filter((l) => l !== cb);
      }),
    },
  };
  const simulateMessage = (data) => {
    messageListeners.forEach((cb) => cb({ data: JSON.stringify(data) }));
  };
  useAetherSocket.mockReturnValue({ wsRef, sendMessage: null });
  return { wsRef, simulateMessage };
}

function renderPanel(getPayload) {
  const { wsRef, simulateMessage } = makeWsRef();
  render(<BrowserPolicyPanel wsRef={wsRef} />);
  act(() => {
    simulateMessage({ type: 'BROWSER_POLICY_GET_RESPONSE', payload: getPayload });
  });
  return { wsRef, simulateMessage };
}

const optionLabels = (testId) =>
  Array.from(screen.getByTestId(testId).querySelectorAll('option')).map((o) => o.textContent);

describe('BrowserPolicyPanel payments modes (D2-03)', () => {
  test('payments offers Always confirm only; other categories keep all three modes', () => {
    renderPanel({
      enabled: true,
      vault_autofill: false,
      envelopes_enabled: true,
      categories: {
        payments: 'always_confirm',
        sends_publishes: 'always_confirm',
        deletes: 'always_confirm',
        account_changes: 'always_confirm',
      },
    });
    expect(optionLabels('browser-policy-mode-payments')).toEqual(['Always confirm']);
    for (const id of ['sends_publishes', 'deletes', 'account_changes']) {
      expect(optionLabels(`browser-policy-mode-${id}`)).toEqual([
        'Always confirm',
        'Allow with envelope',
        'Allow freely',
      ]);
    }
  });

  test('a legacy stored allow_freely for payments displays as Always confirm', () => {
    renderPanel({
      enabled: true,
      vault_autofill: false,
      envelopes_enabled: true,
      categories: {
        payments: 'allow_freely',
        sends_publishes: 'allow_freely',
        deletes: 'always_confirm',
        account_changes: 'always_confirm',
      },
    });
    expect(screen.getByTestId('browser-policy-mode-payments')).toHaveValue('always_confirm');
    expect(screen.getByTestId('browser-policy-mode-sends_publishes')).toHaveValue('allow_freely');
  });
});
