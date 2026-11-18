import React from 'react';
import { render, screen, fireEvent, waitFor, act, within } from '@testing-library/react';
import '@testing-library/jest-dom';
import LocalModelsTab from '../LocalModelsTab';

// Model Hub (H4): the tab is driven entirely by the MODEL_HUB_*
// contracts. Search/detail/downloaded are request/response pairs
// correlated by request_id; download progress and lifecycle arrive
// as the shared task event stream (LOCAL_MODEL_DOWNLOAD_PROGRESS /
// TASK_COMPLETED / TASK_FAILED / TASK_CANCELLED).

const HF_GGUF_ROW = {
  source: 'hf', id: 'bartowski/Qwen3-8B-GGUF', name: 'Qwen3-8B-GGUF',
  family: 'brain', downloads: 12345, licence: 'apache-2.0',
  formats: ['gguf'], runnability: 'ollama',
  runnability_reason: 'GGUF build — runs via Ollama.', size_bytes: null,
};

const HF_STT_ROW = {
  source: 'hf', id: 'Systran/faster-whisper-small', name: 'faster-whisper-small',
  family: 'speech-to-text', downloads: 500000, licence: 'mit',
  formats: ['ctranslate2'], runnability: 'in-app',
  runnability_reason: 'faster-whisper build — runs in the app.',
  size_bytes: null,
};

const HF_BRAIN_ROW = {
  source: 'hf', id: 'meta-llama/Llama-3.1-8B', name: 'Llama-3.1-8B',
  family: 'brain', downloads: 999, licence: null,
  formats: ['safetensors'], runnability: 'not-runnable',
  runnability_reason: 'Safetensors weights need a serving stack Aether does not carry.',
  size_bytes: null,
};

const HF_UNKNOWN_ROW = {
  source: 'hf', id: 'someone/mystery-model', name: 'mystery-model',
  family: 'other', downloads: 3, licence: null,
  formats: [], runnability: 'unknown',
  runnability_reason: 'Aether cannot tell how this model runs.',
  size_bytes: null,
};

const OLLAMA_ROW = {
  source: 'ollama', id: 'qwen3:8b', name: 'qwen3:8b',
  family: 'brain', downloads: null, licence: null,
  formats: ['gguf'], runnability: 'ollama',
  runnability_reason: 'GGUF build — runs via Ollama.',
  size_bytes: 5261334937, params: '8B', quant: 'Q4_K_M',
  description: 'Fast model',
};

const GGUF_DETAIL = {
  ...HF_GGUF_ROW, size_bytes: 13500000000,
  pipeline_tag: 'text-generation', library_name: 'transformers',
  variants: [
    { kind: 'gguf', file: 'Qwen3-8B-Q4_K_M.gguf', size_bytes: 5000000000, quant: 'Q4_K_M' },
    { kind: 'gguf', file: 'Qwen3-8B-Q8_0.gguf', size_bytes: 8500000000, quant: 'Q8_0' },
  ],
};

const BRAIN_DETAIL = {
  ...HF_BRAIN_ROW, size_bytes: 16000000000,
  pipeline_tag: 'text-generation', library_name: 'transformers',
  variants: [
    { kind: 'safetensors', files: ['model-00001-of-00002.safetensors', 'model-00002-of-00002.safetensors'], size_bytes: 16000000000 },
  ],
};

const DL_OLLAMA = {
  source: 'ollama', id: 'qwen3:8b', name: 'qwen3:8b', family: 'brain',
  downloads: null, licence: null, formats: ['gguf'], runnability: 'ollama',
  runnability_reason: 'GGUF build — runs via Ollama.',
  size_bytes: 5261334937, downloaded: true, path: '/models/qwen3',
};

const DL_HF = {
  source: 'hf', id: 'Systran/faster-whisper-small', name: 'faster-whisper-small',
  family: 'speech-to-text', downloads: null, licence: null,
  formats: ['ctranslate2'], runnability: 'in-app',
  runnability_reason: 'faster-whisper build — runs in the app.',
  size_bytes: 483000000, downloaded: true,
  path: '/cache/models--Systran--faster-whisper-small',
};

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

function sentMessages(wsRef, type) {
  return wsRef.current.send.mock.calls
    .map(([raw]) => JSON.parse(raw))
    .filter((m) => !type || m.type === type);
}

function lastSent(wsRef, type) {
  const calls = sentMessages(wsRef, type);
  return calls[calls.length - 1];
}

// Deliver a response to the most recent request of `reqType`,
// echoing its request_id the way the engine's Event envelope does.
function respond(getHandler, wsRef, reqType, resType, payload) {
  const req = lastSent(wsRef, reqType);
  expect(req).toBeTruthy();
  act(() => {
    getHandler()({
      data: JSON.stringify({ type: resType, request_id: req.request_id, payload }),
    });
  });
}

function broadcast(getHandler, type, payload) {
  act(() => {
    getHandler()({ data: JSON.stringify({ type, payload }) });
  });
}

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  jest.restoreAllMocks();
});

async function renderTab(props = {}) {
  const { wsRef, getHandler } = makeWsRef();
  render(<LocalModelsTab wsRef={wsRef} {...props} />);
  await waitFor(() => expect(lastSent(wsRef, 'MODEL_HUB_SEARCH_REQUEST')).toBeTruthy());
  await waitFor(() => expect(lastSent(wsRef, 'MODEL_HUB_DOWNLOADED_REQUEST')).toBeTruthy());
  return { wsRef, getHandler };
}

