/** PRD Open Point 14 (decided YES by the owner, 1 Nov 2026): the
 * vault panel's Authenticator section. Standalone TOTP entries
 * enrol through VAULT_TOTP_* events (label/issuer + the Base32
 * setup key), and live codes arrive pre-computed from the engine
 * via VAULT_TOTP_CODES_RESPONSE — the renderer only ever holds
 * the current 6-digit code and its countdown, never a secret.
 * The countdown ticks locally and a fresh codes request goes out
 * as each 30-second period rolls over. */
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

const TOTP_ENTRY = {
  id: 'totp-1',
  label: 'octocat',
  issuer: 'GitHub',
  created_at: 1761900000,
  updated_at: 1761900000,
};

const CODES_PAYLOAD = {
  generated_at: 1761900059,
  codes: [
    {
      entry_id: 'totp-1',
      kind: 'authenticator',
      label: 'octocat',
      issuer: 'GitHub',
      code: '287082',
      period: 30,
      seconds_remaining: 12,
    },
    {
      entry_id: 'login-1',
      kind: 'login',
      label: 'example.com',
      issuer: '',
      username: 'me@example.com',
      code: '005924',
      period: 30,
      seconds_remaining: 12,
    },
  ],
};

function renderPanel({ codes = CODES_PAYLOAD } = {}) {
  const { wsRef, simulateMessage } = makeWsRef();
  render(<VaultPanel wsRef={wsRef} />);
  const sent = () => wsRef.current.send.mock.calls.map((c) => JSON.parse(c[0]));
  act(() => {
    simulateMessage({ type: 'VAULT_ENTRY_LIST_RESPONSE', payload: { entries: [] } });
    simulateMessage({ type: 'VAULT_TOTP_LIST_RESPONSE', payload: { entries: [TOTP_ENTRY] } });
    if (codes) {
      simulateMessage({ type: 'VAULT_TOTP_CODES_RESPONSE', payload: codes });
    }
  });
  return { wsRef, simulateMessage, sent };
}

