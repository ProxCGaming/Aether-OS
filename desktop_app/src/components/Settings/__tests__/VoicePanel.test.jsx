/** Wave 5 (Annex C row 7): VoicePanel extracted from
 * SettingsPanel.jsx as a controlled panel — the shell keeps the
 * slider values and commit-on-release handlers (D2-08); these
 * tests pin the load gating, the drag/commit wiring and the
 * queued-value notes. */
import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import VoicePanel from '../VoicePanel';

function baseProps(overrides = {}) {
  return {
    configAnswered: true,
    configLoadError: '',
    isConnected: true,
    onRetryConfigLoad: jest.fn(),
    vadThreshold: 0.5,
    setVadThreshold: jest.fn(),
    commitVadThreshold: jest.fn(),
    pendingVadValue: undefined,
    silenceDurationMs: 700,
    setSilenceDurationMs: jest.fn(),
    commitSilenceDuration: jest.fn(),
    pendingSilenceValue: undefined,
    smartTurnSensitivity: 0.5,
    setSmartTurnSensitivity: jest.fn(),
    commitSmartTurnSensitivity: jest.fn(),
    pendingSmartTurnValue: undefined,
    ...overrides,
  };
}

describe('VoicePanel', () => {
  test('before the config answer the load state shows and the sliders are inert (RUX-C-01)', () => {
    render(<VoicePanel {...baseProps({ configAnswered: false })} />);
    expect(screen.getByTestId('voice-config-load-state')).toBeInTheDocument();
    expect(screen.getByTestId('vad-threshold-slider')).toBeDisabled();
    expect(screen.getByTestId('silence-duration-slider')).toBeDisabled();
  });

  test('a drag updates the local value only; release commits (D2-08)', () => {
    const props = baseProps();
    render(<VoicePanel {...props} />);
    const slider = screen.getByTestId('vad-threshold-slider');
    expect(screen.getByTestId('vad-threshold-value')).toHaveTextContent('0.50');
    fireEvent.change(slider, { target: { value: '0.7' } });
    expect(props.setVadThreshold).toHaveBeenCalledWith(0.7);
    expect(props.commitVadThreshold).not.toHaveBeenCalled();
    fireEvent.pointerUp(slider);
    expect(props.commitVadThreshold).toHaveBeenCalledTimes(1);
  });

  test('the silence slider parses whole milliseconds and commits on release', () => {
    const props = baseProps();
    render(<VoicePanel {...props} />);
    const slider = screen.getByTestId('silence-duration-slider');
    expect(screen.getByTestId('silence-duration-value')).toHaveTextContent('700 ms');
    fireEvent.change(slider, { target: { value: '900' } });
    expect(props.setSilenceDurationMs).toHaveBeenCalledWith(900);
    fireEvent.pointerUp(slider);
    expect(props.commitSilenceDuration).toHaveBeenCalledTimes(1);
  });

  test('queued slider values are named under their sliders (RUX-A-15)', () => {
    render(<VoicePanel {...baseProps({ pendingVadValue: 0.7, pendingSilenceValue: 900 })} />);
    expect(screen.getByTestId('vad-threshold-queued')).toHaveTextContent('VAD threshold change to 0.70 queued');
    expect(screen.getByTestId('silence-duration-queued')).toHaveTextContent('Silence duration change to 900 ms queued');
  });

  // S21 (voice G8): the Smart Turn slider — the engine field
  // (smart_turn_sensitivity) existed and was applied on
  // CONFIG_UPDATE all along; only the control was missing. It
  // follows the same D2-08 commit-on-release discipline, and
  // 0.00 (Smart Turn off) is a real, displayable value.
  test('the Smart Turn slider renders, drags locally and commits on release', () => {
    const props = baseProps();
    render(<VoicePanel {...props} />);
    const slider = screen.getByTestId('smart-turn-sensitivity-slider');
    expect(screen.getByTestId('smart-turn-sensitivity-value')).toHaveTextContent('0.50');
    fireEvent.change(slider, { target: { value: '0.8' } });
    expect(props.setSmartTurnSensitivity).toHaveBeenCalledWith(0.8);
    expect(props.commitSmartTurnSensitivity).not.toHaveBeenCalled();
    fireEvent.pointerUp(slider);
    expect(props.commitSmartTurnSensitivity).toHaveBeenCalledTimes(1);
  });

  test('the Smart Turn slider is inert before the config answer and shows 0.00 as off, not unset', () => {
    render(<VoicePanel {...baseProps({ configAnswered: false, smartTurnSensitivity: 0 })} />);
    expect(screen.getByTestId('smart-turn-sensitivity-slider')).toBeDisabled();
    expect(screen.getByTestId('smart-turn-sensitivity-value')).toHaveTextContent('0.00');
  });

  test('a queued Smart Turn value is named under its slider (RUX-A-15)', () => {
    render(<VoicePanel {...baseProps({ pendingSmartTurnValue: 0.25 })} />);
    expect(screen.getByTestId('smart-turn-sensitivity-queued')).toHaveTextContent('Smart Turn sensitivity change to 0.25 queued');
  });
});