describe('Model Hub search', () => {
  it('searches Hugging Face on mount with an unfiltered payload', async () => {
    const { wsRef } = await renderTab();
    const req = lastSent(wsRef, 'MODEL_HUB_SEARCH_REQUEST');
    expect(req.payload).toEqual({ source: 'hf', query: '', family: null });
  });

  it('dispatches a fresh search for the Ollama source when switched', async () => {
    const { wsRef, getHandler } = await renderTab();
    respond(getHandler, wsRef, 'MODEL_HUB_SEARCH_REQUEST', 'MODEL_HUB_SEARCH_RESPONSE', { models: [], error: null });

    fireEvent.click(screen.getByRole('button', { name: 'Ollama' }));

    await waitFor(() => {
      expect(lastSent(wsRef, 'MODEL_HUB_SEARCH_REQUEST').payload.source).toBe('ollama');
    });
    respond(getHandler, wsRef, 'MODEL_HUB_SEARCH_REQUEST', 'MODEL_HUB_SEARCH_RESPONSE', { models: [OLLAMA_ROW], error: null });
    await waitFor(() => expect(screen.getByTestId('result-row-ollama:qwen3:8b')).toBeInTheDocument());
  });

  it('sends the typed query with the search', async () => {
    const { wsRef } = await renderTab();
    fireEvent.change(screen.getByPlaceholderText(/search models/i), { target: { value: 'whisper' } });
    await waitFor(() => {
      expect(lastSent(wsRef, 'MODEL_HUB_SEARCH_REQUEST').payload.query).toBe('whisper');
    });
  });

  it('family chips filter the search and All clears the filter', async () => {
    const { wsRef } = await renderTab();
    fireEvent.click(screen.getByRole('button', { name: 'Speech-to-text' }));
    await waitFor(() => {
      expect(lastSent(wsRef, 'MODEL_HUB_SEARCH_REQUEST').payload.family).toBe('speech-to-text');
    });
    fireEvent.click(screen.getByRole('button', { name: 'All' }));
    await waitFor(() => {
      expect(lastSent(wsRef, 'MODEL_HUB_SEARCH_REQUEST').payload.family).toBeNull();
    });
  });

  it('says so plainly when a search returns nothing', async () => {
    const { wsRef, getHandler } = await renderTab();
    respond(getHandler, wsRef, 'MODEL_HUB_SEARCH_REQUEST', 'MODEL_HUB_SEARCH_RESPONSE', { models: [], error: null });
    await waitFor(() => expect(screen.getByText(/no models found/i)).toBeInTheDocument());
  });

  it('shows an HF error in the results area while the Ollama source stays usable', async () => {
    const { wsRef, getHandler } = await renderTab();
    respond(getHandler, wsRef, 'MODEL_HUB_SEARCH_REQUEST', 'MODEL_HUB_SEARCH_RESPONSE', {
      models: [],
      error: 'Hugging Face catalogue is unreachable — check the connection and retry.',
    });
    await waitFor(() => expect(
      screen.getByText('Hugging Face catalogue is unreachable — check the connection and retry.')
    ).toBeInTheDocument());

    fireEvent.click(screen.getByRole('button', { name: 'Ollama' }));
    await waitFor(() => {
      expect(lastSent(wsRef, 'MODEL_HUB_SEARCH_REQUEST').payload.source).toBe('ollama');
    });
    respond(getHandler, wsRef, 'MODEL_HUB_SEARCH_REQUEST', 'MODEL_HUB_SEARCH_RESPONSE', { models: [OLLAMA_ROW], error: null });
    await waitFor(() => expect(screen.getByTestId('result-row-ollama:qwen3:8b')).toBeInTheDocument());
  });
});

