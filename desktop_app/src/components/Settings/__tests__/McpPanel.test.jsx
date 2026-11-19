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

  describe('multi-pair header/env (D4-3 form half)', () => {
    test('a stdio add can carry several env pairs; all of them reach the staged config', () => {
      const { sendMessage } = makeHarness();
      render(<McpPanel />);
      fireEvent.change(screen.getByPlaceholderText('Server Name (e.g. SQLite DB)'), {
        target: { value: 'multi-env' },
      });
      fireEvent.change(screen.getByPlaceholderText('Command (e.g. python, npx)'), {
        target: { value: 'npx' },
      });
      fireEvent.change(screen.getByPlaceholderText('Env var name (local servers)'), {
        target: { value: 'FIRST' },
      });
      fireEvent.change(screen.getByPlaceholderText('Env var value'), {
        target: { value: 'one' },
      });
      fireEvent.click(screen.getByText('Add another env var'));
      const keyInputs = screen.getAllByPlaceholderText('Env var name (local servers)');
      const valueInputs = screen.getAllByPlaceholderText('Env var value');
      fireEvent.change(keyInputs[1], { target: { value: 'SECOND' } });
      fireEvent.change(valueInputs[1], { target: { value: 'two' } });
      fireEvent.click(screen.getByText('Add Server'));
      fireEvent.click(screen.getByText('Confirm and add'));
      expect(sendMessage).toHaveBeenCalledWith('MCP_SERVER_ADD_REQUEST', expect.objectContaining({
        payload: expect.objectContaining({
          name: 'multi-env',
          config: expect.objectContaining({ env: { FIRST: 'one', SECOND: 'two' } }),
        }),
      }));
    });

    test('a remote add can carry several headers', () => {
      const { sendMessage } = makeHarness();
      render(<McpPanel />);
      fireEvent.change(screen.getByPlaceholderText('Server Name (e.g. SQLite DB)'), {
        target: { value: 'multi-header' },
      });
      fireEvent.change(screen.getByPlaceholderText(/Server URL for remote/), {
        target: { value: 'https://example.test/mcp' },
      });
      fireEvent.change(screen.getByPlaceholderText('Header name (e.g. x-api-key)'), {
        target: { value: 'x-api-key' },
      });
      fireEvent.change(screen.getByPlaceholderText('Header value (use ${VAR_NAME} for secrets)'), {
        target: { value: '${KEY}' },
      });
      fireEvent.click(screen.getByText('Add another header'));
      const keyInputs = screen.getAllByPlaceholderText('Header name (e.g. x-api-key)');
      const valueInputs = screen.getAllByPlaceholderText('Header value (use ${VAR_NAME} for secrets)');
      fireEvent.change(keyInputs[1], { target: { value: 'x-team' } });
      fireEvent.change(valueInputs[1], { target: { value: 'blue' } });
      fireEvent.click(screen.getByText('Add Server'));
      fireEvent.click(screen.getByText('Confirm and add'));
      expect(sendMessage).toHaveBeenCalledWith('MCP_SERVER_ADD_REQUEST', expect.objectContaining({
        payload: expect.objectContaining({
          config: expect.objectContaining({
            headers: { 'x-api-key': '${KEY}', 'x-team': 'blue' },
          }),
        }),
      }));
    });
  });

  describe('per-server tool detail (D4-2)', () => {
    const CONNECTED = {
      db: {
        command: 'python', args: ['-m', 'db'], status: 'connected', tool_count: 1,
        confirmed: true,
        tools: [{
          name: 'db__query', tool_name: 'query', description: 'Run a query',
          risk: 'execute', source: 'mcp:db', policy: 'Require Approval',
          effective_policy: 'ask', policy_is_default: true,
          always_allow_permitted: false,
        }],
      },
    };

    test('expanding a connected server lists its tools with their policy state', () => {
      const { simulateMessage } = makeHarness();
      render(<McpPanel />);
      simulateMessage({ type: 'MCP_SERVER_LIST_RESPONSE', payload: { servers: CONNECTED } });
      fireEvent.click(screen.getByText('Show tools (1)'));
      expect(screen.getByText('query')).toBeInTheDocument();
      expect(screen.getByText('Run a query')).toBeInTheDocument();
      expect(screen.getByText(/Default policy — no override set/)).toBeInTheDocument();
    });

    test('setting a policy from the detail sends the shared policy request for the prefixed name', () => {
      const { simulateMessage, sendMessage } = makeHarness();
      render(<McpPanel />);
      simulateMessage({ type: 'MCP_SERVER_LIST_RESPONSE', payload: { servers: CONNECTED } });
      fireEvent.click(screen.getByText('Show tools (1)'));
      fireEvent.click(screen.getByRole('button', { name: 'Require Approval' }));
      fireEvent.click(screen.getByText('Deny'));
      expect(sendMessage).toHaveBeenCalledWith('TOOL_POLICY_SET_REQUEST', expect.objectContaining({
        payload: { tool_name: 'db__query', policy: 'Deny' },
      }));
    });

    test('a disconnected server says honestly that its tools need a connection', () => {
      const { simulateMessage } = makeHarness();
      render(<McpPanel />);
      simulateMessage({
        type: 'MCP_SERVER_LIST_RESPONSE',
        payload: { servers: { db: { command: 'python', args: [], status: 'disconnected', confirmed: true, tools: null } } },
      });
      fireEvent.click(screen.getByText('Show tools'));
      expect(screen.getByText('Connect this server to see the tools it offers.')).toBeInTheDocument();
    });

    test('a connected server with an empty tool list says so', () => {
      const { simulateMessage } = makeHarness();
      render(<McpPanel />);
      simulateMessage({
        type: 'MCP_SERVER_LIST_RESPONSE',
        payload: { servers: { db: { command: 'python', args: [], status: 'connected', tool_count: 0, confirmed: true, tools: [] } } },
      });
      fireEvent.click(screen.getByText('Show tools (0)'));
      expect(screen.getByText('This server offers no tools.')).toBeInTheDocument();
    });
  });

  describe('edit-in-place (D4-3 remainder)', () => {
    const STDIO = {
      srv: { command: 'npx', args: ['-y', 'old-pkg'], status: 'disconnected', confirmed: true, env: [], headers: [] },
    };

    test('changing what runs stages the edit behind the card naming the new command', () => {
      const { simulateMessage, sendMessage } = makeHarness();
      render(<McpPanel />);
      simulateMessage({ type: 'MCP_SERVER_LIST_RESPONSE', payload: { servers: STDIO } });
      fireEvent.click(screen.getByText('Edit'));
      // The edit form's args field is the second with this
      // placeholder (the Add form's is the first).
      fireEvent.change(screen.getAllByPlaceholderText('Arguments (comma separated)')[1], {
        target: { value: '-y, new-pkg' },
      });
      fireEvent.click(screen.getByText('Save changes'));
      // Nothing sent yet: the card names the NEW command first.
      expect(sendMessage).not.toHaveBeenCalledWith('MCP_SERVER_UPDATE_REQUEST', expect.anything());
      expect(screen.getByText('Before it runs')).toBeInTheDocument();
      expect(screen.getByText(/Saving changes to/)).toBeInTheDocument();
      expect(screen.getAllByText(/npx -y new-pkg/).length).toBeGreaterThan(0);
      fireEvent.click(screen.getByText('Confirm and save changes'));
      expect(sendMessage).toHaveBeenCalledWith('MCP_SERVER_UPDATE_REQUEST', expect.objectContaining({
        payload: expect.objectContaining({
          name: 'srv',
          confirmed: true,
          config: expect.objectContaining({ command: 'npx', args: ['-y', 'new-pkg'] }),
        }),
      }));
    });

    test('an edit that changes nothing runnable saves directly, with no card and no confirmation claim', () => {
      const { simulateMessage, sendMessage } = makeHarness();
      render(<McpPanel />);
      simulateMessage({ type: 'MCP_SERVER_LIST_RESPONSE', payload: { servers: STDIO } });
      fireEvent.click(screen.getByText('Edit'));
      fireEvent.change(screen.getByPlaceholderText('Timeout (seconds)'), {
        target: { value: '60' },
      });
      fireEvent.click(screen.getByText('Save changes'));
      expect(screen.queryByText('Before it runs')).not.toBeInTheDocument();
      expect(sendMessage).toHaveBeenCalledWith('MCP_SERVER_UPDATE_REQUEST', expect.objectContaining({
        payload: expect.objectContaining({
          name: 'srv',
          config: expect.objectContaining({ timeout: 60 }),
        }),
      }));
      const updateCall = sendMessage.mock.calls.find((c) => c[0] === 'MCP_SERVER_UPDATE_REQUEST');
      expect(updateCall[1].payload).not.toHaveProperty('confirmed');
    });

    test('a stored header left blank in the edit is sent as keep (null), never as an empty value', () => {
      const { simulateMessage, sendMessage } = makeHarness();
      render(<McpPanel />);
      simulateMessage({
        type: 'MCP_SERVER_LIST_RESPONSE',
        payload: {
          servers: {
            remote: {
              url: 'https://example.test/mcp', status: 'disconnected', confirmed: true,
              headers: ['x-api-key'], env: [],
            },
          },
        },
      });
      fireEvent.click(screen.getByText('Edit'));
      fireEvent.change(screen.getByPlaceholderText('Timeout (seconds)'), {
        target: { value: '45' },
      });
      fireEvent.click(screen.getByText('Save changes'));
      expect(sendMessage).toHaveBeenCalledWith('MCP_SERVER_UPDATE_REQUEST', expect.objectContaining({
        payload: expect.objectContaining({
          config: expect.objectContaining({ headers: { 'x-api-key': null } }),
        }),
      }));
    });
  });

  describe('registry import + update state (FF4-04)', () => {
    const SERVER_JSON = JSON.stringify({
      name: 'io.example/widgets',
      version: '1.0.0',
      packages: [{
        registryType: 'npm', identifier: '@example/widgets-mcp', version: '1.0.0',
        transport: { type: 'stdio' },
        environmentVariables: [{ name: 'WIDGET_TOKEN' }],
      }],
    });

    test('pasting a server.json fills the form and the add carries the provenance', () => {
      const { sendMessage } = makeHarness();
      render(<McpPanel />);
      fireEvent.change(screen.getByPlaceholderText('Paste a server.json from the MCP registry…'), {
        target: { value: SERVER_JSON },
      });
      fireEvent.click(screen.getByText('Import server.json'));
      expect(screen.getByPlaceholderText('Server Name (e.g. SQLite DB)')).toHaveValue('io.example/widgets');
      expect(screen.getByPlaceholderText('Command (e.g. python, npx)')).toHaveValue('npx');
      fireEvent.click(screen.getByText('Add Server'));
      fireEvent.click(screen.getByText('Confirm and add'));
      expect(sendMessage).toHaveBeenCalledWith('MCP_SERVER_ADD_REQUEST', expect.objectContaining({
        payload: expect.objectContaining({
          name: 'io.example/widgets',
          config: expect.objectContaining({
            command: 'npx',
            args: ['-y', '@example/widgets-mcp@1.0.0'],
            env: { WIDGET_TOKEN: '' },
            registry_name: 'io.example/widgets',
            version: '1.0.0',
          }),
        }),
      }));
    });

    test('an imported server with a recorded update shows it and matches the Updates chip', () => {
      const { simulateMessage, sendMessage } = makeHarness();
      render(<McpPanel />);
      simulateMessage({
        type: 'MCP_SERVER_LIST_RESPONSE',
        payload: {
          servers: {
            widgets: {
              command: 'npx', args: ['-y', '@example/widgets-mcp@1.0.0'], status: 'disconnected',
              confirmed: true, registry_name: 'io.example/widgets', version: '1.0.0',
              registry_latest_version: '1.2.0', has_update: true,
            },
            plain: { command: 'python', args: [], status: 'disconnected', confirmed: true },
          },
        },
      });
      expect(screen.getByText(/update available: 1.2.0/)).toBeInTheDocument();
      fireEvent.click(screen.getByText('Updates · 1'));
      expect(screen.getByText('widgets')).toBeInTheDocument();
      expect(screen.queryByText('plain')).not.toBeInTheDocument();
      fireEvent.click(screen.getByText('Check for updates'));
      expect(sendMessage).toHaveBeenCalledWith('MCP_SERVER_UPDATE_CHECK_REQUEST', expect.objectContaining({
        payload: { name: 'widgets' },
      }));
    });
  });
});
