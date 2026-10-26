import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
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

  it('sends MCP_SERVER_ADD_REQUEST with a streamable_http config when a URL is given', () => {
    render(<DiagnosticsPanel activeMenu="MCP" />);

    fireEvent.change(screen.getByPlaceholderText('Server Name (e.g. SQLite DB)'), {
      target: { value: 'fake-remote' },
    });
    fireEvent.change(
      screen.getByPlaceholderText(/Server URL for remote/),
      { target: { value: 'http://127.0.0.1:9000/mcp' } }
    );
    fireEvent.click(screen.getByText('Add Server'));

    expect(mockSendMessage).toHaveBeenCalledWith(
      'MCP_SERVER_ADD_REQUEST',
      expect.objectContaining({
        payload: {
          name: 'fake-remote',
          config: { transport: 'streamable_http', url: 'http://127.0.0.1:9000/mcp' },
        },
      })
    );
  });

  it('sends headers with a remote server add (S6: 21st.dev without hand-editing JSON)', () => {
    render(<DiagnosticsPanel activeMenu="MCP" />);

    fireEvent.change(screen.getByPlaceholderText('Server Name (e.g. SQLite DB)'), {
      target: { value: '21st' },
    });
    fireEvent.change(
      screen.getByPlaceholderText(/Server URL for remote/),
      { target: { value: 'https://21st.dev/api/mcp' } }
    );
    fireEvent.change(screen.getByPlaceholderText('Header name (e.g. x-api-key)'), {
      target: { value: 'x-api-key' },
    });
    fireEvent.change(screen.getByPlaceholderText(/Header value/), {
      target: { value: '${TWENTY_FIRST_KEY}' },
    });
    fireEvent.click(screen.getByText('Add Server'));

    expect(mockSendMessage).toHaveBeenCalledWith(
      'MCP_SERVER_ADD_REQUEST',
      expect.objectContaining({
        payload: {
          name: '21st',
          config: {
            transport: 'streamable_http',
            url: 'https://21st.dev/api/mcp',
            headers: { 'x-api-key': '${TWENTY_FIRST_KEY}' },
          },
        },
      })
    );
  });

  it('warns instead of silently sending only the URL when both command and URL are filled (S6)', () => {
    render(<DiagnosticsPanel activeMenu="MCP" />);

    fireEvent.change(screen.getByPlaceholderText('Server Name (e.g. SQLite DB)'), {
      target: { value: 'confused' },
    });
    fireEvent.change(screen.getByPlaceholderText('Command (e.g. python, npx)'), {
      target: { value: 'npx' },
    });
    fireEvent.change(
      screen.getByPlaceholderText(/Server URL for remote/),
      { target: { value: 'http://127.0.0.1:9000/mcp' } }
    );
    fireEvent.click(screen.getByText('Add Server'));

    expect(mockSendMessage).not.toHaveBeenCalledWith(
      'MCP_SERVER_ADD_REQUEST',
      expect.anything()
    );
    expect(screen.getByText(/Both a command and a URL are filled/)).toBeInTheDocument();
  });

  it('sends env and cwd with a local server add (S6)', () => {
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
    fireEvent.change(screen.getByPlaceholderText('Env var name (local servers)'), {
      target: { value: 'MY_VAR' },
    });
    fireEvent.change(screen.getByPlaceholderText('Env var value'), {
      target: { value: 'hello' },
    });
    fireEvent.change(screen.getByPlaceholderText('Working directory (cwd, local servers)'), {
      target: { value: '/tmp/work' },
    });
    fireEvent.click(screen.getByText('Add Server'));

    expect(mockSendMessage).toHaveBeenCalledWith(
      'MCP_SERVER_ADD_REQUEST',
      expect.objectContaining({
        payload: {
          name: 'my-server',
          config: {
            command: 'npx',
            args: ['--yes', 'mcp-server'],
            env: { MY_VAR: 'hello' },
            cwd: '/tmp/work',
          },
        },
      })
    );
  });

  it('shows a failed Connect instead of doing nothing (S9)', () => {
    const listeners = {};
    const mockWs = {
      addEventListener: jest.fn((type, fn) => { listeners[type] = fn; }),
      removeEventListener: jest.fn(),
    };
    useAetherSocket.mockReturnValue({
      wsRef: { current: mockWs },
      sendMessage: mockSendMessage,
    });
    render(<DiagnosticsPanel activeMenu="MCP" />);

    act(() => {
      listeners.message({
        data: JSON.stringify({
          type: 'MCP_SERVER_CONNECT_RESPONSE',
          payload: { success: false, name: 'fake', error: 'connection refused' },
        }),
      });
    });
    expect(screen.getByText('Connect fake failed: connection refused')).toBeInTheDocument();
  });

  it('shows a config file error from the server list (B1)', () => {
    const listeners = {};
    const mockWs = {
      addEventListener: jest.fn((type, fn) => { listeners[type] = fn; }),
      removeEventListener: jest.fn(),
    };
    useAetherSocket.mockReturnValue({
      wsRef: { current: mockWs },
      sendMessage: mockSendMessage,
    });
    render(<DiagnosticsPanel activeMenu="MCP" />);

    act(() => {
      listeners.message({
        data: JSON.stringify({
          type: 'MCP_SERVER_LIST_RESPONSE',
          payload: { servers: {}, config_error: 'MCP config file is corrupt' },
        }),
      });
    });
    expect(screen.getByText(/MCP config file error: MCP config file is corrupt/)).toBeInTheDocument();
  });

  it('shows auto-connect and enabled toggles from the server list (004)', () => {
    const listeners = {};
    const mockWs = {
      addEventListener: jest.fn((type, fn) => { listeners[type] = fn; }),
      removeEventListener: jest.fn(),
    };
    useAetherSocket.mockReturnValue({
      wsRef: { current: mockWs },
      sendMessage: mockSendMessage,
    });
    render(<DiagnosticsPanel activeMenu="MCP" />);

    act(() => {
      listeners.message({
        data: JSON.stringify({
          type: 'MCP_SERVER_LIST_RESPONSE',
          payload: {
            servers: {
              fake: { transport: 'streamable_http', url: 'http://127.0.0.1:9000/mcp', status: 'connected', tool_count: 1, auto_connect: true, enabled: true },
              off: { transport: 'streamable_http', url: 'http://127.0.0.1:9001/mcp', status: 'disabled', tool_count: 0, auto_connect: false, enabled: false },
            },
          },
        }),
      });
    });

    const autoConnectBoxes = screen.getAllByLabelText('Auto-connect on startup');
    expect(autoConnectBoxes[0].checked).toBe(true);
    expect(autoConnectBoxes[1].checked).toBe(false);
    const enabledBoxes = screen.getAllByLabelText('Enabled');
    expect(enabledBoxes[0].checked).toBe(true);
    expect(enabledBoxes[1].checked).toBe(false);
    expect(screen.getByText(/Status: connected — 1 tool/)).toBeInTheDocument();
    expect(screen.getByText(/Status: disabled/)).toBeInTheDocument();
  });

  it('unchecking auto-connect sends MCP_SERVER_UPDATE_REQUEST (004)', () => {
    const listeners = {};
    const mockWs = {
      addEventListener: jest.fn((type, fn) => { listeners[type] = fn; }),
      removeEventListener: jest.fn(),
    };
    useAetherSocket.mockReturnValue({
      wsRef: { current: mockWs },
      sendMessage: mockSendMessage,
    });
    render(<DiagnosticsPanel activeMenu="MCP" />);

    act(() => {
      listeners.message({
        data: JSON.stringify({
          type: 'MCP_SERVER_LIST_RESPONSE',
          payload: {
            servers: {
              fake: { transport: 'streamable_http', url: 'http://127.0.0.1:9000/mcp', status: 'connected', tool_count: 1, auto_connect: true, enabled: true },
            },
          },
        }),
      });
    });

    fireEvent.click(screen.getByLabelText('Auto-connect on startup'));
    expect(mockSendMessage).toHaveBeenCalledWith(
      'MCP_SERVER_UPDATE_REQUEST',
      expect.objectContaining({
        payload: { name: 'fake', auto_connect: false },
      })
    );
  });

  it('shows a failed server update instead of doing nothing (004)', () => {
    const listeners = {};
    const mockWs = {
      addEventListener: jest.fn((type, fn) => { listeners[type] = fn; }),
      removeEventListener: jest.fn(),
    };
    useAetherSocket.mockReturnValue({
      wsRef: { current: mockWs },
      sendMessage: mockSendMessage,
    });
    render(<DiagnosticsPanel activeMenu="MCP" />);

    act(() => {
      listeners.message({
        data: JSON.stringify({
          type: 'MCP_SERVER_UPDATE_RESPONSE',
          payload: { success: false, name: 'fake', error: "Unknown MCP server 'fake'" },
        }),
      });
    });
    expect(screen.getByText("Update fake failed: Unknown MCP server 'fake'")).toBeInTheDocument();
  });

  it('requests the plugin list when the Plugins menu is active', () => {
    render(<DiagnosticsPanel activeMenu="Plugins" />);
    expect(mockSendMessage).toHaveBeenCalledWith(
      'PLUGIN_LIST_REQUEST',
      expect.objectContaining({ payload: {} })
    );
  });
});