describe('Model Hub result rows', () => {
  async function renderWithRows(rows) {
    const ctx = await renderTab();
    respond(ctx.getHandler, ctx.wsRef, 'MODEL_HUB_SEARCH_REQUEST', 'MODEL_HUB_SEARCH_RESPONSE', { models: rows, error: null });
    return ctx;
  }

  it('renders family, downloads, licence and the in-app runnability badge', async () => {
    await renderWithRows([HF_STT_ROW]);
    const card = await screen.findByTestId('result-row-hf:Systran/faster-whisper-small');
    expect(within(card).getByText('faster-whisper-small')).toBeInTheDocument();
    expect(within(card).getByText('Speech-to-text')).toBeInTheDocument();
    expect(within(card).getByText(/500\.0k downloads/i)).toBeInTheDocument();
    expect(within(card).getByText(/mit/)).toBeInTheDocument();
    expect(within(card).getByText('In-app')).toBeInTheDocument();
  });

  it('renders the via-Ollama badge for GGUF rows', async () => {
    await renderWithRows([HF_GGUF_ROW]);
    const card = await screen.findByTestId('result-row-hf:bartowski/Qwen3-8B-GGUF');
    expect(within(card).getByText('Via Ollama')).toBeInTheDocument();
    expect(within(card).getByText(/apache-2\.0/)).toBeInTheDocument();
  });

  it('shows "Licence unknown" when the licence is null — never an invented one', async () => {
    await renderWithRows([HF_BRAIN_ROW]);
    const card = await screen.findByTestId('result-row-hf:meta-llama/Llama-3.1-8B');
    expect(within(card).getByText('Licence unknown')).toBeInTheDocument();
  });

  it('not-runnable rows show the reason and offer no download', async () => {
    await renderWithRows([HF_BRAIN_ROW]);
    const card = await screen.findByTestId('result-row-hf:meta-llama/Llama-3.1-8B');
    expect(within(card).getByText('Not locally runnable')).toBeInTheDocument();
    expect(within(card).getByText(HF_BRAIN_ROW.runnability_reason)).toBeInTheDocument();
    expect(within(card).queryByRole('button', { name: /download/i })).not.toBeInTheDocument();
  });

  it('unknown-runnability rows offer no download either', async () => {
    await renderWithRows([HF_UNKNOWN_ROW]);
    const card = await screen.findByTestId('result-row-hf:someone/mystery-model');
    expect(within(card).queryByRole('button', { name: /download/i })).not.toBeInTheDocument();
  });

  it('marks results that are already downloaded and hides their download button', async () => {
    const { wsRef, getHandler } = await renderTab();
    respond(getHandler, wsRef, 'MODEL_HUB_DOWNLOADED_REQUEST', 'MODEL_HUB_DOWNLOADED_RESPONSE', { models: [DL_OLLAMA], error: null });
    fireEvent.click(screen.getByRole('button', { name: 'Ollama' }));
    await waitFor(() => {
      expect(lastSent(wsRef, 'MODEL_HUB_SEARCH_REQUEST').payload.source).toBe('ollama');
    });
    respond(getHandler, wsRef, 'MODEL_HUB_SEARCH_REQUEST', 'MODEL_HUB_SEARCH_RESPONSE', { models: [OLLAMA_ROW], error: null });
    const card = await screen.findByTestId('result-row-ollama:qwen3:8b');
    expect(within(card).getByText(/downloaded/i)).toBeInTheDocument();
    expect(within(card).queryByRole('button', { name: /^download$/i })).not.toBeInTheDocument();
  });
});

