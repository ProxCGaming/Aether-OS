import React from 'react';
import { render, screen, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import App from '../App';

// E6-F04 (App side): App stored a CONFIG_RESET_RESPONSE only in
// engineConfig. The chat window's model display derives from the
// PROVIDER LIST (the is_default provider's default_model), and
// nothing refetched that list after a reset — Chat kept displaying
// the pre-reset model (gpt-4o-mini) although the engine had reset
// to google_gemini / gemini-3.5-flash. A reset must refetch the
// provider list, like provider save/remove already do.

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

describe('App reset refreshes provider defaults (E6-F04)', () => {
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

  const connect = () => emitEngineMessage({ type: '_ws_status', status: 'connected' });

  const sentPackets = () => electronAPI.sendEngineMessage.mock.calls
    .map(([raw]) => { try { return JSON.parse(raw); } catch (e) { return null; } })
    .filter(Boolean);

  const provider = (overrides = {}) => ({
    name: 'openai',
    display_name: 'OpenAI',
    has_key: true,
    models: ['gpt-4o-mini'],
    is_default: true,
    default_model: 'gpt-4o-mini',
    ...overrides,
  });

  const geminiProvider = (overrides = {}) => provider({
    name: 'google_gemini',
    display_name: 'Google Gemini',
    models: ['gemini-3.5-flash'],
    is_default: false,
    default_model: 'gemini-3.5-flash',
    ...overrides,
  });

  it('refetches the provider list on reset and the chat model follows', async () => {
    render(<App />);
    await flushPromises();
    connect();

    // Pre-reset world: openai is the default, Chat shows gpt-4o-mini.
    emitEngineMessage({
      type: 'PROVIDER_LIST_RESPONSE',
      payload: { providers: [provider(), geminiProvider()] },
    });
    emitEngineMessage({
      type: 'CONFIG_GET_RESPONSE',
      payload: {
        config: {
          onboarding_completed: true,
          default_provider: 'openai',
          default_model: 'gpt-4o-mini',
        },
      },
    });
    expect(screen.getAllByText('gpt-4o-mini').length).toBeGreaterThan(0);

    electronAPI.sendEngineMessage.mockClear();

    // The engine confirms the reset and returns the default config.
    emitEngineMessage({
      type: 'CONFIG_RESET_RESPONSE',
      payload: {
        success: true,
        config: {
          onboarding_completed: true,
          default_provider: 'google_gemini',
          default_model: 'gemini-3.5-flash',
        },
      },
    });

    // The reset must trigger a provider-list refetch.
    expect(sentPackets().map((p) => p.type)).toContain('PROVIDER_LIST_REQUEST');

    // The engine answers with the reset defaults marked as default.
    emitEngineMessage({
      type: 'PROVIDER_LIST_RESPONSE',
      payload: {
        providers: [
          provider({ is_default: false }),
          geminiProvider({ is_default: true }),
        ],
      },
    });

    expect(screen.getAllByText('gemini-3.5-flash').length).toBeGreaterThan(0);
    expect(screen.queryAllByText('gpt-4o-mini')).toHaveLength(0);
  });
});
