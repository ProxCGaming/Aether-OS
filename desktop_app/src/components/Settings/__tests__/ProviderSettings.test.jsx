import React from 'react';
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react';
import ProviderSettings from '../ProviderSettings';
import { useAetherSocket } from '../../../contexts/SocketContext';

// Mock the context hook
jest.mock('../../../contexts/SocketContext', () => ({
  useAetherSocket: jest.fn()
}));

describe('ProviderSettings Component', () => {
  let mockSendMessage;
  let mockWsRef;
  let messageListeners;

  beforeEach(() => {
    mockSendMessage = jest.fn();
    messageListeners = [];
    mockWsRef = {
      current: {
        addEventListener: jest.fn((event, cb) => {
          if (event === 'message') {
            messageListeners.push(cb);
          }
        }),
        removeEventListener: jest.fn((event, cb) => {
          if (event === 'message') {
            messageListeners = messageListeners.filter(l => l !== cb);
          }
        }),
        readyState: 1 // OPEN
      }
    };

    useAetherSocket.mockReturnValue({
      wsRef: mockWsRef,
      sendMessage: mockSendMessage
    });
  });

  const simulateMessage = (data) => {
    messageListeners.forEach(cb => cb({ data: JSON.stringify(data) }));
  };

  test('renders provider list and tests successful connection', async () => {
    const mockProviders = [
      { name: 'google_gemini', display_name: 'Google Gemini', has_key: true }
    ];

    render(<ProviderSettings providers={mockProviders} />);
    
    // Verify provider is listed
    expect(screen.getByText('Google Gemini')).toBeInTheDocument();
    
    // Expand provider
    fireEvent.click(screen.getByText('Google Gemini'));
    
    // Click test button
    const testButton = screen.getByText('Test');
    fireEvent.click(testButton);

    expect(mockSendMessage).toHaveBeenCalledWith('SETTINGS_PROVIDER_VALIDATE_REQUEST', expect.objectContaining({
      provider: 'google_gemini'
    }));

    // Simulate success response
    act(() => {
      simulateMessage({
        type: 'SETTINGS_PROVIDER_VALIDATE_RESULT',
        payload: { provider: 'google_gemini', success: true }
      });
    });

    // Check success message
    await waitFor(() => {
      expect(screen.getByText('Test Successful')).toBeInTheDocument();
    });
  });

  test('handles network failure/offline state predictively', async () => {
    const mockProviders = [
      { name: 'openai', display_name: 'OpenAI', has_key: false }
    ];

    render(<ProviderSettings providers={mockProviders} />);
    
    // Expand provider
    fireEvent.click(screen.getByText('OpenAI'));
    
    // Input API Key
    const input = screen.getByPlaceholderText(/Enter OpenAI API Key/i);
    fireEvent.change(input, { target: { value: 'sk-test' } });
    
    // Simulate offline state (no websocket connection)
    mockWsRef.current.readyState = 3; // CLOSED
    
    const saveButton = screen.getByText('Save Configuration');
    fireEvent.click(saveButton);
    
    // As per Phase 2, useAetherSocket queues messages when offline.
    // The component continues normally and passes the message to the hook.
    expect(mockSendMessage).toHaveBeenCalledWith('PROVIDER_SAVE_REQUEST', expect.objectContaining({
      provider: 'openai',
      api_key: 'sk-test'
    }));
  });
});
