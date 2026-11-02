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

  it('lists a connected server\'s resources and loads one into chat (006)', () => {
    const listeners = {};
    const mockWs = {
      addEventListener: jest.fn((type, fn) => { listeners[type] = fn; }),
      removeEventListener: jest.fn(),
    };
    useAetherSocket.mockReturnValue({
      wsRef: { current: mockWs },
      sendMessage: mockSendMessage,
    });
    render(<DiagnosticsPanel activeMenu="MCP" activeSession="sess-1" />);

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
    expect(mockSendMessage).toHaveBeenCalledWith(
      'MCP_RESOURCE_LIST_REQUEST',
      expect.objectContaining({ payload: { name: 'fake' } })
    );

    act(() => {
      listeners.message({
        data: JSON.stringify({
          type: 'MCP_RESOURCE_LIST_RESPONSE',
          payload: {
            success: true, name: 'fake',
            resources: [{ uri: 'fake://notes', name: 'notes', description: 'A note', mimeType: 'text/plain' }],
          },
        }),
      });
    });
    expect(screen.getByText('notes')).toBeInTheDocument();

    fireEvent.click(screen.getByText('Load into chat'));
    expect(mockSendMessage).toHaveBeenCalledWith(
      'MCP_RESOURCE_READ_REQUEST',
      expect.objectContaining({
        payload: { name: 'fake', uri: 'fake://notes', session_id: 'sess-1' },
      })
    );
  });

  it('shows a loaded resource instead of doing nothing (006)', () => {
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
          type: 'MCP_RESOURCE_READ_RESPONSE',
          payload: { success: true, name: 'fake', uri: 'fake://notes', truncated: false },
        }),
      });
    });
    expect(screen.getByText('Loaded fake://notes into chat')).toBeInTheDocument();

    act(() => {
      listeners.message({
        data: JSON.stringify({
          type: 'MCP_RESOURCE_READ_RESPONSE',
          payload: { success: false, name: 'fake', uri: 'fake://notes', error: 'not connected' },
        }),
      });
    });
    expect(screen.getByText('Load fake://notes failed: not connected')).toBeInTheDocument();
  });

  it('lists a connected server\'s prompts and runs one into chat (007)', () => {
    const listeners = {};
    const mockWs = {
      addEventListener: jest.fn((type, fn) => { listeners[type] = fn; }),
      removeEventListener: jest.fn(),
    };
    useAetherSocket.mockReturnValue({
      wsRef: { current: mockWs },
      sendMessage: mockSendMessage,
    });
    render(<DiagnosticsPanel activeMenu="MCP" activeSession="sess-1" />);

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
    expect(mockSendMessage).toHaveBeenCalledWith(
      'MCP_PROMPT_LIST_REQUEST',
      expect.objectContaining({ payload: { name: 'fake' } })
    );

    act(() => {
      listeners.message({
        data: JSON.stringify({
          type: 'MCP_PROMPT_LIST_RESPONSE',
          payload: {
            success: true, name: 'fake',
            prompts: [{ name: 'greet', description: 'Greet someone', arguments: [{ name: 'name', description: 'Who to greet', required: true }] }],
          },
        }),
      });
    });
    expect(screen.getByText('greet')).toBeInTheDocument();

    fireEvent.change(screen.getByPlaceholderText('name'), { target: { value: 'Arindam' } });
    fireEvent.click(screen.getByText('Run in chat'));
    expect(mockSendMessage).toHaveBeenCalledWith(
      'MCP_PROMPT_RUN_REQUEST',
      expect.objectContaining({
        payload: { name: 'fake', prompt: 'greet', arguments: { name: 'Arindam' }, session_id: 'sess-1' },
      })
    );
  });

  it('shows a run prompt result instead of doing nothing (007)', () => {
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
          type: 'MCP_PROMPT_RUN_RESPONSE',
          payload: { success: true, name: 'fake', prompt: 'greet' },
        }),
      });
    });
    expect(screen.getByText("Ran prompt 'greet' into chat")).toBeInTheDocument();

    act(() => {
      listeners.message({
        data: JSON.stringify({
          type: 'MCP_PROMPT_RUN_RESPONSE',
          payload: { success: false, name: 'fake', prompt: 'greet', error: 'not connected' },
        }),
      });
    });
    expect(screen.getByText("Run prompt 'greet' failed: not connected")).toBeInTheDocument();
  });

  it('requests the plugin list when the Plugins menu is active', () => {
    render(<DiagnosticsPanel activeMenu="Plugins" />);
    expect(mockSendMessage).toHaveBeenCalledWith(
      'PLUGIN_LIST_REQUEST',
      expect.objectContaining({ payload: {} })
    );
  });

  // E7-F05: the panel displays plugin.manifest.name but used to send
  // plugin.name (undefined on real LIST entries) on both management
  // buttons — JSON dropped the field, toggle failed unseen and
  // uninstall silently "succeeded". A real LIST record carries the
  // name only under manifest; the buttons must send THAT identity.
  const renderWithPlugin = () => {
    const listeners = {};
    const mockWs = {
      addEventListener: jest.fn((type, fn) => { listeners[type] = fn; }),
      removeEventListener: jest.fn(),
    };
    useAetherSocket.mockReturnValue({
      wsRef: { current: mockWs },
      sendMessage: mockSendMessage,
    });
    render(<DiagnosticsPanel activeMenu="Plugins" />);
    act(() => {
      listeners.message({
        data: JSON.stringify({
          type: 'PLUGIN_LIST_RESPONSE',
          payload: {
            plugins: [{
              manifest: { name: 'CoolPlugin', version: '2.0', description: 'A cool plugin' },
              enabled: true,
              installed_skills: [],
              installed_tools: [],
              installed_mcp: [],
            }],
          },
        }),
      });
    });
    return listeners;
  };

  it('sends the manifest name on plugin toggle and uninstall (E7-F05)', () => {
    renderWithPlugin();
    expect(screen.getByText('CoolPlugin')).toBeInTheDocument();

    fireEvent.click(screen.getByText('Enabled'));
    expect(mockSendMessage).toHaveBeenCalledWith(
      'PLUGIN_TOGGLE_REQUEST',
      expect.objectContaining({
        payload: { name: 'CoolPlugin', enabled: false },
      })
    );

    fireEvent.click(screen.getByText('Uninstall'));
    expect(mockSendMessage).toHaveBeenCalledWith(
      'PLUGIN_UNINSTALL_REQUEST',
      expect.objectContaining({
        payload: { name: 'CoolPlugin' },
      })
    );
  });

  it('shows a visible error when a plugin toggle fails (E7-F05)', () => {
    const listeners = renderWithPlugin();
    fireEvent.click(screen.getByText('Enabled'));
    act(() => {
      listeners.message({
        data: JSON.stringify({
          type: 'PLUGIN_TOGGLE_RESPONSE',
          payload: { success: false, name: 'CoolPlugin', error: 'Plugin not found: CoolPlugin' },
        }),
      });
    });
    expect(screen.getByText('Toggle CoolPlugin failed: Plugin not found: CoolPlugin')).toBeInTheDocument();
  });

  it('shows a visible error when a plugin uninstall fails (E7-F05)', () => {
    const listeners = renderWithPlugin();
    fireEvent.click(screen.getByText('Uninstall'));
    act(() => {
      listeners.message({
        data: JSON.stringify({
          type: 'PLUGIN_UNINSTALL_RESPONSE',
          payload: { success: false, name: 'CoolPlugin', error: 'Plugin not found: CoolPlugin' },
        }),
      });
    });
    expect(screen.getByText('Uninstall CoolPlugin failed: Plugin not found: CoolPlugin')).toBeInTheDocument();
  });
});
