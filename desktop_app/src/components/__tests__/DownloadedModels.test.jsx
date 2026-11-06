import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import {
  DownloadProgressRow,
  DownloadedModelRow,
  DownloadedModelsSection,
} from '../DownloadedModels';

// Wave 5 (Annex C row 9): the download-manager presentation split
// out of LocalModelsTab. These tests pin the components' prop
// contract directly — the tab's own suite drives the same surfaces
// end-to-end through the hub messages.

const noopPending = () => undefined;

const sectionProps = (overrides = {}) => ({
  downloaded: [],
  inventoryLoad: 'ready',
  activeDefault: { provider: '', model: '' },
  activeEmbedder: '',
  embedderSwitchPending: '',
  setActivePending: '',
  pendingSetActiveFor: noopPending,
  pendingDeleteFor: noopPending,
  pendingEmbedderFor: noopPending,
  onRefresh: jest.fn(),
  onSetActive: jest.fn(),
  onUseAsEmbedder: jest.fn(),
  onDeleteDownloaded: jest.fn(),
  ...overrides,
});

const OLLAMA_MODEL = {
  source: 'ollama', id: 'qwen3:8b', name: 'qwen3:8b', family: 'brain',
  type: 'chat', params: '8B', quant: 'Q4_K_M', size_bytes: 5 * 1024 * 1024 * 1024,
  path: '/models/qwen3-8b',
};

const EMBED_MODEL = {
  source: 'ollama', id: 'nomic-embed-text', name: 'nomic-embed-text',
  family: 'embedding', type: 'embedding', size_bytes: 274 * 1024 * 1024,
};

describe('DownloadedModelsSection — inventory load states (D2-13)', () => {
  it('renders the loading state, never an answered-empty inventory', () => {
    render(<DownloadedModelsSection {...sectionProps({ inventoryLoad: 'loading' })} />);
    expect(screen.getByTestId('downloaded-loading')).toBeInTheDocument();
    expect(screen.queryByText(/No models downloaded yet/)).not.toBeInTheDocument();
  });

  it('renders queued and error states distinctly', () => {
    const { rerender } = render(
      <DownloadedModelsSection {...sectionProps({ inventoryLoad: 'queued' })} />
    );
    expect(screen.getByTestId('downloaded-queued')).toBeInTheDocument();
    rerender(<DownloadedModelsSection {...sectionProps({ inventoryLoad: 'error' })} />);
    expect(screen.getByTestId('downloaded-error')).toBeInTheDocument();
  });

  it('renders the honest empty state only when ready with no models', () => {
    render(<DownloadedModelsSection {...sectionProps()} />);
    expect(screen.getByText(/No models downloaded yet/)).toBeInTheDocument();
  });

  it('fires onRefresh from the section header button', () => {
    const props = sectionProps();
    render(<DownloadedModelsSection {...props} />);
    fireEvent.click(screen.getByTitle('Refresh downloaded models'));
    expect(props.onRefresh).toHaveBeenCalledTimes(1);
  });
});

describe('DownloadedModelsSection — rows derive their state from props', () => {
  it('marks the engine default row Active and passes row handlers through', () => {
    const props = sectionProps({
      downloaded: [OLLAMA_MODEL],
      activeDefault: { provider: 'ollama', model: 'qwen3:8b' },
    });
    render(<DownloadedModelsSection {...props} />);
    expect(screen.getByTestId('downloaded-row-ollama:qwen3:8b')).toBeInTheDocument();
    expect(screen.getByText('Active')).toBeInTheDocument();
    // 5 GiB through the shared formatter (C2): trimmed decimal.
    expect(screen.getByText('5 GB')).toBeInTheDocument();
    fireEvent.click(screen.getByTitle('Delete Model'));
    expect(props.onDeleteDownloaded).toHaveBeenCalledWith(OLLAMA_MODEL);
  });

  it('shows Set Active for an inactive ollama row and fires onSetActive with the id', () => {
    const props = sectionProps({ downloaded: [OLLAMA_MODEL] });
    render(<DownloadedModelsSection {...props} />);
    fireEvent.click(screen.getByText('Set Active'));
    expect(props.onSetActive).toHaveBeenCalledWith('qwen3:8b');
  });

  it('derives the queued markings from the pending* props (RUX-A-19)', () => {
    const props = sectionProps({
      downloaded: [OLLAMA_MODEL],
      pendingDeleteFor: (row) => (row.id === 'qwen3:8b' ? { type: 'MODEL_HUB_DELETE_REQUEST' } : undefined),
    });
    render(<DownloadedModelsSection {...props} />);
    expect(screen.getByTestId('hub-delete-queued-ollama:qwen3:8b')).toBeInTheDocument();
    expect(screen.getByTitle('Delete already queued — will apply when the engine reconnects')).toBeDisabled();
  });
});

describe('DownloadedModelRow — embedder states', () => {
  const rowProps = (overrides = {}) => ({
    row: EMBED_MODEL,
    rowKey: 'ollama:nomic-embed-text',
    onSetActive: jest.fn(),
    onUseAsEmbedder: jest.fn(),
    onDeleteDownloaded: jest.fn(),
    ...overrides,
  });

  it('offers Use as Embedder for an embedding-capable row and fires the handler', () => {
    const props = rowProps();
    render(<DownloadedModelRow {...props} />);
    fireEvent.click(screen.getByTestId('use-as-embedder-ollama:nomic-embed-text'));
    expect(props.onUseAsEmbedder).toHaveBeenCalledWith('nomic-embed-text');
  });

  it('shows the confirmed Embedder state when the row is the active embedder', () => {
    render(<DownloadedModelRow {...rowProps({ activeEmbedder: 'nomic-embed-text' })} />);
    expect(screen.getByTestId('embedder-active-ollama:nomic-embed-text')).toBeInTheDocument();
    expect(screen.queryByTestId('use-as-embedder-ollama:nomic-embed-text')).not.toBeInTheDocument();
  });

  it('omits the size text entirely when the engine reported no size', () => {
    const { container } = render(
      <DownloadedModelRow {...rowProps({ row: { ...EMBED_MODEL, size_bytes: null } })} />
    );
    expect(container.textContent).not.toContain('0 B');
  });
});

describe('DownloadProgressRow — RUX-A-19 cancel marking', () => {
  it('renders percent and status, and cancels through onCancel with the engine key', () => {
    const onCancel = jest.fn();
    render(
      <DownloadProgressRow
        progress={{ key: 'qwen3:8b', percent: 42.4, status: 'pulling' }}
        onCancel={onCancel}
      />
    );
    expect(screen.getByText('42%')).toBeInTheDocument();
    expect(screen.getByText('pulling')).toBeInTheDocument();
    fireEvent.click(screen.getByTitle('Cancel Download'));
    expect(onCancel).toHaveBeenCalledWith('qwen3:8b');
  });

  it('marks a queued cancel and disables the button — the download is still running', () => {
    render(
      <DownloadProgressRow
        progress={{ key: 'qwen3:8b', percent: 10, status: 'pulling' }}
        cancelQueued
        onCancel={jest.fn()}
      />
    );
    expect(screen.getByTestId('cancel-queued-qwen3:8b')).toBeInTheDocument();
    expect(screen.getByTitle('Cancel already queued — will apply when the engine reconnects')).toBeDisabled();
  });
});