describe('Model Hub detail and downloads', () => {
  async function renderWithRows(rows) {
    const ctx = await renderTab();
    respond(ctx.getHandler, ctx.wsRef, 'MODEL_HUB_SEARCH_REQUEST', 'MODEL_HUB_SEARCH_RESPONSE', { models: rows, error: null });
    return ctx;
  }

  it('expanding a GGUF row loads detail and lists each quant variant with its size', async () => {
    const { wsRef, getHandler } = await renderWithRows([HF_GGUF_ROW]);
    const card = await screen.findByTestId('result-row-hf:bartowski/Qwen3-8B-GGUF');

    fireEvent.click(within(card).getByRole('button', { name: /details/i }));

    await waitFor(() => expect(lastSent(wsRef, 'MODEL_HUB_DETAIL_REQUEST')).toBeTruthy());
    expect(lastSent(wsRef, 'MODEL_HUB_DETAIL_REQUEST').payload).toEqual({ source: 'hf', id: 'bartowski/Qwen3-8B-GGUF' });

    respond(getHandler, wsRef, 'MODEL_HUB_DETAIL_REQUEST', 'MODEL_HUB_DETAIL_RESPONSE', { detail: GGUF_DETAIL, error: null });

    await waitFor(() => expect(within(card).getByText('Q4_K_M')).toBeInTheDocument());
    expect(within(card).getByText('Q8_0')).toBeInTheDocument();
    expect(within(card).getByText(/4\.7 GB/)).toBeInTheDocument();
    expect(within(card).getByText(/7\.9 GB/)).toBeInTheDocument();
  });

  it('a GGUF variant download sends the quant as the variant string', async () => {
    const { wsRef, getHandler } = await renderWithRows([HF_GGUF_ROW]);
    const card = await screen.findByTestId('result-row-hf:bartowski/Qwen3-8B-GGUF');
    fireEvent.click(within(card).getByRole('button', { name: /details/i }));
    respond(getHandler, wsRef, 'MODEL_HUB_DETAIL_REQUEST', 'MODEL_HUB_DETAIL_RESPONSE', { detail: GGUF_DETAIL, error: null });
    await waitFor(() => expect(within(card).getByText('Q4_K_M')).toBeInTheDocument());

    const variantRow = within(card).getByTestId('variant-Q4_K_M');
    fireEvent.click(within(variantRow).getByRole('button', { name: /download/i }));

    await waitFor(() => expect(lastSent(wsRef, 'MODEL_HUB_DOWNLOAD_REQUEST')).toBeTruthy());
    expect(lastSent(wsRef, 'MODEL_HUB_DOWNLOAD_REQUEST').payload).toEqual({
      source: 'hf', id: 'bartowski/Qwen3-8B-GGUF', variant: 'Q4_K_M',
    });
  });

  it('an in-app speech row downloads the whole repo with no variant', async () => {
    const { wsRef } = await renderWithRows([HF_STT_ROW]);
    const card = await screen.findByTestId('result-row-hf:Systran/faster-whisper-small');
    fireEvent.click(within(card).getByRole('button', { name: /download/i }));
    await waitFor(() => expect(lastSent(wsRef, 'MODEL_HUB_DOWNLOAD_REQUEST')).toBeTruthy());
    const payload = lastSent(wsRef, 'MODEL_HUB_DOWNLOAD_REQUEST').payload;
    expect(payload.source).toBe('hf');
    expect(payload.id).toBe('Systran/faster-whisper-small');
    expect(payload.variant).toBeUndefined();
  });

  it('an Ollama row downloads by model name from the ollama source', async () => {
    const { wsRef, getHandler } = await renderTab();
    fireEvent.click(screen.getByRole('button', { name: 'Ollama' }));
    await waitFor(() => {
      expect(lastSent(wsRef, 'MODEL_HUB_SEARCH_REQUEST').payload.source).toBe('ollama');
    });
    respond(getHandler, wsRef, 'MODEL_HUB_SEARCH_REQUEST', 'MODEL_HUB_SEARCH_RESPONSE', { models: [OLLAMA_ROW], error: null });
    const card = await screen.findByTestId('result-row-ollama:qwen3:8b');
    fireEvent.click(within(card).getByRole('button', { name: /download/i }));
    await waitFor(() => expect(lastSent(wsRef, 'MODEL_HUB_DOWNLOAD_REQUEST')).toBeTruthy());
    expect(lastSent(wsRef, 'MODEL_HUB_DOWNLOAD_REQUEST').payload).toEqual({ source: 'ollama', id: 'qwen3:8b' });
  });

  it('detail of a not-runnable repo lists variants but still offers no download', async () => {
    const { wsRef, getHandler } = await renderWithRows([HF_BRAIN_ROW]);
    const card = await screen.findByTestId('result-row-hf:meta-llama/Llama-3.1-8B');
    fireEvent.click(within(card).getByRole('button', { name: /details/i }));
    respond(getHandler, wsRef, 'MODEL_HUB_DETAIL_REQUEST', 'MODEL_HUB_DETAIL_RESPONSE', { detail: BRAIN_DETAIL, error: null });
    // The safetensors variant renders as one grouped entry (2 files).
    await waitFor(() => expect(within(card).getByText('2 files')).toBeInTheDocument());
    expect(within(card).queryByRole('button', { name: /download/i })).not.toBeInTheDocument();
  });

  it('a refused download surfaces the engine error text inline', async () => {
    const { wsRef, getHandler } = await renderWithRows([HF_STT_ROW]);
    const card = await screen.findByTestId('result-row-hf:Systran/faster-whisper-small');
    fireEvent.click(within(card).getByRole('button', { name: /download/i }));
    respond(getHandler, wsRef, 'MODEL_HUB_DOWNLOAD_REQUEST', 'MODEL_HUB_DOWNLOAD_RESPONSE', {
      success: false,
      error: 'Voice components are not installed — the speech download needs them (huggingface_hub ships with the voice extra).',
    });
    await waitFor(() => expect(
      screen.getByText(/Voice components are not installed/)
    ).toBeInTheDocument());
  });

  it('progress renders on the row and cancel targets the engine download key', async () => {
    const { wsRef, getHandler } = await renderWithRows([HF_GGUF_ROW]);
    const card = await screen.findByTestId('result-row-hf:bartowski/Qwen3-8B-GGUF');
    fireEvent.click(within(card).getByRole('button', { name: /details/i }));
    respond(getHandler, wsRef, 'MODEL_HUB_DETAIL_REQUEST', 'MODEL_HUB_DETAIL_RESPONSE', { detail: GGUF_DETAIL, error: null });
    await waitFor(() => expect(within(card).getByText('Q4_K_M')).toBeInTheDocument());
    const variantRow = within(card).getByTestId('variant-Q4_K_M');
    fireEvent.click(within(variantRow).getByRole('button', { name: /download/i }));

    // The engine keys this download by its Ollama ref, not the repo id.
    respond(getHandler, wsRef, 'MODEL_HUB_DOWNLOAD_REQUEST', 'MODEL_HUB_DOWNLOAD_RESPONSE', {
      success: true, model: 'hf.co/bartowski/Qwen3-8B-GGUF:Q4_K_M', task_id: 'hub-dl-1',
    });
    broadcast(getHandler, 'LOCAL_MODEL_DOWNLOAD_PROGRESS', {
      task_id: 'hub-dl-1', model: 'hf.co/bartowski/Qwen3-8B-GGUF:Q4_K_M',
      download_percent: 42, status: 'downloading',
    });

    await waitFor(() => expect(within(card).getByText(/42%/)).toBeInTheDocument());

    fireEvent.click(within(card).getByTitle('Cancel Download'));
    const cancel = lastSent(wsRef, 'LOCAL_MODEL_DOWNLOAD_CANCEL');
    expect(cancel.payload.model).toBe('hf.co/bartowski/Qwen3-8B-GGUF:Q4_K_M');
  });

  it('a failed download clears progress and shows the failure inline', async () => {
    const { wsRef, getHandler } = await renderWithRows([HF_STT_ROW]);
    const card = await screen.findByTestId('result-row-hf:Systran/faster-whisper-small');
    fireEvent.click(within(card).getByRole('button', { name: /download/i }));
    respond(getHandler, wsRef, 'MODEL_HUB_DOWNLOAD_REQUEST', 'MODEL_HUB_DOWNLOAD_RESPONSE', {
      success: true, model: 'Systran/faster-whisper-small', task_id: 'hub-dl-2',
    });
    broadcast(getHandler, 'LOCAL_MODEL_DOWNLOAD_PROGRESS', {
      task_id: 'hub-dl-2', model: 'Systran/faster-whisper-small',
      download_percent: 10, status: '1/4 files',
    });
    await waitFor(() => expect(within(card).getByText(/10%/)).toBeInTheDocument());

    broadcast(getHandler, 'TASK_FAILED', {
      task_id: 'hub-dl-2', model: 'Systran/faster-whisper-small',
      error: 'Downloading Systran/faster-whisper-small failed: boom',
    });
    await waitFor(() => expect(
      screen.getByText('Downloading Systran/faster-whisper-small failed: boom')
    ).toBeInTheDocument());
    expect(within(card).queryByText(/10%/)).not.toBeInTheDocument();
  });
});

