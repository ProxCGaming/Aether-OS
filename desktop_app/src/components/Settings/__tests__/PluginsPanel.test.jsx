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
});
