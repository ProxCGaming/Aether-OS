import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
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

// Epic 1 display halves: E1-16 THINKING events stream model reasoning
// into the streaming bubble's Thinking section and NEVER into the
// answer content; E1-17 SCRATCHPAD events drive the display-only
// scratchpad panel (latest per session, dismiss + re-show).
describe('App thinking stream + scratchpad (Epic 1, E1-16/E1-17)', () => {
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

  const thoughtTexts = () =>
    [...document.querySelectorAll('.thought-step-content')].map((el) => el.textContent);

  const answerStub = (needle) =>
    [...document.querySelectorAll('[data-testid="markdown-stub"]')]
      .find((el) => el.textContent.includes(needle));

  const connectAndRender = async () => {
    render(<App />);
    await flushPromises();
    emitEngineMessage({ type: '_ws_status', status: 'connected' });
    await flushPromises();
    // Establish the session first (as a real send does): until the
    // session is active, task traffic for it routes to the session
    // cache instead of the visible message list.
    emitEngineMessage({ type: 'SESSION_CREATE_RESPONSE', payload: { session_id: 's1' } });
    await flushPromises();
  };

  it('streams THINKING deltas into thoughts in order, never into the answer content', async () => {
    await connectAndRender();

    emitEngineMessage({ type: 'TASK_STARTED', payload: { task_id: 't1', session_id: 's1' } });
    emitEngineMessage({ type: 'THINKING', payload: { task_id: 't1', node: 'planner', text_delta: 'weighing ' } });
    emitEngineMessage({ type: 'THINKING', payload: { task_id: 't1', node: 'planner', text_delta: 'the options' } });
    emitEngineMessage({ type: 'TASK_PROGRESS', payload: { task_id: 't1', node: 'assistant', text_delta: 'Final answer.' } });

    // Both deltas accumulated, in order, as one thought entry.
    expect(thoughtTexts()).toEqual(['weighing the options']);

    // The answer bubble holds only the answer — no reasoning leaked in.
    const stub = answerStub('Final answer.');
    expect(stub).toBeTruthy();
    expect(stub.textContent).toBe('Final answer.');
    expect(stub.textContent).not.toContain('weighing');
  });

  it('buffers THINKING that arrives before any bubble and flushes it when the task starts', async () => {
    await connectAndRender();

    // Reasoning beats the task start: no bubble exists yet.
    emitEngineMessage({ type: 'THINKING', payload: { task_id: 't9', node: 'coder', text_delta: 'early reasoning' } });
    expect(thoughtTexts()).toEqual([]);

    emitEngineMessage({ type: 'TASK_STARTED', payload: { task_id: 't9', session_id: 's1' } });
    emitEngineMessage({ type: 'TASK_PROGRESS', payload: { task_id: 't9', node: 'assistant', text_delta: 'Done.' } });

    expect(thoughtTexts()).toEqual(['early reasoning']);
    const stub = answerStub('Done.');
    expect(stub.textContent).toBe('Done.');
  });

  it('flushes still-buffered THINKING into the final message on TASK_COMPLETED', async () => {
    await connectAndRender();

    emitEngineMessage({ type: 'THINKING', payload: { task_id: 't7', node: 'researcher', text_delta: 'last-second reasoning' } });
    emitEngineMessage({ type: 'TASK_COMPLETED', payload: { task_id: 't7', session_id: 's1', response: 'Wrapped up.' } });

    const stub = answerStub('Wrapped up.');
    expect(stub).toBeTruthy();
    expect(stub.textContent).toBe('Wrapped up.');

    // The completed message carries the reasoning in its (collapsed)
    // Thinking section — expand it to see the thought text.
    fireEvent.click(screen.getByText(/Execution Pipeline/));
    expect(thoughtTexts()).toEqual(['last-second reasoning']);
  });

  it('shows a SCRATCHPAD svg in the panel, dismisses it, and re-shows on the next event', async () => {
    await connectAndRender();

    emitEngineMessage({
      type: 'SCRATCHPAD',
      payload: { task_id: 't1', kind: 'svg', content: '<svg data-testid="pad-art"><circle r="5" /></svg>', agent: 'coder' },
    });

    expect(screen.getByTestId('scratchpad-panel')).toBeInTheDocument();
    expect(screen.getByText('Scratchpad')).toBeInTheDocument();
    expect(screen.getByText('svg')).toBeInTheDocument();
    expect(screen.getByText('coder')).toBeInTheDocument();
    expect(screen.getByTestId('pad-art')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Dismiss scratchpad' }));
    expect(screen.queryByTestId('scratchpad-panel')).not.toBeInTheDocument();
    expect(screen.queryByTestId('pad-art')).not.toBeInTheDocument();

    // A later event brings the panel back with the new content.
    emitEngineMessage({
      type: 'SCRATCHPAD',
      payload: { task_id: 't1', kind: 'text', content: 'note to self', agent: 'planner' },
    });

    expect(screen.getByTestId('scratchpad-panel')).toBeInTheDocument();
    expect(screen.getByText('text')).toBeInTheDocument();
    expect(screen.getByText('planner')).toBeInTheDocument();
    const pre = document.querySelector('pre.scratchpad-text');
    expect(pre).toBeTruthy();
    expect(pre.textContent).toBe('note to self');
  });
});