describe('Model Hub downloaded inventory', () => {
  async function renderWithDownloaded(models, props = {}) {
    const ctx = await renderTab(props);
    respond(ctx.getHandler, ctx.wsRef, 'MODEL_HUB_DOWNLOADED_REQUEST', 'MODEL_HUB_DOWNLOADED_RESPONSE', { models, error: null });
    return ctx;
  }

  it('renders the merged inventory with real sizes from both stores', async () => {
    await renderWithDownloaded([DL_OLLAMA, DL_HF]);
    const section = await screen.findByTestId('downloaded-section');
    expect(within(section).getByText('qwen3:8b')).toBeInTheDocument();
    expect(within(section).getByText('faster-whisper-small')).toBeInTheDocument();
    expect(within(section).getByText(/4\.9 GB/)).toBeInTheDocument();
    expect(within(section).getByText(/460\.6 MB/)).toBeInTheDocument();
    expect(within(section).getByText('Ollama')).toBeInTheDocument();
    expect(within(section).getByText('Hugging Face')).toBeInTheDocument();
  });

  it('delete dispatches MODEL_HUB_DELETE_REQUEST and refreshes on success', async () => {
    const { wsRef, getHandler } = await renderWithDownloaded([DL_OLLAMA, DL_HF]);
    const row = await screen.findByTestId('downloaded-row-hf:Systran/faster-whisper-small');
    const before = sentMessages(wsRef, 'MODEL_HUB_DOWNLOADED_REQUEST').length;

    fireEvent.click(within(row).getByTitle('Delete Model'));
    await waitFor(() => expect(lastSent(wsRef, 'MODEL_HUB_DELETE_REQUEST')).toBeTruthy());
    expect(lastSent(wsRef, 'MODEL_HUB_DELETE_REQUEST').payload).toEqual({
      source: 'hf', id: 'Systran/faster-whisper-small',
    });

    respond(getHandler, wsRef, 'MODEL_HUB_DELETE_REQUEST', 'MODEL_HUB_DELETE_RESPONSE', {
      success: true, deleted: true, error: null,
    });
    await waitFor(() => {
      expect(sentMessages(wsRef, 'MODEL_HUB_DOWNLOADED_REQUEST').length).toBeGreaterThan(before);
    });
  });

  it('a failed delete shows the engine error and keeps the row', async () => {
    const { wsRef, getHandler } = await renderWithDownloaded([DL_OLLAMA]);
    const row = await screen.findByTestId('downloaded-row-ollama:qwen3:8b');
    fireEvent.click(within(row).getByTitle('Delete Model'));
    respond(getHandler, wsRef, 'MODEL_HUB_DELETE_REQUEST', 'MODEL_HUB_DELETE_RESPONSE', {
      success: false, deleted: false,
      error: 'Ollama is not running — hub deletes need the daemon to verify and remove its models.',
    });
    await waitFor(() => expect(
      screen.getByText(/Ollama is not running/)
    ).toBeInTheDocument());
    expect(screen.getByTestId('downloaded-row-ollama:qwen3:8b')).toBeInTheDocument();
  });

  it('refreshes the inventory when a download completes', async () => {
    const { wsRef, getHandler } = await renderWithDownloaded([DL_OLLAMA]);
    const before = sentMessages(wsRef, 'MODEL_HUB_DOWNLOADED_REQUEST').length;
    broadcast(getHandler, 'TASK_COMPLETED', { task_id: 'hub-dl-9', model: 'qwen3:8b' });
    await waitFor(() => {
      expect(sentMessages(wsRef, 'MODEL_HUB_DOWNLOADED_REQUEST').length).toBeGreaterThan(before);
    });
  });

  it('says plainly when nothing is downloaded yet', async () => {
    await renderWithDownloaded([]);
    const section = await screen.findByTestId('downloaded-section');
    expect(within(section).getByText(/no models downloaded yet/i)).toBeInTheDocument();
  });
});

