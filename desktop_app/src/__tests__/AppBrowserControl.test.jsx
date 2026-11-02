import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import App from '../App';

// Epic 8, E8-6.1 + review finding M04: browser control addressing.
// The desktop owns the CHAT session id; the engine's browser has
// its own session UUID. Before the fix, App sent the chat id in
// the session_id field, which never matched a browser session on
// the engine — every pause / resume / stop silently failed. App
// now sends the chat id in the dedicated chat_session_id field,
// keys browser state by the payload's chat_session_id, and
// remembers the browser session id the engine reports so later
// control messages address the exact browser session.

jest.mock('react-force-graph-3d', () => ({
  __esModule: true,
  default: () => <div data-testid="force-graph-stub" />,
}));

jest.mock('react-markdown', () => ({
  __esModule: true,
  default: ({ children }) => <div data-testid="markdown-stub">{children}</div>,
}));

Element.prototype.scrollIntoView = jest.fn();
global.WebSocket = { OPEN: 1, CLOSED: 3, CONNECTING: 0, CLOSING: 2 };

describe('App browser control addressing (M04)', () => {
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

  const connectAndRender = async () => {
    render(<App />);
    await flushPromises();
    emitEngineMessage({ type: '_ws_status', status: 'connected' });
    await flushPromises();
    emitEngineMessage({ type: 'SESSION_CREATE_RESPONSE', payload: { session_id: 's1' } });
    await flushPromises();
  };

  const sentPayloads = (type) => electronAPI.sendEngineMessage.mock.calls
    .map(([raw]) => { try { return JSON.parse(raw); } catch { return null; } })
    .filter((m) => m && m.type === type)
    .map((m) => m.payload);

  it('a challenge event keys state by chat session and control addresses the browser session', async () => {
    await connectAndRender();
    emitEngineMessage({
      type: 'BROWSER_CHALLENGE_DETECTED',
      payload: {
        session_id: 'browser-uuid-1',
        chat_session_id: 's1',
        task_id: 'task-1',
        url: 'https://example.com/',
        challenge: 'captcha',
      },
    });
    await flushPromises();

    // The card belongs to chat session s1 (state keyed by
    // chat_session_id, not by the browser UUID): it renders in the
    // waiting-for-you state with a resume control.
    const resumeBtn = await screen.findByTestId('browser-resume-btn');
    fireEvent.click(resumeBtn);

    const payloads = sentPayloads('BROWSER_TASK_PAUSE_REQUEST');
    expect(payloads.length).toBeGreaterThan(0);
    expect(payloads[payloads.length - 1]).toEqual({
      chat_session_id: 's1',
      session_id: 'browser-uuid-1',
      action: 'resume',
    });
  });

  it('a control response keyed by chat_session_id updates the card, and stop uses the learned browser id', async () => {
    await connectAndRender();
    emitEngineMessage({
      type: 'BROWSER_TASK_PAUSE_RESPONSE',
      payload: {
        success: true,
        session_id: 'browser-uuid-1',
        chat_session_id: 's1',
        task_id: 'task-1',
        control_state: 'paused',
        current_url: 'https://example.com/',
        challenge: null,
      },
    });
    await flushPromises();

    const stopBtn = await screen.findByTestId('browser-stop-btn');
    fireEvent.click(stopBtn);

    const payloads = sentPayloads('BROWSER_TASK_PAUSE_REQUEST');
    expect(payloads.length).toBeGreaterThan(0);
    expect(payloads[payloads.length - 1]).toEqual({
      chat_session_id: 's1',
      session_id: 'browser-uuid-1',
      action: 'stop',
    });
  });
});
