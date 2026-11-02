import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import App from '../App';

// E6-F01: the onboarding overlay, the command palette and the shortcuts
// help are shell-level overlays — they must mount on the default Chat
// tab exactly as on every other tab. They used to be children of the
// `activeTab !== 'Chat'` branch, so on Chat none of them could render:
// no first-run onboarding, Ctrl+K flipped state but showed nothing,
// and an already-open palette/dialog vanished when returning to Chat.

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

describe('App shell overlays mount on every tab (E6-F01)', () => {
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

  const noKeyProvider = (overrides = {}) => ({
    name: 'openai',
    display_name: 'OpenAI',
    has_key: false,
    models: [],
    is_default: true,
    default_model: '',
    ...overrides,
  });

  // The audit's synthetic state: providers loaded, none with a key, and
  // the engine config says onboarding has not been completed.
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
  const goToChat = () => fireEvent.click(screen.getByText('Start New Workflow'));
  const pressCtrlK = () => fireEvent.keyDown(window, { key: 'k', ctrlKey: true });

  it('shows onboarding on the default Chat tab for a no-key, unfinished config', async () => {
    render(<App />);
    await flushPromises();

    emitUnfinishedConfig();

    expect(screen.getByTestId('onboarding-overlay')).toBeInTheDocument();
    expect(screen.getAllByTestId('onboarding-overlay')).toHaveLength(1);
  });

  it('keeps onboarding mounted when switching from Dashboard back to Chat', async () => {
    render(<App />);
    await flushPromises();

    emitUnfinishedConfig();
    goToDashboard();
    expect(screen.getByTestId('onboarding-overlay')).toBeInTheDocument();

    goToChat();
    expect(screen.getByTestId('onboarding-overlay')).toBeInTheDocument();
    expect(screen.getAllByTestId('onboarding-overlay')).toHaveLength(1);
  });

  it('shows no onboarding on any tab when a provider has a key', async () => {
    render(<App />);
    await flushPromises();

    emitEngineMessage({
      type: 'PROVIDER_LIST_RESPONSE',
      payload: { providers: [noKeyProvider({ has_key: true, models: ['gpt-4'], default_model: 'gpt-4' })] },
    });
    emitEngineMessage({
      type: 'CONFIG_GET_RESPONSE',
      payload: { config: { onboarding_completed: false } },
    });

    expect(screen.queryByTestId('onboarding-overlay')).not.toBeInTheDocument();
    goToDashboard();
    expect(screen.queryByTestId('onboarding-overlay')).not.toBeInTheDocument();
  });

  it('shows no onboarding on any tab when onboarding is already completed', async () => {
    render(<App />);
    await flushPromises();

    emitEngineMessage({
      type: 'PROVIDER_LIST_RESPONSE',
      payload: { providers: [noKeyProvider()] },
    });
    emitEngineMessage({
      type: 'CONFIG_GET_RESPONSE',
      payload: { config: { onboarding_completed: true } },
    });

    expect(screen.queryByTestId('onboarding-overlay')).not.toBeInTheDocument();
    goToDashboard();
    expect(screen.queryByTestId('onboarding-overlay')).not.toBeInTheDocument();
  });

  it('opens the command palette with Ctrl+K on the Chat tab', async () => {
    render(<App />);
    await flushPromises();

    pressCtrlK();

    expect(screen.getByTestId('command-palette')).toBeInTheDocument();
    expect(screen.getAllByTestId('command-palette')).toHaveLength(1);
  });

  it('keeps the already-open palette mounted when switching from Dashboard to Chat', async () => {
    render(<App />);
    await flushPromises();

    goToDashboard();
    pressCtrlK();
    expect(screen.getByTestId('command-palette')).toBeInTheDocument();

    goToChat();
    expect(screen.getByTestId('command-palette')).toBeInTheDocument();
    expect(screen.getAllByTestId('command-palette')).toHaveLength(1);
  });

  it('opens shortcuts help on the Chat tab via the palette', async () => {
    render(<App />);
    await flushPromises();

    pressCtrlK();
    fireEvent.click(screen.getByTestId('palette-item-act-shortcuts'));

    expect(screen.getByTestId('shortcuts-dialog')).toBeInTheDocument();
    expect(screen.getAllByTestId('shortcuts-dialog')).toHaveLength(1);
  });

  it('keeps shortcuts help mounted when switching from Dashboard to Chat', async () => {
    render(<App />);
    await flushPromises();

    goToDashboard();
    pressCtrlK();
    fireEvent.click(screen.getByTestId('palette-item-act-shortcuts'));
    expect(screen.getByTestId('shortcuts-dialog')).toBeInTheDocument();

    goToChat();
    expect(screen.getByTestId('shortcuts-dialog')).toBeInTheDocument();
    expect(screen.getAllByTestId('shortcuts-dialog')).toHaveLength(1);
  });

  it('mounts each overlay exactly once on a non-Chat tab', async () => {
    render(<App />);
    await flushPromises();

    emitUnfinishedConfig();
    goToDashboard();
    pressCtrlK();

    expect(screen.getAllByTestId('onboarding-overlay')).toHaveLength(1);
    expect(screen.getAllByTestId('command-palette')).toHaveLength(1);
  });
});
