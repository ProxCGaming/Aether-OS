/** Wave 5 (Annex C row 7): VoicePanel extracted from
 * SettingsPanel.jsx as a controlled panel — the shell keeps the
 * slider values and commit-on-release handlers (D2-08); these
 * tests pin the load gating, the drag/commit wiring and the
 * queued-value notes. */
import React from 'react';
import { render, screen, fireEvent, within } from '@testing-library/react';
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

describe('VoicePanel Input card (Wave 4)', () => {
  const DEVICES = [
    { kind: 'audioinput', deviceId: 'mic-usb', label: 'USB Headset Mic' },
    { kind: 'audioinput', deviceId: 'mic-2', label: '' },
    { kind: 'videoinput', deviceId: 'cam-1', label: 'Webcam' },
  ];

  beforeEach(() => {
    Object.defineProperty(window.navigator, 'mediaDevices', {
      configurable: true,
      value: {
        enumerateDevices: jest.fn(() => Promise.resolve(DEVICES)),
        addEventListener: jest.fn(),
        removeEventListener: jest.fn(),
        // No getUserMedia: the Test button must fail honestly.
      },
    });
  });

  afterEach(() => {
    delete window.navigator.mediaDevices;
  });

  function openMicSelect() {
    const wrapper = screen.getByTestId('input-device-select');
    fireEvent.click(wrapper.querySelector('button'));
    return wrapper;
  }

  test('lists System default first, then the microphones (unnamed ones numbered, cameras excluded)', async () => {
    render(<VoicePanel {...baseProps()} />);
    const wrapper = openMicSelect();
    // The selected label shows in the trigger AND the option list.
    expect((await within(wrapper).findAllByText('System default')).length).toBeGreaterThan(0);
    expect(within(wrapper).getByText('USB Headset Mic')).toBeInTheDocument();
    expect(within(wrapper).getByText('Microphone 2')).toBeInTheDocument();
    expect(within(wrapper).queryByText('Webcam')).not.toBeInTheDocument();
  });

  test('picking a microphone commits speech_input_device_id through the shell callback', async () => {
    const onInputDeviceChange = jest.fn();
    render(<VoicePanel {...baseProps({ onInputDeviceChange })} />);
    const wrapper = openMicSelect();
    fireEvent.click(await within(wrapper).findByText('USB Headset Mic'));
    expect(onInputDeviceChange).toHaveBeenCalledWith('mic-usb');
  });

  test('a saved microphone that is not connected stays selectable, labelled as such', async () => {
    render(<VoicePanel {...baseProps({ inputDeviceId: 'gone-1' })} />);
    const wrapper = openMicSelect();
    expect((await within(wrapper).findAllByText('Saved microphone (not connected)')).length).toBeGreaterThan(0);
  });

  test('the dictation mode cards reflect the config and commit dictation_mode', () => {
    const onDictationModeChange = jest.fn();
    render(<VoicePanel {...baseProps({ dictationMode: 'hold', onDictationModeChange })} />);
    expect(screen.getByTestId('dictation-mode-hold')).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByTestId('dictation-mode-toggle')).toHaveAttribute('aria-pressed', 'false');
    fireEvent.click(screen.getByTestId('dictation-mode-toggle'));
    expect(onDictationModeChange).toHaveBeenCalledWith('toggle');
  });

  test('queued input changes show their notes', () => {
    render(<VoicePanel {...baseProps({
      pendingInputDeviceValue: 'mic-usb',
      pendingDictationModeValue: 'toggle',
    })} />);
    expect(screen.getByTestId('input-device-queued')).toBeInTheDocument();
    expect(screen.getByTestId('dictation-mode-queued')).toHaveTextContent('Click to start & stop');
  });

  test('Test microphone fails honestly when capture is unavailable', async () => {
    render(<VoicePanel {...baseProps()} />);
    fireEvent.click(screen.getByTestId('mic-test-toggle'));
    expect(await screen.findByTestId('mic-test-error')).toHaveTextContent('blocked');
  });
});
