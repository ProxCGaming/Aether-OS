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

// Epic 1 UI batch: plan-mode toggle (E1-09 UI), regenerate, session
// rename/titles and session search (E1-08), driven through the real
// App + chat window over the SocketContext facade.
describe('App Epic 1 UI batch', () => {
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

  const connect = async () => {
    render(<App />);
    await flushPromises();
    emitEngineMessage({ type: '_ws_status', status: 'connected' });
    await flushPromises();
  };

  const sentPackets = () =>
    electronAPI.sendEngineMessage.mock.calls.map(([raw]) => JSON.parse(raw));

  const packetsOfType = (type) => sentPackets().filter((p) => p.type === type);

  const sendChatText = (text) => {
    const textarea = screen.getByPlaceholderText('Message Aether...');
    fireEvent.change(textarea, { target: { value: text } });
    fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: false });
  };

  const seedSessions = () => {
    emitEngineMessage({
      type: 'SESSION_LIST_RESPONSE',
      payload: {
        sessions: [
          { id: 's1', title: 'Old Title' },
          { id: 's2', title: 'Second Chat' },
        ],
      },
    });
  };

  describe('plan mode toggle (E1-09 UI)', () => {
    it('sends plan_mode:false by default and plan_mode:true when toggled on', async () => {
      await connect();

      sendChatText('plain task');
      let starts = packetsOfType('START_TASK');
      expect(starts).toHaveLength(1);
      expect(starts[0].payload.plan_mode).toBe(false);

      // Finish the task so the next send is a fresh START_TASK.
      emitEngineMessage({
        type: 'TASK_COMPLETED',
        payload: { response: 'done', session_id: 'sess-plan' },
      });

      const toggle = screen.getByRole('button', { name: 'Toggle plan mode' });
      expect(toggle).toHaveAttribute('aria-pressed', 'false');
      expect(toggle).toHaveAttribute('title', 'Plan mode: agents only use read-only tools');
      fireEvent.click(toggle);
      expect(toggle).toHaveAttribute('aria-pressed', 'true');

      sendChatText('planned task');
      starts = packetsOfType('START_TASK');
      expect(starts).toHaveLength(2);
      expect(starts[1].payload.prompt).toBe('planned task');
      expect(starts[1].payload.plan_mode).toBe(true);
    });
  });

  describe('regenerate (E1-08)', () => {
    it('truncates at the last user message and resends the same prompt with an empty composer', async () => {
      await connect();

      sendChatText('do the thing');
      emitEngineMessage({
        type: 'TASK_COMPLETED',
        payload: { response: 'first answer', session_id: 'sess-1' },
      });
      expect(screen.getByText('first answer')).toBeInTheDocument();

      fireEvent.click(screen.getByRole('button', { name: 'Regenerate response' }));

      const truncates = packetsOfType('SESSION_TRUNCATE_REQUEST');
      expect(truncates).toHaveLength(1);
      expect(truncates[0].payload.session_id).toBe('sess-1');
      expect(truncates[0].payload.content).toBe('do the thing');
      expect(truncates[0].payload.role).toBe('user');

      const starts = packetsOfType('START_TASK');
      expect(starts).toHaveLength(2);
      expect(starts[1].payload.prompt).toBe('do the thing');

      // The old exchange is gone and the composer was never filled.
      expect(screen.queryByText('first answer')).not.toBeInTheDocument();
      expect(screen.getByPlaceholderText('Message Aether...')).toHaveValue('');
      // Exactly one user bubble for the resent prompt (the send path's own).
      expect(screen.getAllByText('do the thing')).toHaveLength(1);
    });
  });

  describe('session rename and titles (E1-08)', () => {
    it('commits an inline rename via SESSION_RENAME_REQUEST and applies the confirmed title', async () => {
      await connect();
      seedSessions();
      expect(screen.getByText('Old Title')).toBeInTheDocument();

      fireEvent.click(screen.getAllByRole('button', { name: 'Rename session' })[0]);
      const input = screen.getByLabelText('Session title');
      expect(input).toHaveValue('Old Title');
      fireEvent.change(input, { target: { value: 'New Title' } });
      fireEvent.keyDown(input, { key: 'Enter' });

      const renames = packetsOfType('SESSION_RENAME_REQUEST');
      expect(renames).toHaveLength(1);
      expect(renames[0].payload).toEqual({ session_id: 's1', title: 'New Title' });
      expect(screen.queryByLabelText('Session title')).not.toBeInTheDocument();
      // Not applied locally until the engine confirms.
      expect(screen.getByText('Old Title')).toBeInTheDocument();

      emitEngineMessage({
        type: 'SESSION_RENAME_RESPONSE',
        payload: { session_id: 's1', title: 'New Title', renamed: true },
      });
      expect(screen.getByText('New Title')).toBeInTheDocument();
      expect(screen.queryByText('Old Title')).not.toBeInTheDocument();
    });

    it('Escape cancels a rename without sending anything', async () => {
      await connect();
      seedSessions();

      fireEvent.click(screen.getAllByRole('button', { name: 'Rename session' })[1]);
      const input = screen.getByLabelText('Session title');
      fireEvent.change(input, { target: { value: 'Typed But Discarded' } });
      fireEvent.keyDown(input, { key: 'Escape' });

      expect(packetsOfType('SESSION_RENAME_REQUEST')).toHaveLength(0);
      expect(screen.queryByLabelText('Session title')).not.toBeInTheDocument();
      expect(screen.getByText('Second Chat')).toBeInTheDocument();
    });

    it('applies SESSION_TITLE_UPDATED to the session list', async () => {
      await connect();
      seedSessions();

      emitEngineMessage({
        type: 'SESSION_TITLE_UPDATED',
        payload: { session_id: 's2', title: 'Model Written Title' },
      });
      expect(screen.getByText('Model Written Title')).toBeInTheDocument();
      expect(screen.queryByText('Second Chat')).not.toBeInTheDocument();
    });
  });

  describe('session search (E1-08)', () => {
    it('debounces a SESSION_SEARCH_REQUEST, renders results, and restores the list on clear', async () => {
      await connect();
      seedSessions();

      const search = screen.getByLabelText('Search chats');
      fireEvent.change(search, { target: { value: 'hello' } });
      // Debounced: nothing sent yet.
      expect(packetsOfType('SESSION_SEARCH_REQUEST')).toHaveLength(0);

      await act(async () => {
        await new Promise((r) => setTimeout(r, 400));
      });
      const searches = packetsOfType('SESSION_SEARCH_REQUEST');
      expect(searches).toHaveLength(1);
      expect(searches[0].payload).toEqual({ query: 'hello' });

      emitEngineMessage({
        type: 'SESSION_SEARCH_RESPONSE',
        payload: {
          query: 'hello',
          results: [
            { id: 's9', title: 'Found Chat', updated_at: 1720000000, snippet: 'a matching snippet' },
          ],
        },
      });
      expect(screen.getByText('Found Chat')).toBeInTheDocument();
      expect(screen.getByText('a matching snippet')).toBeInTheDocument();
      expect(screen.queryByText('Old Title')).not.toBeInTheDocument();

      // Clicking a result opens that session.
      fireEvent.click(screen.getByText('Found Chat'));
      const gets = packetsOfType('SESSION_GET_REQUEST');
      expect(gets[gets.length - 1].payload.session_id).toBe('s9');

      // Clearing the query restores the normal list.
      fireEvent.change(search, { target: { value: '' } });
      expect(screen.getByText('Old Title')).toBeInTheDocument();
      expect(screen.queryByText('Found Chat')).not.toBeInTheDocument();
    });
  });
});
