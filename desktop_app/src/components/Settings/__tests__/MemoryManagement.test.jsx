import React from 'react';
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react';
import MemoryManagement from '../MemoryManagement';
import { useAetherSocket } from '../../../contexts/SocketContext';

jest.mock('../../../contexts/SocketContext', () => ({
  useAetherSocket: jest.fn()
}));

describe('MemoryManagement Component', () => {
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
    
    // Mock window.confirm
    window.confirm = jest.fn(() => true);
  });

  const simulateMessage = (data) => {
    messageListeners.forEach(cb => cb({ data: JSON.stringify(data) }));
  };

  test('fetches and displays episodic memories on mount', async () => {
    render(<MemoryManagement />);
    
    // Ensure request is sent on mount
    expect(mockSendMessage).toHaveBeenCalledWith('MEMORY_EPISODES_REQUEST', expect.any(Object));

    act(() => {
      simulateMessage({
        type: 'MEMORY_EPISODES_RESPONSE',
        payload: {
          episodes: [
            { task_id: 'task-1', user_prompt: 'Create a test file', timestamp: Date.now() / 1000 }
          ]
        }
      });
    });

    await waitFor(() => {
      expect(screen.getByText(/Create a test file/i)).toBeInTheDocument();
    });
  });

  test('switches to knowledge graph and renders facts', async () => {
    render(<MemoryManagement />);
    
    const kgTab = screen.getByText('Knowledge Graph');
    fireEvent.click(kgTab);

    expect(mockSendMessage).toHaveBeenCalledWith('MEMORY_GRAPH_REQUEST', expect.any(Object));

    act(() => {
      simulateMessage({
        type: 'MEMORY_GRAPH_RESPONSE',
        payload: {
          facts: [
            { fact_id: 'fact-1', entity_name: 'testing_entity', entity_type: 'Concept', attributes: [], relationships: [] }
          ]
        }
      });
    });

    await waitFor(() => {
      expect(screen.getByText('testing_entity')).toBeInTheDocument();
    });
  });

  test('handles network failure offline state', async () => {
    render(<MemoryManagement />);
    
    // Must add an episode so the clear button appears
    act(() => {
      simulateMessage({
        type: 'MEMORY_EPISODES_RESPONSE',
        payload: {
          episodes: [
            { task_id: 'task-1', user_prompt: 'Test episode', timestamp: Date.now() / 1000 }
          ]
        }
      });
    });
    
    // Simulate offline state
    mockWsRef.current.readyState = 3; // CLOSED
    
    const clearButton = await screen.findByText('Clear All Memory');
    fireEvent.click(clearButton);
    
    expect(mockSendMessage).toHaveBeenCalledWith('MEMORY_EPISODES_CLEAR_REQUEST', expect.any(Object));
  });
});
