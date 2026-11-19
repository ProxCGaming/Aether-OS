/**
 * The llama.cpp router section of the Local Models tab: the
 * second local serving backend, beside Ollama. Its four states
 * (off / not running / running / read failed) are distinct and
 * honest, rows change only on the engine's confirming reply, and
 * a refusal shows the engine's reason verbatim — the Profile /
 * Workflows discipline, over the LLAMACPP_* pairs.
 */
import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import LlamaCppPanel from '../LlamaCppPanel';
import { useAetherSocket } from '../../../contexts/SocketContext';

jest.mock('../../../contexts/SocketContext', () => ({
  useAetherSocket: jest.fn(),
}));

function makeHarness({ isConnected = true } = {}) {
  let messageListeners = [];
  const mockWs = {
    addEventListener: jest.fn((event, cb) => {
      if (event === 'message') messageListeners.push(cb);
    }),
    removeEventListener: jest.fn((event, cb) => {
      if (event === 'message') messageListeners = messageListeners.filter((l) => l !== cb);
    }),
  };
  const sendMessage = jest.fn();
  useAetherSocket.mockReturnValue({
    wsRef: { current: mockWs },
    sendMessage,
    isConnected,
  });
  const simulateMessage = (data) => {
    act(() => {
      messageListeners.forEach((cb) => cb({ data: JSON.stringify(data) }));
    });
  };
  const callsOf = (type) => sendMessage.mock.calls.filter(([t]) => t === type);
  return { simulateMessage, sendMessage, callsOf };
}

const STATUS_OFF = {
  enabled: false, running: false, url: 'http://127.0.0.1:9931',
  version: null, models_loaded: null, supervised: false,
};

const STATUS_NOT_RUNNING = {
  enabled: true, running: false, url: 'http://127.0.0.1:9931',
  version: null, models_loaded: null, supervised: true,
};

const STATUS_RUNNING = {
  enabled: true, running: true, url: 'http://127.0.0.1:9931',
  version: 'b4321', models_loaded: 2, supervised: true,
};

const MODELS = [
  {
    id: 'm-tiny', name: 'qwen-tiny.gguf', size_bytes: 734003200,
    loaded: true, preset: 'Tiny resident', tier: 'resident', fit: 'comfortable',
  },
  {
    id: 'm-daily', name: 'llama-daily.gguf', size_bytes: 4831838208,
    loaded: false, preset: 'Daily driver', tier: 'daily', fit: 'tight',
  },
  {
    id: 'm-heavy', name: 'big-heavy.gguf', size_bytes: null,
    loaded: null, preset: null, tier: 'heavy', fit: null,
  },
];

const PRESETS = [
  {
    id: 'p-tiny', name: 'Tiny resident', tier: 'resident',
    description: 'Always ready for quick jobs.', available: true,
  },
  {
    id: 'p-heavy', name: 'Heavy hitter', tier: 'heavy',
    description: 'The big model for hard jobs.', available: false,
  },
];

function renderWithStatus(harness, status, modelsPayload) {
  render(<LlamaCppPanel />);
  harness.simulateMessage({ type: 'LLAMACPP_STATUS_RESPONSE', payload: status });
  if (modelsPayload !== undefined) {
    harness.simulateMessage({ type: 'LLAMACPP_MODELS_RESPONSE', payload: modelsPayload });
  }
}

function renderRunning(harness, modelsPayload = { models: MODELS, presets: PRESETS }) {
  renderWithStatus(harness, STATUS_RUNNING, modelsPayload);
}

describe('LlamaCppPanel reads', () => {
  test('requests status and models on mount and shows a load state until the status answers', () => {
    const harness = makeHarness();
    render(<LlamaCppPanel />);
    expect(harness.sendMessage).toHaveBeenCalledWith(
      'LLAMACPP_STATUS_REQUEST',
      expect.objectContaining({ payload: {} }),
    );
    expect(harness.sendMessage).toHaveBeenCalledWith(
      'LLAMACPP_MODELS_REQUEST',
      expect.objectContaining({ payload: {} }),
    );
    expect(screen.getByTestId('llamacpp-load-state')).toHaveAttribute('data-load-display', 'loading');
    expect(screen.queryByTestId('llamacpp-off-state')).not.toBeInTheDocument();
  });

  test('a failed status read is an error state carrying the engine reason verbatim', () => {
    const harness = makeHarness();
    renderWithStatus(harness, { error: 'The router config could not be read.' });
    const row = screen.getByTestId('llamacpp-load-state');
    expect(row).toHaveAttribute('data-load-display', 'error');
    expect(row).toHaveTextContent('The router config could not be read.');
    expect(screen.queryByTestId('llamacpp-off-state')).not.toBeInTheDocument();
  });

  test('the Refresh button re-requests status and models', () => {
    const harness = makeHarness();
    renderRunning(harness);
    fireEvent.click(screen.getByTestId('llamacpp-refresh'));
    expect(harness.callsOf('LLAMACPP_STATUS_REQUEST')).toHaveLength(2);
    expect(harness.callsOf('LLAMACPP_MODELS_REQUEST')).toHaveLength(2);
  });
});

