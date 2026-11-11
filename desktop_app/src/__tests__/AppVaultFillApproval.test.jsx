import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import App from '../App';

// The 3D force graph needs WebGL; replace with a stub for jsdom.
jest.mock('react-force-graph-3d', () => ({
  __esModule: true,
  default: () => <div data-testid="force-graph-stub" />,
}));

// react-markdown is ESM-only; stub it for Jest.
jest.mock('react-markdown', () => ({
  __esModule: true,
  default: ({ children }) => <div data-testid="markdown-stub">{children}</div>,
}));

// jsdom has no scrollIntoView; stub it.
Element.prototype.scrollIntoView = jest.fn();

// jsdom has no WebSocket; the socket layer only needs the constants.
global.WebSocket = { OPEN: 1, CLOSED: 3, CONNECTING: 0, CLOSING: 2 };

// F2 (post-Wave-6 linkage audit): the vault ask-every-time round trip.
// The engine pushes VAULT_FILL_APPROVAL_REQUEST {request_id, entry_id,
// site, username, task_id} and waits; the desktop must render the
// prompt and answer VAULT_FILL_APPROVAL_RESPONSE {request_id,
// approved} (contracts.py) — before this fix nothing answered, so
// every ask-every-time fill timed out and was refused.
describe('App vault fill approval (F2)', () => {
  let engineListeners;
  let electronAPI;

  beforeEach(() => {
    engineListeners = [];
    electronAPI = {
      sendEngineMessage: jest.fn(),
      onEngineMessage: jest.fn((cb) => {
        engineListeners.push(cb);
        return () => {
          const idx = engineListeners.indexOf(cb);
          if (idx !== -1) engineListeners.splice(idx, 1);
        };
      }),
      getWsStatus: jest.fn(() => Promise.resolve(false)),
      getMemoryUsage: jest.fn(() => Promise.resolve(128)),
      onChatDetached: jest.fn(() => jest.fn()),
      onChatAttached: jest.fn(() => jest.fn()),
    };
    window.electronAPI = electronAPI;
  });

  afterEach(() => {
    delete window.electronAPI;
  });

  const emitEngineMessage = (obj) => {
    const raw = typeof obj === 'string' ? obj : JSON.stringify(obj);
    act(() => {
      [...engineListeners].forEach((cb) => cb(raw));
    });
  };

  const flushPromises = async () => {
    await act(async () => {});
  };

  const connect = async () => {
    render(<App />);
    await flushPromises();
    emitEngineMessage({ type: '_ws_status', status: 'connected' });
    await flushPromises();
  };

  const sentPackets = () =>
    electronAPI.sendEngineMessage.mock.calls.map(([raw]) => JSON.parse(raw));

  const packetsOfType = (type) => sentPackets().filter((p) => p.type === type);

  const emitVaultRequest = (overrides = {}) => {
    emitEngineMessage({
      type: 'VAULT_FILL_APPROVAL_REQUEST',
      request_id: 'vault-req-1',
      payload: {
        request_id: 'vault-req-1',
        entry_id: 'entry-9',
        site: 'https://bank.example.com',
        username: 'arindam',
        task_id: 'task-7',
        ...overrides,
      },
    });
  };

  it('renders the prompt with the site and username from the payload', async () => {
    await connect();
    emitVaultRequest();
    const card = screen.getByTestId('vault-fill-approval-card');
    expect(card).toBeInTheDocument();
    expect(card.textContent).toContain('https://bank.example.com');
    expect(card.textContent).toContain('arindam');
  });

  it('Approve sends VAULT_FILL_APPROVAL_RESPONSE {request_id, approved: true} and clears the card', async () => {
    await connect();
    emitVaultRequest();
    fireEvent.click(screen.getByTestId('vault-fill-approve-btn'));
    const responses = packetsOfType('VAULT_FILL_APPROVAL_RESPONSE');
    expect(responses).toHaveLength(1);
    expect(responses[0].payload).toEqual({ request_id: 'vault-req-1', approved: true });
    expect(responses[0].request_id).toBe('vault-req-1');
    expect(screen.queryByTestId('vault-fill-approval-card')).not.toBeInTheDocument();
  });

  it('Deny sends VAULT_FILL_APPROVAL_RESPONSE with approved: false and clears the card', async () => {
    await connect();
    emitVaultRequest();
    fireEvent.click(screen.getByTestId('vault-fill-deny-btn'));
    const responses = packetsOfType('VAULT_FILL_APPROVAL_RESPONSE');
    expect(responses).toHaveLength(1);
    expect(responses[0].payload).toEqual({ request_id: 'vault-req-1', approved: false });
    expect(screen.queryByTestId('vault-fill-approval-card')).not.toBeInTheDocument();
  });
});
