import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import fs from 'fs';
import path from 'path';
import App from '../App';

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

describe('App', () => {
  it('renders the main navigation', () => {
    render(<App />);
    expect(screen.getByText('Chat')).toBeInTheDocument();
    expect(screen.getByText('Dashboard')).toBeInTheDocument();
    expect(screen.getByText('Projects')).toBeInTheDocument();
    expect(screen.getByText('Settings')).toBeInTheDocument();
  });

  it('switches to the Dashboard tab and back via Start New Workflow', () => {
    render(<App />);

    fireEvent.click(screen.getByText('Dashboard'));
    expect(screen.getByText('System Dashboard')).toBeInTheDocument();

    // Phase 9, B3: the button must actually do something (navigates to Chat).
    fireEvent.click(screen.getByText('Start New Workflow'));
    expect(screen.queryByText('System Dashboard')).not.toBeInTheDocument();
  });

  it('renders the decorative knowledge graph background without fabricated nodes', () => {
    // Phase 9, B3: no random ambient nodes simulating a populated network.
    const { container } = render(<App />);
    expect(screen.getByTestId('force-graph-stub')).toBeInTheDocument();
    expect(container.textContent).not.toMatch(/amb_\d+/);
  });
});

// Phase 10.3 C1: App must use the single SocketContext facade — no second
// mock wsRef, no second onEngineMessage subscription, no local dedup hack —
// and providers must be an array everywhere so MODEL_DEFAULT_CHANGED works.
describe('App socket facade and providers (Phase 10.3 C1)', () => {
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

  const openaiProvider = (overrides = {}) => ({
    name: 'openai',
    display_name: 'OpenAI',
    has_key: true,
    models: ['gpt-4'],
    is_default: true,
    default_model: 'gpt-4',
    ...overrides,
  });

  const anthropicProvider = (overrides = {}) => ({
    name: 'anthropic',
    display_name: 'Anthropic',
    has_key: true,
    models: ['claude-sonnet-4'],
    is_default: false,
    default_model: '',
    ...overrides,
  });

  it('does not build its own engine-message facade (no direct subscription, no local dedup hack)', () => {
    const source = fs.readFileSync(path.join(process.cwd(), 'src', 'App.jsx'), 'utf8');
    // No actual subscription call (comments may name the API, code must not call it).
    expect(source).not.toMatch(/electronAPI\.onEngineMessage/);
    expect(source).not.toContain('lastRawIpcData');
    expect(source).not.toContain('lastRawIpcTime');
  });

  it('MODEL_DEFAULT_CHANGED updates the default model in the chat model picker', async () => {
    render(<App />);
    await flushPromises();

    emitEngineMessage({
      type: 'PROVIDER_LIST_RESPONSE',
      payload: { providers: [openaiProvider(), anthropicProvider()] },
    });
    expect(screen.getByText('gpt-4')).toBeInTheDocument();

    emitEngineMessage({
      type: 'MODEL_DEFAULT_CHANGED',
      payload: { provider: 'anthropic', model: 'claude-sonnet-4' },
    });

    expect(screen.getByText('claude-sonnet-4')).toBeInTheDocument();
    expect(screen.queryByText('gpt-4')).not.toBeInTheDocument();
  });

  it('normalises a provider list that arrives as an object into an array, so MODEL_DEFAULT_CHANGED works on it', async () => {
    render(<App />);
    await flushPromises();

    // The defect shape: providers stored as an object. Children expect an
    // array, and MODEL_DEFAULT_CHANGED silently no-ops on a non-array.
    emitEngineMessage({
      type: 'PROVIDER_LIST_RESPONSE',
      payload: {
        providers: {
          openai: openaiProvider(),
          anthropic: anthropicProvider(),
        },
      },
    });
    expect(screen.getByText('gpt-4')).toBeInTheDocument();

    emitEngineMessage({
      type: 'MODEL_DEFAULT_CHANGED',
      payload: { provider: 'anthropic', model: 'claude-sonnet-4' },
    });

    expect(screen.getByText('claude-sonnet-4')).toBeInTheDocument();
    expect(screen.queryByText('gpt-4')).not.toBeInTheDocument();
  });

  it('queues a model change made while offline and sends it on reconnect', async () => {
    render(<App />);
    await flushPromises();

    emitEngineMessage({
      type: 'PROVIDER_LIST_RESPONSE',
      payload: { providers: [openaiProvider(), anthropicProvider()] },
    });
    expect(screen.getByText('gpt-4')).toBeInTheDocument();

    // Open the model picker and choose the Anthropic model.
    fireEvent.click(screen.getByText('gpt-4'));
    fireEvent.click(screen.getByText('claude-sonnet-4'));

    // Offline: the request must be queued by the single facade, not dropped
    // and not sent into the void.
    expect(electronAPI.sendEngineMessage).not.toHaveBeenCalled();

    emitEngineMessage({ type: '_ws_status', status: 'connected' });
    await flushPromises();

    expect(electronAPI.sendEngineMessage).toHaveBeenCalled();
    const firstSent = JSON.parse(electronAPI.sendEngineMessage.mock.calls[0][0]);
    expect(firstSent.type).toBe('MODEL_SET_DEFAULT');
    expect(firstSent.payload).toEqual({ provider: 'anthropic', model: 'claude-sonnet-4' });
  });
});