describe('LlamaCppPanel states', () => {
  test('disabled: the off state explains itself and offers no controls', () => {
    const harness = makeHarness();
    renderWithStatus(harness, STATUS_OFF, { models: [], presets: [] });
    expect(screen.getByTestId('llamacpp-off-state')).toBeInTheDocument();
    expect(screen.getByText('The llama.cpp router is off')).toBeInTheDocument();
    expect(screen.getByText(/Turning it on happens in Aether's config/)).toBeInTheDocument();
    expect(screen.queryByTestId('llamacpp-start-btn')).not.toBeInTheDocument();
    expect(screen.queryByTestId('llamacpp-stop-btn')).not.toBeInTheDocument();
    expect(screen.queryByTestId('llamacpp-presets')).not.toBeInTheDocument();
  });

  test('enabled but not running: names the URL it tried and offers Start', () => {
    const harness = makeHarness();
    renderWithStatus(harness, STATUS_NOT_RUNNING, { models: [], presets: [] });
    expect(screen.getByTestId('llamacpp-not-running')).toBeInTheDocument();
    expect(screen.getByText('Router not running')).toBeInTheDocument();
    expect(screen.getByText(/http:\/\/127\.0\.0\.1:9931/)).toBeInTheDocument();
    expect(screen.getByTestId('llamacpp-start-btn')).toBeInTheDocument();
    expect(screen.queryByTestId('llamacpp-status')).not.toBeInTheDocument();
  });

  test('running and supervised: the status line reports URL, version, loaded count and the Stop button', () => {
    const harness = makeHarness();
    renderRunning(harness);
    const line = screen.getByTestId('llamacpp-status');
    expect(line).toHaveTextContent('Running at http://127.0.0.1:9931');
    expect(line).toHaveTextContent('version b4321');
    expect(line).toHaveTextContent('2 models loaded');
    expect(line).toHaveTextContent('Managed by Aether');
    expect(screen.getByTestId('llamacpp-stop-btn')).toBeInTheDocument();
  });

  test('running but not supervised: no Stop button and no managed claim', () => {
    const harness = makeHarness();
    renderWithStatus(
      harness,
      { ...STATUS_RUNNING, supervised: false, version: null, models_loaded: null },
      { models: [], presets: [] },
    );
    expect(screen.queryByTestId('llamacpp-stop-btn')).not.toBeInTheDocument();
    expect(screen.getByTestId('llamacpp-status')).not.toHaveTextContent('Managed by Aether');
    expect(screen.getByTestId('llamacpp-status')).not.toHaveTextContent('version');
  });
});

describe('LlamaCppPanel router control', () => {
  test('Start sends the control request; a refusal shows the engine reason verbatim', () => {
    const harness = makeHarness();
    renderWithStatus(harness, STATUS_NOT_RUNNING, { models: [], presets: [] });
    fireEvent.click(screen.getByTestId('llamacpp-start-btn'));
    const calls = harness.callsOf('LLAMACPP_ROUTER_CONTROL_REQUEST');
    expect(calls).toHaveLength(1);
    expect(calls[0][1].payload).toEqual({ action: 'start' });
    harness.simulateMessage({
      type: 'LLAMACPP_ROUTER_CONTROL_RESPONSE',
      payload: { success: false, running: false, error: 'No llama.cpp binary is configured.' },
    });
    expect(screen.getByTestId('llamacpp-control-error')).toHaveTextContent('No llama.cpp binary is configured.');
    // Still not running — the refusal changed nothing on screen.
    expect(screen.getByTestId('llamacpp-not-running')).toBeInTheDocument();
  });

  test('a confirmed Start re-reads the status', () => {
    const harness = makeHarness();
    renderWithStatus(harness, STATUS_NOT_RUNNING, { models: [], presets: [] });
    fireEvent.click(screen.getByTestId('llamacpp-start-btn'));
    harness.simulateMessage({
      type: 'LLAMACPP_ROUTER_CONTROL_RESPONSE',
      payload: { success: true, running: true },
    });
    expect(harness.callsOf('LLAMACPP_STATUS_REQUEST')).toHaveLength(2);
    harness.simulateMessage({ type: 'LLAMACPP_STATUS_RESPONSE', payload: STATUS_RUNNING });
    expect(screen.getByTestId('llamacpp-status')).toBeInTheDocument();
  });

  test('Stop sends the control request with action stop', () => {
    const harness = makeHarness();
    renderRunning(harness);
    fireEvent.click(screen.getByTestId('llamacpp-stop-btn'));
    const calls = harness.callsOf('LLAMACPP_ROUTER_CONTROL_REQUEST');
    expect(calls).toHaveLength(1);
    expect(calls[0][1].payload).toEqual({ action: 'stop' });
  });
});

describe('LlamaCppPanel models', () => {
  test('rows show name, size, fit badge, loaded badge, preset and tier in plain words', () => {
    const harness = makeHarness();
    renderRunning(harness);
    const tiny = screen.getByTestId('llamacpp-model-m-tiny');
    expect(tiny).toHaveTextContent('qwen-tiny.gguf');
    expect(tiny).toHaveTextContent('700 MB');
    expect(tiny).toHaveTextContent('Tiny resident · Always loaded');
    expect(screen.getByTestId('llamacpp-fit-m-tiny')).toHaveTextContent('Comfortable');
    expect(screen.getByTestId('llamacpp-load-state-m-tiny')).toHaveTextContent('Loaded');

    const daily = screen.getByTestId('llamacpp-model-m-daily');
    expect(daily).toHaveTextContent('4.5 GB');
    expect(daily).toHaveTextContent('Daily driver · Everyday — loads when needed');
    expect(screen.getByTestId('llamacpp-fit-m-daily')).toHaveTextContent('Tight');
    expect(screen.getByTestId('llamacpp-load-state-m-daily')).toHaveTextContent('On disk');

    const heavy = screen.getByTestId('llamacpp-model-m-heavy');
    expect(heavy).toHaveTextContent('Heavy — loads for a job, then unloads');
    // No size was reported, no fit was judged: neither is invented.
    expect(screen.queryByTestId('llamacpp-fit-m-heavy')).not.toBeInTheDocument();
  });

  test('an unknown load state is stated plainly and offers neither action', () => {
    const harness = makeHarness();
    renderRunning(harness);
    expect(screen.getByTestId('llamacpp-load-unknown-m-heavy')).toHaveTextContent('Load state unknown');
    expect(screen.queryByTestId('llamacpp-load-state-m-heavy')).not.toBeInTheDocument();
    expect(screen.queryByTestId('llamacpp-load-m-heavy')).not.toBeInTheDocument();
    expect(screen.queryByTestId('llamacpp-unload-m-heavy')).not.toBeInTheDocument();
  });

  test('Load sends the model id, the row waits for the reply, and the confirmed list replaces it', () => {
    const harness = makeHarness();
    renderRunning(harness);
    fireEvent.click(screen.getByTestId('llamacpp-load-m-daily'));
    const calls = harness.callsOf('LLAMACPP_MODEL_LOAD_REQUEST');
    expect(calls).toHaveLength(1);
    expect(calls[0][1].payload).toEqual({ model_id: 'm-daily' });
    // Nothing changed yet: the button is in flight, not flipped.
    expect(screen.getByTestId('llamacpp-load-m-daily')).toHaveTextContent('Loading…');
    expect(screen.queryByTestId('llamacpp-unload-m-daily')).not.toBeInTheDocument();
    expect(harness.callsOf('LLAMACPP_MODELS_REQUEST')).toHaveLength(1);

    harness.simulateMessage({ type: 'LLAMACPP_MODEL_LOAD_RESPONSE', payload: { success: true } });
    expect(harness.callsOf('LLAMACPP_MODELS_REQUEST')).toHaveLength(2);
    harness.simulateMessage({
      type: 'LLAMACPP_MODELS_RESPONSE',
      payload: {
        models: MODELS.map((m) => (m.id === 'm-daily' ? { ...m, loaded: true } : m)),
        presets: PRESETS,
      },
    });
    expect(screen.getByTestId('llamacpp-unload-m-daily')).toBeInTheDocument();
    expect(screen.getByTestId('llamacpp-load-state-m-daily')).toHaveTextContent('Loaded');
  });

  test('Unload sends the model id', () => {
    const harness = makeHarness();
    renderRunning(harness);
    fireEvent.click(screen.getByTestId('llamacpp-unload-m-tiny'));
    const calls = harness.callsOf('LLAMACPP_MODEL_UNLOAD_REQUEST');
    expect(calls).toHaveLength(1);
    expect(calls[0][1].payload).toEqual({ model_id: 'm-tiny' });
  });

  test('a refused unload shows the engine reason verbatim and leaves the row loaded', () => {
    const harness = makeHarness();
    renderRunning(harness);
    fireEvent.click(screen.getByTestId('llamacpp-unload-m-tiny'));
    harness.simulateMessage({
      type: 'LLAMACPP_MODEL_UNLOAD_RESPONSE',
      payload: { success: false, error: 'The model is serving a request right now.' },
    });
    expect(screen.getByTestId('llamacpp-action-error')).toHaveTextContent('The model is serving a request right now.');
    expect(screen.getByTestId('llamacpp-unload-m-tiny')).toBeInTheDocument();
    expect(screen.getByTestId('llamacpp-load-state-m-tiny')).toHaveTextContent('Loaded');
    // A refusal does not trigger a re-read.
    expect(harness.callsOf('LLAMACPP_MODELS_REQUEST')).toHaveLength(1);
  });

  test('a failed models read while running is an error state, never an empty list', () => {
    const harness = makeHarness();
    renderWithStatus(harness, STATUS_RUNNING, { error: 'The router stopped answering.' });
    const row = screen.getByTestId('llamacpp-models-load-state');
    expect(row).toHaveAttribute('data-load-display', 'error');
    expect(row).toHaveTextContent('The router stopped answering.');
    expect(screen.queryByText('The router reports no models on this machine yet.')).not.toBeInTheDocument();
  });

  test('an answered-empty models list says so plainly', () => {
    const harness = makeHarness();
    renderRunning(harness, { models: [], presets: [] });
    expect(screen.getByText('The router reports no models on this machine yet.')).toBeInTheDocument();
  });
});

describe('LlamaCppPanel presets', () => {
  test('presets render with plain-word tiers, descriptions and the not-on-this-machine note', () => {
    const harness = makeHarness();
    renderRunning(harness);
    expect(screen.getByTestId('llamacpp-presets')).toBeInTheDocument();
    const tiny = screen.getByTestId('llamacpp-preset-p-tiny');
    expect(tiny).toHaveTextContent('Tiny resident');
    expect(tiny).toHaveTextContent('Always loaded');
    expect(tiny).toHaveTextContent('Always ready for quick jobs.');
    expect(screen.queryByTestId('llamacpp-preset-unavailable-p-tiny')).not.toBeInTheDocument();

    const heavy = screen.getByTestId('llamacpp-preset-p-heavy');
    expect(heavy).toHaveTextContent('Heavy hitter');
    expect(heavy).toHaveTextContent('Heavy — loads for a job, then unloads');
    expect(screen.getByTestId('llamacpp-preset-unavailable-p-heavy')).toHaveTextContent('Not on this machine yet');
    // Presets carry no download controls — the hub owns downloads.
    expect(heavy.querySelector('button')).toBeNull();
  });

  test('presets also render while the router is not running', () => {
    const harness = makeHarness();
    renderWithStatus(harness, STATUS_NOT_RUNNING, { models: [], presets: PRESETS });
    expect(screen.getByTestId('llamacpp-not-running')).toBeInTheDocument();
    expect(screen.getByTestId('llamacpp-presets')).toBeInTheDocument();
  });

  test('no presets in the answer, no presets block', () => {
    const harness = makeHarness();
    renderRunning(harness, { models: MODELS, presets: [] });
    expect(screen.queryByTestId('llamacpp-presets')).not.toBeInTheDocument();
  });
});
