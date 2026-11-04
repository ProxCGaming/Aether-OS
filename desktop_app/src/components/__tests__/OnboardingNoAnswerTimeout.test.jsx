import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import Onboarding from '../Onboarding';
import { useAetherSocket } from '../../contexts/SocketContext';

jest.mock('../../contexts/SocketContext', () => ({
  useAetherSocket: jest.fn(),
}));

// Lane C grouped S4: a save made while CONNECTED to a wedged engine
// showed "Saving…" with no timeout — the step's button stayed
// disabled and the only way out was skipping. ProviderSettings'
// 35s pattern (RUX-C-03) is now mirrored here: after 35s with no
// acknowledgement the saving state resolves to an honest no-answer
// note and the step can be retried; a late acknowledgement still
// lands and advances the step.
describe('Onboarding save no-answer timeout (grouped S4)', () => {
  let mockSendMessage;
  let listeners;
  let socket;

  const providers = [{ name: 'google_gemini', display_name: 'Google Gemini', has_key: false }];
  const emit = (data) => act(() => listeners.forEach((cb) => cb({ data: JSON.stringify(data) })));
  const advance = (ms) => act(() => { jest.advanceTimersByTime(ms); });

  beforeEach(() => {
    jest.useFakeTimers();
    mockSendMessage = jest.fn();
    listeners = [];
    socket = {
      sendMessage: mockSendMessage,
      isConnected: true,
      wsRef: {
        current: {
          addEventListener: jest.fn((ev, cb) => { if (ev === 'message') listeners.push(cb); }),
          removeEventListener: jest.fn(),
          readyState: 1,
        },
      },
    };
    useAetherSocket.mockImplementation(() => socket);
    window.electronAPI = { showOpenDialog: jest.fn(async () => '/data/ws') };
  });

  afterEach(() => {
    jest.useRealTimers();
    delete window.electronAPI;
  });

  it('provider key save: 35s without an answer resolves Saving… to the no-answer note, and a late ack still advances', () => {
    render(<Onboarding providers={providers} onDone={jest.fn()} />);
    fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'key-123' } });
    fireEvent.click(screen.getByText('Save & continue'));
    expect(screen.getByText('Saving…')).toBeDisabled();

    // Just under the wire: still honestly saving, no note yet.
    advance(34999);
    expect(screen.getByText('Saving…')).toBeDisabled();
    expect(screen.queryByText(/No answer from the engine/)).not.toBeInTheDocument();

    advance(1);
    expect(screen.getByText(/No answer from the engine in 35s/)).toBeInTheDocument();
    expect(screen.getByText('Save & continue')).not.toBeDisabled();

    // The wedged engine finally answers: the save lands as before.
    emit({ type: 'PROVIDER_SAVE_RESPONSE', payload: { success: true } });
    expect(screen.getByText('Choose your workspace')).toBeInTheDocument();
  });

  it('provider key save: retrying after the timeout sends again and can succeed', () => {
    render(<Onboarding providers={providers} onDone={jest.fn()} />);
    fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'key-123' } });
    fireEvent.click(screen.getByText('Save & continue'));
    advance(35000);
    expect(screen.getByText(/No answer from the engine in 35s/)).toBeInTheDocument();

    fireEvent.click(screen.getByText('Save & continue'));
    expect(mockSendMessage.mock.calls.filter(([t]) => t === 'PROVIDER_SAVE_REQUEST')).toHaveLength(2);
    expect(screen.getByText('Saving…')).toBeDisabled();

    emit({ type: 'PROVIDER_SAVE_RESPONSE', payload: { success: true } });
    expect(screen.getByText('Choose your workspace')).toBeInTheDocument();
  });

  it('workspace save: the timeout restores the picker with the no-answer note', async () => {
    render(<Onboarding providers={providers} onDone={jest.fn()} />);
    fireEvent.click(screen.getByText('Skip step'));
    expect(screen.getByText('Choose your workspace')).toBeInTheDocument();
    await act(async () => {
      fireEvent.click(screen.getByText('Choose folder…'));
    });
    expect(mockSendMessage).toHaveBeenCalledWith('WORKSPACE_ADD_REQUEST', expect.objectContaining({
      payload: expect.objectContaining({ path: '/data/ws' }),
    }));
    expect(screen.getByText('Saving…')).toBeInTheDocument();
    expect(screen.queryByText('Choose folder…')).not.toBeInTheDocument();

    advance(35000);
    expect(screen.getByText(/No answer from the engine in 35s/)).toBeInTheDocument();
    expect(screen.getByText('Choose folder…')).toBeInTheDocument();
    expect(screen.getByText('Continue')).toBeDisabled();
  });

  it('approval save: the timeout re-enables Continue with the no-answer note, and the retry advances', () => {
    render(<Onboarding providers={providers} onDone={jest.fn()} />);
    fireEvent.click(screen.getByText('Skip step'));
    fireEvent.click(screen.getByText('Skip step'));
    expect(screen.getByText('Approval default')).toBeInTheDocument();

    fireEvent.click(screen.getByText('Continue'));
    expect(screen.getByText('Saving…')).toBeDisabled();
    advance(35000);

    expect(screen.getByText(/No answer from the engine in 35s/)).toBeInTheDocument();
    expect(screen.getByText('Continue')).not.toBeDisabled();
    expect(screen.queryByText("You're set")).not.toBeInTheDocument();

    fireEvent.click(screen.getByText('Continue'));
    expect(mockSendMessage.mock.calls.filter(([t]) => t === 'APPROVAL_MODE_SET_REQUEST')).toHaveLength(2);
    emit({ type: 'APPROVAL_MODE_SET_RESPONSE', payload: { success: true, mode: 'ask' } });
    expect(screen.getByText("You're set")).toBeInTheDocument();
  });

  it('an answer inside 35s never shows the note (the timer is cleared by the reply)', () => {
    render(<Onboarding providers={providers} onDone={jest.fn()} />);
    fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'key-123' } });
    fireEvent.click(screen.getByText('Save & continue'));
    advance(10000);
    emit({ type: 'PROVIDER_SAVE_RESPONSE', payload: { success: true } });
    expect(screen.getByText('Choose your workspace')).toBeInTheDocument();
    advance(60000);
    expect(screen.queryByText(/No answer from the engine/)).not.toBeInTheDocument();
  });
});