// Phase 10.3 C2: App.jsx carried dead state — `messages`, `input`,
// `defaultModel` (setters never called, values never read; the live chat
// state is `chatMessages` / `initialInput`) and `graphData` (a useState
// wrapper around a static constant, setter never called). The dead state
// is gone; the decorative graph still renders from the module constant.
describe('App dead state removed (Phase 10.3 C2)', () => {
  it('App.jsx declares none of the dead state variables', () => {
    const source = fs.readFileSync(path.join(process.cwd(), 'src', 'App.jsx'), 'utf8');
    expect(source).not.toContain('const [messages, setMessages]');
    expect(source).not.toContain('const [input, setInput]');
    expect(source).not.toContain('const [defaultModel, setDefaultModel]');
    expect(source).not.toContain('const [graphData, setGraphData]');
    // No setter calls remain either.
    expect(source).not.toContain('setMessages(');
    expect(source).not.toContain('setDefaultModel(');
    expect(source).not.toContain('setGraphData(');
  });

  it('the decorative graph still renders from the static constant', () => {
    const source = fs.readFileSync(path.join(process.cwd(), 'src', 'App.jsx'), 'utf8');
    expect(source).toContain('graphData={initGraphData}');
    const { container } = render(<App />);
    expect(screen.getByTestId('force-graph-stub')).toBeInTheDocument();
    expect(container.textContent).not.toMatch(/amb_\d+/);
  });

  it('SettingsPanel no longer takes the dead defaultModel prop', () => {
    const source = fs.readFileSync(path.join(process.cwd(), 'src', 'SettingsPanel.jsx'), 'utf8');
    expect(source).not.toMatch(/defaultModel\s*=\s*''/);
    const appSource = fs.readFileSync(path.join(process.cwd(), 'src', 'App.jsx'), 'utf8');
    expect(appSource).not.toContain('defaultModel={defaultModel}');
  });
});

// N06: the artifacts canvas must DOCK beside the chat in the App layout —
// a panel in the layout flow whose space the chat gives up — not an
// absolute overlay floating above the chat.
describe('App canvas docking (N06)', () => {
  beforeEach(() => {
    window.electronAPI = {
      sendEngineMessage: jest.fn(),
      onEngineMessage: jest.fn(() => () => {}),
      getWsStatus: jest.fn(() => Promise.resolve(false)),
      getMemoryUsage: jest.fn(() => Promise.resolve(128)),
      onChatDetached: jest.fn(() => jest.fn()),
      onChatAttached: jest.fn(() => jest.fn()),
      openPath: jest.fn(() => Promise.resolve('')),
    };
  });

  afterEach(() => {
    delete window.electronAPI;
  });

  it('opens the canvas as an in-flow docked panel; the chat reserves space beside it', async () => {
    render(<App />);

    // Never auto-opens: no canvas until the owner asks for it.
    expect(screen.queryByTestId('artifacts-canvas')).not.toBeInTheDocument();

    await act(async () => {
      fireEvent.click(screen.getByTestId('artifacts-header-btn'));
    });

    const canvas = screen.getByTestId('artifacts-canvas');
    expect(canvas).toBeInTheDocument();

    // In the layout flow of the stage (a flex row), not an overlay.
    expect(canvas.style.position).not.toBe('absolute');
    expect(canvas.style.position).not.toBe('fixed');
    expect(canvas.parentElement.style.display).toBe('flex');

    // The chat gives up space for the dock (its right edge stops at
    // 530px while the canvas is open) instead of being covered by it.
    const siblings = Array.from(canvas.parentElement.children);
    const chatPanel = siblings.find((el) => el.style && el.style.right === '530px');
    expect(chatPanel).toBeDefined();
    expect(chatPanel).not.toBe(canvas);
  });
});
