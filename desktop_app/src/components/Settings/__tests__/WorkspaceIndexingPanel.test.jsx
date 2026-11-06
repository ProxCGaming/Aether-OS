import React from 'react';
import { render, screen, fireEvent, act, within } from '@testing-library/react';
import '@testing-library/jest-dom';
import WorkspaceIndexingPanel from '../WorkspaceIndexingPanel';
import { useAetherSocket } from '../../../contexts/SocketContext';

jest.mock('../../../contexts/SocketContext', () => ({
  useAetherSocket: jest.fn(),
}));

function makeWsRef() {
  let messageListeners = [];
  const wsRef = {
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
  const simulateMessage = (data) => {
    messageListeners.forEach((cb) => cb({ data: JSON.stringify(data) }));
  };
  useAetherSocket.mockReturnValue({
    wsRef,
    sendMessage: null,
  });
  return { wsRef, simulateMessage };
}

describe('WorkspaceIndexingPanel Component', () => {
  const mockWorkspace = '/home/user/workspace/my-project';

  const mockStatusPayload = {
    status: 'idle',
    total_files: 10,
    indexed_files: 4,
    total_chunks: 18,
    embedder: 'qwen3-embedding:0.6b',
    paused: false,
    indexed_docs: [
      { file_path: 'README.md', file_size: 1024, status: 'indexed' },
      { file_path: 'docs/architecture.md', file_size: 4096, status: 'indexed' },
      { file_path: 'docs/api.md', file_size: 2048, status: 'indexed' },
      { file_path: 'notes.txt', file_size: 512, status: 'pending' },
    ],
    excluded_files: [
      { file_path: '.env', reason: 'gitignore' },
      { file_path: 'package-lock.json', reason: 'lockfile' },
      { file_path: 'dataset-large.txt', reason: 'too_large' },
      { file_path: 'firmware.bin', reason: 'binary' },
    ],
  };

  test('fetches status on mount and renders metrics and controls', async () => {
    const { wsRef, simulateMessage } = makeWsRef();
    render(<WorkspaceIndexingPanel wsRef={wsRef} initialWorkspace={mockWorkspace} />);

    // Expect status request sent
    expect(wsRef.current.send).toHaveBeenCalled();
    const sent = wsRef.current.send.mock.calls.map((c) => JSON.parse(c[0]));
    expect(sent.some((m) => m.type === 'WORKSPACE_INDEX_STATUS_REQUEST')).toBe(true);

    act(() => {
      simulateMessage({
        type: 'WORKSPACE_INDEX_STATUS_RESPONSE',
        payload: mockStatusPayload,
      });
    });

    // Check metrics are rendered
    expect(screen.getByText('Workspace Indexing')).toBeInTheDocument();
    expect(screen.getAllByText('4').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText('18')).toBeInTheDocument(); // chunks
    expect(screen.getByText('qwen3-embedding:0.6b')).toBeInTheDocument();
  });

  test('renders indexed files list and excluded files list with reasons', async () => {
    const { wsRef, simulateMessage } = makeWsRef();
    render(<WorkspaceIndexingPanel wsRef={wsRef} initialWorkspace={mockWorkspace} />);

    act(() => {
      simulateMessage({
        type: 'WORKSPACE_INDEX_STATUS_RESPONSE',
        payload: mockStatusPayload,
      });
    });

    // Default tab is Indexed Files
    expect(screen.getByText('README.md')).toBeInTheDocument();
    expect(screen.getByText('docs/architecture.md')).toBeInTheDocument();

    // Switch to Excluded Files tab
    const excludedTab = screen.getByRole('button', { name: /Excluded Files/i });
    fireEvent.click(excludedTab);

    // Verify excluded files and reasons appear
    expect(screen.getByText('.env')).toBeInTheDocument();
    expect(screen.getByText(/Gitignored/i)).toBeInTheDocument();
    expect(screen.getByText('package-lock.json')).toBeInTheDocument();
    expect(screen.getByText(/Lockfile/i)).toBeInTheDocument();
    expect(screen.getByText('dataset-large.txt')).toBeInTheDocument();
    expect(screen.getByText(/Too large/i)).toBeInTheDocument();
    expect(screen.getByText('firmware.bin')).toBeInTheDocument();
    expect(screen.getByText(/Binary/i)).toBeInTheDocument();
  });

  test('filters files list when search query is entered', async () => {
    const { wsRef, simulateMessage } = makeWsRef();
    render(<WorkspaceIndexingPanel wsRef={wsRef} initialWorkspace={mockWorkspace} />);

    act(() => {
      simulateMessage({
        type: 'WORKSPACE_INDEX_STATUS_RESPONSE',
        payload: mockStatusPayload,
      });
    });

    const searchInput = screen.getByPlaceholderText(/Filter files/i);
    fireEvent.change(searchInput, { target: { value: 'architecture' } });

    expect(screen.getByText('docs/architecture.md')).toBeInTheDocument();
    expect(screen.queryByText('README.md')).not.toBeInTheDocument();
  });

  test('clicking pause sends WORKSPACE_INDEX_PAUSE_REQUEST with pause: true and updates button', async () => {
    const { wsRef, simulateMessage } = makeWsRef();
    render(<WorkspaceIndexingPanel wsRef={wsRef} initialWorkspace={mockWorkspace} />);

    act(() => {
      simulateMessage({
        type: 'WORKSPACE_INDEX_STATUS_RESPONSE',
        payload: mockStatusPayload,
      });
    });

    const pauseBtn = screen.getByRole('button', { name: /Pause Indexing/i });
    fireEvent.click(pauseBtn);

    const sent = wsRef.current.send.mock.calls.map((c) => JSON.parse(c[0]));
    const pauseMsg = sent.find((m) => m.type === 'WORKSPACE_INDEX_PAUSE_REQUEST');
    expect(pauseMsg).toBeDefined();
    expect(pauseMsg.payload.pause).toBe(true);
    expect(pauseMsg.payload.workspace_root).toBe(mockWorkspace);

    // Simulate response
    act(() => {
      simulateMessage({
        type: 'WORKSPACE_INDEX_PAUSE_RESPONSE',
        payload: { paused: true },
      });
    });

    expect(screen.getByRole('button', { name: /Resume Indexing/i })).toBeInTheDocument();
  });

  test('clicking resume sends WORKSPACE_INDEX_PAUSE_REQUEST with pause: false', async () => {
    const { wsRef, simulateMessage } = makeWsRef();
    render(<WorkspaceIndexingPanel wsRef={wsRef} initialWorkspace={mockWorkspace} />);

    act(() => {
      simulateMessage({
        type: 'WORKSPACE_INDEX_STATUS_RESPONSE',
        payload: { ...mockStatusPayload, status: 'paused', paused: true },
      });
    });

    const resumeBtn = screen.getByRole('button', { name: /Resume Indexing/i });
    fireEvent.click(resumeBtn);

    const sent = wsRef.current.send.mock.calls.map((c) => JSON.parse(c[0]));
    const resumeMsg = sent.find((m) => m.type === 'WORKSPACE_INDEX_PAUSE_REQUEST' && m.payload.pause === false);
    expect(resumeMsg).toBeDefined();
    expect(resumeMsg.payload.pause).toBe(false);
  });

  test('embedder picker is fed from downloaded embedding models', async () => {
    const { wsRef, simulateMessage } = makeWsRef();
    render(<WorkspaceIndexingPanel wsRef={wsRef} initialWorkspace={mockWorkspace} />);

    // The panel asks the engine for the local model list on mount.
    const sent = wsRef.current.send.mock.calls.map((c) => JSON.parse(c[0]));
    expect(sent.some((m) => m.type === 'LOCAL_MODEL_LIST_REQUEST')).toBe(true);

    act(() => {
      simulateMessage({
        type: 'WORKSPACE_INDEX_STATUS_RESPONSE',
        payload: mockStatusPayload,
      });
    });
    act(() => {
      simulateMessage({
        type: 'LOCAL_MODEL_LIST_RESPONSE',
        payload: {
          models: [
            { name: 'nomic-embed-text:latest', downloaded: true, type: 'embedding' },
            { name: 'llama3:8b', downloaded: true, type: 'chat' },
            { name: 'qwen3-embedding:4b', downloaded: false, type: 'embedding' },
          ],
        },
      });
    });

    const embedderSelect = screen.getByLabelText(/Embedding Model/i);
    const optionLabels = Array.from(embedderSelect.querySelectorAll('option')).map((o) => o.textContent);
    // Built-ins remain, the downloaded embedding model joins them,
    // chat models and not-downloaded models do not.
    expect(optionLabels.some((t) => t.includes('qwen3-embedding:0.6b'))).toBe(true);
    expect(optionLabels.some((t) => t.includes('nomic-embed-text:latest'))).toBe(true);
    expect(optionLabels.some((t) => t.includes('llama3:8b'))).toBe(false);
    expect(embedderSelect.querySelectorAll('option').length).toBe(3);
  });

  test('embedder switch displays warning modal before wipe and does not send config on cancel', async () => {
    const { wsRef, simulateMessage } = makeWsRef();
    render(<WorkspaceIndexingPanel wsRef={wsRef} initialWorkspace={mockWorkspace} />);

    act(() => {
      simulateMessage({
        type: 'WORKSPACE_INDEX_STATUS_RESPONSE',
        payload: mockStatusPayload,
      });
    });

    const embedderSelect = screen.getByLabelText(/Embedding Model/i);
    fireEvent.change(embedderSelect, { target: { value: 'qwen3-embedding:4b' } });

    // Warning modal must be open BEFORE any config request is sent
    const modal = screen.getByTestId('reindex-warning-modal');
    expect(modal).toBeInTheDocument();
    expect(within(modal).getByText(/Full Re-index Required/i)).toBeInTheDocument();
    expect(within(modal).getByText(/qwen3-embedding:4b/i)).toBeInTheDocument();

    // Verify NO WORKSPACE_INDEX_CONFIG_REQUEST was sent yet
    let sent = wsRef.current.send.mock.calls.map((c) => JSON.parse(c[0]));
    expect(sent.some((m) => m.type === 'WORKSPACE_INDEX_CONFIG_REQUEST')).toBe(false);

    // Click cancel in modal
    const cancelBtn = screen.getByRole('button', { name: /Cancel/i });
    fireEvent.click(cancelBtn);

    expect(screen.queryByTestId('reindex-warning-modal')).not.toBeInTheDocument();
    sent = wsRef.current.send.mock.calls.map((c) => JSON.parse(c[0]));
    expect(sent.some((m) => m.type === 'WORKSPACE_INDEX_CONFIG_REQUEST')).toBe(false);
  });

  test('confirming embedder switch modal sends WORKSPACE_INDEX_CONFIG_REQUEST and triggers re-index', async () => {
    const { wsRef, simulateMessage } = makeWsRef();
    render(<WorkspaceIndexingPanel wsRef={wsRef} initialWorkspace={mockWorkspace} />);

    act(() => {
      simulateMessage({
        type: 'WORKSPACE_INDEX_STATUS_RESPONSE',
        payload: mockStatusPayload,
      });
    });

    const embedderSelect = screen.getByLabelText(/Embedding Model/i);
    fireEvent.change(embedderSelect, { target: { value: 'qwen3-embedding:4b' } });

    const confirmBtn = screen.getByRole('button', { name: /Confirm & Re-index/i });
    fireEvent.click(confirmBtn);

    const sent = wsRef.current.send.mock.calls.map((c) => JSON.parse(c[0]));
    const configMsg = sent.find((m) => m.type === 'WORKSPACE_INDEX_CONFIG_REQUEST');
    expect(configMsg).toBeDefined();
    expect(configMsg.payload.embedder).toBe('qwen3-embedding:4b');

    // Also verify re-index start request is sent
    const startMsg = sent.find((m) => m.type === 'WORKSPACE_INDEX_START_REQUEST');
    expect(startMsg).toBeDefined();
    expect(startMsg.payload.confirm).toBe(true);
    expect(startMsg.payload.embedder).toBe('qwen3-embedding:4b');
  });

  test('displays indexing progress when WORKSPACE_INDEX_PROGRESS event is received', async () => {
    const { wsRef, simulateMessage } = makeWsRef();
    render(<WorkspaceIndexingPanel wsRef={wsRef} initialWorkspace={mockWorkspace} />);

    act(() => {
      simulateMessage({
        type: 'WORKSPACE_INDEX_STATUS_RESPONSE',
        payload: { ...mockStatusPayload, status: 'indexing' },
      });
      simulateMessage({
        type: 'WORKSPACE_INDEX_PROGRESS',
        payload: {
          workspace_root: mockWorkspace,
          indexed: 3,
          total: 10,
          current_file: 'docs/api.md',
          status: 'indexing',
        },
      });
    });

    expect(screen.getByText(/Processing docs\/api.md/i)).toBeInTheDocument();
    expect(screen.getByText(/3 \/ 10 files/i)).toBeInTheDocument();
  });

  test('handles endpoint-down / Ollama offline state honestly without crashing', async () => {
    const { wsRef, simulateMessage } = makeWsRef();
    render(<WorkspaceIndexingPanel wsRef={wsRef} initialWorkspace={mockWorkspace} />);

    act(() => {
      simulateMessage({
        type: 'WORKSPACE_INDEX_STATUS_RESPONSE',
        payload: {
          ...mockStatusPayload,
          status: 'idle',
          indexed_files: 0,
          indexed_docs: [{ file_path: 'notes.txt', file_size: 512, status: 'pending' }],
        },
      });
    });

    // When files are pending due to breaker open / endpoint down, pending status is indicated
    expect(screen.getByText('notes.txt')).toBeInTheDocument();
    expect(screen.getByText(/pending/i)).toBeInTheDocument();
  });
});

describe('WorkspaceIndexingPanel load states (RUX-C-07)', () => {
  const mockWorkspace = '/home/user/workspace/my-project';

  test('before the first status reply, the fabricated placeholder is never rendered as fact', () => {
    const { wsRef } = makeWsRef();
    render(<WorkspaceIndexingPanel wsRef={wsRef} initialWorkspace={mockWorkspace} />);
    const row = screen.getByTestId('indexing-status-load-state');
    expect(row.dataset.loadDisplay).toBe('loading');
    expect(row.textContent).toMatch(/Loading indexing status/);
    // The invented record — an embedder presented as the user's,
    // zero files, an Idle badge — must not appear pre-answer.
    expect(screen.queryByText('Idle / Up-to-date')).not.toBeInTheDocument();
    expect(screen.queryByText('Indexed Files')).not.toBeInTheDocument();
    expect(screen.queryByText('No indexed prose files found.')).not.toBeInTheDocument();
    // The embedder select is inert until the real value lands.
    expect(screen.getByLabelText('Embedding Model')).toBeDisabled();
  });

  test('while disconnected, the gate reads as waiting for the engine', () => {
    const { wsRef } = makeWsRef();
    useAetherSocket.mockReturnValue({ wsRef, sendMessage: null, isConnected: false });
    render(<WorkspaceIndexingPanel wsRef={wsRef} initialWorkspace={mockWorkspace} />);
    const row = screen.getByTestId('indexing-status-load-state');
    expect(row.dataset.loadDisplay).toBe('offline');
    expect(row.textContent).toMatch(/Waiting for the engine/);
  });

  test('a status reply carrying an error is the error state, not idle zeros', () => {
    const { wsRef, simulateMessage } = makeWsRef();
    render(<WorkspaceIndexingPanel wsRef={wsRef} initialWorkspace={mockWorkspace} />);
    act(() => {
      simulateMessage({
        type: 'WORKSPACE_INDEX_STATUS_RESPONSE',
        payload: { error: 'index store unavailable' },
      });
    });
    const row = screen.getByTestId('indexing-status-load-state');
    expect(row.dataset.loadDisplay).toBe('error');
    expect(row.textContent).toMatch(/could not be loaded/);
    expect(screen.queryByText('Idle / Up-to-date')).not.toBeInTheDocument();
  });

  test('an answered status renders the real card and re-enables the embedder select', () => {
    const { wsRef, simulateMessage } = makeWsRef();
    render(<WorkspaceIndexingPanel wsRef={wsRef} initialWorkspace={mockWorkspace} />);
    act(() => {
      simulateMessage({
        type: 'WORKSPACE_INDEX_STATUS_RESPONSE',
        payload: {
          status: 'idle', total_files: 2, indexed_files: 2, total_chunks: 5,
          embedder: 'qwen3-embedding:4b', paused: false, indexed_docs: [], excluded_files: [],
        },
      });
    });
    expect(screen.queryByTestId('indexing-status-load-state')).not.toBeInTheDocument();
    expect(screen.getByText('Idle / Up-to-date')).toBeInTheDocument();
    expect(screen.getByLabelText('Embedding Model')).not.toBeDisabled();
    // An answered-empty file list is a genuine empty.
    expect(screen.getByText('No indexed prose files found.')).toBeInTheDocument();
  });
});

describe('WorkspaceIndexingPanel action honesty (D2-15) and correlation (D2-16)', () => {
  const mockWorkspace = '/home/user/workspace/my-project';
  const otherWorkspace = '/home/user/workspace/other-project';

  const mockStatusPayload = {
    status: 'idle',
    total_files: 10,
    indexed_files: 4,
    total_chunks: 18,
    embedder: 'qwen3-embedding:0.6b',
    paused: false,
    indexed_docs: [
      { file_path: 'README.md', file_size: 1024, status: 'indexed' },
    ],
    excluded_files: [],
  };

  function renderReady(payload = mockStatusPayload) {
    const { wsRef, simulateMessage } = makeWsRef();
    render(<WorkspaceIndexingPanel wsRef={wsRef} initialWorkspace={mockWorkspace} />);
    act(() => {
      simulateMessage({ type: 'WORKSPACE_INDEX_STATUS_RESPONSE', payload });
    });
    return { wsRef, simulateMessage };
  }

  test('a refused embedder switch rolls back the optimistic embedder and counts (D2-15)', () => {
    const { simulateMessage } = renderReady();
    const embedderSelect = screen.getByLabelText(/Embedding Model/i);
    fireEvent.change(embedderSelect, { target: { value: 'qwen3-embedding:4b' } });
    fireEvent.click(screen.getByRole('button', { name: /Confirm & Re-index/i }));

    // Optimistic: the panel claims the new embedder (the metrics
    // line renders the embedder as an exact text node) and zeroes
    // the counts.
    expect(screen.getByText('qwen3-embedding:4b')).toBeInTheDocument();
    expect(screen.queryByText('18')).not.toBeInTheDocument();

    act(() => {
      simulateMessage({
        type: 'WORKSPACE_INDEX_CONFIG_RESPONSE',
        payload: { ok: false, error: 'Embedding model could not be saved: disk full' },
      });
    });

    expect(screen.getByTestId('indexing-action-error')).toHaveTextContent(/could not be saved/);
    // Rolled back to the last engine-confirmed state.
    expect(screen.queryByText('qwen3-embedding:4b')).not.toBeInTheDocument();
    expect(screen.getByText('qwen3-embedding:0.6b')).toBeInTheDocument();
    expect(screen.getByText('18')).toBeInTheDocument();
  });

  test('a start the engine did not start rolls back the optimistic indexing state (D2-15)', () => {
    const { simulateMessage } = renderReady();
    fireEvent.click(screen.getByRole('button', { name: /Re-index Workspace/i }));
    expect(screen.getByText('Indexing')).toBeInTheDocument();

    act(() => {
      simulateMessage({
        type: 'WORKSPACE_INDEX_START_RESPONSE',
        payload: { started: false, estimated_files: 0, estimated_bytes: 0, embedder: 'qwen3-embedding:0.6b', destination: '' },
      });
    });

    expect(screen.getByTestId('indexing-action-error')).toBeInTheDocument();
    expect(screen.getByText('Idle / Up-to-date')).toBeInTheDocument();
  });

  test('an unanswered pause rolls back instead of standing forever (D2-15)', () => {
    renderReady();
    jest.useFakeTimers();
    try {
      fireEvent.click(screen.getByRole('button', { name: /Pause Indexing/i }));
      // Optimistic: the button flips to Resume.
      expect(screen.getByRole('button', { name: /Resume Indexing/i })).toBeInTheDocument();
      // No PAUSE response ever arrives.
      act(() => { jest.advanceTimersByTime(10001); });
      expect(screen.getByTestId('indexing-action-error')).toHaveTextContent(/did not answer/i);
      expect(screen.getByRole('button', { name: /Pause Indexing/i })).toBeInTheDocument();
    } finally {
      jest.useRealTimers();
    }
  });

  test('a late status reply for a previous workspace does not overwrite the selected one (D2-16)', () => {
    const { wsRef, simulateMessage } = makeWsRef();
    render(<WorkspaceIndexingPanel wsRef={wsRef} initialWorkspace="" />);
    act(() => {
      simulateMessage({
        type: 'WORKSPACE_LIST_RESPONSE',
        payload: {
          workspaces: [
            { path: mockWorkspace, name: 'my-project', is_active: true },
            { path: otherWorkspace, name: 'other-project', is_active: false },
          ],
        },
      });
    });

    const statusRequests = () =>
      wsRef.current.send.mock.calls
        .map((c) => JSON.parse(c[0]))
        .filter((m) => m.type === 'WORKSPACE_INDEX_STATUS_REQUEST');
    expect(statusRequests().length).toBeGreaterThanOrEqual(1);
    const reqA = statusRequests()[statusRequests().length - 1];
    expect(reqA.payload.workspace_root).toBe(mockWorkspace);

    // Switch workspaces while A's status reply is still in flight.
    fireEvent.change(screen.getByLabelText(/Workspace Root/i), { target: { value: otherWorkspace } });
    const reqB = statusRequests()[statusRequests().length - 1];
    expect(reqB.payload.workspace_root).toBe(otherWorkspace);
    expect(reqB.request_id).not.toBe(reqA.request_id);

    // A's late reply lands — it must NOT overwrite B's view.
    act(() => {
      simulateMessage({
        type: 'WORKSPACE_INDEX_STATUS_RESPONSE',
        request_id: reqA.request_id,
        payload: { ...mockStatusPayload, embedder: 'embedder-a', indexed_files: 111, total_chunks: 111 },
      });
    });
    expect(screen.queryByText('embedder-a')).not.toBeInTheDocument();

    // B's own reply applies normally. (The name appears twice by
    // design once applied: the metrics line and the embedder
    // select's always-present current-value option.)
    act(() => {
      simulateMessage({
        type: 'WORKSPACE_INDEX_STATUS_RESPONSE',
        request_id: reqB.request_id,
        payload: { ...mockStatusPayload, embedder: 'embedder-b', indexed_files: 7, total_chunks: 7 },
      });
    });
    expect(screen.getAllByText('embedder-b').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText('7').length).toBeGreaterThanOrEqual(1);
  });
});
