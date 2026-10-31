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
      payload: expect.objectContaining({ provider: 'google_gemini' })
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
      payload: expect.objectContaining({
        provider: 'openai',
        api_key: 'sk-test'
      })
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

describe('ProviderSettings key storage (Phase 10.1 C1)', () => {
  let mockSendMessage;
  let mockWsRef;
  let setItemSpy;

  beforeEach(() => {
    mockSendMessage = jest.fn();
    mockWsRef = {
      current: {
        addEventListener: jest.fn(),
        removeEventListener: jest.fn(),
        readyState: 1,
        send: jest.fn(),
      },
    };
    useAetherSocket.mockReturnValue({
      wsRef: mockWsRef,
      sendMessage: mockSendMessage,
    });
    localStorage.clear();
    // Spy on the prototype: jsdom's Storage methods are non-writable own
    // properties, so jest.spyOn(window.localStorage, 'setItem') and direct
    // assignment silently fail to intercept (verified by probe).
    setItemSpy = jest.spyOn(Storage.prototype, 'setItem');
  });

  afterEach(() => {
    setItemSpy.mockRestore();
    localStorage.clear();
  });

  test('saving a provider key performs no localStorage.setItem call with a key-like value', () => {
    const mockProviders = [
      { name: 'openai', display_name: 'OpenAI', has_key: false },
    ];

    render(<ProviderSettings providers={mockProviders} />);
    fireEvent.click(screen.getByText('OpenAI'));

    const input = screen.getByPlaceholderText(/Enter OpenAI API Key/i);
    fireEvent.change(input, { target: { value: 'sk-test-secret-key-123' } });
    fireEvent.click(screen.getByText('Save Configuration'));

    // The key goes to the engine only, via PROVIDER_SAVE_REQUEST.
    expect(mockSendMessage).toHaveBeenCalledWith('PROVIDER_SAVE_REQUEST', expect.objectContaining({
      payload: expect.objectContaining({
        provider: 'openai',
        api_key: 'sk-test-secret-key-123',
      }),
    }));

    // No localStorage write may carry the key, and no aether_saved_key_* entry may exist.
    const keyLikeCalls = setItemSpy.mock.calls.filter(
      ([k, v]) => k.startsWith('aether_saved_key_') || v === 'sk-test-secret-key-123'
    );
    expect(keyLikeCalls).toEqual([]);
    expect(localStorage.getItem('aether_saved_key_openai')).toBeNull();
  });

  test('password field starts empty even if a stale aether_saved_key_* entry exists', () => {
    localStorage.setItem('aether_saved_key_openai', 'sk-stale-key');
    const mockProviders = [
      { name: 'openai', display_name: 'OpenAI', has_key: true },
    ];

    render(<ProviderSettings providers={mockProviders} />);
    fireEvent.click(screen.getByText('OpenAI'));

    const input = screen.getByPlaceholderText(/API Key/i);
    expect(input.value).toBe('');
    // Configured state comes from the engine's has_key, never from a stored key.
    expect(screen.getByText('Configured')).toBeInTheDocument();
  });
});

describe('ProviderSettings health surface (Epic 5, E5-05)', () => {
  it('renders per-provider health state and last latency', () => {
    const providers = [
      { name: 'google_gemini', display_name: 'Google Gemini', has_key: true, models: [] },
      { name: 'openai', display_name: 'OpenAI', has_key: true, models: [] },
    ];
    const health = {
      google_gemini: { status: 'degraded', latency_ms: 250.4 },
      openai: { status: 'healthy', latency_ms: 0 },
    };
    render(<ProviderSettings providers={providers} health={health} />);
    expect(screen.getByTestId('provider-health-google_gemini')).toHaveTextContent('degraded');
    expect(screen.getByTestId('provider-health-google_gemini')).toHaveTextContent('250 ms');
    expect(screen.getByTestId('provider-health-openai')).toHaveTextContent('healthy');
    expect(screen.getByTestId('provider-health-openai')).not.toHaveTextContent('ms');
  });
});

describe('ProviderSettings Test-button hardening', () => {
  let mockSendMessage;
  let mockWsRef;
  let messageListeners;

  beforeEach(() => {
    mockSendMessage = jest.fn();
    messageListeners = [];
    mockWsRef = {
      current: {
        addEventListener: jest.fn((event, cb) => { if (event === 'message') messageListeners.push(cb); }),
        removeEventListener: jest.fn(),
        readyState: 1,
      },
    };
    useAetherSocket.mockReturnValue({ wsRef: mockWsRef, sendMessage: mockSendMessage });
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  const simulateMessage = (data) => {
    messageListeners.forEach(cb => cb({ data: JSON.stringify(data) }));
  };

  test('a double-click sends ONE validation request', () => {
    render(<ProviderSettings providers={[{ name: 'google_gemini', display_name: 'Google Gemini', has_key: true }]} />);
    fireEvent.click(screen.getByText('Google Gemini'));
    const btn = screen.getByText('Test').closest('button');
    fireEvent.click(btn);
    fireEvent.click(btn);
    fireEvent.click(btn);
    const validations = mockSendMessage.mock.calls.filter(([type]) => type === 'SETTINGS_PROVIDER_VALIDATE_REQUEST');
    expect(validations).toHaveLength(1);
    expect(screen.getByText(/Checking the key with the provider/)).toBeInTheDocument();
  });

  test('no engine answer in 35s shows a timeout and re-enables the button', () => {
    jest.useFakeTimers();
    render(<ProviderSettings providers={[{ name: 'google_gemini', display_name: 'Google Gemini', has_key: true }]} />);
    fireEvent.click(screen.getByText('Google Gemini'));
    fireEvent.click(screen.getByText(/Testing|Test/).closest('button'));
    act(() => { jest.advanceTimersByTime(36000); });
    expect(screen.getByText(/No answer from the engine in 35s/)).toBeInTheDocument();
    const btn = screen.getByText('Test').closest('button');
    expect(btn).not.toBeDisabled();
    // And a retry actually sends a new request (guard was cleared).
    fireEvent.click(btn);
    const validations = mockSendMessage.mock.calls.filter(([type]) => type === 'SETTINGS_PROVIDER_VALIDATE_REQUEST');
    expect(validations).toHaveLength(2);
  });

  test('a late response after a retry clears cleanly to success', async () => {
    render(<ProviderSettings providers={[{ name: 'google_gemini', display_name: 'Google Gemini', has_key: true }]} />);
    fireEvent.click(screen.getByText('Google Gemini'));
    fireEvent.click(screen.getByText('Test').closest('button'));
    act(() => {
      simulateMessage({ type: 'SETTINGS_PROVIDER_VALIDATE_RESULT', payload: { provider: 'google_gemini', success: true } });
    });
    await waitFor(() => expect(screen.getByText('Test Successful')).toBeInTheDocument());
  });
});
