/** Master build S21 (R17 wiring): the engine's Ollama list
 * section — LOCAL_MODEL_LIST_REQUEST's inventory with the
 * LOCAL_MODEL_DELETE_REQUEST sender that never existed. Pins:
 * the list is requested on mount and only downloaded models get
 * a delete; a delete confirms first, sends the model name, and
 * the list refreshes only on the engine's deleted:true answer;
 * deleted:false surfaces an honest not-deleted message (the
 * engine answers exactly that when Ollama is offline); a queued
 * delete renders from the real queue, not a local flag. */
import React from 'react';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import OllamaModelList from '../OllamaModelList';

const MODELS = [
  { name: 'qwen3:8b', size: '~4.9GB', downloaded: true, path: '~/.ollama/models/manifests/qwen3:8b', type: 'brain', family: 'qwen3', parameter_size: '8B', quantization: 'Q4_K_M' },
  { name: 'llama3.1:8b', size: '~4.7GB', downloaded: true, path: '~/.ollama/models/manifests/llama3.1:8b', type: 'brain', family: 'llama', parameter_size: '8B', quantization: 'Q4_K_M' },
  { name: 'phi4:14b', size: '~9.1GB', downloaded: false, path: '', type: 'brain', family: '', parameter_size: '', quantization: '' },
];

function makeSendRequest(responses = {}) {
  return jest.fn((type, payload, onResponse) => {
    if (onResponse && type in responses) {
      onResponse(responses[type]);
    }
    return 'sent';
  });
}

