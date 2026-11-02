import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import SettingsPanel from '../SettingsPanel';

jest.mock('../contexts/SocketContext', () => ({
  useAetherSocket: () => ({
    wsRef: { current: { addEventListener: jest.fn(), removeEventListener: jest.fn() } },
    sendMessage: jest.fn()
  })
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

function feed(getHandler, type, payload) {
  act(() => {
    getHandler()({ data: JSON.stringify({ type, payload }) });
  });
}

function sentPackets(wsRef) {
  return wsRef.current.send.mock.calls.map(([raw]) => JSON.parse(raw));
}

function openTools(wsRef) {
  render(<SettingsPanel wsRef={wsRef} providers={[]} localModels={[]} />);
  fireEvent.click(screen.getByText('Tools'));
}

// FUP-1 (xref S2-M1): Settings → Tools gains the approval-binding
// control next to Approval mode. It reflects the stored
// approval_binding_mode from config replies and sends changes
// through the one CONFIG_UPDATE_REQUEST flow.
describe('SettingsPanel FUP-1 — approval binding mode', () => {
  it('renders both options with their plain-language sentences, defaulting to rebind', () => {
    const { wsRef } = makeWsRef();
    openTools(wsRef);

    const rebind = screen.getByRole('radio', { name: /Rebind approval to final arguments \(strict\)/ });
    const carry = screen.getByRole('radio', { name: /Carry my approval to rewritten arguments/ });
    expect(rebind).toBeInTheDocument();
    expect(carry).toBeInTheDocument();
    expect(screen.getByText('If a hook changes a call you approved and the changed call needs approval, you are asked again on the final version.')).toBeInTheDocument();
    expect(screen.getByText('Your approval of the original call also covers hook-rewritten versions, unless the rewrite is denied.')).toBeInTheDocument();
    // Strict is the default before any config reply lands.
    expect(rebind).toBeChecked();
    expect(carry).not.toBeChecked();
  });

  it('reflects the stored value from a config reply', () => {
    const { wsRef, getHandler } = makeWsRef();
    openTools(wsRef);

    feed(getHandler, 'CONFIG_GET_RESPONSE', {
      config: { approval_binding_mode: 'carry' },
    });

    expect(screen.getByRole('radio', { name: /Carry my approval to rewritten arguments/ })).toBeChecked();
    expect(screen.getByRole('radio', { name: /Rebind approval to final arguments \(strict\)/ })).not.toBeChecked();
  });

  it('changing the option sends CONFIG_UPDATE_REQUEST carrying approval_binding_mode', () => {
    const { wsRef, getHandler } = makeWsRef();
    openTools(wsRef);
    feed(getHandler, 'CONFIG_GET_RESPONSE', {
      config: { approval_binding_mode: 'carry' },
    });

    fireEvent.click(screen.getByRole('radio', { name: /Rebind approval to final arguments \(strict\)/ }));

    const updates = sentPackets(wsRef).filter(
      (p) => p.type === 'CONFIG_UPDATE_REQUEST' && p.payload?.config?.approval_binding_mode !== undefined);
    expect(updates).toHaveLength(1);
    expect(updates[0].payload.config.approval_binding_mode).toBe('rebind');
    // The control reflects the choice immediately.
    expect(screen.getByRole('radio', { name: /Rebind approval to final arguments \(strict\)/ })).toBeChecked();
  });

  it('selecting carry sends approval_binding_mode "carry"', () => {
    const { wsRef } = makeWsRef();
    openTools(wsRef);

    fireEvent.click(screen.getByRole('radio', { name: /Carry my approval to rewritten arguments/ }));

    const updates = sentPackets(wsRef).filter(
      (p) => p.type === 'CONFIG_UPDATE_REQUEST' && p.payload?.config?.approval_binding_mode !== undefined);
    expect(updates).toHaveLength(1);
    expect(updates[0].payload.config.approval_binding_mode).toBe('carry');
  });
});
