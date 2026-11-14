/** Master build S10: the "This machine" card — Settings →
 * Capabilities' hardware readout. Pins: the report is
 * requested on mount, the engine's verdicts and reasons are
 * rendered verbatim (the card computes nothing), and Re-probe
 * re-requests with force so the engine bypasses its cache. */
import React from 'react';
import { render, screen, fireEvent, act, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom';
import MachineCapabilityCard from '../MachineCapabilityCard';
import { useAetherSocket } from '../../../contexts/SocketContext';

jest.mock('../../../contexts/SocketContext', () => ({
  useAetherSocket: jest.fn()
}));

const FIXTURE_REPORT = {
  probed_at: 1700000000,
  cached: false,
  machine: {
    platform: { system: 'Windows', release: '11', architecture: 'AMD64', python: '3.12.3' },
    cpu: { logical_cores: 8, model: 'AMD Ryzen 7 5800X' },
    ram: { total_bytes: 16 * 1024 ** 3 },
    disk: { path: 'C:\\Users\\test\\.aether', total_bytes: 500 * 1024 ** 3, free_bytes: 200 * 1024 ** 3 },
    gpu: { name: 'NVIDIA GeForce RTX 4070', vendor: 'nvidia', memory_bytes: 12 * 1024 ** 3 },
    microphone: { state: 'unknown', detail: 'engine cannot see it' },
    network: { state: 'online', reachable: ['api.openai.com'] },
    ollama_reachable: true,
    voice_dependencies: { faster_whisper: true, kokoro: true, onnxruntime: true, numpy: true, aiortc: true },
    browser: { playwright_installed: true, chromium_found: false }
  },
  features: [
    { id: 'local_voice', name: 'Local voice (speech-to-text + text-to-speech)', status: 'available', reason: 'A GPU was detected (NVIDIA GeForce RTX 4070), so the local speech models can run accelerated instead of on CPU.' },
    { id: 'local_models', name: 'Local LLM models (Ollama)', status: 'available', reason: 'Ollama is running and a GPU was detected.' },
    { id: 'cloud_providers', name: 'Cloud model providers', status: 'available', reason: 'Provider endpoints are reachable from this machine (api.openai.com).' },
    { id: 'semantic_embeddings', name: 'Semantic memory embeddings', status: 'available', reason: 'Ollama is answering at 127.0.0.1:11434.' },
    { id: 'browser_automation', name: 'Browser automation', status: 'degraded', reason: 'Playwright is installed, but no Chromium build was found in the default browsers directory.' },
    { id: 'desktop_control', name: 'Desktop control (Computer Use)', status: 'degraded', reason: 'This platform supports desktop automation (Windows UI Automation), but Aether\u2019s Computer Use feature is not built yet.' }
  ]
};

describe('MachineCapabilityCard', () => {
  let mockSendMessage;
  let mockWsRef;
  let messageListeners;

  beforeEach(() => {
    mockSendMessage = jest.fn();
    messageListeners = [];
    mockWsRef = {
      current: {
        addEventListener: jest.fn((event, cb) => {
          if (event === 'message') messageListeners.push(cb);
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
      sendMessage: mockSendMessage,
      isConnected: true,
      reconnectCount: 0
    });
  });

  const simulateReport = (payload = FIXTURE_REPORT) => {
    messageListeners.forEach(cb => cb({
      data: JSON.stringify({ type: 'MACHINE_CAPABILITY_RESPONSE', payload })
    }));
  };

  test('requests the report on mount and shows a probing state first', () => {
    render(<MachineCapabilityCard />);
    expect(mockSendMessage).toHaveBeenCalledWith(
      'MACHINE_CAPABILITY_REQUEST', expect.objectContaining({ payload: {} }));
    expect(screen.getByText('Probing this machine…')).toBeInTheDocument();
  });

  test('renders the machine summary and every verdict with its reason', async () => {
    render(<MachineCapabilityCard />);
    act(() => simulateReport());

    await waitFor(() => {
      expect(screen.getByTestId('machine-summary')).toHaveTextContent('AMD Ryzen 7 5800X');
    });
    expect(screen.getByTestId('machine-summary')).toHaveTextContent('NVIDIA GeForce RTX 4070');
    expect(screen.getByTestId('machine-summary')).toHaveTextContent('RAM 16 GB');

    expect(screen.getByTestId('machine-feature-status-local_voice')).toHaveTextContent('Available');
    expect(screen.getByTestId('machine-feature-local_voice')).toHaveTextContent('run accelerated instead of on CPU');
    expect(screen.getByTestId('machine-feature-status-browser_automation')).toHaveTextContent('Degraded');
    expect(screen.getByTestId('machine-feature-browser_automation')).toHaveTextContent('no Chromium build was found');
    expect(screen.getByTestId('machine-feature-desktop_control')).toHaveTextContent('not built yet');
  });

  test('an unavailable and an unknown verdict render their own chips', async () => {
    render(<MachineCapabilityCard />);
    act(() => simulateReport({
      ...FIXTURE_REPORT,
      features: [
        { id: 'local_voice', name: 'Local voice', status: 'unavailable', reason: 'The local voice stack is not installed (missing: kokoro).' },
        { id: 'cloud_providers', name: 'Cloud model providers', status: 'unknown', reason: 'Network reachability could not be determined.' }
      ]
    }));

    await waitFor(() => {
      expect(screen.getByTestId('machine-feature-status-local_voice')).toHaveTextContent('Unavailable');
    });
    expect(screen.getByTestId('machine-feature-local_voice')).toHaveTextContent('missing: kokoro');
    expect(screen.getByTestId('machine-feature-status-cloud_providers')).toHaveTextContent('Unknown');
  });

  test('Re-probe re-requests with force (cache bypass)', async () => {
    render(<MachineCapabilityCard />);
    act(() => simulateReport());
    await waitFor(() => {
      expect(screen.getByTestId('machine-summary')).toBeInTheDocument();
    });

    fireEvent.click(screen.getByTestId('machine-capability-reprobe'));
    const forceCall = mockSendMessage.mock.calls.find(
      ([type, msg]) => type === 'MACHINE_CAPABILITY_REQUEST' && msg.payload.force === true);
    expect(forceCall).toBeDefined();
  });
});
