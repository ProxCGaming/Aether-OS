import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import App from '../App';

// Wave 5, D3-19 remainder: ArtifactsCanvas has honoured an isLoading
// prop since Wave 3, but App never passed it. These tests pin the
// artifact-content loading lifecycle App now runs: a load starts
// when an ARTIFACT_GET_REQUEST goes out for the selected artifact,
// ends when the ARTIFACT_GET_RESPONSE lands, ends when the session
// context changes underneath it, and resolves itself after the
// no-answer window (35s, the D3-03 rule) so it can never stick.
//
// The harness mirrors AppD1Fixes: the real SocketProvider inside
// App, driven through a mock electronAPI.

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

const MESSAGES_S1 = [
  { id: 'u1', role: 'user', content: 'first task', thoughts: [] },
  { id: 'a1', role: 'assistant', content: 'first answer', thoughts: [] },
];
const MESSAGES_S2 = [
  { id: 'u9', role: 'user', content: 'other task', thoughts: [] },
  { id: 'a9', role: 'assistant', content: 'other answer', thoughts: [] },
];

describe('App D3-19 artifact-content loading lifecycle', () => {
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
    jest.restoreAllMocks();
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

  const sentPackets = () =>
    electronAPI.sendEngineMessage.mock.calls
      .map(([raw]) => { try { return JSON.parse(raw); } catch { return null; } })
      .filter(Boolean);

  const packetsOfType = (type) => sentPackets().filter((p) => p.type === type);

  const respondToLastGet = (session) => {
    const gets = packetsOfType('SESSION_GET_REQUEST');
    const req = gets[gets.length - 1];
    emitEngineMessage({
      type: 'SESSION_GET_RESPONSE',
      request_id: req.request_id,
      payload: { session },
    });
  };

  // Land in s1 with its history rendered and one artifact on the
  // canvas list; the canvas itself is still closed.
  const seedWithArtifact = async () => {
    render(<App />);
    await flushPromises();
    emitEngineMessage({ type: '_ws_status', status: 'connected' });
    await flushPromises();
    emitEngineMessage({
      type: 'SESSION_LIST_RESPONSE',
      payload: {
        sessions: [
          { id: 's1', title: 'First Chat' },
          { id: 's2', title: 'Second Chat' },
        ],
      },
    });
    await flushPromises();
    respondToLastGet({ id: 's1', title: 'First Chat', messages: MESSAGES_S1 });
    await flushPromises();
    expect(screen.getByText('first answer')).toBeInTheDocument();
    emitEngineMessage({
      type: 'ARTIFACT_LIST_RESPONSE',
      request_id: 'list-1',
      payload: { artifacts: [{ id: 'art-1', title: 'notes.txt', kind: 'text' }] },
    });
    await flushPromises();
  };

  const openCanvas = async () => {
    fireEvent.click(screen.getByRole('button', { name: 'Toggle Artifacts Canvas' }));
    await flushPromises();
  };

  it('opening the canvas on an unloaded artifact shows loading until the content lands', async () => {
    await seedWithArtifact();
    await openCanvas();
    // The toggle selected the first artifact and requested it.
    expect(packetsOfType('ARTIFACT_GET_REQUEST')
      .some((p) => p.payload && p.payload.artifact_id === 'art-1')).toBe(true);
    expect(screen.getByTestId('artifact-content-loading')).toBeInTheDocument();

    emitEngineMessage({
      type: 'ARTIFACT_GET_RESPONSE',
      payload: { artifact_id: 'art-1', kind: 'text', content: 'the file body' },
    });
    await flushPromises();
    expect(screen.queryByTestId('artifact-content-loading')).not.toBeInTheDocument();
  });

  it('a load the engine never answers resolves itself after the no-answer window', async () => {
    const realSetTimeout = global.setTimeout;
    const noAnswerTimers = [];
    jest.spyOn(global, 'setTimeout').mockImplementation((cb, ms, ...args) => {
      if (ms === 35000) { noAnswerTimers.push(cb); return 990001; }
      return realSetTimeout(cb, ms, ...args);
    });

    await seedWithArtifact();
    await openCanvas();
    expect(screen.getByTestId('artifact-content-loading')).toBeInTheDocument();
    expect(noAnswerTimers.length).toBeGreaterThan(0);

    // The 35s no-answer window expires with no ARTIFACT_GET_RESPONSE.
    act(() => { noAnswerTimers[noAnswerTimers.length - 1](); });
    await flushPromises();
    expect(screen.queryByTestId('artifact-content-loading')).not.toBeInTheDocument();
  });

  it('switching sessions clears an in-flight load', async () => {
    await seedWithArtifact();
    await openCanvas();
    expect(screen.getByTestId('artifact-content-loading')).toBeInTheDocument();

    fireEvent.click(screen.getByText('Second Chat'));
    await flushPromises();
    respondToLastGet({ id: 's2', title: 'Second Chat', messages: MESSAGES_S2 });
    await flushPromises();
    expect(screen.getByText('other answer')).toBeInTheDocument();
    expect(screen.queryByTestId('artifact-content-loading')).not.toBeInTheDocument();
  });
});
