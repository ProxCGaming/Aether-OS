import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import App from '../App';

// E6-F02: onboarding Finish/Skip must (1) persist the completion flag
// through the queue-capable socket facade — a Skip pressed while
// disconnected is delivered on reconnect, exactly once, so the engine's
// flag turns true and the wizard never resurrects — and (2) hand the
// user into Chat. The old callback sent CONFIG_UPDATE_REQUEST only
// when the raw socket happened to be OPEN (dropping it offline) and
// never selected the Chat tab (an online Finish stayed on Dashboard).

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

describe('Onboarding completion persistence and Chat handoff (E6-F02)', () => {
  let engineListeners;
  let electronAPI;

  beforeEach(() => {
    engineListeners = [];
    electronAPI = {
      sendEngineMessage: jest.fn(),
      onEngineMessage: jest.fn((cb) => {
        engineListeners.push(cb);
        return () => {
          const idx = engineListeners.indexOf(cb);
          if (idx !== -1) engineListeners.splice(idx, 1);
        };
      }),
      // The harness starts DISCONNECTED: the facade reports not-open,
      // so sends must queue until a `_ws_status` connected event.
      getWsStatus: jest.fn(() => Promise.resolve(false)),
      getMemoryUsage: jest.fn(() => Promise.resolve(128)),
      onChatDetached: jest.fn(() => jest.fn()),
      onChatAttached: jest.fn(() => jest.fn()),
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

  const flushPromises = async () => {
    await act(async () => {});
  };

  const connect = () => emitEngineMessage({ type: '_ws_status', status: 'connected' });
  const disconnect = () => emitEngineMessage({ type: '_ws_status', status: 'disconnected' });

  const sentPackets = () => electronAPI.sendEngineMessage.mock.calls
    .map(([raw]) => { try { return JSON.parse(raw); } catch (e) { return null; } })
    .filter(Boolean);

  const configUpdates = () => sentPackets().filter((p) => p.type === 'CONFIG_UPDATE_REQUEST');

  const noKeyProvider = (overrides = {}) => ({
    name: 'openai',
    display_name: 'OpenAI',
    has_key: false,
    models: [],
    is_default: true,
    default_model: '',
    ...overrides,
  });

  // The audit's eligible-wizard state: providers loaded, none with a
  // key, and the engine config says onboarding is not completed.
  const emitUnfinishedConfig = () => {
    emitEngineMessage({
      type: 'PROVIDER_LIST_RESPONSE',
      payload: { providers: [noKeyProvider()] },
    });
    emitEngineMessage({
      type: 'CONFIG_GET_RESPONSE',
      payload: { config: { onboarding_completed: false } },
    });
  };

  const goToDashboard = () => fireEvent.click(screen.getByText('Dashboard'));

  it('delivers a disconnected Skip on reconnect, exactly once, and the wizard does not return', async () => {
    render(<App />);
    await flushPromises();

    emitUnfinishedConfig();
    expect(screen.getByTestId('onboarding-overlay')).toBeInTheDocument();

    // Skip while disconnected: the wizard hides locally…
    fireEvent.click(screen.getByText('Skip setup'));
    expect(screen.queryByTestId('onboarding-overlay')).not.toBeInTheDocument();
    // …but nothing can be sent yet — it must be queued, not dropped.
    expect(configUpdates()).toHaveLength(0);

    // On reconnect the queued completion is delivered, exactly once,
    // carrying the persisted flag.
    connect();
    expect(configUpdates()).toHaveLength(1);
    expect(configUpdates()[0].payload.config.onboarding_completed).toBe(true);

    // The engine's now-true flag state must not resurrect the wizard.
    emitEngineMessage({
      type: 'CONFIG_GET_RESPONSE',
      payload: { config: { onboarding_completed: true } },
    });
    expect(screen.queryByTestId('onboarding-overlay')).not.toBeInTheDocument();
  });

  it('does not duplicate the completion after a later reconnect when a stale flag reply resurrected the wizard', async () => {
    render(<App />);
    await flushPromises();

    emitUnfinishedConfig();
    fireEvent.click(screen.getByText('Skip setup'));
    connect();
    expect(configUpdates()).toHaveLength(1);

    // A stale pre-update flag reply lands after the flush and brings
    // the wizard back; the user finishes again. The completion send
    // must still have been issued exactly once for this session.
    emitEngineMessage({
      type: 'CONFIG_GET_RESPONSE',
      payload: { config: { onboarding_completed: false } },
    });
    expect(screen.getByTestId('onboarding-overlay')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Skip setup'));
    expect(configUpdates()).toHaveLength(1);

    // The real update reply settles it: the wizard stays gone.
    emitEngineMessage({
      type: 'CONFIG_UPDATE_RESPONSE',
      payload: { config: { onboarding_completed: true } },
    });
    expect(screen.queryByTestId('onboarding-overlay')).not.toBeInTheDocument();
  });

  it('hands the user into Chat on an online Finish from Dashboard', async () => {
    render(<App />);
    await flushPromises();
    connect();

    emitUnfinishedConfig();
    goToDashboard();
    expect(screen.getByText('System Dashboard')).toBeInTheDocument();
    expect(screen.getByTestId('onboarding-overlay')).toBeInTheDocument();

    // Walk the wizard's own steps to its Finish ("Start chatting").
    fireEvent.click(screen.getByText('Skip step'));
    fireEvent.click(screen.getByText('Skip step'));
    fireEvent.click(screen.getByText('Continue'));
    // E6-F03: the approval step advances on the engine's reply.
    emitEngineMessage({
      type: 'APPROVAL_MODE_SET_RESPONSE',
      payload: { success: true, mode: 'ask' },
    });
    fireEvent.click(screen.getByText('Start chatting'));

    expect(configUpdates()).toHaveLength(1);
    expect(configUpdates()[0].payload.config.onboarding_completed).toBe(true);
    expect(screen.queryByTestId('onboarding-overlay')).not.toBeInTheDocument();
    // Handoff: no longer on Dashboard — Chat is the active tab.
    expect(screen.queryByText('System Dashboard')).not.toBeInTheDocument();
    expect(screen.getByPlaceholderText('Message Aether...')).toBeInTheDocument();
  });

  it('hands the user into Chat on an online Skip from Dashboard', async () => {
    render(<App />);
    await flushPromises();
    connect();

    emitUnfinishedConfig();
    goToDashboard();
    expect(screen.getByText('System Dashboard')).toBeInTheDocument();

    fireEvent.click(screen.getByText('Skip setup'));

    expect(configUpdates()).toHaveLength(1);
    expect(configUpdates()[0].payload.config.onboarding_completed).toBe(true);
    expect(screen.queryByTestId('onboarding-overlay')).not.toBeInTheDocument();
    expect(screen.queryByText('System Dashboard')).not.toBeInTheDocument();
    expect(screen.getByPlaceholderText('Message Aether...')).toBeInTheDocument();
  });

  it('sends no second completion when reconnecting after an online Finish', async () => {
    render(<App />);
    await flushPromises();
    connect();

    emitUnfinishedConfig();
    fireEvent.click(screen.getByText('Skip setup'));
    expect(configUpdates()).toHaveLength(1);

    disconnect();
    connect();
    expect(configUpdates()).toHaveLength(1);
  });

  it('sends nothing on reconnect for a user who never finishes, and the wizard stays', async () => {
    render(<App />);
    await flushPromises();

    emitUnfinishedConfig();
    expect(screen.getByTestId('onboarding-overlay')).toBeInTheDocument();

    connect();
    expect(configUpdates()).toHaveLength(0);

    emitEngineMessage({
      type: 'CONFIG_GET_RESPONSE',
      payload: { config: { onboarding_completed: false } },
    });
    expect(screen.getByTestId('onboarding-overlay')).toBeInTheDocument();
  });
});
