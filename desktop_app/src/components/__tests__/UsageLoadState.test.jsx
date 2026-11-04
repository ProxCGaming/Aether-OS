/** Wave 3 slice 1 (RUX-C-04): Usage & Budget must not render
 * $0.00 / 0 tokens as fact before the ledger query answers. Until
 * then the surface shows a load state (loading / offline); a reply
 * without totals is an error state; zeros appear only behind a
 * real answer. */
import React from 'react';
import { render, screen, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import UsageSettingsPanel from '../Settings/UsageSettingsPanel';

function makeWsRef() {
  let messageListeners = [];
  const wsRef = {
    current: {
      readyState: WebSocket.OPEN,
      send: jest.fn(),
      addEventListener: jest.fn((event, cb) => {
        if (event === 'message') messageListeners.push(cb);
      }),
      removeEventListener: jest.fn((event, cb) => {
        if (event === 'message') messageListeners = messageListeners.filter((l) => l !== cb);
      }),
    },
  };
  const simulateMessage = (data) => {
    act(() => {
      messageListeners.forEach((cb) => cb({ data: JSON.stringify(data) }));
    });
  };
  return { wsRef, simulateMessage };
}

describe('UsageSettingsPanel load states (RUX-C-04)', () => {
  test('before the ledger answers: loading state and NO $0.00 anywhere', () => {
    const { wsRef } = makeWsRef();
    render(<UsageSettingsPanel wsRef={wsRef} isConnected={true} />);
    const row = screen.getByTestId('usage-ledger-load-state');
    expect(row).toHaveAttribute('data-load-display', 'loading');
    expect(document.body.textContent).not.toContain('$0.00');
    expect(screen.queryByText(/No model calls recorded/)).not.toBeInTheDocument();
    // The context snapshot is a load state too, not a blank.
    expect(screen.getByTestId('usage-context-load-state')).toHaveAttribute('data-load-display', 'loading');
  });

  test('while disconnected: the offline state, still no $0.00', () => {
    const { wsRef } = makeWsRef();
    render(<UsageSettingsPanel wsRef={wsRef} isConnected={false} />);
    expect(screen.getByTestId('usage-ledger-load-state')).toHaveAttribute('data-load-display', 'offline');
    expect(screen.getByTestId('usage-ledger-load-state')).toHaveTextContent(/Waiting for the engine/);
    expect(document.body.textContent).not.toContain('$0.00');
  });

  test('an answered EMPTY ledger legitimately shows the empty states (and its real $0.00)', () => {
    const { wsRef, simulateMessage } = makeWsRef();
    render(<UsageSettingsPanel wsRef={wsRef} isConnected={true} />);
    simulateMessage({
      type: 'USAGE_LEDGER_QUERY_RESPONSE',
      payload: {
        totals: { total_cost_usd: 0, input_tokens: 0, output_tokens: 0, by_model: {}, by_agent: {} },
      },
    });
    expect(screen.queryByTestId('usage-ledger-load-state')).not.toBeInTheDocument();
    expect(screen.getByText(/No model calls recorded in the ledger yet/)).toBeInTheDocument();
    expect(screen.getByText(/No agent attribution recorded yet/)).toBeInTheDocument();
    // Behind a real answer, zero is a fact and renders as one.
    expect(document.body.textContent).toContain('$0.00');
  });

  test('a ledger reply without totals is an error state, never zeros', () => {
    const { wsRef, simulateMessage } = makeWsRef();
    render(<UsageSettingsPanel wsRef={wsRef} isConnected={true} />);
    simulateMessage({
      type: 'USAGE_LEDGER_QUERY_RESPONSE',
      payload: { error: 'ledger store unavailable' },
    });
    const row = screen.getByTestId('usage-ledger-load-state');
    expect(row).toHaveAttribute('data-load-display', 'error');
    expect(row).toHaveTextContent('ledger store unavailable');
    expect(document.body.textContent).not.toContain('$0.00');
  });

  test('a failed context reply is an error state on the context card', () => {
    const { wsRef, simulateMessage } = makeWsRef();
    render(<UsageSettingsPanel wsRef={wsRef} isConnected={true} />);
    simulateMessage({
      type: 'CONTEXT_BREAKDOWN_RESPONSE',
      payload: { success: false, error: 'no active session' },
    });
    const row = screen.getByTestId('usage-context-load-state');
    expect(row).toHaveAttribute('data-load-display', 'error');
    expect(row).toHaveTextContent('no active session');
  });
});
