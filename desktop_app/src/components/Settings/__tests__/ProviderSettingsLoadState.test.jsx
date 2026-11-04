/** Wave 3 slice 1 (RUX-C-03): the API Providers section must not
 * render unexplained blank space before the provider list lands,
 * and a Save whose response never comes must resolve to the same
 * "no answer from the engine" honesty the Test path already has —
 * not sit on "Saving..." forever. */
import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import ProviderSettings from '../ProviderSettings';
import { useAetherSocket } from '../../../contexts/SocketContext';

jest.mock('../../../contexts/SocketContext', () => ({
  useAetherSocket: jest.fn(),
}));

function makeHarness({ isConnected = true } = {}) {
  let messageListeners = [];
  const mockSendMessage = jest.fn();
  const mockWsRef = {
    current: {
      readyState: 1, // WebSocket.OPEN
      send: jest.fn(),
      addEventListener: jest.fn((event, cb) => {
        if (event === 'message') messageListeners.push(cb);
      }),
      removeEventListener: jest.fn((event, cb) => {
        if (event === 'message') messageListeners = messageListeners.filter((l) => l !== cb);
      }),
    },
  };
  useAetherSocket.mockReturnValue({ wsRef: mockWsRef, sendMessage: mockSendMessage, isConnected });
  const simulateMessage = (data) => {
    act(() => {
      messageListeners.forEach((cb) => cb({ data: JSON.stringify(data) }));
    });
  };
  return { mockWsRef, mockSendMessage, simulateMessage };
}

describe('ProviderSettings load states (RUX-C-03)', () => {
  test('list not yet loaded: a loading state, not blank space', () => {
    const { mockWsRef } = makeHarness();
    render(<ProviderSettings providers={[]} providersLoaded={false} />);
    const row = screen.getByTestId('providers-load-state');
    expect(row).toHaveAttribute('data-load-display', 'loading');
    expect(row).toHaveTextContent(/Loading providers/);
    expect(screen.queryByTestId('providers-empty')).not.toBeInTheDocument();
  });

  test('list not loaded while disconnected: the offline state', () => {
    makeHarness({ isConnected: false });
    render(<ProviderSettings providers={[]} providersLoaded={false} />);
    const row = screen.getByTestId('providers-load-state');
    expect(row).toHaveAttribute('data-load-display', 'offline');
    expect(row).toHaveTextContent(/Waiting for the engine/);
  });

  test('answered-empty: the genuine empty state, distinct from loading', () => {
    makeHarness();
    render(<ProviderSettings providers={[]} providersLoaded={true} />);
    expect(screen.queryByTestId('providers-load-state')).not.toBeInTheDocument();
    expect(screen.getByTestId('providers-empty')).toHaveTextContent(/No providers were reported by the engine/);
  });

  test('a populated list renders regardless of the loaded flag (the data IS the answer)', () => {
    makeHarness();
    render(<ProviderSettings
      providers={[{ name: 'openai', display_name: 'OpenAI', has_key: false }]}
      providersLoaded={false}
    />);
    expect(screen.getByText('OpenAI')).toBeInTheDocument();
    expect(screen.queryByTestId('providers-load-state')).not.toBeInTheDocument();
  });
});

describe('ProviderSettings Save no-answer timeout (RUX-C-03)', () => {
  afterEach(() => {
    jest.useRealTimers();
  });

  function renderExpandable(harness) {
    render(<ProviderSettings providers={[{ name: 'openai', display_name: 'OpenAI', has_key: false }]} providersLoaded={true} />);
    fireEvent.click(screen.getByText('OpenAI'));
    fireEvent.change(screen.getByPlaceholderText(/Enter OpenAI API Key/i), { target: { value: 'sk-test' } });
  }

  test('a save with no response in 35s resolves to the no-answer note and re-enables the button', () => {
    jest.useFakeTimers();
    const harness = makeHarness();
    renderExpandable(harness);
    fireEvent.click(screen.getByText('Save Configuration'));
    expect(harness.mockSendMessage).toHaveBeenCalledWith('PROVIDER_SAVE_REQUEST', expect.objectContaining({
      payload: expect.objectContaining({ provider: 'openai' }),
    }));
    expect(screen.getByText('Saving...')).toBeInTheDocument();
    act(() => { jest.advanceTimersByTime(35000); });
    expect(screen.getByText(/No answer from the engine in 35s/)).toBeInTheDocument();
    const btn = screen.getByText('Save Configuration').closest('button');
    expect(btn).not.toBeDisabled();
  });

  test('a save response inside 35s lands as Saved and no timeout error follows', () => {
    jest.useFakeTimers();
    const harness = makeHarness();
    renderExpandable(harness);
    fireEvent.click(screen.getByText('Save Configuration'));
    act(() => {
      harness.simulateMessage({
        type: 'PROVIDER_SAVE_RESPONSE',
        payload: { provider: 'openai', success: true },
      });
    });
    expect(screen.getByText(/Saved!/)).toBeInTheDocument();
    act(() => { jest.advanceTimersByTime(40000); });
    expect(screen.queryByText(/No answer from the engine in 35s/)).not.toBeInTheDocument();
  });
});
