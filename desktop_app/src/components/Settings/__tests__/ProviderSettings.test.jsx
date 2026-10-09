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

describe('ProviderSettings discovery v2 (excluded models + real errors)', () => {
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
        removeEventListener: jest.fn(),
        readyState: 1,
        send: jest.fn(),
      },
    };
    useAetherSocket.mockReturnValue({
      wsRef: mockWsRef,
      sendMessage: mockSendMessage,
    });
  });

  const simulateMessage = (data) => {
    act(() => {
      messageListeners.forEach(cb => cb({ data: JSON.stringify(data) }));
    });
  };

  const providersWithExcluded = [
    {
      name: 'openai',
      display_name: 'OpenAI',
      has_key: true,
      models: ['gpt-4o'],
      excluded_models: [
        { id: 'text-embedding-3-small', kind: 'embedding' },
        { id: 'dall-e-3', kind: 'image' },
      ],
    },
  ];

  test('View all expander lists chat models and kind-tagged excluded models', () => {
    render(<ProviderSettings providers={providersWithExcluded} />);
    fireEvent.click(screen.getByText('OpenAI'));

    const toggle = screen.getByText(/View all 3 models/);
    expect(toggle).toBeInTheDocument();
    // Excluded models are hidden until expanded.
    expect(screen.queryByText('text-embedding-3-small')).not.toBeInTheDocument();

    fireEvent.click(toggle);
    expect(screen.getByText('gpt-4o')).toBeInTheDocument();
    expect(screen.getByText('text-embedding-3-small')).toBeInTheDocument();
    expect(screen.getByText('dall-e-3')).toBeInTheDocument();
    expect(screen.getByText('embedding')).toBeInTheDocument();
    expect(screen.getByText('image')).toBeInTheDocument();
  });

  test('validate result surfaces discovery error with HTTP code', () => {
    render(<ProviderSettings providers={providersWithExcluded} />);
    fireEvent.click(screen.getByText('OpenAI'));

    const input = screen.getByPlaceholderText(/API Key/i);
    fireEvent.change(input, { target: { value: 'sk-bad' } });
    fireEvent.click(screen.getByText('Test'));
    const beforeBtn = screen.queryByText('Testing...');

    simulateMessage({
      type: 'SETTINGS_PROVIDER_VALIDATE_RESULT',
      payload: {
        provider: 'openai',
        success: false,
        status: 'network_error',
        message: 'Model list fetch failed (HTTP 401): bad key',
        discovery_error: { code: 401, message: 'bad key' },
      },
    });

    const errDivs = document.querySelectorAll('div');
    let found = [];
    errDivs.forEach(d => { if (d.textContent && d.textContent.includes('failed')) found.push(d.textContent.slice(0,80)); });
    const allText = document.body.textContent;
    expect(screen.getByText(/fetch failed.*HTTP 401/i)).toBeInTheDocument();
  });

  test('save response keeps Saved! but appends discovery error text', () => {
    render(<ProviderSettings providers={providersWithExcluded} />);
    fireEvent.click(screen.getByText('OpenAI'));

    const input = screen.getByPlaceholderText(/API Key/i);
    fireEvent.change(input, { target: { value: 'sk-x' } });
    fireEvent.click(screen.getByText('Save Configuration'));

    simulateMessage({
      type: 'PROVIDER_SAVE_RESPONSE',
      payload: {
        provider: 'openai',
        success: true,
        models: ['gpt-4o'],
        discovery_error: { code: null, message: 'dns fail' },
      },
    });

    expect(screen.getByText(/Saved!/)).toBeInTheDocument();
    expect(screen.getByText(/no response/)).toBeInTheDocument();
  });
});
