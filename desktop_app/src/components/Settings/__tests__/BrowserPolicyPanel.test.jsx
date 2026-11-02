import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import BrowserPolicyPanel from '../BrowserPolicyPanel';
import { useAetherSocket } from '../../../contexts/SocketContext';

jest.mock('../../../contexts/SocketContext', () => ({
  useAetherSocket: jest.fn(),
}));

function makeWsRef() {
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
  const simulateMessage = (data) => {
    messageListeners.forEach((cb) => cb({ data: JSON.stringify(data) }));
  };
  useAetherSocket.mockReturnValue({ wsRef, sendMessage: null });
  return { wsRef, simulateMessage };
}

const PRESET = {
  enabled: true,
  vault_autofill: false,
  envelopes_enabled: true,
  categories: {
    payments: 'always_confirm',
    sends_publishes: 'always_confirm',
    deletes: 'always_confirm',
    account_changes: 'always_confirm',
  },
};

function renderPanel() {
  const { wsRef, simulateMessage } = makeWsRef();
  render(<BrowserPolicyPanel wsRef={wsRef} />);
  const sent = () => wsRef.current.send.mock.calls.map((c) => JSON.parse(c[0]));
  expect(sent().some((m) => m.type === 'BROWSER_POLICY_GET_REQUEST')).toBe(true);
  act(() => {
    simulateMessage({ type: 'BROWSER_POLICY_GET_RESPONSE', payload: PRESET });
  });
  return { wsRef, simulateMessage, sent };
}

describe('BrowserPolicyPanel (E8-6.2)', () => {
  test('renders the shipped preset: every category Always confirm', () => {
    renderPanel();
    expect(screen.getByText('Browser Agent Policy')).toBeInTheDocument();
    for (const id of ['payments', 'sends_publishes', 'deletes', 'account_changes']) {
      expect(screen.getByTestId(`browser-policy-mode-${id}`)).toHaveValue('always_confirm');
    }
    expect(screen.getByTestId('browser-policy-enabled')).toBeChecked();
    expect(screen.getByTestId('browser-policy-envelopes')).toBeChecked();
    expect(screen.getByTestId('browser-policy-vault-autofill')).not.toBeChecked();
    // Payments carry the individual-confirmation note.
    expect(screen.getByText(/payments are always confirmed individually/i)).toBeInTheDocument();
  });

  test('changing a category sends a partial SET with only that category', () => {
    const { wsRef, simulateMessage } = renderPanel();
    wsRef.current.send.mockClear();
    fireEvent.change(screen.getByTestId('browser-policy-mode-sends_publishes'), {
      target: { value: 'allow_with_envelope' },
    });
    const sent = wsRef.current.send.mock.calls.map((c) => JSON.parse(c[0]));
    const setMsg = sent.find((m) => m.type === 'BROWSER_POLICY_SET_REQUEST');
    expect(setMsg).toBeTruthy();
    expect(setMsg.payload).toEqual({ categories: { sends_publishes: 'allow_with_envelope' } });
    act(() => {
      simulateMessage({
        type: 'BROWSER_POLICY_SET_RESPONSE',
        payload: {
          success: true,
          policy: {
            ...PRESET,
            categories: { ...PRESET.categories, sends_publishes: 'allow_with_envelope' },
          },
        },
      });
    });
    expect(screen.getByTestId('browser-policy-mode-sends_publishes')).toHaveValue('allow_with_envelope');
  });

  test('flipping the master toggle sends {enabled: false}', () => {
    const { wsRef } = renderPanel();
    wsRef.current.send.mockClear();
    fireEvent.click(screen.getByTestId('browser-policy-enabled'));
    const sent = wsRef.current.send.mock.calls.map((c) => JSON.parse(c[0]));
    const setMsg = sent.find((m) => m.type === 'BROWSER_POLICY_SET_REQUEST');
    expect(setMsg.payload).toEqual({ enabled: false });
  });

  test('a rejected SET keeps the displayed policy and shows the error', () => {
    const { simulateMessage } = renderPanel();
    fireEvent.change(screen.getByTestId('browser-policy-mode-payments'), {
      target: { value: 'allow_freely' },
    });
    act(() => {
      simulateMessage({
        type: 'BROWSER_POLICY_SET_RESPONSE',
        payload: { success: false, policy: PRESET, error: 'Unknown mode.' },
      });
    });
    expect(screen.getByTestId('browser-policy-mode-payments')).toHaveValue('always_confirm');
    expect(screen.getByText('Unknown mode.')).toBeInTheDocument();
  });
});
