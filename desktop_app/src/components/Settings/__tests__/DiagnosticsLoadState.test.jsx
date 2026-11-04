/** Wave 3 slice 1 (RUX-C-06, under the RUX-C-01 umbrella): the
 * DiagnosticsPanel lists (MCP servers, plugins, agents) must not
 * render their empty/loading texts before the engine answers.
 * Loading, offline, error and answered-empty are four distinct
 * renders. */
import React from 'react';
import { render, screen, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import DiagnosticsPanel from '../DiagnosticsPanel';
import { useAetherSocket } from '../../../contexts/SocketContext';

jest.mock('../../../contexts/SocketContext', () => ({
  useAetherSocket: jest.fn(),
}));

function makeHarness({ isConnected = true } = {}) {
  let messageListeners = [];
  const mockWs = {
    addEventListener: jest.fn((event, cb) => {
      if (event === 'message') messageListeners.push(cb);
    }),
    removeEventListener: jest.fn((event, cb) => {
      if (event === 'message') messageListeners = messageListeners.filter((l) => l !== cb);
    }),
  };
  useAetherSocket.mockReturnValue({
    wsRef: { current: mockWs },
    sendMessage: jest.fn(),
    isConnected,
  });
  const simulateMessage = (data) => {
    act(() => {
      messageListeners.forEach((cb) => cb({ data: JSON.stringify(data) }));
    });
  };
  return { simulateMessage };
}

describe('DiagnosticsPanel load states (RUX-C-06)', () => {
  test('MCP: before the reply a loading state shows — not "No MCP servers configured."', () => {
    makeHarness();
    render(<DiagnosticsPanel activeMenu="MCP" />);
    expect(screen.getByTestId('mcp-servers-load-state')).toHaveAttribute('data-load-display', 'loading');
    expect(screen.queryByText('No MCP servers configured.')).not.toBeInTheDocument();
  });

  test('MCP: while disconnected the offline state shows', () => {
    makeHarness({ isConnected: false });
    render(<DiagnosticsPanel activeMenu="MCP" />);
    const row = screen.getByTestId('mcp-servers-load-state');
    expect(row).toHaveAttribute('data-load-display', 'offline');
    expect(row).toHaveTextContent(/Waiting for the engine/);
  });

  test('MCP: an answered-empty reply shows the genuine empty state', () => {
    const { simulateMessage } = makeHarness();
    render(<DiagnosticsPanel activeMenu="MCP" />);
    simulateMessage({ type: 'MCP_SERVER_LIST_RESPONSE', payload: { servers: {} } });
    expect(screen.queryByTestId('mcp-servers-load-state')).not.toBeInTheDocument();
    expect(screen.getByText('No MCP servers configured.')).toBeInTheDocument();
  });

  test('MCP: a config_error reply is an error state, not an empty list', () => {
    const { simulateMessage } = makeHarness();
    render(<DiagnosticsPanel activeMenu="MCP" />);
    simulateMessage({
      type: 'MCP_SERVER_LIST_RESPONSE',
      payload: { servers: {}, config_error: 'unexpected key on line 3' },
    });
    expect(screen.getByTestId('mcp-servers-load-state')).toHaveAttribute('data-load-display', 'error');
    expect(screen.getByText(/MCP config file error/)).toBeInTheDocument();
    expect(screen.queryByText('No MCP servers configured.')).not.toBeInTheDocument();
  });

  test('Plugins: loading before the reply, genuine empty after an answered-empty reply', () => {
    const { simulateMessage } = makeHarness();
    render(<DiagnosticsPanel activeMenu="Plugins" />);
    expect(screen.getByTestId('plugins-load-state')).toHaveAttribute('data-load-display', 'loading');
    expect(screen.queryByText('No plugins installed.')).not.toBeInTheDocument();
    simulateMessage({ type: 'PLUGIN_LIST_RESPONSE', payload: { plugins: [] } });
    expect(screen.queryByTestId('plugins-load-state')).not.toBeInTheDocument();
    expect(screen.getByText('No plugins installed.')).toBeInTheDocument();
  });

  test('Agents: before the reply the load state shows (offline variant when down); answered-empty is its own text', () => {
    const { simulateMessage } = makeHarness({ isConnected: false });
    render(<DiagnosticsPanel activeMenu="Agents" />);
    const row = screen.getByTestId('agents-load-state');
    expect(row).toHaveAttribute('data-load-display', 'offline');
    // The old permanent "Loading agents..." label is gone.
    expect(screen.queryByText('Loading agents...')).not.toBeInTheDocument();
    simulateMessage({ type: 'AGENT_LIST_RESPONSE', payload: { agents: [] } });
    expect(screen.queryByTestId('agents-load-state')).not.toBeInTheDocument();
    expect(screen.getByText('No agents were reported by the engine.')).toBeInTheDocument();
  });
});