describe('VaultPanel Authenticator (Open Point 14)', () => {
  afterEach(() => {
    jest.useRealTimers();
  });

  test('requests the TOTP list and the live codes on mount', () => {
    const { wsRef } = makeWsRef();
    render(<VaultPanel wsRef={wsRef} />);
    const sent = wsRef.current.send.mock.calls.map((c) => JSON.parse(c[0]));
    expect(sent.some((m) => m.type === 'VAULT_TOTP_LIST_REQUEST')).toBe(true);
    expect(sent.some((m) => m.type === 'VAULT_TOTP_CODES_REQUEST')).toBe(true);
  });

  test('renders each served code with its countdown — and no secret field holds a code', () => {
    renderPanel();
    expect(screen.getByTestId('vault-totp-code-totp-1')).toHaveTextContent('287082');
    expect(screen.getByTestId('vault-totp-countdown-totp-1')).toHaveTextContent('12');
    expect(screen.getByTestId('vault-totp-entry-totp-1')).toHaveTextContent('GitHub');
    expect(screen.getByTestId('vault-totp-entry-totp-1')).toHaveTextContent('octocat');
    // Login-attached codes render too, labelled by site + username.
    expect(screen.getByTestId('vault-totp-code-login-1')).toHaveTextContent('005924');
    expect(screen.getByTestId('vault-totp-entry-login-1')).toHaveTextContent('me@example.com');
    // The enrolment secret input never displays a served code.
    expect(screen.getByTestId('vault-totp-secret-input')).toHaveValue('');
  });

  test('the countdown ticks down locally without new requests', () => {
    jest.useFakeTimers();
    const { sent } = renderPanel();
    act(() => { jest.advanceTimersByTime(1000); });
    expect(screen.getByTestId('vault-totp-countdown-totp-1')).toHaveTextContent('11');
    expect(sent().filter((m) => m.type === 'VAULT_TOTP_CODES_REQUEST')).toHaveLength(1);
  });

  test('a fresh codes request goes out as the period rolls over', () => {
    jest.useFakeTimers();
    const nearBoundary = {
      generated_at: 1761900059,
      codes: [{ ...CODES_PAYLOAD.codes[0], seconds_remaining: 1 }],
    };
    const { sent, simulateMessage } = renderPanel({ codes: nearBoundary });
    expect(sent().filter((m) => m.type === 'VAULT_TOTP_CODES_REQUEST')).toHaveLength(1);
    act(() => { jest.advanceTimersByTime(1000); });
    expect(sent().filter((m) => m.type === 'VAULT_TOTP_CODES_REQUEST')).toHaveLength(2);
    // The fresh response re-arms the countdown at the new period.
    act(() => {
      simulateMessage({
        type: 'VAULT_TOTP_CODES_RESPONSE',
        payload: { generated_at: 1761900060, codes: [{ ...CODES_PAYLOAD.codes[0], code: '111111', seconds_remaining: 30 }] },
      });
    });
    expect(screen.getByTestId('vault-totp-countdown-totp-1')).toHaveTextContent('30');
    expect(screen.getByTestId('vault-totp-code-totp-1')).toHaveTextContent('111111');
  });

  test('enrolment posts label, issuer and the setup key — nothing else', () => {
    const { sent } = renderPanel({ codes: null });
    fireEvent.change(screen.getByTestId('vault-totp-label-input'), { target: { value: 'octocat' } });
    fireEvent.change(screen.getByTestId('vault-totp-issuer-input'), { target: { value: 'GitHub' } });
    fireEvent.change(screen.getByTestId('vault-totp-secret-input'), { target: { value: 'GEZDGNBVGY3TQOJQ' } });
    fireEvent.click(screen.getByTestId('vault-totp-save-button'));
    const save = sent().find((m) => m.type === 'VAULT_TOTP_SAVE_REQUEST');
    expect(save).toBeTruthy();
    expect(save.payload).toEqual({
      id: null,
      label: 'octocat',
      issuer: 'GitHub',
      secret: 'GEZDGNBVGY3TQOJQ',
    });
  });

  test('a missing label is refused in the panel and nothing is sent', () => {
    const { sent } = renderPanel({ codes: null });
    fireEvent.change(screen.getByTestId('vault-totp-secret-input'), { target: { value: 'GEZDGNBVGY3TQOJQ' } });
    fireEvent.click(screen.getByTestId('vault-totp-save-button'));
    expect(screen.getByTestId('vault-totp-error')).toHaveTextContent(/label/i);
    expect(sent().some((m) => m.type === 'VAULT_TOTP_SAVE_REQUEST')).toBe(false);
  });

  test('a missing setup key on a new entry is refused in the panel', () => {
    const { sent } = renderPanel({ codes: null });
    fireEvent.change(screen.getByTestId('vault-totp-label-input'), { target: { value: 'octocat' } });
    fireEvent.click(screen.getByTestId('vault-totp-save-button'));
    expect(screen.getByTestId('vault-totp-error')).toHaveTextContent(/setup key/i);
    expect(sent().some((m) => m.type === 'VAULT_TOTP_SAVE_REQUEST')).toBe(false);
  });

  test('an engine refusal (malformed Base32) is shown verbatim', () => {
    const { simulateMessage } = renderPanel({ codes: null });
    fireEvent.change(screen.getByTestId('vault-totp-label-input'), { target: { value: 'octocat' } });
    fireEvent.change(screen.getByTestId('vault-totp-secret-input'), { target: { value: 'ABC!0189' } });
    fireEvent.click(screen.getByTestId('vault-totp-save-button'));
    act(() => {
      simulateMessage({
        type: 'VAULT_TOTP_SAVE_RESPONSE',
        payload: {
          success: false,
          id: '',
          error: 'That TOTP secret is not valid Base32: setup keys use only the letters A-Z and the digits 2-7.',
        },
      });
    });
    expect(screen.getByTestId('vault-totp-error')).toHaveTextContent(/not valid Base32/i);
  });

  test('editing an authenticator entry prefills label and issuer, secret blank', () => {
    renderPanel();
    fireEvent.click(screen.getByTestId('vault-totp-edit-totp-1'));
    expect(screen.getByTestId('vault-totp-label-input')).toHaveValue('octocat');
    expect(screen.getByTestId('vault-totp-issuer-input')).toHaveValue('GitHub');
    expect(screen.getByTestId('vault-totp-secret-input')).toHaveValue('');
  });

  test('deleting an authenticator entry sends the delete request', () => {
    const { sent } = renderPanel();
    fireEvent.click(screen.getByTestId('vault-totp-delete-totp-1'));
    const del = sent().find((m) => m.type === 'VAULT_TOTP_DELETE_REQUEST');
    expect(del).toBeTruthy();
    expect(del.payload).toEqual({ id: 'totp-1' });
  });

  test('login-kind codes offer no edit or delete controls', () => {
    renderPanel();
    expect(screen.queryByTestId('vault-totp-edit-login-1')).not.toBeInTheDocument();
    expect(screen.queryByTestId('vault-totp-delete-login-1')).not.toBeInTheDocument();
  });
});
