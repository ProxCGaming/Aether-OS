import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import Onboarding from '../Onboarding';
import { useAetherSocket } from '../../contexts/SocketContext';

jest.mock('../../contexts/SocketContext', () => ({
  useAetherSocket: jest.fn(),
}));

describe('Onboarding (Epic 6, E6-02)', () => {
  let mockSendMessage;
  let listeners;

  beforeEach(() => {
    mockSendMessage = jest.fn();
    listeners = [];
    useAetherSocket.mockReturnValue({
      sendMessage: mockSendMessage,
      wsRef: {
        current: {
          addEventListener: jest.fn((ev, cb) => { if (ev === 'message') listeners.push(cb); }),
          removeEventListener: jest.fn(),
          readyState: 1,
        },
      },
    });
  });

  const providers = [{ name: 'google_gemini', display_name: 'Google Gemini', has_key: false }];
  const emit = (data) => act(() => listeners.forEach((cb) => cb({ data: JSON.stringify(data) })));

  it('saves the provider key, then advances on the engine response', () => {
    render(<Onboarding providers={providers} onDone={jest.fn()} />);
    fireEvent.change(screen.getByLabelText('API key'), { target: { value: '<redacted>' } });
    fireEvent.click(screen.getByText('Save & continue'));
    expect(mockSendMessage).toHaveBeenCalledWith('PROVIDER_SAVE_REQUEST', expect.objectContaining({
      payload: expect.objectContaining({ provider: 'google_gemini', api_key: '<redacted>' }),
    }));
    emit({ type: 'PROVIDER_SAVE_RESPONSE', payload: { success: true } });
    expect(screen.getByText('Choose your workspace')).toBeInTheDocument();
  });

  it('skip setup finishes immediately from any step', () => {
    const onDone = jest.fn();
    render(<Onboarding providers={providers} onDone={onDone} />);
    fireEvent.click(screen.getByText('Skip setup'));
    expect(onDone).toHaveBeenCalledTimes(1);
  });

  it('walks the approval step and finishes', () => {
    const onDone = jest.fn();
    render(<Onboarding providers={providers} onDone={onDone} />);
    fireEvent.click(screen.getByText('Skip step')); // provider
    fireEvent.click(screen.getByText('Skip step')); // workspace
    expect(screen.getByText('Approval default')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Continue'));
    expect(mockSendMessage).toHaveBeenCalledWith('APPROVAL_MODE_SET_REQUEST', expect.objectContaining({
      payload: { mode: 'ask' },
    }));
    fireEvent.click(screen.getByText('Start chatting'));
    expect(onDone).toHaveBeenCalledTimes(1);
  });
});