// Preserved behaviour (Phase 10.3 C2): a downloaded Ollama model can
// be made the engine default from this tab; the engine's
// MODEL_DEFAULT_CHANGED broadcast flips the control to Active.
describe('Model Hub Set Active (preserved from the Ollama tab)', () => {
  async function renderDownloadedOllama(props = {}) {
    const ctx = await renderTab(props);
    respond(ctx.getHandler, ctx.wsRef, 'MODEL_HUB_DOWNLOADED_REQUEST', 'MODEL_HUB_DOWNLOADED_RESPONSE', { models: [DL_OLLAMA], error: null });
    return ctx;
  }

  it('offers "Set Active" on a downloaded Ollama model and sends MODEL_SET_DEFAULT', async () => {
    const { wsRef } = await renderDownloadedOllama();
    const row = await screen.findByTestId('downloaded-row-ollama:qwen3:8b');
    fireEvent.click(within(row).getByRole('button', { name: 'Set Active' }));
    const setDefault = sentMessages(wsRef, 'MODEL_SET_DEFAULT');
    expect(setDefault).toHaveLength(1);
    expect(setDefault[0].payload).toEqual({ provider: 'ollama', model: 'qwen3:8b' });
  });

  it('flips to a disabled "Active" state on MODEL_DEFAULT_CHANGED', async () => {
    const { getHandler } = await renderDownloadedOllama();
    const row = await screen.findByTestId('downloaded-row-ollama:qwen3:8b');
    broadcast(getHandler, 'MODEL_DEFAULT_CHANGED', { provider: 'ollama', model: 'qwen3:8b' });
    const active = within(row).getByRole('button', { name: 'Active' });
    expect(active).toBeDisabled();
    expect(within(row).queryByRole('button', { name: 'Set Active' })).not.toBeInTheDocument();
  });

  it('shows the disabled "Active" state from props when the model already is the default', async () => {
    await renderDownloadedOllama({ defaultProvider: 'ollama', defaultModel: 'qwen3:8b' });
    const row = await screen.findByTestId('downloaded-row-ollama:qwen3:8b');
    expect(within(row).getByRole('button', { name: 'Active' })).toBeDisabled();
  });

  it('does not offer Set Active for HF speech inventory rows', async () => {
    const ctx = await renderTab();
    respond(ctx.getHandler, ctx.wsRef, 'MODEL_HUB_DOWNLOADED_REQUEST', 'MODEL_HUB_DOWNLOADED_RESPONSE', { models: [DL_HF], error: null });
    const row = await screen.findByTestId('downloaded-row-hf:Systran/faster-whisper-small');
    expect(within(row).queryByRole('button', { name: 'Set Active' })).not.toBeInTheDocument();
  });
});

const DL_EMBEDDER = {
  source: 'ollama', id: 'qwen3-embedding:0.6b', name: 'qwen3-embedding:0.6b', family: 'embedding',
  downloads: null, licence: null, formats: ['gguf'], runnability: 'ollama',
  runnability_reason: 'GGUF build — runs via Ollama.',
  size_bytes: 639000000, downloaded: true, path: '/models/qwen3-embedding',
  type: 'embedding', params: '0.6B', quant: 'Q8_0',
};

const DL_CHAT_TYPED = {
  ...DL_OLLAMA, type: 'chat', params: '8.0B', quant: 'Q4_0',
};

describe('Downloaded model typing + embedder (catalogue sync)', () => {
  it('shows the daemon type badge and params/quant chips on downloaded rows', async () => {
    const { wsRef, getHandler } = await renderTab();
    respond(getHandler, wsRef, 'MODEL_HUB_DOWNLOADED_REQUEST', 'MODEL_HUB_DOWNLOADED_RESPONSE', { models: [DL_EMBEDDER, DL_CHAT_TYPED], error: null });

    const embRow = await screen.findByTestId('downloaded-row-ollama:qwen3-embedding:0.6b');
    expect(within(embRow).getByTestId('downloaded-type-ollama:qwen3-embedding:0.6b')).toHaveTextContent('Embedding');
    expect(within(embRow).getByText('0.6B Params')).toBeInTheDocument();
    expect(within(embRow).getByText('Q8_0')).toBeInTheDocument();

    const chatRow = await screen.findByTestId('downloaded-row-ollama:qwen3:8b');
    expect(within(chatRow).getByTestId('downloaded-type-ollama:qwen3:8b')).toHaveTextContent('Chat');
    expect(within(chatRow).getByText('8.0B Params')).toBeInTheDocument();
  });

  it('offers Use as Embedder only for embedding-capable models', async () => {
    const { wsRef, getHandler } = await renderTab();
    respond(getHandler, wsRef, 'MODEL_HUB_DOWNLOADED_REQUEST', 'MODEL_HUB_DOWNLOADED_RESPONSE', { models: [DL_EMBEDDER, DL_CHAT_TYPED], error: null });

    const embRow = await screen.findByTestId('downloaded-row-ollama:qwen3-embedding:0.6b');
    expect(within(embRow).getByTestId('use-as-embedder-ollama:qwen3-embedding:0.6b')).toBeInTheDocument();
    const chatRow = await screen.findByTestId('downloaded-row-ollama:qwen3:8b');
    expect(within(chatRow).queryByText(/Use as Embedder/)).not.toBeInTheDocument();
  });

  it('Use as Embedder sends the embedder config request and marks the row only on the engine response', async () => {
    const { wsRef, getHandler } = await renderTab();
    respond(getHandler, wsRef, 'MODEL_HUB_DOWNLOADED_REQUEST', 'MODEL_HUB_DOWNLOADED_RESPONSE', { models: [DL_EMBEDDER], error: null });

    const row = await screen.findByTestId('downloaded-row-ollama:qwen3-embedding:0.6b');
    fireEvent.click(within(row).getByTestId('use-as-embedder-ollama:qwen3-embedding:0.6b'));

    const sent = lastSent(wsRef, 'WORKSPACE_INDEX_CONFIG_REQUEST');
    expect(sent).toBeTruthy();
    expect(sent.payload).toEqual({ embedder: 'qwen3-embedding:0.6b' });
    // Not claimed yet: the row shows the pending state, not "Embedder".
    expect(within(row).getByTestId('embedder-pending-ollama:qwen3-embedding:0.6b')).toBeInTheDocument();
    expect(within(row).queryByTestId('embedder-active-ollama:qwen3-embedding:0.6b')).not.toBeInTheDocument();

    broadcast(getHandler, 'WORKSPACE_INDEX_CONFIG_RESPONSE', { ok: true, reindex_required: true });
    await waitFor(() => {
      expect(within(row).getByTestId('embedder-active-ollama:qwen3-embedding:0.6b')).toBeInTheDocument();
    });
  });

  it('marks the configured embedder row from the currentEmbedder prop', async () => {
    const { wsRef, getHandler } = await renderTab({ currentEmbedder: 'qwen3-embedding:0.6b' });
    respond(getHandler, wsRef, 'MODEL_HUB_DOWNLOADED_REQUEST', 'MODEL_HUB_DOWNLOADED_RESPONSE', { models: [DL_EMBEDDER], error: null });

    const row = await screen.findByTestId('downloaded-row-ollama:qwen3-embedding:0.6b');
    expect(within(row).getByTestId('embedder-active-ollama:qwen3-embedding:0.6b')).toBeInTheDocument();
    expect(within(row).queryByTestId('use-as-embedder-ollama:qwen3-embedding:0.6b')).not.toBeInTheDocument();
  });
});

