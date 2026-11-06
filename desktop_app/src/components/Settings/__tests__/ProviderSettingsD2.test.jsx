import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import ProviderSettings from '../ProviderSettings';
import { useAetherSocket } from '../../../contexts/SocketContext';

jest.mock('../../../contexts/SocketContext', () => ({
  useAetherSocket: jest.fn(),
}));

// Wave 3, D2-25 / D2-27:
// - D2-25: a PROVIDER_SAVE_RESPONSE without a `provider` field
//   used to throw inside the handler (provider.startsWith), so
//   the form's save tracking stayed stuck until the 35 s timer.
//   A provider-less reply is now correlated to the one save in
//   flight when there is exactly one, and never throws.
// - D2-27: after a successful save the typed API key is cleared
//   from renderer state instead of lingering for the mount's life.
describe('ProviderSettings D2 fixes', () => {
  let mockSendMessage;
  let messageListeners;

  beforeEach(() => {
    mockSendMessage = jest.fn();
    messageListeners = [];
    const mockWsRef = {
      current: {
        addEventListener: jest.fn((event, cb) => {
          if (event === 'message') messageListeners.push(cb);
        }),
        removeEventListener: jest.fn(),
        readyState: 1,
      },
    };
    useAetherSocket.mockReturnValue({
      wsRef: mockWsRef,
      sendMessage: mockSendMessage,
      isConnected: true,
    });
  });

  const simulateMessage = (data) => {
    act(() => {
      messageListeners.forEach((cb) => cb({ data: JSON.stringify(data) }));
    });
  };

  const renderExpandedForm = () => {
    render(
      <ProviderSettings
        providers={[{ name: 'google_gemini', display_name: 'Google Gemini', has_key: false }]}
        providersLoaded
      />,
    );
    fireEvent.click(screen.getByText('Google Gemini'));
    const keyInput = screen.getByPlaceholderText('Enter Google Gemini API Key');
    fireEvent.change(keyInput, { target: { value: 'sk-test-secret' } });
    return { keyInput };
  };

  test('D2-27: a successful save clears the typed key from the form', () => {
    const { keyInput } = renderExpandedForm();
    fireEvent.click(screen.getByText('Save Configuration'));
    simulateMessage({
      type: 'PROVIDER_SAVE_RESPONSE',
      payload: { success: true, provider: 'google_gemini' },
    });
    expect(screen.getByText(/Saved!/)).toBeInTheDocument();
    expect(keyInput).toHaveValue('');
  });

  test('D2-25: a save response without a provider resolves the one save in flight instead of throwing', () => {
    renderExpandedForm();
    fireEvent.click(screen.getByText('Save Configuration'));
    expect(screen.getByText('Saving...')).toBeInTheDocument();
    simulateMessage({
      type: 'PROVIDER_SAVE_RESPONSE',
      payload: { success: true },
    });
    expect(screen.getByText(/Saved!/)).toBeInTheDocument();
    expect(screen.getByText('Save Configuration')).toBeInTheDocument();
  });

  test('D2-25: a failed save response without a provider still surfaces the failure', () => {
    renderExpandedForm();
    fireEvent.click(screen.getByText('Save Configuration'));
    simulateMessage({
      type: 'PROVIDER_SAVE_RESPONSE',
      payload: { success: false, error: 'key rejected' },
    });
    expect(screen.getByText('key rejected')).toBeInTheDocument();
  });
});
