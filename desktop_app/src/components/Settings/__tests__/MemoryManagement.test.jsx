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

  const goToKgWithFacts = (facts) => {
    render(<MemoryManagement />);
    fireEvent.click(screen.getByText('Knowledge Graph'));
    act(() => {
      simulateMessage({ type: 'MEMORY_GRAPH_RESPONSE', payload: { facts } });
    });
  };

  const graphCalls = () => mockSendMessage.mock.calls.filter(c => c[0] === 'MEMORY_GRAPH_REQUEST');

  test('fact search sends debounced query and clearing re-requests without it', async () => {
    goToKgWithFacts([
      { fact_id: 'f1', entity_name: 'Arindam', entity_type: 'Person', fact_text: 'Likes coffee', status: 'active' }
    ]);

    const input = screen.getByPlaceholderText(/search facts/i);
    fireEvent.change(input, { target: { value: 'coffee' } });

    await waitFor(() => {
      expect(mockSendMessage).toHaveBeenCalledWith('MEMORY_GRAPH_REQUEST', expect.objectContaining({
        payload: expect.objectContaining({ query: 'coffee' })
      }));
    });

    fireEvent.change(input, { target: { value: '' } });
    await waitFor(() => {
      const calls = graphCalls();
      expect(calls[calls.length - 1][1].payload.query).toBeUndefined();
    });
  });

  test('show superseded toggle sends include_history and renders superseded badge', async () => {
    goToKgWithFacts([
      { fact_id: 'f1', entity_name: 'Arindam', entity_type: 'Person', fact_text: 'Likes tea', status: 'active' }
    ]);

    fireEvent.click(screen.getByRole('checkbox', { name: /show superseded/i }));

    await waitFor(() => {
      expect(mockSendMessage).toHaveBeenCalledWith('MEMORY_GRAPH_REQUEST', expect.objectContaining({
        payload: expect.objectContaining({ include_history: true })
      }));
    });

    act(() => {
      simulateMessage({
        type: 'MEMORY_GRAPH_RESPONSE',
        payload: {
          facts: [
            { fact_id: 'f1', entity_name: 'Arindam', entity_type: 'Person', fact_text: 'Likes tea', status: 'active' },
            { fact_id: 'f2', entity_name: 'Arindam', entity_type: 'Person', fact_text: 'Likes coffee', status: 'superseded', superseded_by: 'f1' }
          ]
        }
      });
    });

    await waitFor(() => {
      expect(screen.getByText('superseded')).toBeInTheDocument();
      expect(screen.getByText('Likes coffee')).toBeInTheDocument();
    });
    // Superseded rows render dimmed
    expect(screen.getByText('Likes coffee').parentElement).toHaveStyle({ opacity: '0.55' });
    expect(screen.getByText('Likes tea').parentElement).toHaveStyle({ opacity: '1' });
  });

  test('editing a fact saves via MEMORY_FACT_UPDATE_REQUEST and renders the refreshed list', async () => {
    goToKgWithFacts([
      { fact_id: 'f1', entity_name: 'Arindam', entity_type: 'Person', fact_text: 'Likes tea', status: 'active' }
    ]);

    fireEvent.click(await screen.findByRole('button', { name: /^edit$/i }));

    const editor = screen.getByDisplayValue('Likes tea');
    fireEvent.change(editor, { target: { value: 'Likes green tea' } });
    fireEvent.click(screen.getByRole('button', { name: /^save$/i }));

    expect(mockSendMessage).toHaveBeenCalledWith('MEMORY_FACT_UPDATE_REQUEST', expect.objectContaining({
      payload: expect.objectContaining({ fact_id: 'f1', fact_text: 'Likes green tea', include_history: false })
    }));

    act(() => {
      simulateMessage({
        type: 'MEMORY_FACT_UPDATE_RESPONSE',
        payload: {
          success: true,
          fact_id: 'f1',
          facts: [
            { fact_id: 'f1', entity_name: 'Arindam', entity_type: 'Person', fact_text: 'Likes green tea', status: 'active' }
          ]
        }
      });
    });

    await waitFor(() => {
      expect(screen.getByText('Likes green tea')).toBeInTheDocument();
    });
    expect(screen.queryByRole('button', { name: /^save$/i })).not.toBeInTheDocument();
  });

  test('export button sends MEMORY_EXPORT_REQUEST and a failure response shows the error', async () => {
    goToKgWithFacts([
      { fact_id: 'f1', entity_name: 'Arindam', entity_type: 'Person', fact_text: 'Likes tea', status: 'active' }
    ]);

    fireEvent.click(screen.getByRole('button', { name: /export memory/i }));
    expect(mockSendMessage).toHaveBeenCalledWith('MEMORY_EXPORT_REQUEST', expect.any(Object));

    act(() => {
      simulateMessage({
        type: 'MEMORY_EXPORT_RESPONSE',
        payload: { success: false, error: 'Export blew up' }
      });
    });

    await waitFor(() => {
      expect(screen.getByText('Export blew up')).toBeInTheDocument();
    });
  });

  test('export success response is handled without error', async () => {
    goToKgWithFacts([
      { fact_id: 'f1', entity_name: 'Arindam', entity_type: 'Person', fact_text: 'Likes tea', status: 'active' }
    ]);

    fireEvent.click(screen.getByRole('button', { name: /export memory/i }));
    expect(mockSendMessage).toHaveBeenCalledWith('MEMORY_EXPORT_REQUEST', expect.any(Object));

    act(() => {
      simulateMessage({
        type: 'MEMORY_EXPORT_RESPONSE',
        payload: { success: true, export: { facts: [], episodes: [] } }
      });
    });

    // No error line appears after a successful export
    expect(screen.queryByText(/export failed/i)).not.toBeInTheDocument();
  });

  test('episodic stats line renders backend, embedding counts, pending and last failure', async () => {
    render(<MemoryManagement />);

    act(() => {
      simulateMessage({
        type: 'MEMORY_EPISODES_RESPONSE',
        payload: {
          episodes: [],
          backend: 'native',
          embedding_stats: {
            episodes: 5,
            embedded: 3,
            pending: 2,
            model: 'nomic-embed-text',
            last_failure: { error_type: 'TimeoutError', error: 'embed timed out', at: 1700000000 }
          }
        }
      });
    });

    await waitFor(() => {
      expect(screen.getByText(/Backend: native/)).toBeInTheDocument();
      expect(screen.getByText(/Embeddings: 3\/5 embedded/)).toBeInTheDocument();
      expect(screen.getByText(/2 pending/)).toBeInTheDocument();
      expect(screen.getByText(/TimeoutError/)).toBeInTheDocument();
    });
  });
});
