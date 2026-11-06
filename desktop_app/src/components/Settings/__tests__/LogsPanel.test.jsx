import React from 'react';
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import LogsPanel from '../LogsPanel';

// Unified logging (5 Nov 2026): the Logs & Diagnostics cards are
// driven entirely by the read-logs IPC; these tests pin the
// status/viewer/controls behaviour against a mocked bridge, plus
// the honest unavailable state without one.

const READ_LOGS = {
  appTail: [
    '2026-11-05T10:00:00.000Z [main] INFO App ready.',
    '2026-11-05T10:01:00.000Z [ui] WARN engine socket close',
    '2026-11-05T10:02:00.000Z [main] ERROR WebSocket error: boom',
  ],
  engineTail: [
    '2026-11-05T10:00:30.000Z [engine] [engine] spawned bundled engine (pid 123)',
    '2026-11-05T10:03:00.000Z [engine] engine is healthy',
  ],
  inventory: [
    { name: 'app.log', size: 2048, mtime: '2026-11-05T10:02:00.000Z' },
    { name: 'engine.log', size: 512, mtime: '2026-11-05T10:03:00.000Z' },
  ],
  level: 'INFO',
};

function installBridge(overrides = {}) {
  window.electronAPI = {
    readLogs: jest.fn().mockResolvedValue(READ_LOGS),
    openLogsFolder: jest.fn(),
    openEngineLog: jest.fn(),
    clearLogs: jest.fn().mockResolvedValue({ ok: true }),
    setLogLevel: jest.fn().mockResolvedValue({ ok: true, level: 'DEBUG' }),
    ...overrides,
  };
  return window.electronAPI;
}

afterEach(() => {
  delete window.electronAPI;
  jest.useRealTimers();
});

describe('LogsPanel unavailable state', () => {
  it('says logs are unavailable without the desktop bridge', () => {
    delete window.electronAPI;
    render(<LogsPanel isConnected={false} />);
    expect(screen.getByTestId('logs-panel')).toHaveTextContent(/only available in the desktop app/i);
    expect(screen.queryByTestId('logs-lines')).not.toBeInTheDocument();
  });
});

describe('LogsPanel status card', () => {
  it('shows connection, version and the per-source inventory', async () => {
    installBridge();
    render(<LogsPanel isConnected />);
    expect(await screen.findByText('Engine Connected')).toBeInTheDocument();
    await screen.findByTestId('logs-inventory');
    expect(screen.getByTestId('logs-inventory')).toHaveTextContent('app.log');
    expect(screen.getByTestId('logs-inventory')).toHaveTextContent('2.0 KB');
    expect(screen.getByTestId('logs-inventory')).toHaveTextContent('engine.log');
    // crash-electron.log is absent from the inventory: no writes yet.
    expect(screen.getByTestId('logs-inventory')).toHaveTextContent('no writes yet');
  });

  it('shows the waiting state when the engine is not connected', async () => {
    installBridge();
    render(<LogsPanel isConnected={false} />);
    expect(await screen.findByText('Waiting for the Engine')).toBeInTheDocument();
  });
});

