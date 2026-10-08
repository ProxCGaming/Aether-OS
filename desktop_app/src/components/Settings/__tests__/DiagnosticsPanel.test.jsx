import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import DiagnosticsPanel from '../DiagnosticsPanel';
import { useAetherSocket } from '../../../contexts/SocketContext';

jest.mock('../../../contexts/SocketContext', () => ({
  useAetherSocket: jest.fn(),
}));

describe('DiagnosticsPanel', () => {
  let mockSendMessage;

  beforeEach(() => {
    mockSendMessage = jest.fn();
    useAetherSocket.mockReturnValue({
      wsRef: { current: null },
      sendMessage: mockSendMessage,
    });
  });

  it('requests the MCP server list when the MCP menu is active', () => {
    render(<DiagnosticsPanel activeMenu="MCP" />);
    expect(mockSendMessage).toHaveBeenCalledWith(
      'MCP_SERVER_LIST_REQUEST',
      expect.objectContaining({ payload: {} })
    );
  });

  it('sends MCP_SERVER_ADD_REQUEST with parsed args when adding a server', () => {
    render(<DiagnosticsPanel activeMenu="MCP" />);

    fireEvent.change(screen.getByPlaceholderText('Server Name (e.g. SQLite DB)'), {
      target: { value: 'my-server' },
    });
    fireEvent.change(screen.getByPlaceholderText('Command (e.g. python, npx)'), {
      target: { value: 'npx' },
    });
    fireEvent.change(screen.getByPlaceholderText('Arguments (comma separated)'), {
      target: { value: '--yes, mcp-server' },
    });
    fireEvent.click(screen.getByText('Add Server'));

    expect(mockSendMessage).toHaveBeenCalledWith(
      'MCP_SERVER_ADD_REQUEST',
      expect.objectContaining({
        payload: {
          name: 'my-server',
          config: { command: 'npx', args: ['--yes', 'mcp-server'] },
        },
      })
    );
  });

  it('requests the plugin list when the Plugins menu is active', () => {
    render(<DiagnosticsPanel activeMenu="Plugins" />);
    expect(mockSendMessage).toHaveBeenCalledWith(
      'PLUGIN_LIST_REQUEST',
      expect.objectContaining({ payload: {} })
    );
  });
});
