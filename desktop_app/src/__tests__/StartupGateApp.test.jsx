import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import App from '../App';

// Wave 1 slice 4: the startup gate at the App level. The gate is
// active because this harness's electronAPI DOES expose the
// engine-state surface (getEngineState/onEngineState) — the same
// condition the real Electron shell satisfies. Harnesses without it
// (all pre-slice App tests) keep the old ungated behaviour.

// The 3D force graph needs WebGL; replace with a stub for jsdom.
jest.mock('react-force-graph-3d', () => ({
  __esModule: true,
  default: () => <div data-testid="force-graph-stub" />,
}));

// react-markdown is ESM-only; stub it for Jest.
jest.mock('react-markdown', () => ({
  __esModule: true,
  default: ({ children }) => <div data-testid="markdown-stub">{children}</div>,
}));

// jsdom has no scrollIntoView; stub it.
Element.prototype.scrollIntoView = jest.fn();

// jsdom has no WebSocket; the socket layer only needs the constants.
global.WebSocket = { OPEN: 1, CLOSED: 3, CONNECTING: 0, CLOSING: 2 };

describe('Startup gate (Wave 1 slice 4)', () => {
  let engineListeners;
  let engineStateListeners;
  let electronAPI;
  let initialEngineState;

  beforeEach(() => {
    engineListeners = [];
    engineStateListeners = [];
    initialEngineState = { state: 'starting', logFilePath: '/userData/logs/engine.log' };
    electronAPI = {
      sendEngineMessage: jest.fn(),
      onEngineMessage: jest.fn((cb) => {
        engineListeners.push(cb);
        return () => {
          const idx = engineListeners.indexOf(cb);
          if (idx !== -1) engineListeners.splice(idx, 1);
        };
      }),
      getWsStatus: jest.fn(() => Promise.resolve(false)),
      getMemoryUsage: jest.fn(() => Promise.resolve(128)),
      onChatDetached: jest.fn(() => jest.fn()),
      onChatAttached: jest.fn(() => jest.fn()),
      getEngineState: jest.fn(() => Promise.resolve(initialEngineState)),
      onEngineState: jest.fn((cb) => {
        engineStateListeners.push(cb);
        return () => {
          const idx = engineStateListeners.indexOf(cb);
          if (idx !== -1) engineStateListeners.splice(idx, 1);
        };
      }),
      retryEngineStart: jest.fn(() => Promise.resolve({ state: 'starting', logFilePath: '/userData/logs/engine.log' })),
      openEngineLog: jest.fn(() => Promise.resolve('')),
    };
    window.electronAPI = electronAPI;
  });

  afterEach(() => {
    delete window.electronAPI;
  });

  const emitEngineMessage = (obj) => {
    const raw = typeof obj === 'string' ? obj : JSON.stringify(obj);
    act(() => {
      [...engineListeners].forEach((cb) => cb(raw));
    });
  };

  const emitEngineState = (state) => {
    act(() => {
      [...engineStateListeners].forEach((cb) => cb({ state, logFilePath: '/userData/logs/engine.log' }));
    });
  };

  const flushPromises = async () => {
    await act(async () => {});
  };

  const keyedProvider = {
    name: 'openai', display_name: 'OpenAI', has_key: true,
    models: ['gpt-4'], is_default: true, default_model: 'gpt-4',
  };

  // The ready sequence: socket connects, then the two initial data
  // replies the onboarding gate also waits for.
  const becomeReady = ({ onboardingCompleted = true, providers = [keyedProvider] } = {}) => {
    emitEngineMessage({ type: '_ws_status', status: 'connected' });
    emitEngineMessage({
      type: 'CONFIG_GET_RESPONSE',
      payload: { config: { onboarding_completed: onboardingCompleted } },
    });
    emitEngineMessage({
      type: 'PROVIDER_LIST_RESPONSE',
      payload: { providers },
    });
  };

  it('replaces the shell with the gate until the ready signals arrive', async () => {
    render(<App />);
    await flushPromises();

    expect(screen.getByTestId('startup-gate')).toBeInTheDocument();
    expect(screen.getByText('Starting the engine…')).toBeInTheDocument();
    expect(screen.queryByText('Dashboard')).not.toBeInTheDocument();

    // Socket alone is not ready: config + provider list must land too.
    emitEngineMessage({ type: '_ws_status', status: 'connected' });
    await flushPromises();
    expect(screen.getByTestId('startup-gate')).toBeInTheDocument();

    emitEngineMessage({
      type: 'CONFIG_GET_RESPONSE',
      payload: { config: { onboarding_completed: true } },
    });
    await flushPromises();
    expect(screen.getByTestId('startup-gate')).toBeInTheDocument();

    emitEngineMessage({
      type: 'PROVIDER_LIST_RESPONSE',
      payload: { providers: [keyedProvider] },
    });
    await flushPromises();
    expect(screen.queryByTestId('startup-gate')).not.toBeInTheDocument();
    expect(screen.getByText('Dashboard')).toBeInTheDocument();
  });

  it('hands a first run to the onboarding overlay after the gate', async () => {
    render(<App />);
    await flushPromises();
    expect(screen.queryByTestId('onboarding-overlay')).not.toBeInTheDocument();

    becomeReady({
      onboardingCompleted: false,
      providers: [{ ...keyedProvider, has_key: false, models: [] }],
    });
    await flushPromises();

    expect(screen.queryByTestId('startup-gate')).not.toBeInTheDocument();
    expect(screen.getByTestId('onboarding-overlay')).toBeInTheDocument();
  });

  it('shows FAILED only on the lifecycle failure signal, and Retry returns to STARTING', async () => {
    render(<App />);
    await flushPromises();
    expect(screen.getByText('Starting the engine…')).toBeInTheDocument();

    emitEngineState('failed');
    expect(screen.getByText(/The engine couldn’t start/)).toBeInTheDocument();
    expect(screen.queryByText('Dashboard')).not.toBeInTheDocument();

    fireEvent.click(screen.getByText('Open engine log'));
    expect(electronAPI.openEngineLog).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByText('Retry'));
    expect(electronAPI.retryEngineStart).toHaveBeenCalledTimes(1);
    // Back to STARTING immediately, before main's outcome broadcast.
    expect(screen.getByText('Starting the engine…')).toBeInTheDocument();
    expect(screen.queryByText(/The engine couldn’t start/)).not.toBeInTheDocument();

    // The retried start succeeds: state broadcast alone is not ready…
    emitEngineState('started');
    expect(screen.getByTestId('startup-gate')).toBeInTheDocument();
    // …the ready signals are.
    becomeReady();
    await flushPromises();
    expect(screen.queryByTestId('startup-gate')).not.toBeInTheDocument();
    expect(screen.getByText('Dashboard')).toBeInTheDocument();
  });

  it('passes the gate when the engine answers anyway, despite a failed lifecycle signal', async () => {
    initialEngineState = { state: 'failed', logFilePath: '/userData/logs/engine.log' };
    render(<App />);
    await flushPromises();
    expect(screen.getByText(/The engine couldn’t start/)).toBeInTheDocument();

    // An external engine (dev flow) starts answering on the port:
    // socket + initial data beat the lifecycle's failure report.
    becomeReady();
    await flushPromises();
    expect(screen.queryByTestId('startup-gate')).not.toBeInTheDocument();
    expect(screen.getByText('Dashboard')).toBeInTheDocument();
  });

  it('never brings the gate back after ready, even on a mid-session disconnect', async () => {
    render(<App />);
    await flushPromises();
    becomeReady();
    await flushPromises();
    expect(screen.queryByTestId('startup-gate')).not.toBeInTheDocument();

    emitEngineMessage({ type: '_ws_status', status: 'disconnected' });
    await flushPromises();
    expect(screen.queryByTestId('startup-gate')).not.toBeInTheDocument();
    expect(screen.getByText('Dashboard')).toBeInTheDocument();
    // The shell's own offline handling owns the drop.
    expect(screen.getByText(/Offline \/ Engine Disconnected/)).toBeInTheDocument();
  });
});