// D2-13: the downloaded inventory initialised to [] with no load
// state, so "No models downloaded yet" rendered from the first
// frame — and for the whole time a refresh sat queued offline —
// impersonating an answered-empty inventory.
describe('Model Hub downloaded inventory load state (D2-13)', () => {
  it('shows a loading state — never the empty text — before the first inventory answer', async () => {
    await renderTab();
    const section = screen.getByTestId('downloaded-section');
    expect(within(section).getByTestId('downloaded-loading')).toBeInTheDocument();
    expect(within(section).queryByText(/no models downloaded yet/i)).not.toBeInTheDocument();
  });

  it('reads as queued, not empty, while the inventory request sits in the offline queue', async () => {
    const { wsRef } = makeWsRef();
    const safeSend = jest.fn(() => 'queued');
    render(<LocalModelsTab wsRef={wsRef} safeSend={safeSend} />);
    const section = screen.getByTestId('downloaded-section');
    expect(within(section).getByTestId('downloaded-queued')).toBeInTheDocument();
    expect(within(section).queryByText(/no models downloaded yet/i)).not.toBeInTheDocument();
  });

  it('an error answer for the inventory is an error state, not an empty list', async () => {
    const { wsRef, getHandler } = await renderTab();
    respond(getHandler, wsRef, 'MODEL_HUB_DOWNLOADED_REQUEST', 'MODEL_HUB_DOWNLOADED_RESPONSE', {
      error: 'hub store unavailable',
    });
    const section = screen.getByTestId('downloaded-section');
    expect(within(section).getByTestId('downloaded-error')).toBeInTheDocument();
    expect(within(section).queryByText(/no models downloaded yet/i)).not.toBeInTheDocument();
    expect(screen.getByText(/hub store unavailable/)).toBeInTheDocument();
  });
});

// D2-14: "Set Active" fired MODEL_SET_DEFAULT and listened only
// for the success broadcast — a refusal or a lost answer left the
// row unchanged with nothing telling the user why.
describe('Model Hub Set Active confirmation (D2-14)', () => {
  async function renderDownloadedOllama() {
    const ctx = await renderTab();
    respond(ctx.getHandler, ctx.wsRef, 'MODEL_HUB_DOWNLOADED_REQUEST', 'MODEL_HUB_DOWNLOADED_RESPONSE', { models: [DL_OLLAMA], error: null });
    return ctx;
  }

  it('shows a pending state after the click until the engine confirms', async () => {
    const { wsRef, getHandler } = await renderDownloadedOllama();
    const row = await screen.findByTestId('downloaded-row-ollama:qwen3:8b');
    fireEvent.click(within(row).getByRole('button', { name: 'Set Active' }));
    expect(within(row).getByTestId('set-active-pending-ollama:qwen3:8b')).toBeInTheDocument();
    expect(within(row).queryByRole('button', { name: 'Set Active' })).not.toBeInTheDocument();

    respond(getHandler, wsRef, 'MODEL_SET_DEFAULT', 'MODEL_DEFAULT_CHANGED', { provider: 'ollama', model: 'qwen3:8b' });
    await waitFor(() => expect(within(row).getByRole('button', { name: 'Active' })).toBeDisabled());
  });

  it('an engine ERROR refusal is surfaced and the row offers Set Active again', async () => {
    const { wsRef, getHandler } = await renderDownloadedOllama();
    const row = await screen.findByTestId('downloaded-row-ollama:qwen3:8b');
    fireEvent.click(within(row).getByRole('button', { name: 'Set Active' }));
    respond(getHandler, wsRef, 'MODEL_SET_DEFAULT', 'ERROR', {
      error: 'Default model could not be saved: disk full',
    });
    await waitFor(() => expect(screen.getByText(/Default model could not be saved/)).toBeInTheDocument());
    expect(within(row).getByRole('button', { name: 'Set Active' })).toBeInTheDocument();
    expect(within(row).queryByTestId('set-active-pending-ollama:qwen3:8b')).not.toBeInTheDocument();
  });

  it('no answer inside the confirm window surfaces an error instead of silence', async () => {
    await renderDownloadedOllama();
    const row = await screen.findByTestId('downloaded-row-ollama:qwen3:8b');
    jest.useFakeTimers();
    try {
      fireEvent.click(within(row).getByRole('button', { name: 'Set Active' }));
      expect(within(row).getByTestId('set-active-pending-ollama:qwen3:8b')).toBeInTheDocument();
      act(() => { jest.advanceTimersByTime(10001); });
      expect(screen.getByText(/did not confirm/i)).toBeInTheDocument();
      expect(within(row).getByRole('button', { name: 'Set Active' })).toBeInTheDocument();
    } finally {
      jest.useRealTimers();
    }
  });
});

