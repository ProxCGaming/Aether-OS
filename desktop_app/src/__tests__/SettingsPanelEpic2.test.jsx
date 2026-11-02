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

function packetsOfType(wsRef, type) {
  return sentPackets(wsRef).filter((p) => p.type === type);
}

function openTools(wsRef) {
  render(<SettingsPanel wsRef={wsRef} providers={[]} localModels={[]} />);
  fireEvent.click(screen.getByText('Tools'));
}

// Epic 2: Settings → Tools gains the approval-mode selector (E2-08)
// and the approval-grants manager (E2-09). The mode initialises from
// TOOL_LIST_RESPONSE.approval_mode; grants are fetched when the
// section opens and the list is replaced by both grants responses.
describe('SettingsPanel Epic 2 — approval mode', () => {
  it('opening Tools requests the tool list and the approval grants', () => {
    const { wsRef } = makeWsRef();
    openTools(wsRef);
    expect(packetsOfType(wsRef, 'TOOL_LIST_REQUEST')).toHaveLength(1);
    expect(packetsOfType(wsRef, 'APPROVAL_GRANTS_REQUEST')).toHaveLength(1);
  });

  it('defaults to "Ask for approval" before any response', () => {
    const { wsRef } = makeWsRef();
    openTools(wsRef);
    expect(screen.getByRole('button', { name: /Ask for approval/ })).toBeInTheDocument();
  });

  it('reflects approval_mode "write" from TOOL_LIST_RESPONSE', () => {
    const { wsRef, getHandler } = makeWsRef();
    openTools(wsRef);
    feed(getHandler, 'TOOL_LIST_RESPONSE', { tools: [], approval_mode: 'write' });
    expect(screen.getByRole('button', { name: /Auto-approve writes/ })).toBeInTheDocument();
  });

  it('changing the mode emits APPROVAL_MODE_SET_REQUEST with the new mode', () => {
    const { wsRef, getHandler } = makeWsRef();
    openTools(wsRef);
    feed(getHandler, 'TOOL_LIST_RESPONSE', { tools: [], approval_mode: 'write' });
    // Open the dropdown and pick the other mode.
    fireEvent.click(screen.getByRole('button', { name: /Auto-approve writes/ }));
    fireEvent.click(screen.getByText('Ask for approval'));
    const reqs = packetsOfType(wsRef, 'APPROVAL_MODE_SET_REQUEST');
    expect(reqs).toHaveLength(1);
    expect(reqs[0].payload.mode).toBe('ask');
    // The set response confirms the mode and the selector follows it.
    feed(getHandler, 'APPROVAL_MODE_SET_RESPONSE', { success: true, mode: 'ask' });
    expect(screen.getByRole('button', { name: /Ask for approval/ })).toBeInTheDocument();
  });
});

describe('SettingsPanel Epic 2 — approval grants', () => {
  const grants = [
    {
      id: 'g1', prefix: '/home/user/proj', ceiling: 'write', tool_name: 'edit_file',
      created_at: '2026-10-31T00:00:00Z', last_used_at: '2026-10-31T01:00:00Z',
    },
    {
      id: 'g2', prefix: '/home/user/notes.txt', ceiling: 'read_only', tool_name: null,
      created_at: '2026-10-31T00:00:00Z', last_used_at: null,
    },
  ];

  it('shows the empty state when there are no grants', () => {
    const { wsRef, getHandler } = makeWsRef();
    openTools(wsRef);
    feed(getHandler, 'APPROVAL_GRANTS_RESPONSE', { grants: [] });
    expect(screen.getByText('No approval grants remembered.')).toBeInTheDocument();
  });

  it('renders grants with prefix, ceiling and tool name', () => {
    const { wsRef, getHandler } = makeWsRef();
    openTools(wsRef);
    feed(getHandler, 'APPROVAL_GRANTS_RESPONSE', { grants });
    expect(screen.getByText('/home/user/proj')).toBeInTheDocument();
    expect(screen.getByText('up to write')).toBeInTheDocument();
    expect(screen.getByText('edit_file')).toBeInTheDocument();
    expect(screen.getByText('/home/user/notes.txt')).toBeInTheDocument();
    expect(screen.getByText('up to read_only')).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: 'Revoke' })).toHaveLength(2);
  });

  it('revoke emits APPROVAL_GRANT_REVOKE_REQUEST and the response list replaces the old', () => {
    const { wsRef, getHandler } = makeWsRef();
    openTools(wsRef);
    feed(getHandler, 'APPROVAL_GRANTS_RESPONSE', { grants });
    fireEvent.click(screen.getAllByRole('button', { name: 'Revoke' })[0]);
    const reqs = packetsOfType(wsRef, 'APPROVAL_GRANT_REVOKE_REQUEST');
    expect(reqs).toHaveLength(1);
    expect(reqs[0].payload.id).toBe('g1');
    feed(getHandler, 'APPROVAL_GRANT_REVOKE_RESPONSE', { id: 'g1', revoked: true, grants: [grants[1]] });
    expect(screen.queryByText('/home/user/proj')).not.toBeInTheDocument();
    expect(screen.getByText('/home/user/notes.txt')).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: 'Revoke' })).toHaveLength(1);
  });
});