describe('LogsPanel viewer', () => {
  it('merges app and engine lines in the All tab, ordered by time', async () => {
    installBridge();
    render(<LogsPanel isConnected />);
    const lines = await screen.findByTestId('logs-lines');
    expect(lines).toHaveTextContent('App ready.');
    expect(lines).toHaveTextContent('spawned bundled engine');
    const text = lines.textContent;
    expect(text.indexOf('App ready.')).toBeLessThan(text.indexOf('spawned bundled engine'));
    expect(text.indexOf('spawned bundled engine')).toBeLessThan(text.indexOf('engine socket close'));
  });

  it('filters by source tab', async () => {
    installBridge();
    render(<LogsPanel isConnected />);
    await screen.findByTestId('logs-lines');
    fireEvent.click(screen.getByRole('button', { name: 'Engine' }));
    expect(screen.getByTestId('logs-lines')).toHaveTextContent('engine is healthy');
    expect(screen.getByTestId('logs-lines')).not.toHaveTextContent('App ready.');
    fireEvent.click(screen.getByRole('button', { name: 'App' }));
    expect(screen.getByTestId('logs-lines')).toHaveTextContent('App ready.');
    expect(screen.getByTestId('logs-lines')).not.toHaveTextContent('engine is healthy');
  });

  it('filters by search text', async () => {
    installBridge();
    render(<LogsPanel isConnected />);
    await screen.findByTestId('logs-lines');
    fireEvent.change(screen.getByPlaceholderText('Search logs…'), { target: { value: 'socket' } });
    expect(screen.getByTestId('logs-lines')).toHaveTextContent('engine socket close');
    expect(screen.getByTestId('logs-lines')).not.toHaveTextContent('App ready.');
  });

  it('filters by level: Errors shows only ERROR lines', async () => {
    installBridge();
    render(<LogsPanel isConnected />);
    await screen.findByTestId('logs-lines');
    fireEvent.change(screen.getByLabelText('Level filter'), { target: { value: 'ERROR' } });
    expect(screen.getByTestId('logs-lines')).toHaveTextContent('WebSocket error: boom');
    expect(screen.getByTestId('logs-lines')).not.toHaveTextContent('engine socket close');
    expect(screen.getByTestId('logs-lines')).not.toHaveTextContent('App ready.');
  });

  it('colours lines by level', async () => {
    installBridge();
    render(<LogsPanel isConnected />);
    const lines = await screen.findByTestId('logs-lines');
    const errorLine = [...lines.querySelectorAll('[data-log-level="ERROR"]')];
    expect(errorLine).toHaveLength(1);
    expect(errorLine[0]).toHaveTextContent('WebSocket error: boom');
  });

  it('copies the visible lines to the clipboard', async () => {
    installBridge();
    const writeText = jest.fn().mockResolvedValue();
    Object.defineProperty(window.navigator, 'clipboard', { value: { writeText }, configurable: true });
    render(<LogsPanel isConnected />);
    await screen.findByTestId('logs-lines');
    fireEvent.click(screen.getByRole('button', { name: 'Copy visible lines' }));
    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    expect(writeText.mock.calls[0][0]).toContain('App ready.');
    expect(writeText.mock.calls[0][0]).toContain('engine is healthy');
    expect(await screen.findByRole('button', { name: 'Copied' })).toBeInTheDocument();
  });

  it('polls every 3s while unpaused and stops while paused', async () => {
    jest.useFakeTimers();
    const bridge = installBridge();
    render(<LogsPanel isConnected />);
    await act(async () => {});
    expect(bridge.readLogs).toHaveBeenCalledTimes(1);

    await act(async () => { jest.advanceTimersByTime(3000); });
    expect(bridge.readLogs).toHaveBeenCalledTimes(2);

    fireEvent.click(screen.getByRole('button', { name: 'Pause' }));
    await act(async () => { jest.advanceTimersByTime(9000); });
    expect(bridge.readLogs).toHaveBeenCalledTimes(2);

    fireEvent.click(screen.getByRole('button', { name: 'Resume' }));
    await act(async () => { jest.advanceTimersByTime(3000); });
    expect(bridge.readLogs).toHaveBeenCalledTimes(3);
  });
});

describe('LogsPanel controls', () => {
  it('export button calls the onExportBundle prop', async () => {
    installBridge();
    const onExportBundle = jest.fn(() => 'sent');
    render(<LogsPanel isConnected onExportBundle={onExportBundle} />);
    await screen.findByTestId('logs-lines');
    fireEvent.click(screen.getByRole('button', { name: 'Export full bundle' }));
    expect(onExportBundle).toHaveBeenCalledTimes(1);
    expect(screen.getByText(/Export requested/)).toBeInTheDocument();
  });

  it('falls back to sendSettingsAction for the export when no callback is given', async () => {
    installBridge();
    const sendSettingsAction = jest.fn(() => 'queued');
    render(<LogsPanel isConnected sendSettingsAction={sendSettingsAction} />);
    await screen.findByTestId('logs-lines');
    fireEvent.click(screen.getByRole('button', { name: 'Export full bundle' }));
    expect(sendSettingsAction).toHaveBeenCalledWith('DIAGNOSTICS_EXPORT_REQUEST', {});
    expect(screen.getByText(/Export queued/)).toBeInTheDocument();
  });

  it('opens the logs folder and the engine log through the bridge', async () => {
    const bridge = installBridge();
    render(<LogsPanel isConnected />);
    await screen.findByTestId('logs-lines');
    fireEvent.click(screen.getByRole('button', { name: 'Open logs folder' }));
    expect(bridge.openLogsFolder).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: 'Open engine log' }));
    expect(bridge.openEngineLog).toHaveBeenCalledTimes(1);
  });

  it('clear logs needs a second, confirming click', async () => {
    const bridge = installBridge();
    render(<LogsPanel isConnected />);
    await screen.findByTestId('logs-lines');
    fireEvent.click(screen.getByRole('button', { name: 'Clear logs' }));
    expect(bridge.clearLogs).not.toHaveBeenCalled();
    expect(screen.getByText(/Click again to confirm/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Confirm clear logs' }));
    await waitFor(() => expect(bridge.clearLogs).toHaveBeenCalledTimes(1));
    expect(await screen.findByText(/Logs cleared/)).toBeInTheDocument();
  });

  it('log level select goes through the bridge', async () => {
    const bridge = installBridge();
    render(<LogsPanel isConnected />);
    await screen.findByTestId('logs-lines');
    fireEvent.change(screen.getByLabelText('Log level'), { target: { value: 'DEBUG' } });
    await waitFor(() => expect(bridge.setLogLevel).toHaveBeenCalledWith('DEBUG'));
  });
});