describe('Spec D — fit badges, blocked downloads, models location', () => {
  const TOO_LARGE_ROW = {
    ...OLLAMA_ROW, id: 'huge:70b', name: 'huge:70b', size_bytes: 40 * 1024 * 1024 * 1024,
    fit: { tier: 'too_large', reason: "Exceeds this machine's ≈16 GB RAM budget." },
  };

  it('a too-large row shows the badge and the reason, and offers no download', async () => {
    const ctx = await renderTab();
    respond(ctx.getHandler, ctx.wsRef, 'MODEL_HUB_SEARCH_REQUEST', 'MODEL_HUB_SEARCH_RESPONSE', { models: [TOO_LARGE_ROW], error: null });
    const card = await screen.findByTestId('result-row-ollama:huge:70b');
    expect(within(card).getByTestId('result-fit-ollama:huge:70b')).toHaveTextContent('Too large');
    expect(within(card).getByTestId('result-fit-reason-ollama:huge:70b')).toHaveTextContent('≈16 GB RAM budget');
    expect(within(card).queryByRole('button', { name: /^download$/i })).not.toBeInTheDocument();
    expect(within(card).getByText(/download unavailable/i)).toBeInTheDocument();
  });

  it('a comfortable row keeps its download button and shows the badge', async () => {
    const fitted = { ...OLLAMA_ROW, fit: { tier: 'comfortable', reason: 'Fits comfortably.' } };
    const ctx = await renderTab();
    respond(ctx.getHandler, ctx.wsRef, 'MODEL_HUB_SEARCH_REQUEST', 'MODEL_HUB_SEARCH_RESPONSE', { models: [fitted], error: null });
    const card = await screen.findByTestId('result-row-ollama:qwen3:8b');
    expect(within(card).getByTestId('result-fit-ollama:qwen3:8b')).toHaveTextContent('Comfortable');
    expect(within(card).getByRole('button', { name: /^download$/i })).toBeInTheDocument();
  });

  it('a too-large variant is blocked while a fitting variant stays downloadable', async () => {
    const detail = {
      ...GGUF_DETAIL,
      variants: [
        { kind: 'gguf', file: 'Qwen3-8B-Q4_K_M.gguf', size_bytes: 5000000000, quant: 'Q4_K_M', fit: { tier: 'comfortable', reason: 'Fits.' } },
        { kind: 'gguf', file: 'Qwen3-8B-Q8_0.gguf', size_bytes: 40000000000, quant: 'Q8_0', fit: { tier: 'too_large', reason: 'Too big.' } },
      ],
    };
    const ctx = await renderTab();
    respond(ctx.getHandler, ctx.wsRef, 'MODEL_HUB_SEARCH_REQUEST', 'MODEL_HUB_SEARCH_RESPONSE', { models: [HF_GGUF_ROW], error: null });
    const card = await screen.findByTestId('result-row-hf:bartowski/Qwen3-8B-GGUF');
    fireEvent.click(within(card).getByRole('button', { name: /details/i }));
    respond(ctx.getHandler, ctx.wsRef, 'MODEL_HUB_DETAIL_REQUEST', 'MODEL_HUB_DETAIL_RESPONSE', { detail, error: null });
    const small = await within(card).findByTestId('variant-Q4_K_M');
    expect(within(small).getByRole('button', { name: /download/i })).toBeInTheDocument();
    const big = within(card).getByTestId('variant-Q8_0');
    expect(within(big).queryByRole('button', { name: /download/i })).not.toBeInTheDocument();
    expect(within(big).getByText('Too large for this machine')).toBeInTheDocument();
  });

  it('the Downloaded header shows the models location the payload names', async () => {
    const ctx = await renderTab();
    respond(ctx.getHandler, ctx.wsRef, 'MODEL_HUB_DOWNLOADED_REQUEST', 'MODEL_HUB_DOWNLOADED_RESPONSE', {
      models: [DL_OLLAMA], error: null, models_location: '/home/user/.ollama/models',
    });
    const section = await screen.findByTestId('downloaded-section');
    expect(within(section).getByTestId('downloaded-models-location')).toHaveTextContent('/home/user/.ollama/models');
    expect(within(section).getByTestId('downloaded-models-location')).toHaveTextContent('1 model');
  });
});
