/** Wave 5 (Annex C row 8): McpPanel extracted from
 * DiagnosticsPanel.jsx. The panel owns the MCP tab's socket
 * state on its own: list request on mount, load state until the
 * engine answers, resource/prompt fan-out for connected servers. */
import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import McpPanel from '../McpPanel';
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
  const sendMessage = jest.fn();
  useAetherSocket.mockReturnValue({
    wsRef: { current: mockWs },
    sendMessage,
    isConnected,
  });
  const simulateMessage = (data) => {
    act(() => {
      messageListeners.forEach((cb) => cb({ data: JSON.stringify(data) }));
    });
  };
  return { simulateMessage, sendMessage };
}

describe('McpPanel', () => {
  test('requests the server list on mount and shows the loading state until it answers', () => {
    const { sendMessage } = makeHarness();
    render(<McpPanel />);
    expect(sendMessage).toHaveBeenCalledWith(
      'MCP_SERVER_LIST_REQUEST',
      expect.objectContaining({ payload: {} })
    );
    expect(screen.getByTestId('mcp-servers-load-state')).toHaveAttribute('data-load-display', 'loading');
    expect(screen.queryByText('No MCP servers configured.')).not.toBeInTheDocument();
  });

  test('a connected server renders and fans out resource and prompt list requests', () => {
    const { simulateMessage, sendMessage } = makeHarness();
    render(<McpPanel />);
    simulateMessage({
      type: 'MCP_SERVER_LIST_RESPONSE',
      payload: { servers: { localdb: { command: 'python', args: ['-m', 'db'], status: 'connected', tool_count: 3 } } },
    });
    expect(screen.getByText('localdb')).toBeInTheDocument();
    expect(sendMessage).toHaveBeenCalledWith(
      'MCP_RESOURCE_LIST_REQUEST',
      expect.objectContaining({ payload: { name: 'localdb' } })
    );
    expect(sendMessage).toHaveBeenCalledWith(
      'MCP_PROMPT_LIST_REQUEST',
      expect.objectContaining({ payload: { name: 'localdb' } })
    );
  });

  test('Add with a name but no transport sends nothing and explains what is missing (D2-18)', () => {
    const { sendMessage } = makeHarness();
    render(<McpPanel />);
    fireEvent.change(screen.getByPlaceholderText('Server Name (e.g. SQLite DB)'), {
      target: { value: 'half-filled' },
    });
    fireEvent.click(screen.getByText('Add Server'));
    expect(sendMessage).not.toHaveBeenCalledWith('MCP_SERVER_ADD_REQUEST', expect.anything());
    expect(screen.getByText(/a name alone is not enough/)).toBeInTheDocument();
  });
});
