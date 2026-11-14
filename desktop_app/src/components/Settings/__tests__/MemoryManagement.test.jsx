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

  test('shows the embedder-unavailable note when the startup verdict says ollama_off', async () => {
    render(<MemoryManagement />);

    act(() => {
      simulateMessage({
        type: 'MEMORY_EPISODES_RESPONSE',
        payload: {
          episodes: [],
          backend: 'native',
          embedding_stats: {
            episodes: 5,
            embedded: 0,
            pending: 5,
            model: 'ollama/qwen3-embedding:0.6b',
            last_failure: null,
            embedder_status: { model: 'qwen3-embedding:0.6b', state: 'ollama_off', detail: 'Ollama is not running' }
          }
        }
      });
    });

    await waitFor(() => {
      const note = screen.getByTestId('embedder-unavailable-note');
      expect(note).toBeInTheDocument();
      expect(note.textContent).toMatch(/Embedder unavailable/);
      expect(note.textContent).toMatch(/Ollama isn't running/);
      expect(note.textContent).toMatch(/qwen3-embedding:0\.6b/);
      expect(note.textContent).toMatch(/nothing is lost/i);
    });
  });

  test('no embedder note when the embedder is ready', async () => {
    render(<MemoryManagement />);

    act(() => {
      simulateMessage({
        type: 'MEMORY_EPISODES_RESPONSE',
        payload: {
          episodes: [],
          backend: 'native',
          embedding_stats: {
            episodes: 5,
            embedded: 5,
            pending: 0,
            model: 'ollama/qwen3-embedding:0.6b',
            last_failure: null,
            embedder_status: { model: 'qwen3-embedding:0.6b', state: 'ready', detail: '' }
          }
        }
      });
    });

    await waitFor(() => {
      expect(screen.getByText(/Embeddings: 5\/5 embedded/)).toBeInTheDocument();
    });
    expect(screen.queryByTestId('embedder-unavailable-note')).not.toBeInTheDocument();
  });

  // S9: the memory-backend picker. The engine's report lists
  // ALL four backends (owner decision — never installed-only);
  // these fixtures mirror memory/backend_status.py's shape.
  describe('memory backend picker (S9)', () => {
    const BACKENDS = [
      {
        id: 'native', name: 'Native (built-in)',
        description: "Aether's own memory in local storage.",
        needs: 'Nothing — built into Aether.',
        state: 'available', available: true, reason: null,
        warnings: [], detail: 'The default backend, fully tested with Aether.',
        current: true
      },
      {
        id: 'mem0', name: 'Mem0',
        description: 'The Mem0 service library as the memory store.',
        needs: 'The mem0ai package, plus an LLM provider key.',
        state: 'not_installed', available: false,
        reason: "The mem0ai package is not installed in the engine's environment (pip install mem0ai).",
        warnings: ['Experimental adapter: Aether\'s tests exercise it with stand-in doubles.'],
        detail: null, current: false
      },
      {
        id: 'langmem', name: 'LangMem',
        description: 'A LangMem-style adapter with a local JSON store.',
        needs: 'The langmem package.',
        state: 'available', available: true, reason: null,
        warnings: ['Search is word-matching (substring), not semantic.'],
        detail: null, current: false
      },
      {
        id: 'graphiti', name: 'Graphiti',
        description: 'Graphiti temporal knowledge graph in Neo4j.',
        needs: 'The graphiti-core package, plus a running Neo4j database.',
        state: 'not_configured', available: false,
        reason: "The graphiti-core package is installed, but NEO4J_PASSWORD is not set in the engine's environment.",
        warnings: ['Facts and episodes cannot be edited, deleted or cleared from Aether on this backend.'],
        detail: null, current: false
      }
    ];

    const renderWithBackends = (backends = BACKENDS) => {
      render(<MemoryManagement />);
      act(() => {
        simulateMessage({
          type: 'MEMORY_BACKENDS_RESPONSE',
          payload: { current: 'native', backends }
        });
      });
    };

    test('requests the backend report on mount and renders all four backends', async () => {
      renderWithBackends();
      expect(mockSendMessage).toHaveBeenCalledWith('MEMORY_BACKENDS_REQUEST', expect.any(Object));
      for (const id of ['native', 'mem0', 'langmem', 'graphiti']) {
        expect(await screen.findByTestId(`memory-backend-${id}`)).toBeInTheDocument();
      }
      // Status chips: Current / Available / Not installed / Not configured.
      expect(screen.getByTestId('memory-backend-native')).toHaveTextContent('Current');
      expect(screen.getByTestId('memory-backend-langmem')).toHaveTextContent('Available');
      expect(screen.getByTestId('memory-backend-mem0')).toHaveTextContent('Not installed');
      expect(screen.getByTestId('memory-backend-graphiti')).toHaveTextContent('Not configured');
    });

    test('unavailable backends show their reason and cannot be selected', async () => {
      renderWithBackends();
      expect(await screen.findByText(/mem0ai package is not installed/)).toBeInTheDocument();
      expect(screen.getByText(/NEO4J_PASSWORD is not set/)).toBeInTheDocument();
      expect(screen.getByTestId('memory-backend-switch-mem0')).toBeDisabled();
      expect(screen.getByTestId('memory-backend-switch-graphiti')).toBeDisabled();
      expect(screen.getByTestId('memory-backend-switch-native')).toBeDisabled();
      expect(screen.getByTestId('memory-backend-switch-langmem')).not.toBeDisabled();
      // Warnings are visible on the entries that carry them.
      expect(screen.getByText(/stand-in doubles/)).toBeInTheDocument();
      expect(screen.getByText(/word-matching \(substring\), not semantic/)).toBeInTheDocument();
    });

    test('switch asks first: confirmation names the no-migration consequence and only confirm sends', async () => {
      renderWithBackends();
      fireEvent.click(await screen.findByTestId('memory-backend-switch-langmem'));

      const confirmPanel = await screen.findByTestId('memory-backend-confirm');
      expect(confirmPanel).toHaveTextContent('Switch memory backend to LangMem?');
      expect(confirmPanel).toHaveTextContent(/switches stores/);
      expect(confirmPanel).toHaveTextContent(/not/);
      expect(confirmPanel).toHaveTextContent(/moved or copied/);
      expect(confirmPanel).toHaveTextContent(/switch back at any time/);
      expect(confirmPanel).toHaveTextContent(/embeddings do not transfer/i);
      expect(confirmPanel).toHaveTextContent(/next memory operation/);
      // Nothing sent yet — opening the confirmation is not a switch.
      expect(mockSendMessage.mock.calls.filter(c => c[0] === 'CONFIG_UPDATE_REQUEST')).toHaveLength(0);

      // Cancel sends nothing either and closes the panel.
      fireEvent.click(screen.getByTestId('memory-backend-cancel-switch'));
      expect(screen.queryByTestId('memory-backend-confirm')).not.toBeInTheDocument();
      expect(mockSendMessage.mock.calls.filter(c => c[0] === 'CONFIG_UPDATE_REQUEST')).toHaveLength(0);

      // Re-open and confirm: exactly one config update, for langmem.
      fireEvent.click(screen.getByTestId('memory-backend-switch-langmem'));
      fireEvent.click(await screen.findByTestId('memory-backend-confirm-switch'));
      const updates = mockSendMessage.mock.calls.filter(c => c[0] === 'CONFIG_UPDATE_REQUEST');
      expect(updates).toHaveLength(1);
      expect(updates[0][1].payload).toEqual({ config: { memory_backend: 'langmem' } });
    });

    test('a server refusal is surfaced verbatim and the panel closes', async () => {
      renderWithBackends();
      fireEvent.click(await screen.findByTestId('memory-backend-switch-langmem'));
      fireEvent.click(await screen.findByTestId('memory-backend-confirm-switch'));

      const refusal = "The langmem package is not installed in the engine's environment (pip install langmem).";
      act(() => {
        simulateMessage({
          type: 'CONFIG_UPDATE_RESPONSE',
          payload: { success: false, error: refusal }
        });
      });

      expect(await screen.findByTestId('memory-backend-error')).toHaveTextContent(refusal);
      expect(screen.queryByTestId('memory-backend-confirm')).not.toBeInTheDocument();
      // The Current marker did not move.
      expect(screen.getByTestId('memory-backend-native')).toHaveTextContent('Current');
    });

    test('a successful switch re-reads the report and the Current marker moves', async () => {
      renderWithBackends();
      fireEvent.click(await screen.findByTestId('memory-backend-switch-langmem'));
      fireEvent.click(await screen.findByTestId('memory-backend-confirm-switch'));

      const requestsBefore = mockSendMessage.mock.calls.filter(c => c[0] === 'MEMORY_BACKENDS_REQUEST').length;
      act(() => {
        simulateMessage({
          type: 'CONFIG_UPDATE_RESPONSE',
          payload: { success: true, config: { memory_backend: 'langmem' } }
        });
      });

      expect(await screen.findByTestId('memory-backend-notice')).toHaveTextContent(/switched to LangMem/);
      // The panel re-requested the report…
      expect(mockSendMessage.mock.calls.filter(c => c[0] === 'MEMORY_BACKENDS_REQUEST').length)
        .toBeGreaterThan(requestsBefore);
      // …and when the new report arrives, Current has moved.
      const moved = BACKENDS.map(b => ({ ...b, current: b.id === 'langmem' }));
      act(() => {
        simulateMessage({
          type: 'MEMORY_BACKENDS_RESPONSE',
          payload: { current: 'langmem', backends: moved }
        });
      });
      await waitFor(() => {
        expect(screen.getByTestId('memory-backend-langmem')).toHaveTextContent('Current');
      });
      expect(screen.getByTestId('memory-backend-native')).toHaveTextContent('Available');
    });
  });
});
