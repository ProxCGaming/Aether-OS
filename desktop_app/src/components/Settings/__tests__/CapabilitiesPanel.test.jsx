/** Wave 5 (Annex C row 7): CapabilitiesPanel extracted from
 * SettingsPanel.jsx as a controlled panel — the diagnostics
 * run's state and sends stay in the shell; these tests pin the
 * status card rendering and the run/export handler props. */
import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import CapabilitiesPanel from '../CapabilitiesPanel';

function baseProps(overrides = {}) {
  return {
    capabilityStatus: null,
    capabilityHistory: [],
    isDiagnosticsRunning: false,
    diagnosticsQueued: false,
    diagnosticsError: '',
    onRunDiagnostics: jest.fn(),
    isConnected: true,
    sendSettingsAction: jest.fn(),
    onExportBundle: jest.fn(),
    ...overrides,
  };
}

describe('CapabilitiesPanel', () => {
  test('with no run yet the status is unknown and the run button fires the handler', () => {
    const props = baseProps();
    render(<CapabilitiesPanel {...props} />);
    expect(screen.getByText('Status Unknown')).toBeInTheDocument();
    expect(screen.getByText('[System] Ready to run diagnostics. Click the button above.')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Run Diagnostics Now'));
    expect(props.onRunDiagnostics).toHaveBeenCalledTimes(1);
  });

  test('a clean summary renders System Healthy and history lines', () => {
    render(<CapabilitiesPanel {...baseProps({
      capabilityStatus: { failed_count: 0 },
      capabilityHistory: [{ timestamp: 1700000000, target_name: 'engine', status: 'OK', details: '' }],
    })} />);
    expect(screen.getByText('System Healthy')).toBeInTheDocument();
    expect(screen.getByText(/engine: OK/)).toBeInTheDocument();
  });

  test('a queued run and a run error are both surfaced (RUX-A-13 / D2-07)', () => {
    render(<CapabilitiesPanel {...baseProps({
      diagnosticsQueued: true,
      diagnosticsError: 'No answer from the engine in 35s — try again.',
    })} />);
    expect(screen.getByText('Run Queued')).toBeInTheDocument();
    expect(screen.getByTestId('diagnostics-run-queued')).toHaveTextContent('Diagnostics run queued');
    expect(screen.getByTestId('diagnostics-run-error')).toHaveTextContent('No answer from the engine in 35s');
  });
});
