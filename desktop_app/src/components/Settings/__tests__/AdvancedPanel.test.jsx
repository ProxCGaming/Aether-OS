/** Wave 5 (Annex C row 7): AdvancedPanel extracted from
 * SettingsPanel.jsx as a controlled panel — the shell keeps the
 * reset/backup/snip state and the sends; these tests pin the
 * reset arm flow, the action handlers and the snip field wiring. */
import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import AdvancedPanel from '../AdvancedPanel';

function baseProps(overrides = {}) {
  return {
    configAnswered: true,
    isConnected: true,
    resetArmed: false,
    resetDone: false,
    resetError: '',
    onResetClick: jest.fn(),
    includeSecrets: false,
    onIncludeSecretsChange: jest.fn(),
    backupStatus: '',
    onCreateBackup: jest.fn(),
    restorePath: '',
    onRestorePathChange: jest.fn(),
    restoreStatus: '',
    onRestoreBackup: jest.fn(),
    versionCheck: '',
    onCheckUpdates: jest.fn(),
    onExportDiagnostics: jest.fn(),
    configUpdateError: '',
    disableFallbacks: false,
    onDisableFallbacksChange: jest.fn(),
    fallbackChain: '',
    onFallbackChainChange: jest.fn(),
    onFallbackChainCommit: jest.fn(),
    projectConfigReady: false,
    projectLayer: { workspaceRoot: '', content: '', exists: false, forbiddenKeys: [], warning: '', status: '' },
    onProjectLayerSave: jest.fn(),
    snipHotkey: 'Ctrl+S',
    onSnipHotkeyChange: jest.fn(),
    onSnipHotkeyBlur: jest.fn(),
    onSnipHotkeyReset: jest.fn(),
    snipHotkeyError: '',
    pendingSnipValue: undefined,
    ...overrides,
  };
}

describe('AdvancedPanel', () => {
  test('before the config answer the section shows its load state (RUX-C-01)', () => {
    render(<AdvancedPanel {...baseProps({ configAnswered: false })} />);
    expect(screen.getByTestId('advanced-config-load-state')).toBeInTheDocument();
  });

  test('the reset button fires the handler; armed and done states render their copy', () => {
    const props = baseProps();
    const { rerender } = render(<AdvancedPanel {...props} />);
    fireEvent.click(screen.getByRole('button', { name: 'Reset all settings' }));
    expect(props.onResetClick).toHaveBeenCalledTimes(1);
    rerender(<AdvancedPanel {...baseProps({ resetArmed: true })} />);
    expect(screen.getByText('Click again to confirm reset')).toBeInTheDocument();
    rerender(<AdvancedPanel {...baseProps({ resetDone: true })} />);
    expect(screen.getByText('Settings were reset to defaults.')).toBeInTheDocument();
  });

  test('backup, update check and export buttons fire their handlers', () => {
    const props = baseProps();
    render(<AdvancedPanel {...props} />);
    fireEvent.click(screen.getByText('Create backup'));
    expect(props.onCreateBackup).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByText('Check for updates'));
    expect(props.onCheckUpdates).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByText('Export diagnostics'));
    expect(props.onExportDiagnostics).toHaveBeenCalledTimes(1);
  });

  test('the snip field wires change, blur and reset through the handlers', () => {
    const props = baseProps();
    render(<AdvancedPanel {...props} />);
    const input = screen.getByTestId('snip-hotkey-input');
    expect(input).toHaveValue('Ctrl+S');
    fireEvent.change(input, { target: { value: 'Ctrl+Alt+S' } });
    expect(props.onSnipHotkeyChange).toHaveBeenCalledWith('Ctrl+Alt+S');
    fireEvent.blur(input);
    expect(props.onSnipHotkeyBlur).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByTestId('snip-hotkey-reset'));
    expect(props.onSnipHotkeyReset).toHaveBeenCalledTimes(1);
  });

  test('a queued snip change and a snip error both render (RUX-A-12)', () => {
    render(<AdvancedPanel {...baseProps({
      pendingSnipValue: 'Ctrl+Alt+S',
      snipHotkeyError: 'Could not set "Ctrl+Alt+S" — it may be invalid.',
    })} />);
    expect(screen.getByTestId('snip-hotkey-queued')).toHaveTextContent('Snip hotkey change to Ctrl+Alt+S queued');
    expect(screen.getByTestId('snip-hotkey-error')).toHaveTextContent('Could not set');
  });
});
