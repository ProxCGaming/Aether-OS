/** Wave 5 (Annex C row 8): PluginsPanel extracted from
 * DiagnosticsPanel.jsx. The panel owns the Plugins tab's socket
 * state on its own: list request on mount, load state until the
 * engine answers, action failures visible (E7-F05). */
import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import PluginsPanel from '../PluginsPanel';
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

describe('PluginsPanel', () => {
  test('requests the plugin list on mount and shows the loading state until it answers', () => {
    const { sendMessage } = makeHarness();
    render(<PluginsPanel />);
    expect(sendMessage).toHaveBeenCalledWith(
      'PLUGIN_LIST_REQUEST',
      expect.objectContaining({ payload: {} })
    );
    expect(screen.getByTestId('plugins-load-state')).toHaveAttribute('data-load-display', 'loading');
    expect(screen.queryByText('No plugins installed.')).not.toBeInTheDocument();
  });

  test('a listed plugin renders and its toggle sends PLUGIN_TOGGLE_REQUEST', () => {
    const { simulateMessage, sendMessage } = makeHarness();
    render(<PluginsPanel />);
    simulateMessage({
      type: 'PLUGIN_LIST_RESPONSE',
      payload: { plugins: [{ name: 'weather', enabled: true, manifest: { name: 'weather', version: '2.0', description: 'Weather tools' } }] },
    });
    expect(screen.getByText('weather')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Enabled'));
    expect(sendMessage).toHaveBeenCalledWith(
      'PLUGIN_TOGGLE_REQUEST',
      expect.objectContaining({ payload: { name: 'weather', enabled: false } })
    );
  });

  test('a failed uninstall is visible (E7-F05)', () => {
    const { simulateMessage } = makeHarness();
    render(<PluginsPanel />);
    simulateMessage({
      type: 'PLUGIN_UNINSTALL_RESPONSE',
      payload: { success: false, name: 'weather', error: 'plugin is in use' },
    });
    expect(screen.getByText(/Uninstall weather failed: plugin is in use/)).toBeInTheDocument();
  });

  test('a registry load error shows the error state, not "No plugins installed." (D4-5 / FF4-07)', () => {
    const { simulateMessage } = makeHarness();
    render(<PluginsPanel />);
    simulateMessage({
      type: 'PLUGIN_LIST_RESPONSE',
      payload: {
        plugins: [],
        registry_load_error: 'Plugin registry file /home/user/.aether/plugins/registry.json is corrupt and was not loaded: Expecting value: line 1 column 1 (char 0)',
      },
    });
    const errorRow = screen.getByTestId('plugins-registry-error');
    expect(errorRow).toHaveAttribute('data-load-display', 'error');
    expect(errorRow).toHaveTextContent(/is corrupt and was not loaded/);
    expect(screen.queryByText('No plugins installed.')).not.toBeInTheDocument();
  });

  test('a healthy empty registry still reads as genuinely empty (D4-5 control)', () => {
    const { simulateMessage } = makeHarness();
    render(<PluginsPanel />);
    simulateMessage({
      type: 'PLUGIN_LIST_RESPONSE',
      payload: { plugins: [], registry_load_error: null },
    });
    expect(screen.getByText('No plugins installed.')).toBeInTheDocument();
    expect(screen.queryByTestId('plugins-registry-error')).not.toBeInTheDocument();
  });

  describe('uninstall confirmation (D4-4 confirm half / FF4-10)', () => {
    const PLUGIN_ROW = { name: 'weather', enabled: true, manifest: { name: 'weather', version: '2.0', description: 'Weather tools' } };

    afterEach(() => {
      jest.restoreAllMocks();
    });

    test('cancelling the confirmation sends nothing and leaves the plugin listed', () => {
      const confirmSpy = jest.spyOn(window, 'confirm').mockReturnValue(false);
      const { simulateMessage, sendMessage } = makeHarness();
      render(<PluginsPanel />);
      simulateMessage({ type: 'PLUGIN_LIST_RESPONSE', payload: { plugins: [PLUGIN_ROW] } });
      fireEvent.click(screen.getByText('Uninstall'));
      expect(confirmSpy).toHaveBeenCalledWith(expect.stringContaining('"weather"'));
      expect(sendMessage).not.toHaveBeenCalledWith(
        'PLUGIN_UNINSTALL_REQUEST', expect.anything());
      expect(screen.getByText('weather')).toBeInTheDocument();
    });

    test('confirming sends PLUGIN_UNINSTALL_REQUEST with the plugin name', () => {
      jest.spyOn(window, 'confirm').mockReturnValue(true);
      const { simulateMessage, sendMessage } = makeHarness();
      render(<PluginsPanel />);
      simulateMessage({ type: 'PLUGIN_LIST_RESPONSE', payload: { plugins: [PLUGIN_ROW] } });
      fireEvent.click(screen.getByText('Uninstall'));
      expect(sendMessage).toHaveBeenCalledWith(
        'PLUGIN_UNINSTALL_REQUEST',
        expect.objectContaining({ payload: { name: 'weather' } })
      );
    });
  });

  describe('shared capability search/filter (D4-1)', () => {
    const PLUGINS = [
      { name: 'weather', enabled: true, manifest: { name: 'weather', version: '2.0', description: 'Weather tools' } },
      { name: 'stocks', enabled: false, manifest: { name: 'stocks', version: '1.0', description: 'Stock quotes' } },
    ];

    test('search narrows by name and the Disabled chip shows only disabled plugins', () => {
      const { simulateMessage } = makeHarness();
      render(<PluginsPanel />);
      simulateMessage({ type: 'PLUGIN_LIST_RESPONSE', payload: { plugins: PLUGINS } });
      fireEvent.change(screen.getByPlaceholderText('Search plugins by name…'), { target: { value: 'wea' } });
      expect(screen.getByText('weather')).toBeInTheDocument();
      expect(screen.queryByText('stocks')).not.toBeInTheDocument();
      fireEvent.change(screen.getByPlaceholderText('Search plugins by name…'), { target: { value: '' } });
      fireEvent.click(screen.getByText('Disabled · 1'));
      expect(screen.getByText('stocks')).toBeInTheDocument();
      expect(screen.queryByText('weather')).not.toBeInTheDocument();
    });

    test('the Updates chip honestly matches nothing yet and the no-results state clears back', () => {
      const { simulateMessage } = makeHarness();
      render(<PluginsPanel />);
      simulateMessage({ type: 'PLUGIN_LIST_RESPONSE', payload: { plugins: PLUGINS } });
      fireEvent.click(screen.getByText('Updates · 0'));
      expect(screen.getByTestId('plugins-no-results')).toBeInTheDocument();
      fireEvent.click(screen.getByText('Show all plugins'));
      expect(screen.getByText('weather')).toBeInTheDocument();
      expect(screen.getByText('stocks')).toBeInTheDocument();
    });
  });

  describe('in-panel install approval (D4-4)', () => {
    const DETAILS = {
      source: '/tmp/weather.zip',
      manifest: {
        name: 'weather',
        version: '2.0',
        description: 'Weather tools',
        skills: [{ name: 'forecast' }],
        tools: [],
        mcp_servers: [{ name: 'weather-api' }],
        files: { 'skill.md': 'a'.repeat(64) },
      },
      mcp_server_summaries: [{ name: 'weather-api', runs: 'npx -y weather-mcp', env_keys: ['WEATHER_API_KEY'] }],
    };

    function startPanelInstall(simulateMessage, sendMessage) {
      fireEvent.change(screen.getByPlaceholderText('GitHub URL or local path'), { target: { value: '/tmp/weather.zip' } });
      fireEvent.click(screen.getByText('Install'));
      const call = sendMessage.mock.calls.find(([type]) => type === 'PLUGIN_INSTALL_REQUEST');
      expect(call).toBeTruthy();
      return call[1].request_id;
    }

    test('an install started here renders the trust card in the panel and approves under the same id', () => {
      const { simulateMessage, sendMessage } = makeHarness();
      render(<PluginsPanel />);
      const requestId = startPanelInstall(simulateMessage, sendMessage);
      simulateMessage({
        type: 'PLUGIN_APPROVAL_REQUEST',
        request_id: requestId,
        payload: { plugin_name: 'weather', plugin_details: DETAILS },
      });
      expect(screen.getByTestId('plugin-install-approval')).toBeInTheDocument();
      expect(screen.getByText('Adds 1 skill: forecast')).toBeInTheDocument();
      expect(screen.getByText('Adds MCP server `weather-api` (runs `npx -y weather-mcp`)')).toBeInTheDocument();
      fireEvent.click(screen.getByText('Approve install'));
      expect(sendMessage).toHaveBeenCalledWith(
        'PLUGIN_APPROVAL_GRANTED',
        expect.objectContaining({ request_id: requestId, payload: {} }),
      );
      expect(screen.queryByTestId('plugin-install-approval')).not.toBeInTheDocument();
    });

    test('rejecting sends PLUGIN_APPROVAL_REJECTED under the approval id', () => {
      const { simulateMessage, sendMessage } = makeHarness();
      render(<PluginsPanel />);
      const requestId = startPanelInstall(simulateMessage, sendMessage);
      simulateMessage({
        type: 'PLUGIN_APPROVAL_REQUEST',
        request_id: requestId,
        payload: { plugin_name: 'weather', plugin_details: DETAILS },
      });
      fireEvent.click(screen.getByText('Reject'));
      expect(sendMessage).toHaveBeenCalledWith(
        'PLUGIN_APPROVAL_REJECTED',
        expect.objectContaining({ request_id: requestId }),
      );
    });

    test('an approval for an install started elsewhere is NOT claimed by the panel', () => {
      const { simulateMessage } = makeHarness();
      render(<PluginsPanel />);
      simulateMessage({
        type: 'PLUGIN_APPROVAL_REQUEST',
        request_id: 'chat-started-1',
        payload: { plugin_name: 'weather', plugin_details: DETAILS },
      });
      expect(screen.queryByTestId('plugin-install-approval')).not.toBeInTheDocument();
    });
  });

  describe('per-plugin load error row (D4-5 remainder)', () => {
    test('a broken plugin renders its error state with no toggle, uninstall only', () => {
      const { simulateMessage } = makeHarness();
      render(<PluginsPanel />);
      simulateMessage({
        type: 'PLUGIN_LIST_RESPONSE',
        payload: {
          plugins: [
            { name: 'broken', enabled: false, state: 'load_error', error: "stored manifest failed validation: name missing", manifest: null },
          ],
        },
      });
      expect(screen.getByText('Load error')).toBeInTheDocument();
      expect(screen.getByText(/stored manifest failed validation/)).toBeInTheDocument();
      expect(screen.getByText(/None of this plugin's skills or MCP servers are active/)).toBeInTheDocument();
      expect(screen.queryByText('Disabled')).not.toBeInTheDocument();
      expect(screen.getByText('Uninstall')).toBeInTheDocument();
    });
  });

  describe('update checks (FF4-04 / PC-13)', () => {
    const PLUGIN_ROW = {
      name: 'weather',
      enabled: true,
      source: '/tmp/weather-src',
      manifest: { name: 'weather', version: '1.0', description: 'Weather tools' },
    };

    test('a check that finds a grown update surfaces it on the row and feeds the Updates chip', () => {
      const { simulateMessage, sendMessage } = makeHarness();
      render(<PluginsPanel />);
      simulateMessage({ type: 'PLUGIN_LIST_RESPONSE', payload: { plugins: [PLUGIN_ROW] } });
      fireEvent.click(screen.getByText('Check for updates'));
      expect(sendMessage).toHaveBeenCalledWith(
        'PLUGIN_UPDATE_CHECK_REQUEST',
        expect.objectContaining({ payload: { name: 'weather' } }),
      );
      simulateMessage({
        type: 'PLUGIN_UPDATE_CHECK_RESPONSE',
        payload: {
          success: true, name: 'weather', checked: true, error: null,
          installed_version: '1.0', latest_version: '2.0', has_update: true,
          permissions_grew: true,
          added_skills: ['alerts'], removed_skills: [], changed_skills: [],
          added_mcp_servers: [], removed_mcp_servers: [], changed_mcp_servers: [],
        },
      });
      expect(screen.getByText('Update available')).toBeInTheDocument();
      expect(screen.getByText(/Version 2.0 is available \(you have 1.0\)/)).toBeInTheDocument();
      expect(screen.getByText(/adds skill\(s\): alerts/)).toBeInTheDocument();
      expect(screen.getByText(/can do more than the version you approved/)).toBeInTheDocument();
      expect(screen.getByText('Updates · 1')).toBeInTheDocument();
    });

    test('Review update pre-fills the install source so the approval flow re-runs', () => {
      const { simulateMessage } = makeHarness();
      render(<PluginsPanel />);
      simulateMessage({ type: 'PLUGIN_LIST_RESPONSE', payload: { plugins: [PLUGIN_ROW] } });
      simulateMessage({
        type: 'PLUGIN_UPDATE_CHECK_RESPONSE',
        payload: {
          success: true, name: 'weather', checked: true, error: null,
          installed_version: '1.0', latest_version: '2.0', has_update: true,
          permissions_grew: false,
          added_skills: [], removed_skills: [], changed_skills: ['forecast'],
          added_mcp_servers: [], removed_mcp_servers: [], changed_mcp_servers: [],
        },
      });
      fireEvent.click(screen.getByText('Review update'));
      expect(screen.getByPlaceholderText('GitHub URL or local path')).toHaveValue('/tmp/weather-src');
    });

    test('a check that cannot run shows the engine reason on the row', () => {
      const { simulateMessage } = makeHarness();
      render(<PluginsPanel />);
      simulateMessage({ type: 'PLUGIN_LIST_RESPONSE', payload: { plugins: [PLUGIN_ROW] } });
      simulateMessage({
        type: 'PLUGIN_UPDATE_CHECK_RESPONSE',
        payload: {
          success: true, name: 'weather', checked: false,
          error: 'no install source was recorded for this plugin, so it cannot be checked for updates',
          installed_version: '1.0', latest_version: null, has_update: false,
          permissions_grew: false,
          added_skills: [], removed_skills: [], changed_skills: [],
          added_mcp_servers: [], removed_mcp_servers: [], changed_mcp_servers: [],
        },
      });
      expect(screen.getByText(/no install source was recorded/)).toBeInTheDocument();
      expect(screen.queryByText('Update available')).not.toBeInTheDocument();
    });
  });
});
