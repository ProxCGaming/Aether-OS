import React from 'react';
import { render, screen, act } from '@testing-library/react';
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

// Phase 10.3 D2: the streaming merge lives in App.jsx's TASK_PROGRESS
// handler (DraggableChatWindow only renders the messages it is given).
// These tests drive real engine packets through the SocketContext facade
// and assert what the chat window actually shows: deltas must append
// into ONE agent bubble, never spawn a bubble per delta, and a packet
// that arrives after the task finished must be dropped, not appended
// to a dead bubble. (Harness copied from App.test.jsx's C1 describe;
// this file is additive — App.test.jsx itself is untouched.)
describe('App streaming merge (Phase 10.3 D2)', () => {
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

  const progress = (textDelta, node = 'assistant') => ({
    type: 'TASK_PROGRESS',
    payload: { text_delta: textDelta, node },
  });

  const agentBubbles = (container) => container.querySelectorAll('.chat-message.agent');

  it('consecutive assistant deltas append into a single agent bubble', () => {
    const { container } = render(<App />);
    expect(screen.getByPlaceholderText('Message Aether...')).toBeInTheDocument();

    emitEngineMessage(progress('Hello '));
    emitEngineMessage(progress('world'));

    const bubbles = agentBubbles(container);
    expect(bubbles).toHaveLength(1);
    expect(bubbles[0].textContent).toContain('Hello world');
    // Exactly one rendered copy of the merged text — no duplicated bubble.
    expect(screen.getAllByText('Hello world')).toHaveLength(1);
  });

  it('deltas after START_TASK merge into the bubble START_TASK created', () => {
    const { container } = render(<App />);

    emitEngineMessage({ type: 'START_TASK', payload: { task_id: 't1' } });
    expect(agentBubbles(container)).toHaveLength(1);
    expect(agentBubbles(container)[0].textContent).not.toContain('Hello');

    emitEngineMessage(progress('Hello '));
    emitEngineMessage(progress('world'));

    expect(agentBubbles(container)).toHaveLength(1);
    expect(agentBubbles(container)[0].textContent).toContain('Hello world');
  });

  it('thought deltas from the same node merge into one thought entry', () => {
    const { container } = render(<App />);

    emitEngineMessage({ type: 'START_TASK', payload: { task_id: 't1' } });
    emitEngineMessage(progress('plan-A ', 'planner'));
    emitEngineMessage(progress('plan-B', 'planner'));

    const thoughts = container.querySelectorAll('.thought-step-content');
    expect(thoughts).toHaveLength(1);
    expect(thoughts[0].textContent).toBe('plan-A plan-B');
    // Still a single agent bubble — thoughts do not spawn bubbles.
    expect(agentBubbles(container)).toHaveLength(1);
  });

  it('a thought delta from a different node starts a second thought entry', () => {
    const { container } = render(<App />);

    emitEngineMessage({ type: 'START_TASK', payload: { task_id: 't1' } });
    emitEngineMessage(progress('plan-A', 'planner'));
    emitEngineMessage(progress('research-B', 'researcher'));

    const thoughts = container.querySelectorAll('.thought-step-content');
    expect(thoughts).toHaveLength(2);
    expect(thoughts[0].textContent).toBe('plan-A');
    expect(thoughts[1].textContent).toBe('research-B');
  });

  it('a progress packet after TASK_COMPLETED is dropped (ghost packet)', () => {
    const { container } = render(<App />);

    emitEngineMessage({ type: 'START_TASK', payload: { task_id: 't1' } });
    emitEngineMessage(progress('Hello '));
    emitEngineMessage({ type: 'TASK_COMPLETED', payload: { task_id: 't1', response: 'Hello done' } });
    expect(agentBubbles(container)[0].textContent).toContain('Hello done');

    emitEngineMessage(progress('GHOST'));

    expect(container.textContent).not.toContain('GHOST');
    expect(agentBubbles(container)).toHaveLength(1);
    expect(agentBubbles(container)[0].textContent).toContain('Hello done');
  });

  it('a non-string delta is stringified instead of dropped', () => {
    const { container } = render(<App />);

    emitEngineMessage(progress({ key: 'value' }));

    expect(agentBubbles(container)).toHaveLength(1);
    expect(agentBubbles(container)[0].textContent).toContain('"key": "value"');
  });

  // S12 replaced this test: it used to assert that a
  // '[Supervisor] -> Delegating' TASK_PROGRESS string was filtered
  // out of the thoughts. Since S12 the engine never sends delegation
  // as text at all — the transition arrives as a structured
  // NODE_ACTIVITY event (with the S4 delegation counter on the
  // payload) and renders as an activity card outside the answer
  // bubble. The contract is now pinned at the source: structured in,
  // card shown, answer bubble clean.
  it('S12: delegation arrives as NODE_ACTIVITY structure, never as answer text', () => {
    const { container } = render(<App />);

    emitEngineMessage({ type: 'START_TASK', payload: { task_id: 't1' } });
    emitEngineMessage({
      type: 'NODE_ACTIVITY',
      payload: {
        task_id: 't1',
        from_node: 'supervisor',
        to_node: 'researcher',
        action: 'delegation',
        reason: 'look it up',
        delegation_count: 1,
        status: 'completed',
      },
    });
    emitEngineMessage(progress('The answer is 42.'));

    // The answer bubble carries only the genuine answer text.
    expect(agentBubbles(container)).toHaveLength(1);
    expect(agentBubbles(container)[0].textContent).toContain('The answer is 42.');
    expect(container.textContent).not.toContain('[Supervisor]');
    // The handoff is visible as activity: the specialist badge and
    // the delegation ordinal from the engine's counter.
    expect(container.textContent).toContain('RESEARCHER');
    expect(container.textContent).toContain('Delegation 1');
  });
});
