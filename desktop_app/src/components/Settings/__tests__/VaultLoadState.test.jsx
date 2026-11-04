/** Wave 3 slice 1 (RUX-C-05): the vault's two lists must distinguish
 * "not yet answered" from "answered empty". While a list is in
 * flight or the engine is down, the panel shows a load state —
 * never "No credentials saved yet" / "No authenticator codes yet" —
 * and a failure reply is an error state, not an empty list. */
import React from 'react';
import { render, screen, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import VaultPanel from '../VaultPanel';
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
  useAetherSocket.mockReturnValue({ wsRef, sendMessage: jest.fn(), isConnected });
  const simulateMessage = (data) => {
    act(() => {
      messageListeners.forEach((cb) => cb({ data: JSON.stringify(data) }));
    });
  };
  return { wsRef, simulateMessage };
}

function answerBothEmpty(simulateMessage) {
  simulateMessage({ type: 'VAULT_ENTRY_LIST_RESPONSE', payload: { entries: [] } });
  simulateMessage({ type: 'VAULT_TOTP_LIST_RESPONSE', payload: { entries: [] } });
  simulateMessage({ type: 'VAULT_TOTP_CODES_RESPONSE', payload: { codes: [] } });
}

describe('VaultPanel load states (RUX-C-05)', () => {
  test('before any reply: loading states, and neither false empty text', () => {
    const { wsRef } = makeHarness();
    render(<VaultPanel wsRef={wsRef} />);
    expect(screen.getByTestId('vault-entries-load-state')).toHaveAttribute('data-load-display', 'loading');
    expect(screen.getByTestId('vault-totp-load-state')).toHaveAttribute('data-load-display', 'loading');
    expect(screen.queryByText(/No credentials saved yet/)).not.toBeInTheDocument();
    expect(screen.queryByText(/No authenticator codes yet/)).not.toBeInTheDocument();
  });

  test('while disconnected: offline states instead of the empty texts', () => {
    const { wsRef } = makeHarness({ isConnected: false });
    render(<VaultPanel wsRef={wsRef} />);
    expect(screen.getByTestId('vault-entries-load-state')).toHaveAttribute('data-load-display', 'offline');
    expect(screen.getByTestId('vault-entries-load-state')).toHaveTextContent(/Waiting for the engine/);
    expect(screen.getByTestId('vault-totp-load-state')).toHaveAttribute('data-load-display', 'offline');
    expect(screen.queryByText(/No credentials saved yet/)).not.toBeInTheDocument();
    expect(screen.queryByText(/No authenticator codes yet/)).not.toBeInTheDocument();
  });

  test('answered-empty replies show the genuine empty texts', () => {
    const { wsRef, simulateMessage } = makeHarness();
    render(<VaultPanel wsRef={wsRef} />);
    answerBothEmpty(simulateMessage);
    expect(screen.queryByTestId('vault-entries-load-state')).not.toBeInTheDocument();
    expect(screen.queryByTestId('vault-totp-load-state')).not.toBeInTheDocument();
    expect(screen.getByText(/No credentials saved yet/)).toBeInTheDocument();
    expect(screen.getByText(/No authenticator codes yet/)).toBeInTheDocument();
  });

  test('a credential-list failure reply is an error state, NOT an empty vault', () => {
    const { wsRef, simulateMessage } = makeHarness();
    render(<VaultPanel wsRef={wsRef} />);
    simulateMessage({
      type: 'VAULT_ENTRY_LIST_RESPONSE',
      payload: { success: false, error: 'Vault database is locked' },
    });
    expect(screen.getByTestId('vault-entries-load-state')).toHaveAttribute('data-load-display', 'error');
    expect(screen.getByText('Vault database is locked')).toBeInTheDocument();
    expect(screen.queryByText(/No credentials saved yet/)).not.toBeInTheDocument();
  });

  test('a codes failure reply is an error state, NOT "no codes yet"', () => {
    const { wsRef, simulateMessage } = makeHarness();
    render(<VaultPanel wsRef={wsRef} />);
    simulateMessage({ type: 'VAULT_ENTRY_LIST_RESPONSE', payload: { entries: [] } });
    simulateMessage({ type: 'VAULT_TOTP_LIST_RESPONSE', payload: { entries: [] } });
    simulateMessage({
      type: 'VAULT_TOTP_CODES_RESPONSE',
      payload: { success: false, error: 'Code service unavailable' },
    });
    expect(screen.getByTestId('vault-totp-load-state')).toHaveAttribute('data-load-display', 'error');
    expect(screen.getByTestId('vault-totp-error')).toHaveTextContent('Code service unavailable');
    expect(screen.queryByText(/No authenticator codes yet/)).not.toBeInTheDocument();
  });
});
