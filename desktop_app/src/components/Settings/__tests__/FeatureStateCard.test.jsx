import React from 'react';
import { render, screen, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import FeatureStateCard from '../FeatureStateCard';

// Master build S21: the per-feature state+reason card. The
// matrix rows must render the engine's S10 report verbatim
// (status + reason), the switch rows must render the APPLIED
// config handed down as a prop, and the not-built features
// (offline mode O2, packs O3) must say exactly that. Mocked
// per the MachineCapabilityCard pattern: SocketContext spied,
// messages delivered through the captured listener.

const mockSendMessage = jest.fn();
let mockMessageListeners = [];

jest.mock('../../../contexts/SocketContext', () => ({
  useAetherSocket: () => ({
    wsRef: {
      current: {
        addEventListener: (event, cb) => {
          if (event === 'message') mockMessageListeners.push(cb);
        },
        removeEventListener: (event, cb) => {
          if (event === 'message') {
            mockMessageListeners = mockMessageListeners.filter((l) => l !== cb);
          }
        },
        readyState: 1,
      },
    },
    sendMessage: mockSendMessage,
    isConnected: true,
    reconnectCount: 0,
  }),
}));

function simulateMessage(message) {
  act(() => {
    for (const cb of [...mockMessageListeners]) {
      cb({ data: JSON.stringify(message) });
    }
  });
}

// Shaped exactly like capability_matrix()'s report.
const REPORT = {
  schema_version: 1,
  probed_at: 1763126400,
  machine: { cpu_cores: 8, ram_gb: 16, gpu: null, os: 'win32' },
  features: [
    { id: 'local_voice', status: 'available', reason: 'Silero VAD, Smart Turn, faster-whisper and Kokoro TTS are all available.' },
    { id: 'local_models', status: 'degraded', reason: 'Ollama is running, but no chat model is downloaded.' },
    { id: 'cloud_providers', status: 'available', reason: 'google_gemini has a saved API key.' },
    { id: 'semantic_embeddings', status: 'unavailable', reason: 'No embedding path is reachable (Ollama offline and no keyed provider reports an embedder).' },
    { id: 'browser_automation', status: 'available', reason: 'Browser automation is available (Playwright is installed).' },
    { id: 'desktop_control', status: 'unavailable', reason: 'Computer Use is off in Settings; uiautomation is not installed.' },
  ],
};

const CONFIG = {
  memory_backend: 'native',
  model_routing: 'auto',
  computer_use_enabled: true,
  computer_use_observe: true,
  computer_use_control: false,
};

beforeEach(() => {
  jest.clearAllMocks();
  mockMessageListeners = [];
});

describe('FeatureStateCard (S21)', () => {
  test('asks the engine for the capability report and waits honestly before it lands', () => {
    render(<FeatureStateCard config={null} />);
    expect(mockSendMessage).toHaveBeenCalledWith(
      'MACHINE_CAPABILITY_REQUEST', expect.objectContaining({ payload: {} }));
    expect(screen.getByTestId('feature-state-card')).toHaveTextContent(/Asking the engine/i);
  });

  test('renders the matrix rows with the engine statuses, reasons and locality', () => {
    render(<FeatureStateCard config={CONFIG} />);
    simulateMessage({ type: 'MACHINE_CAPABILITY_RESPONSE', payload: REPORT });
    expect(screen.getByTestId('feature-state-status-local_voice')).toHaveTextContent('Available');
    expect(screen.getByTestId('feature-state-local_voice')).toHaveTextContent('faster-whisper and Kokoro TTS are all available');
    expect(screen.getByTestId('feature-state-local_voice')).toHaveTextContent('full → degraded → text-only');
    expect(screen.getByTestId('feature-state-status-local_models')).toHaveTextContent('Degraded');
    expect(screen.getByTestId('feature-state-status-semantic_embeddings')).toHaveTextContent('Unavailable');
    expect(screen.getByTestId('feature-state-desktop_control')).toHaveTextContent(
      'Computer Use is on: Observe on, Control off (Settings → Computer Use).');
  });

  test('renders the config rows from the applied config: backend, routing, desktop switches', () => {
    render(<FeatureStateCard config={CONFIG} />);
    expect(screen.getByTestId('feature-state-memory')).toHaveTextContent('Configured backend: native.');
    expect(screen.getByTestId('feature-state-model_routing')).toHaveTextContent('Auto: the engine picks a model per delegation');
  });

  test('offline mode and packs say Not built, with the reason (O2/O3)', () => {
    render(<FeatureStateCard config={CONFIG} />);
    expect(screen.getByTestId('feature-state-offline_mode')).toHaveTextContent('Not built');
    expect(screen.getByTestId('feature-state-offline_mode')).toHaveTextContent(/no offline mode in this build \(planned, not built\)/i);
    expect(screen.getByTestId('feature-state-packs')).toHaveTextContent('Not built');
    expect(screen.getByTestId('feature-state-packs')).toHaveTextContent(/no on-demand feature packs in this build \(planned, not built\)/i);
  });

  test('with no config yet, the switch rows say they are waiting instead of guessing', () => {
    render(<FeatureStateCard config={null} />);
    simulateMessage({ type: 'MACHINE_CAPABILITY_RESPONSE', payload: REPORT });
    expect(screen.getByTestId('feature-state-memory')).toHaveTextContent(/Waiting for the applied config/i);
    expect(screen.getByTestId('feature-state-model_routing')).toHaveTextContent(/Waiting for the applied config/i);
    // The desktop row shows the matrix verdict only — no switch
    // line is invented without the config.
    expect(screen.getByTestId('feature-state-desktop_control')).not.toHaveTextContent(/Observe/);
  });
});
