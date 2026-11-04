/** Wave 3 slice 1 (RUX-C-01 umbrella, Memory section): while the
 * first episodes/facts replies are in flight — or queued behind a
 * dead engine — MemoryManagement shows a load state instead of
 * "No episodes found." / "No facts in Knowledge Graph." Error
 * replies stay the E4-F14 error states. */
import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import MemoryManagement from '../MemoryManagement';
import { useAetherSocket } from '../../../contexts/SocketContext';

jest.mock('../../../contexts/SocketContext', () => ({
  useAetherSocket: jest.fn(),
}));

function makeHarness({ isConnected = true } = {}) {
  let messageListeners = [];
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
  useAetherSocket.mockReturnValue({ wsRef: mockWsRef, sendMessage: jest.fn(), isConnected });
  const simulateMessage = (data) => {
    act(() => {
      messageListeners.forEach((cb) => cb({ data: JSON.stringify(data) }));
    });
  };
  return { mockWsRef, simulateMessage };
}

describe('MemoryManagement load states (RUX-C-01)', () => {
  test('episodes: loading state before the first reply — not "No episodes found."', () => {
    const { mockWsRef } = makeHarness();
    render(<MemoryManagement />);
    expect(screen.getByTestId('memory-episodes-load-state')).toHaveAttribute('data-load-display', 'loading');
    expect(screen.queryByText('No episodes found.')).not.toBeInTheDocument();
  });

  test('episodes: offline state while the engine is down', () => {
    makeHarness({ isConnected: false });
    render(<MemoryManagement />);
    const row = screen.getByTestId('memory-episodes-load-state');
    expect(row).toHaveAttribute('data-load-display', 'offline');
    expect(row).toHaveTextContent(/Waiting for the engine/);
    expect(screen.queryByText('No episodes found.')).not.toBeInTheDocument();
  });

  test('episodes: an answered-empty reply shows the genuine empty text', () => {
    const { simulateMessage } = makeHarness();
    render(<MemoryManagement />);
    simulateMessage({ type: 'MEMORY_EPISODES_RESPONSE', payload: { episodes: [] } });
    expect(screen.queryByTestId('memory-episodes-load-state')).not.toBeInTheDocument();
    expect(screen.getByText('No episodes found.')).toBeInTheDocument();
  });

  test('facts: the Knowledge Graph tab loads, then shows its genuine empty text', () => {
    const { simulateMessage } = makeHarness();
    render(<MemoryManagement />);
    fireEvent.click(screen.getByText('Knowledge Graph'));
    expect(screen.getByTestId('memory-facts-load-state')).toHaveAttribute('data-load-display', 'loading');
    expect(screen.queryByText('No facts in Knowledge Graph.')).not.toBeInTheDocument();
    simulateMessage({ type: 'MEMORY_GRAPH_RESPONSE', payload: { facts: [] } });
    expect(screen.queryByTestId('memory-facts-load-state')).not.toBeInTheDocument();
    expect(screen.getByText('No facts in Knowledge Graph.')).toBeInTheDocument();
  });
});
