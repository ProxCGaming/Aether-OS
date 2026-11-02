import React from 'react';
import { render, screen, fireEvent, waitFor, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import LocalModelsTab from '../LocalModelsTab';

const CATALOG = [
  { name: 'qwen3:8b', size: 4.9, params: '8B', description: 'Fast model' },
];

function makeWsRef() {
  let messageHandler = null;
  const wsRef = {
    current: {
      readyState: 1,
      send: jest.fn(),
      addEventListener: jest.fn((event, handler) => { messageHandler = handler; }),
      removeEventListener: jest.fn(),
    },
  };
  return { wsRef, getHandler: () => messageHandler };
}

beforeEach(() => {
  global.fetch = jest.fn().mockResolvedValue({ json: () => Promise.resolve(CATALOG) });
  localStorage.clear();
});

afterEach(() => {
  jest.restoreAllMocks();
});

describe('LocalModelsTab', () => {
  it('loads the catalog and renders models', async () => {
    const { wsRef } = makeWsRef();
    render(<LocalModelsTab wsRef={wsRef} localModels={[]} />);
    await waitFor(() => expect(screen.getByText('qwen3:8b')).toBeInTheDocument());
  });

  it('sends download request including destination_dir', async () => {
    const { wsRef } = makeWsRef();
    localStorage.setItem('local_model_download_dir', '/models');
    render(<LocalModelsTab wsRef={wsRef} localModels={[]} />);
    await waitFor(() => expect(screen.getByText('qwen3:8b')).toBeInTheDocument());

    // The download button for a non-downloaded model.
    fireEvent.click(screen.getByRole('button', { name: /download/i }));

    const startCalls = wsRef.current.send.mock.calls
      .map(([raw]) => JSON.parse(raw))
      .filter((m) => m.type === 'LOCAL_MODEL_DOWNLOAD_START');
    expect(startCalls).toHaveLength(1);
    expect(startCalls[0].payload.model).toBe('qwen3:8b');
    expect(startCalls[0].payload.destination_dir).toBe('/models');
  });

  it('marks already-downloaded models as downloaded', async () => {
    const { wsRef } = makeWsRef();
    render(
      <LocalModelsTab
        wsRef={wsRef}
        localModels={[{ name: 'qwen3:8b', size: 4.9 * 1024 * 1024 * 1024, path: '/models/qwen3' }]}
      />
    );
    await waitFor(() => expect(screen.getByText('qwen3:8b')).toBeInTheDocument());
    // Downloaded models show a delete control instead of a per-model download control.
    expect(screen.getByTitle('Delete Model')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^download$/i })).not.toBeInTheDocument();
  });
});

// Phase 10.3 C2: the downloaded-model "Active" button used to have no
// handler at all — a dead control. It now sets the model as the engine's
// default (MODEL_SET_DEFAULT, provider 'ollama'), and the card shows a
// disabled "Active" state only for the model that actually is the default.
describe('LocalModelsTab Set Active (Phase 10.3 C2)', () => {
  const downloaded = [{ name: 'qwen3:8b', size: 4.9 * 1024 * 1024 * 1024, path: '/models/qwen3' }];

  it('offers "Set Active" on a downloaded model that is not the default, and sends MODEL_SET_DEFAULT on click', async () => {
    const { wsRef } = makeWsRef();
    render(<LocalModelsTab wsRef={wsRef} localModels={downloaded} />);
    await waitFor(() => expect(screen.getByText('qwen3:8b')).toBeInTheDocument());

    expect(screen.getByRole('button', { name: 'Set Active' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Active' })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Set Active' }));

    const sent = wsRef.current.send.mock.calls.map(([raw]) => JSON.parse(raw));
    const setDefault = sent.filter(m => m.type === 'MODEL_SET_DEFAULT');
    expect(setDefault).toHaveLength(1);
    expect(setDefault[0].payload).toEqual({ provider: 'ollama', model: 'qwen3:8b' });
  });

  it('flips to a disabled "Active" state when the engine broadcasts MODEL_DEFAULT_CHANGED for the model', async () => {
    const { wsRef, getHandler } = makeWsRef();
    render(<LocalModelsTab wsRef={wsRef} localModels={downloaded} />);
    await waitFor(() => expect(screen.getByText('qwen3:8b')).toBeInTheDocument());

    act(() => {
      getHandler()({ data: JSON.stringify({ type: 'MODEL_DEFAULT_CHANGED', payload: { provider: 'ollama', model: 'qwen3:8b' } }) });
    });

    const active = screen.getByRole('button', { name: 'Active' });
    expect(active).toBeDisabled();
    expect(screen.queryByRole('button', { name: 'Set Active' })).not.toBeInTheDocument();
  });

  it('shows the disabled "Active" state from props when the model already is the default', async () => {
    const { wsRef } = makeWsRef();
    render(
      <LocalModelsTab wsRef={wsRef} localModels={downloaded} defaultProvider="ollama" defaultModel="qwen3:8b" />
    );
    await waitFor(() => expect(screen.getByText('qwen3:8b')).toBeInTheDocument());

    const active = screen.getByRole('button', { name: 'Active' });
    expect(active).toBeDisabled();
    expect(screen.queryByRole('button', { name: 'Set Active' })).not.toBeInTheDocument();
    expect(wsRef.current.send).not.toHaveBeenCalled();
  });

  it('does not mark a local model Active when a cloud provider is the default', async () => {
    const { wsRef } = makeWsRef();
    render(
      <LocalModelsTab wsRef={wsRef} localModels={downloaded} defaultProvider="openai" defaultModel="gpt-4" />
    );
    await waitFor(() => expect(screen.getByText('qwen3:8b')).toBeInTheDocument());

    expect(screen.getByRole('button', { name: 'Set Active' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Active' })).not.toBeInTheDocument();
  });
});