describe('OllamaModelList', () => {
  beforeEach(() => {
    window.confirm = jest.fn(() => true);
  });

  test('requests the engine list on mount and renders downloaded models only, with the catalogue counted', async () => {
    const sendRequest = makeSendRequest({ LOCAL_MODEL_LIST_REQUEST: { models: MODELS } });
    render(<OllamaModelList sendRequest={sendRequest} pendingDeleteFor={() => undefined} />);
    expect(sendRequest).toHaveBeenCalledWith('LOCAL_MODEL_LIST_REQUEST', {}, expect.any(Function));
    await waitFor(() => {
      expect(screen.getByTestId('ollama-model-qwen3:8b')).toBeInTheDocument();
    });
    expect(screen.getByTestId('ollama-model-qwen3:8b')).toHaveTextContent('~4.9GB');
    expect(screen.getByTestId('ollama-model-qwen3:8b')).toHaveTextContent('Q4_K_M');
    expect(screen.queryByTestId('ollama-model-phi4:14b')).not.toBeInTheDocument();
    expect(screen.getByText(/Plus 1 catalogue model not downloaded/)).toBeInTheDocument();
  });

  test('an errored list answer renders the error, not an empty inventory', async () => {
    const sendRequest = makeSendRequest({ LOCAL_MODEL_LIST_REQUEST: { models: [], error: 'daemon exploded' } });
    render(<OllamaModelList sendRequest={sendRequest} pendingDeleteFor={() => undefined} />);
    await waitFor(() => {
      expect(screen.getByTestId('ollama-list-error')).toHaveTextContent('daemon exploded');
    });
  });

  test('delete confirms, sends the model name, and refreshes on deleted:true', async () => {
    const sendRequest = makeSendRequest({
      LOCAL_MODEL_LIST_REQUEST: { models: MODELS },
      LOCAL_MODEL_DELETE_REQUEST: { model: 'qwen3:8b', deleted: true },
    });
    render(<OllamaModelList sendRequest={sendRequest} pendingDeleteFor={() => undefined} />);
    await waitFor(() => {
      expect(screen.getByTestId('ollama-delete-qwen3:8b')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId('ollama-delete-qwen3:8b'));
    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining('qwen3:8b'));
    expect(sendRequest).toHaveBeenCalledWith('LOCAL_MODEL_DELETE_REQUEST', { model: 'qwen3:8b' }, expect.any(Function));
    await waitFor(() => {
      expect(screen.getByTestId('ollama-delete-notice')).toHaveTextContent('qwen3:8b deleted from Ollama.');
    });
    // The list was re-asked after the confirmed delete.
    const listCalls = sendRequest.mock.calls.filter((c) => c[0] === 'LOCAL_MODEL_LIST_REQUEST');
    expect(listCalls.length).toBe(2);
  });

  test('a declined confirmation sends nothing', async () => {
    window.confirm = jest.fn(() => false);
    const sendRequest = makeSendRequest({ LOCAL_MODEL_LIST_REQUEST: { models: MODELS } });
    render(<OllamaModelList sendRequest={sendRequest} pendingDeleteFor={() => undefined} />);
    await waitFor(() => {
      expect(screen.getByTestId('ollama-delete-qwen3:8b')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId('ollama-delete-qwen3:8b'));
    expect(sendRequest.mock.calls.filter((c) => c[0] === 'LOCAL_MODEL_DELETE_REQUEST')).toHaveLength(0);
  });

  test('deleted:false is reported as not deleted, never as success', async () => {
    const sendRequest = makeSendRequest({
      LOCAL_MODEL_LIST_REQUEST: { models: MODELS },
      LOCAL_MODEL_DELETE_REQUEST: { model: 'qwen3:8b', deleted: false },
    });
    render(<OllamaModelList sendRequest={sendRequest} pendingDeleteFor={() => undefined} />);
    await waitFor(() => {
      expect(screen.getByTestId('ollama-delete-qwen3:8b')).toBeInTheDocument();
    });
    fireEvent.click(screen.getByTestId('ollama-delete-qwen3:8b'));
    await waitFor(() => {
      expect(screen.getByTestId('ollama-delete-error')).toHaveTextContent('was not deleted');
    });
    expect(screen.queryByTestId('ollama-delete-notice')).not.toBeInTheDocument();
  });

  test('a delete sitting in the offline queue renders as queued for that model', async () => {
    const sendRequest = makeSendRequest({ LOCAL_MODEL_LIST_REQUEST: { models: MODELS } });
    render(<OllamaModelList
      sendRequest={sendRequest}
      pendingDeleteFor={(name) => (name === 'qwen3:8b' ? { type: 'LOCAL_MODEL_DELETE_REQUEST' } : undefined)}
    />);
    await waitFor(() => {
      expect(screen.getByTestId('ollama-delete-queued-qwen3:8b')).toBeInTheDocument();
    });
    expect(screen.queryByTestId('ollama-delete-qwen3:8b')).not.toBeInTheDocument();
    expect(screen.getByTestId('ollama-delete-llama3.1:8b')).toBeInTheDocument();
  });
});

describe('OllamaModelList — Spec D fit and load state', () => {
  test('renders the fit badge and Loaded from the payload; unreported rows get neither', async () => {
    const models = [
      { ...MODELS[0], fit: { tier: 'comfortable', reason: 'Fits.' }, loaded: true },
      MODELS[1],
    ];
    const sendRequest = makeSendRequest({ LOCAL_MODEL_LIST_REQUEST: { models } });
    render(<OllamaModelList sendRequest={sendRequest} pendingDeleteFor={() => undefined} />);
    await waitFor(() => {
      expect(screen.getByTestId('ollama-model-qwen3:8b')).toBeInTheDocument();
    });
    expect(screen.getByTestId('ollama-fit-qwen3:8b')).toHaveTextContent('Comfortable');
    expect(screen.getByTestId('ollama-load-state-qwen3:8b')).toHaveTextContent('Loaded');
    // The second row's payload carries neither field — honest
    // absence: no fit badge, no load-state badge.
    expect(screen.queryByTestId('ollama-fit-llama3.1:8b')).not.toBeInTheDocument();
    expect(screen.queryByTestId('ollama-load-state-llama3.1:8b')).not.toBeInTheDocument();
  });
});
