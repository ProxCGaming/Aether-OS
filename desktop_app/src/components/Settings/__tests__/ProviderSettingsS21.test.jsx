/** Master build S21 (R17 wiring): ProviderSettings gains the two
 * provider surfaces whose engine handlers existed with no UI
 * sender — reveal-key (PROVIDER_REVEAL_KEY_REQUEST) and
 * refresh-models (REFRESH_MODELS_REQUEST). Pins: reveal is an
 * explicit per-click ask and nothing is fetched before it; the
 * key renders only from the engine's answer, only in the card,
 * and hiding or collapsing clears it; a refused reveal shows the
 * engine's error and no key; refresh sends the right scope,
 * shows its in-flight state, and settles on the response. */
import React from 'react';
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import ProviderSettings from '../ProviderSettings';
import { useAetherSocket } from '../../../contexts/SocketContext';

jest.mock('../../../contexts/SocketContext', () => ({
  useAetherSocket: jest.fn()
}));

const PROVIDERS = [
  { name: 'openai', display_name: 'OpenAI', has_key: true, models: ['gpt-4o'] },
  { name: 'anthropic', display_name: 'Anthropic', has_key: false, models: [] },
];

describe('ProviderSettings S21 — reveal-key + refresh-models', () => {
  let mockSendMessage;
  let mockWsRef;
  let messageListeners;

  beforeEach(() => {
    mockSendMessage = jest.fn();
    messageListeners = [];
    mockWsRef = {
      current: {
        addEventListener: jest.fn((event, cb) => {
          if (event === 'message') messageListeners.push(cb);
        }),
        removeEventListener: jest.fn((event, cb) => {
          if (event === 'message') {
            messageListeners = messageListeners.filter(l => l !== cb);
          }
        }),
        readyState: 1
      }
    };
    useAetherSocket.mockReturnValue({
      wsRef: mockWsRef,
      sendMessage: mockSendMessage,
      isConnected: true
    });
  });

  const simulateMessage = (data) => {
    act(() => {
      messageListeners.forEach(cb => cb({ data: JSON.stringify(data) }));
    });
  };

  const sentTypes = () => mockSendMessage.mock.calls.map((c) => c[0]);

  test('nothing asks for a key before the user clicks Reveal', () => {
    render(<ProviderSettings providers={PROVIDERS} providersLoaded />);
    fireEvent.click(screen.getByText('OpenAI'));
    expect(screen.getByTestId('reveal-key-openai')).toBeInTheDocument();
    expect(sentTypes()).not.toContain('PROVIDER_REVEAL_KEY_REQUEST');
    // An unconfigured provider has no saved key to reveal.
    fireEvent.click(screen.getByText('Anthropic'));
    expect(screen.queryByTestId('reveal-key-anthropic')).not.toBeInTheDocument();
  });

  test('reveal sends the request and renders only the engine\u2019s answer; Hide clears it', async () => {
    render(<ProviderSettings providers={PROVIDERS} providersLoaded />);
    fireEvent.click(screen.getByText('OpenAI'));
    fireEvent.click(screen.getByTestId('reveal-key-openai'));
    expect(mockSendMessage).toHaveBeenCalledWith('PROVIDER_REVEAL_KEY_REQUEST', expect.objectContaining({
      payload: { provider: 'openai' }
    }));
    expect(screen.queryByTestId('revealed-key-openai')).not.toBeInTheDocument();

    simulateMessage({
      type: 'PROVIDER_REVEAL_KEY_RESPONSE',
      payload: { provider: 'openai', api_key: 'sk-test-123', success: true }
    });
    await waitFor(() => {
      expect(screen.getByTestId('revealed-key-openai')).toHaveValue('sk-test-123');
    });

    fireEvent.click(screen.getByText('Hide'));
    expect(screen.queryByTestId('revealed-key-openai')).not.toBeInTheDocument();
    expect(screen.getByTestId('reveal-key-openai')).toBeInTheDocument();
  });

  test('collapsing the card clears a revealed key', async () => {
    render(<ProviderSettings providers={PROVIDERS} providersLoaded />);
    fireEvent.click(screen.getByText('OpenAI'));
    fireEvent.click(screen.getByTestId('reveal-key-openai'));
    simulateMessage({
      type: 'PROVIDER_REVEAL_KEY_RESPONSE',
      payload: { provider: 'openai', api_key: 'sk-test-123', success: true }
    });
    await waitFor(() => {
      expect(screen.getByTestId('revealed-key-openai')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText('OpenAI'));
    fireEvent.click(screen.getByText('OpenAI'));
    expect(screen.queryByTestId('revealed-key-openai')).not.toBeInTheDocument();
  });

  test('a refused reveal shows the engine error and no key field', async () => {
    render(<ProviderSettings providers={PROVIDERS} providersLoaded />);
    fireEvent.click(screen.getByText('OpenAI'));
    fireEvent.click(screen.getByTestId('reveal-key-openai'));
    simulateMessage({
      type: 'PROVIDER_REVEAL_KEY_RESPONSE',
      payload: { provider: 'openai', success: false, error: "Provider 'openai' not found." }
    });
    await waitFor(() => {
      expect(screen.getByTestId('reveal-key-error-openai')).toHaveTextContent("Provider 'openai' not found.");
    });
    expect(screen.queryByTestId('revealed-key-openai')).not.toBeInTheDocument();
  });

  test('refresh-all sends an unscoped request and settles on the response', async () => {
    render(<ProviderSettings providers={PROVIDERS} providersLoaded />);
    fireEvent.click(screen.getByTestId('refresh-models-all'));
    expect(mockSendMessage).toHaveBeenCalledWith('REFRESH_MODELS_REQUEST', expect.objectContaining({
      payload: {}
    }));
    expect(screen.getByTestId('refresh-models-all')).toHaveTextContent('Refreshing…');
    expect(screen.getByTestId('refresh-models-all')).toBeDisabled();

    simulateMessage({
      type: 'REFRESH_MODELS_RESPONSE',
      payload: { providers: PROVIDERS, default_provider: 'openai', default_model: 'gpt-4o' }
    });
    await waitFor(() => {
      expect(screen.getByTestId('refresh-models-msg')).toHaveTextContent('Model lists updated just now.');
    });
    expect(screen.getByTestId('refresh-models-all')).not.toBeDisabled();
  });

  test('per-provider refresh sends the targeted scope', () => {
    render(<ProviderSettings providers={PROVIDERS} providersLoaded />);
    fireEvent.click(screen.getByText('OpenAI'));
    fireEvent.click(screen.getByTestId('refresh-models-openai'));
    expect(mockSendMessage).toHaveBeenCalledWith('REFRESH_MODELS_REQUEST', expect.objectContaining({
      payload: { provider: 'openai' }
    }));
    expect(screen.getByTestId('refresh-models-openai')).toHaveTextContent('Refreshing…');
  });
});
