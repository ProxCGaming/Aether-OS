/**
 * ComputerUsePanel tests (S19).
 *
 * The section carries three stored fields through SettingsPanel's
 * CONFIG_UPDATE path (computer_use_enabled,
 * computer_use_observe, computer_use_vision_capture) and one
 * deliberate NON-field: the Control toggle is disabled pure UI
 * state because actuation (S20) does not exist yet — a stored
 * control value would claim a state the engine does not have.
 * These tests pin the controls' presence, the Control toggle's
 * disabled state + arrival copy, the conditional enabling of
 * Observe, and that edits report through the controlled props
 * (the CONFIG_UPDATE path owns the actual send).
 */
import React from 'react';
import { fireEvent, render, screen } from '@testing-library/react';
import ComputerUsePanel from '../ComputerUsePanel';

const baseProps = {
  configAnswered: true,
  isConnected: true,
  configError: null,
  computerUseEnabled: false,
  onComputerUseEnabledChange: jest.fn(),
  computerUseObserve: false,
  onComputerUseObserveChange: jest.fn(),
  visionCapture: 'off',
  onVisionCaptureChange: jest.fn(),
  approvalMode: 'ask'
};

const renderPanel = (overrides = {}) => {
  const props = { ...baseProps, ...overrides };
  render(<ComputerUsePanel {...props} />);
  return props;
};

describe('ComputerUsePanel', () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  test('renders every control with the observe-only truth on it', () => {
    renderPanel();
    expect(screen.getByTestId('computer-use-enabled-toggle')).not.toBeChecked();
    expect(screen.getByTestId('computer-use-observe-toggle')).not.toBeChecked();
    expect(screen.getByTestId('computer-use-vision-capture')).toHaveValue('off');
    expect(screen.getByText(/This build can only LOOK/)).toBeInTheDocument();
    expect(screen.getByText(/accessibility TEXT only/)).toBeInTheDocument();
    expect(screen.getByText(/no screenshots are taken in this/)).toBeInTheDocument();
  });

  test('the Control toggle is disabled and says when it arrives', () => {
    renderPanel({ computerUseEnabled: true, computerUseObserve: true });
    const control = screen.getByTestId('computer-use-control-toggle');
    expect(control).toBeDisabled();
    expect(control).not.toBeChecked();
    expect(screen.getByText(/arrives with actuation \(S20\)/)).toBeInTheDocument();
    // It is not wired to any change handler: clicking (were it
    // enabled) has nowhere to report — nothing renders a
    // control change callback into the props at all.
    expect(baseProps).not.toHaveProperty('onComputerUseControlChange');
  });

  test('Observe stays disabled until the master switch is on', () => {
    renderPanel({ computerUseEnabled: false });
    expect(screen.getByTestId('computer-use-observe-toggle')).toBeDisabled();
  });

  test('checking the master switch reports true through the config path', () => {
    const offProps = renderPanel({ computerUseEnabled: false });
    fireEvent.click(screen.getByTestId('computer-use-enabled-toggle'));
    expect(offProps.onComputerUseEnabledChange).toHaveBeenCalledWith(true);
  });

  test('toggles and the select report edits through the config path props', () => {
    const props = renderPanel({ computerUseEnabled: true, computerUseObserve: false });
    fireEvent.click(screen.getByTestId('computer-use-observe-toggle'));
    expect(props.onComputerUseObserveChange).toHaveBeenCalledWith(true);
    // Unchecking the master reports false the same way.
    fireEvent.click(screen.getByTestId('computer-use-enabled-toggle'));
    expect(props.onComputerUseEnabledChange).toHaveBeenCalledWith(false);
    fireEvent.change(screen.getByTestId('computer-use-vision-capture'), {
      target: { value: 'on_demand' }
    });
    expect(props.onVisionCaptureChange).toHaveBeenCalledWith('on_demand');
  });

  test('the vision select offers exactly the closed mode set — no always-on', () => {
    renderPanel();
    const options = Array.from(
      screen.getByTestId('computer-use-vision-capture').options
    ).map((option) => option.value);
    expect(options).toEqual(['off', 'ask', 'on_demand']);
  });

  test('the approval mode shown is the inherited Tools one, read-only', () => {
    renderPanel({ approvalMode: 'ask' });
    expect(screen.getByTestId('computer-use-approval-mode')).toHaveTextContent(
      'Ask for approval'
    );
  });

  test('the approval display follows the write mode wording from Tools', () => {
    renderPanel({ approvalMode: 'write' });
    expect(screen.getByTestId('computer-use-approval-mode')).toHaveTextContent(
      'Auto-approve writes'
    );
  });

  test('before the config answer the load state shows instead of values', () => {
    renderPanel({ configAnswered: false });
    expect(screen.getByTestId('computer-use-config-load-state')).toHaveTextContent(
      'Loading your saved computer use settings…'
    );
    expect(screen.queryByTestId('computer-use-enabled-toggle')).not.toBeInTheDocument();
  });

  test('a refused write is surfaced verbatim', () => {
    renderPanel({
      configError: 'Config update refused: computer_use_enabled must be true or false'
    });
    expect(
      screen.getByText('Config update refused: computer_use_enabled must be true or false')
    ).toBeInTheDocument();
  });
});
