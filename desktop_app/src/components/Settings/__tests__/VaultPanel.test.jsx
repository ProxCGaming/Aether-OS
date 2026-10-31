/** Epic 8, E8-6.3: the Settings -> Password Vault surface.
 * Lists entries as metadata only (a password is never rendered —
 * the engine never sends one), adds/edits/deletes through the
 * VAULT_ENTRY_* events, and carries the ask-every-time flag plus
 * the owner-only TOTP field. */
import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import VaultPanel from '../VaultPanel';
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

const ENTRY = {
  id: 'entry-1',
  site: 'github.com',
  username: 'octocat',
  ask_every_time: true,
  created_at: 1761900000,
  created_by: 'agent',
  last_used_at: null,
};

function renderPanel(entries = [ENTRY]) {
  const { wsRef, simulateMessage } = makeWsRef();
  render(<VaultPanel wsRef={wsRef} />);
  const sent = () => wsRef.current.send.mock.calls.map((c) => JSON.parse(c[0]));
  act(() => {
    simulateMessage({ type: 'VAULT_ENTRY_LIST_RESPONSE', payload: { entries } });
  });
  return { wsRef, simulateMessage, sent };
}

describe('VaultPanel (E8-6.3)', () => {
  test('requests the entry list on mount', () => {
    const { wsRef } = makeWsRef();
    render(<VaultPanel wsRef={wsRef} />);
    const sent = wsRef.current.send.mock.calls.map((c) => JSON.parse(c[0]));
    expect(sent.some((m) => m.type === 'VAULT_ENTRY_LIST_REQUEST')).toBe(true);
  });

  test('renders entries as metadata only — no password anywhere', () => {
    renderPanel();
    expect(screen.getByTestId('vault-entry-entry-1')).toBeInTheDocument();
    expect(screen.getByText(/github\.com/)).toBeInTheDocument();
    expect(screen.getByText(/octocat/)).toBeInTheDocument();
    expect(screen.getByText('Ask every time')).toBeInTheDocument();
    expect(screen.getByText(/added by an agent/i)).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/password:/i);
  });

  test('saving the form posts the typed fields (and only those)', () => {
    const { sent } = renderPanel([]);
    fireEvent.change(screen.getByTestId('vault-site-input'), { target: { value: 'example.com' } });
    fireEvent.change(screen.getByTestId('vault-username-input'), { target: { value: 'me@example.com' } });
    fireEvent.change(screen.getByTestId('vault-password-input'), { target: { value: 's3cret-value' } });
    fireEvent.click(screen.getByTestId('vault-ask-checkbox'));
    fireEvent.click(screen.getByTestId('vault-save-button'));
    const save = sent().find((m) => m.type === 'VAULT_ENTRY_SAVE_REQUEST');
    expect(save).toBeTruthy();
    expect(save.payload).toEqual({
      id: null,
      site: 'example.com',
      username: 'me@example.com',
      password: 's3cret-value',
      ask_every_time: true,
      totp_secret: '',
    });
  });

  test('editing prefills site and username but leaves the password blank', () => {
    const { sent } = renderPanel();
    fireEvent.click(screen.getByTestId('vault-edit-entry-1'));
    expect(screen.getByTestId('vault-site-input')).toHaveValue('github.com');
    expect(screen.getByTestId('vault-username-input')).toHaveValue('octocat');
    expect(screen.getByTestId('vault-password-input')).toHaveValue('');
    fireEvent.click(screen.getByTestId('vault-save-button'));
    const save = sent().find((m) => m.type === 'VAULT_ENTRY_SAVE_REQUEST');
    expect(save.payload.id).toBe('entry-1');
    expect(save.payload.password).toBe('');
  });

  test('deleting an entry sends the delete request', () => {
    const { sent } = renderPanel();
    fireEvent.click(screen.getByTestId('vault-delete-entry-1'));
    const del = sent().find((m) => m.type === 'VAULT_ENTRY_DELETE_REQUEST');
    expect(del).toBeTruthy();
    expect(del.payload).toEqual({ id: 'entry-1' });
  });

  test('the TOTP field is labelled owner-only', () => {
    renderPanel([]);
    expect(screen.getByTestId('vault-totp-input')).toBeInTheDocument();
    expect(screen.getByText(/agents can never read it/i)).toBeInTheDocument();
  });
});
