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

  // ------------------------------------------------------------------
  // E4-F14 / E4-F15: failure honesty + individual fact delete.
  // The engine's replies are truthful since E4-F08 (failures carry
  // success=False / an error field); the UI used to ignore that —
  // removing rows optimistically on click, applying the empty list
  // a failure payload carries, rendering list errors as emptiness,
  // and leaving a rejected edit unexplained. Fact rows also had no
  // delete action although the engine's MEMORY_FACT_DELETE handler
  // exists.
  // ------------------------------------------------------------------

  const UNAVAILABLE = 'The configured memory backend is unavailable (see engine log).';

  const loadOneEpisode = () => {
    render(<MemoryManagement />);
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
  };

  test('episodes list failure shows the error instead of an empty list', async () => {
    render(<MemoryManagement />);
    act(() => {
      simulateMessage({
        type: 'MEMORY_EPISODES_RESPONSE',
        payload: { episodes: [], error: UNAVAILABLE }
      });
    });
    await waitFor(() => {
      expect(screen.getByText(/memory backend is unavailable/i)).toBeInTheDocument();
    });
    expect(screen.queryByText('No episodes found.')).not.toBeInTheDocument();
  });

  test('graph list failure shows the error instead of an empty graph', async () => {
    render(<MemoryManagement />);
    fireEvent.click(screen.getByText('Knowledge Graph'));
    act(() => {
      simulateMessage({
        type: 'MEMORY_GRAPH_RESPONSE',
        payload: { facts: [], error: UNAVAILABLE }
      });
    });
    await waitFor(() => {
      expect(screen.getByText(/memory backend is unavailable/i)).toBeInTheDocument();
    });
    expect(screen.queryByText('No facts in Knowledge Graph.')).not.toBeInTheDocument();
  });

  test('episode delete is not optimistic and a failure keeps the row', async () => {
    loadOneEpisode();
    await screen.findByText(/Create a test file/i);

    fireEvent.click(screen.getByTitle('Delete this episode'));
    expect(mockSendMessage).toHaveBeenCalledWith('MEMORY_EPISODE_DELETE_REQUEST', expect.objectContaining({
      payload: expect.objectContaining({ task_id: 'task-1' })
    }));
    // No reply yet — the row must still be on screen.
    expect(screen.getByText(/Create a test file/i)).toBeInTheDocument();

    act(() => {
      simulateMessage({
        type: 'MEMORY_EPISODE_DELETE_RESPONSE',
        payload: { success: false, task_id: 'task-1', episodes: [], error: 'delete refused' }
      });
    });
    await waitFor(() => {
      expect(screen.getByText(/delete refused/i)).toBeInTheDocument();
    });
    expect(screen.getByText(/Create a test file/i)).toBeInTheDocument();
  });

  test('episode delete removes the row only when the response confirms it', async () => {
    loadOneEpisode();
    await screen.findByText(/Create a test file/i);

    fireEvent.click(screen.getByTitle('Delete this episode'));
    act(() => {
      simulateMessage({
        type: 'MEMORY_EPISODE_DELETE_RESPONSE',
        payload: { success: true, task_id: 'task-1', episodes: [] }
      });
    });
    await waitFor(() => {
      expect(screen.queryByText(/Create a test file/i)).not.toBeInTheDocument();
    });
  });

  test('failed clear-all keeps the episodes and shows the error', async () => {
    loadOneEpisode();
    await screen.findByText(/Create a test file/i);

    fireEvent.click(await screen.findByText('Clear All Memory'));
    expect(mockSendMessage).toHaveBeenCalledWith('MEMORY_EPISODES_CLEAR_REQUEST', expect.any(Object));
    expect(screen.getByText(/Create a test file/i)).toBeInTheDocument();

    act(() => {
      simulateMessage({
        type: 'MEMORY_EPISODES_CLEAR_RESPONSE',
        payload: {
          success: false, error: 'clear refused',
          episodes: [{ task_id: 'task-1', user_prompt: 'Create a test file', timestamp: Date.now() / 1000 }]
        }
      });
    });
    await waitFor(() => {
      expect(screen.getByText(/clear refused/i)).toBeInTheDocument();
    });
    expect(screen.getByText(/Create a test file/i)).toBeInTheDocument();
  });

  test('failed entity delete keeps the facts and shows the error', async () => {
    goToKgWithFacts([
      { fact_id: 'f1', entity_name: 'Arindam', entity_type: 'Person', fact_text: 'Likes coffee', status: 'active' }
    ]);
    await screen.findByText('Likes coffee');

    fireEvent.click(screen.getByTitle('Delete all facts for Arindam'));
    expect(mockSendMessage).toHaveBeenCalledWith('MEMORY_ENTITY_DELETE_REQUEST', expect.any(Object));
    expect(screen.getByText('Likes coffee')).toBeInTheDocument();

    act(() => {
      simulateMessage({
        type: 'MEMORY_ENTITY_DELETE_RESPONSE',
        payload: { success: false, entity_name: 'Arindam', facts: [], error: 'entity delete refused' }
      });
    });
    await waitFor(() => {
      expect(screen.getByText(/entity delete refused/i)).toBeInTheDocument();
    });
    expect(screen.getByText('Likes coffee')).toBeInTheDocument();
  });

  test('failed graph clear keeps the facts and shows the error', async () => {
    goToKgWithFacts([
      { fact_id: 'f1', entity_name: 'Arindam', entity_type: 'Person', fact_text: 'Likes coffee', status: 'active' }
    ]);
    await screen.findByText('Likes coffee');

    fireEvent.click(await screen.findByText('Clear All Knowledge'));
    expect(mockSendMessage).toHaveBeenCalledWith('MEMORY_GRAPH_CLEAR_REQUEST', expect.any(Object));

    act(() => {
      simulateMessage({
        type: 'MEMORY_GRAPH_CLEAR_RESPONSE',
        payload: { success: false, facts: [], error: 'clear refused' }
      });
    });
    await waitFor(() => {
      expect(screen.getByText(/clear refused/i)).toBeInTheDocument();
    });
    expect(screen.getByText('Likes coffee')).toBeInTheDocument();
  });

  test('rejected fact edit stays open and explains the rejection', async () => {
    goToKgWithFacts([
      { fact_id: 'f1', entity_name: 'Arindam', entity_type: 'Person', fact_text: 'Likes tea', status: 'active' }
    ]);

    fireEvent.click(await screen.findByRole('button', { name: /^edit$/i }));
    const editor = screen.getByDisplayValue('Likes tea');
    fireEvent.change(editor, { target: { value: '   ' } });
    fireEvent.click(screen.getByRole('button', { name: /^save$/i }));

    act(() => {
      simulateMessage({
        type: 'MEMORY_FACT_UPDATE_RESPONSE',
        payload: {
          success: false, fact_id: 'f1',
          facts: [
            { fact_id: 'f1', entity_name: 'Arindam', entity_type: 'Person', fact_text: 'Likes tea', status: 'active' }
          ]
        }
      });
    });

    await waitFor(() => {
      expect(screen.getByText(/was not changed/i)).toBeInTheDocument();
    });
    // The editor is still open so the user can fix or abandon the edit.
    expect(screen.getByRole('button', { name: /^save$/i })).toBeInTheDocument();
  });

  test('each fact row offers an individual delete wired to MEMORY_FACT_DELETE_REQUEST', async () => {
    goToKgWithFacts([
      { fact_id: 'f1', entity_name: 'Arindam', entity_type: 'Person', fact_text: 'Likes tea', status: 'active' },
      { fact_id: 'f2', entity_name: 'Arindam', entity_type: 'Person', fact_text: 'Likes coffee', status: 'active' }
    ]);
    await screen.findByText('Likes tea');

    const deleteButtons = screen.getAllByRole('button', { name: /^delete$/i });
    expect(deleteButtons).toHaveLength(2);

    fireEvent.click(deleteButtons[0]);
    expect(mockSendMessage).toHaveBeenCalledWith('MEMORY_FACT_DELETE_REQUEST', expect.objectContaining({
      payload: expect.objectContaining({ fact_id: 'f1' })
    }));
    // Not optimistic: the fact is still shown until the delete is confirmed.
    expect(screen.getByText('Likes tea')).toBeInTheDocument();

    act(() => {
      simulateMessage({
        type: 'MEMORY_FACT_DELETE_RESPONSE',
        payload: {
          success: true, fact_id: 'f1',
          facts: [
            { fact_id: 'f2', entity_name: 'Arindam', entity_type: 'Person', fact_text: 'Likes coffee', status: 'active' }
          ]
        }
      });
    });
    await waitFor(() => {
      expect(screen.queryByText('Likes tea')).not.toBeInTheDocument();
    });
    expect(screen.getByText('Likes coffee')).toBeInTheDocument();
  });

  test('failed individual fact delete keeps the fact and shows the error', async () => {
    goToKgWithFacts([
      { fact_id: 'f1', entity_name: 'Arindam', entity_type: 'Person', fact_text: 'Likes tea', status: 'active' }
    ]);
    await screen.findByText('Likes tea');

    fireEvent.click(screen.getByRole('button', { name: /^delete$/i }));
    act(() => {
      simulateMessage({
        type: 'MEMORY_FACT_DELETE_RESPONSE',
        payload: { success: false, fact_id: 'f1', facts: [], error: 'fact delete refused' }
      });
    });
    await waitFor(() => {
      expect(screen.getByText(/fact delete refused/i)).toBeInTheDocument();
    });
    expect(screen.getByText('Likes tea')).toBeInTheDocument();
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
