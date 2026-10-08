import React from 'react';
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react';
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
