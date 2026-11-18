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

  const fillStdioForm = () => {
    fireEvent.change(screen.getByPlaceholderText('Server Name (e.g. SQLite DB)'), {
      target: { value: 'local-tools' },
    });
    fireEvent.change(screen.getByPlaceholderText('Command (e.g. python, npx)'), {
      target: { value: 'npx' },
    });
    fireEvent.change(screen.getByPlaceholderText('Arguments (comma separated)'), {
      target: { value: '-y, @example/tools-server' },
    });
  };

  test('adding a stdio server first shows a confirmation card naming the command — nothing is sent before it', () => {
    const { sendMessage } = makeHarness();
    render(<McpPanel />);
    fillStdioForm();
    fireEvent.click(screen.getByText('Add Server'));
    // No ADD request yet: the card comes first.
    expect(sendMessage).not.toHaveBeenCalledWith('MCP_SERVER_ADD_REQUEST', expect.anything());
    expect(screen.getByText('Before it runs')).toBeInTheDocument();
    expect(screen.getByText(/npx -y @example\/tools-server/)).toBeInTheDocument();
    expect(screen.getByText(/automatically at startup/)).toBeInTheDocument();
  });

  test('confirming the card sends the add with the confirmed marker', () => {
    const { sendMessage } = makeHarness();
    render(<McpPanel />);
    fillStdioForm();
    fireEvent.click(screen.getByText('Add Server'));
    fireEvent.click(screen.getByText('Confirm and add'));
    expect(sendMessage).toHaveBeenCalledWith('MCP_SERVER_ADD_REQUEST', expect.objectContaining({
      payload: expect.objectContaining({
        name: 'local-tools',
        config: expect.objectContaining({
          command: 'npx',
          args: ['-y', '@example/tools-server'],
          confirmed: true,
        }),
      }),
    }));
    // The card is gone once confirmed.
    expect(screen.queryByText('Before it runs')).not.toBeInTheDocument();
  });

  test('cancelling the card sends nothing and keeps the form for editing', () => {
    const { sendMessage } = makeHarness();
    render(<McpPanel />);
    fillStdioForm();
    fireEvent.click(screen.getByText('Add Server'));
    expect(screen.getByText('Before it runs')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Cancel'));
    expect(sendMessage).not.toHaveBeenCalledWith('MCP_SERVER_ADD_REQUEST', expect.anything());
    expect(screen.queryByText('Before it runs')).not.toBeInTheDocument();
    expect(screen.getByPlaceholderText('Server Name (e.g. SQLite DB)')).toHaveValue('local-tools');
  });

  test('an unconfirmed server is surfaced as awaiting confirmation; a confirmed one is not', () => {
    const { simulateMessage } = makeHarness();
    render(<McpPanel />);
    simulateMessage({
      type: 'MCP_SERVER_LIST_RESPONSE',
      payload: {
        servers: {
          legacy: { command: 'python', args: ['-m', 'db'], status: 'disconnected', confirmed: false },
          fresh: { command: 'npx', args: ['srv'], status: 'disconnected', confirmed: true },
        },
      },
    });
    expect(screen.getByText(/Awaiting confirmation/)).toBeInTheDocument();
    expect(screen.getByText(/will not auto-connect at startup/)).toBeInTheDocument();
    // Exactly one awaiting note — the confirmed server does not get one.
    expect(screen.getAllByText(/Awaiting confirmation/)).toHaveLength(1);
  });

  test('confirming an existing server in place sends the update with the confirmed marker', () => {
    const { sendMessage, simulateMessage } = makeHarness();
    render(<McpPanel />);
    simulateMessage({
      type: 'MCP_SERVER_LIST_RESPONSE',
      payload: {
        servers: {
          legacy: { command: 'python', args: ['-m', 'db'], status: 'disconnected', confirmed: false },
        },
      },
    });
    fireEvent.click(screen.getByText('Review and confirm'));
    // The same trust card, naming the stored command before anything is sent
    // (the command now appears twice: the server row and the card).
    expect(screen.getByText('Before it runs')).toBeInTheDocument();
    expect(screen.getAllByText(/python -m db/)).toHaveLength(2);
    expect(sendMessage).not.toHaveBeenCalledWith('MCP_SERVER_UPDATE_REQUEST', expect.objectContaining({
      payload: expect.objectContaining({ confirmed: true }),
    }));
    fireEvent.click(screen.getByText('Confirm server'));
    expect(sendMessage).toHaveBeenCalledWith('MCP_SERVER_UPDATE_REQUEST', expect.objectContaining({
      payload: { name: 'legacy', confirmed: true },
    }));
    expect(screen.queryByText('Before it runs')).not.toBeInTheDocument();
  });

  test('cancelling the in-place confirmation sends nothing', () => {
    const { sendMessage, simulateMessage } = makeHarness();
    render(<McpPanel />);
    simulateMessage({
      type: 'MCP_SERVER_LIST_RESPONSE',
      payload: {
        servers: {
          legacy: { command: 'python', args: ['-m', 'db'], status: 'disconnected', confirmed: false },
        },
      },
    });
    fireEvent.click(screen.getByText('Review and confirm'));
    fireEvent.click(screen.getByText('Cancel'));
    expect(sendMessage).not.toHaveBeenCalledWith('MCP_SERVER_UPDATE_REQUEST', expect.objectContaining({
      payload: expect.objectContaining({ confirmed: true }),
    }));
    expect(screen.queryByText('Before it runs')).not.toBeInTheDocument();
  });

  describe('shared capability search/filter (D4-1)', () => {
    const SERVERS = {
      localdb: { command: 'python', args: ['-m', 'db'], status: 'connected', confirmed: true },
      brokensrv: { command: 'nope', args: [], status: 'failed', error: 'spawn failed', confirmed: true },
      offsrv: { url: 'http://127.0.0.1:9/mcp', status: 'disconnected', enabled: false, confirmed: true },
    };

    test('search narrows by name; Error and Disabled chips classify by server state', () => {
      const { simulateMessage } = makeHarness();
      render(<McpPanel />);
      simulateMessage({ type: 'MCP_SERVER_LIST_RESPONSE', payload: { servers: SERVERS } });
      fireEvent.change(screen.getByPlaceholderText('Search servers by name…'), { target: { value: 'local' } });
      expect(screen.getByText('localdb')).toBeInTheDocument();
      expect(screen.queryByText('brokensrv')).not.toBeInTheDocument();
      expect(screen.queryByText('offsrv')).not.toBeInTheDocument();
      fireEvent.change(screen.getByPlaceholderText('Search servers by name…'), { target: { value: '' } });
      fireEvent.click(screen.getByText('Error · 1'));
      expect(screen.getByText('brokensrv')).toBeInTheDocument();
      expect(screen.queryByText('localdb')).not.toBeInTheDocument();
      fireEvent.click(screen.getByText('Disabled · 1'));
      expect(screen.getByText('offsrv')).toBeInTheDocument();
      expect(screen.queryByText('brokensrv')).not.toBeInTheDocument();
    });

    test('the Updates chip honestly matches nothing yet and clears back to the full list', () => {
      const { simulateMessage } = makeHarness();
      render(<McpPanel />);
      simulateMessage({ type: 'MCP_SERVER_LIST_RESPONSE', payload: { servers: SERVERS } });
      fireEvent.click(screen.getByText('Updates · 0'));
      expect(screen.getByTestId('mcp-no-results')).toBeInTheDocument();
      fireEvent.click(screen.getByText('Show all servers'));
      expect(screen.getByText('localdb')).toBeInTheDocument();
      expect(screen.getByText('brokensrv')).toBeInTheDocument();
      expect(screen.getByText('offsrv')).toBeInTheDocument();
    });
  });
});
