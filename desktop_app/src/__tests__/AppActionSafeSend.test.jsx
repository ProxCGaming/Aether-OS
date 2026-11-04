import React from 'react';
import { render, screen, fireEvent, act, within } from '@testing-library/react';
import '@testing-library/jest-dom';
import App from '../App';

// Wave 2 slice 2 (RUX-A-01…A-08): the App.jsx user-action sites.
// Before the fix, each of these sends sat behind a
// `readyState === WebSocket.OPEN` guard, so an action taken while
// the socket was down was silently discarded — and several of them
// lied locally while doing it (New Chat cleared the view into the
// old session, the rewind modal closed as if completed, an approval
// decision vanished while its card stayed live). Now every one goes
// through SocketContext's safeSend: transmitted immediately while
// connected, queued (and surfaced by the pending indicator) while
// down, and flushed in order on reconnect.
//
// The harness mirrors AppBrowserControl / ForkSession: the real
// SocketProvider inside App, driven through a mock electronAPI.
// No getEngineState surface is exposed, so the startup gate stays
// inactive (the pre-slice harness behaviour) and the shell renders.

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
  { id: 'u2', role: 'user', content: 'second task', thoughts: [] },
  { id: 'a2', role: 'assistant', content: 'second answer', thoughts: [] },
];

const MANIFEST = {
  session_id: 's1',
  task_id: 'task-2',
  entries: [
    { step_index: 0, checkpoint_id: 'cp0000aaaa', label: 'Before this task began', files_written: [], timestamp: 100 },
    { step_index: 1, checkpoint_id: 'cp1111bbbb', label: 'After planner', files_written: ['notes/out.txt'], timestamp: 101 },
  ],
};

describe('App user actions via safeSend (Wave 2 slice 2)', () => {
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

  const connect = async () => {
    render(<App />);
    await flushPromises();
    emitEngineMessage({ type: '_ws_status', status: 'connected' });
    await flushPromises();
  };

  const disconnect = async () => {
    emitEngineMessage({ type: '_ws_status', status: 'disconnected' });
    await flushPromises();
    electronAPI.sendEngineMessage.mockClear();
  };

  const reconnect = async () => {
    emitEngineMessage({ type: '_ws_status', status: 'connected' });
    await flushPromises();
  };

  const sentPackets = () =>
    electronAPI.sendEngineMessage.mock.calls
      .map(([raw]) => { try { return JSON.parse(raw); } catch (e) { return null; } })
      .filter(Boolean);

  const packetsOfType = (type) => sentPackets().filter((p) => p.type === type);

  // Answer the most recent SESSION_GET_REQUEST the way the engine
  // would: the response echoes its request_id and carries the
  // session's stored history.
  const respondToLastGet = (session) => {
    const gets = packetsOfType('SESSION_GET_REQUEST');
    const req = gets[gets.length - 1];
    expect(req).toBeDefined();
    emitEngineMessage({
      type: 'SESSION_GET_RESPONSE',
      request_id: req.request_id,
      payload: { session },
    });
  };

  // Land in s1 ("First Chat") with its history rendered, with s2
  // ("Second Chat") also listed in the sidebar.
  const seedActiveSession = async () => {
    await connect();
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
  };

  const seedManifest = async () => {
    emitEngineMessage({ type: 'REWIND_MANIFEST_GET_RESPONSE', payload: MANIFEST });
    await flushPromises();
  };

  const expectQueuedPill = () => {
    // The pill's exact wording ("N action(s) queued — …"); the
    // approval cards' queued note shares the tail of the sentence.
    expect(screen.getByText(/actions? queued — will send when the engine reconnects/)).toBeInTheDocument();
  };

  describe('RUX-A-02 New Chat', () => {
    it('offline: does not clear into the old session, queues the create, and the next message lands in the NEW session', async () => {
      await seedActiveSession();
      await disconnect();

      fireEvent.click(screen.getByText('New'));

      // The dishonest outcome is gone: the old session's messages
      // are still on screen and it is still the active session.
      expect(screen.getByText('first answer')).toBeInTheDocument();
      // Nothing was transmitted, and the create is visibly queued.
      expect(packetsOfType('SESSION_CREATE_REQUEST')).toHaveLength(0);
      expectQueuedPill();

      await reconnect();
      const creates = packetsOfType('SESSION_CREATE_REQUEST');
      expect(creates).toHaveLength(1);
      expect(creates[0].payload).toEqual({ title: 'New Conversation' });

      // The engine's reply performs the switch + clear.
      emitEngineMessage({ type: 'SESSION_CREATE_RESPONSE', payload: { session_id: 's9' } });
      await flushPromises();
      expect(screen.queryByText('first answer')).not.toBeInTheDocument();

      // The user's next message goes to the new session, not s1.
      const textarea = screen.getByPlaceholderText('Message Aether...');
      fireEvent.change(textarea, { target: { value: 'hello new session' } });
      fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: false });
      const starts = packetsOfType('START_TASK');
      expect(starts).toHaveLength(1);
      expect(starts[0].payload.prompt).toBe('hello new session');
      expect(starts[0].payload.session_id).toBe('s9');
    });

    it('connected: sends immediately and clears immediately, as before', async () => {
      await seedActiveSession();
      electronAPI.sendEngineMessage.mockClear();

      fireEvent.click(screen.getByText('New'));

      const creates = packetsOfType('SESSION_CREATE_REQUEST');
      expect(creates).toHaveLength(1);
      expect(creates[0].payload).toEqual({ title: 'New Conversation' });
      expect(screen.queryByText('first answer')).not.toBeInTheDocument();
      expect(screen.queryByText(/actions? queued — will send when the engine reconnects/)).not.toBeInTheDocument();
    });
  });

  describe('RUX-A-01 session switch', () => {
    it('offline: the switch fetch is queued and the fresh history replaces the cache on reconnect', async () => {
      await seedActiveSession();
      // Visit s2 once so it has a cached (stale) history.
      fireEvent.click(screen.getByText('Second Chat'));
      respondToLastGet({
        id: 's2', title: 'Second Chat',
        messages: [{ id: 'u9', role: 'user', content: 'other task', thoughts: [] },
                   { id: 'a9', role: 'assistant', content: 'second answer', thoughts: [] }],
      });
      await flushPromises();
      expect(screen.getByText('second answer')).toBeInTheDocument();
      fireEvent.click(screen.getByText('First Chat'));
      respondToLastGet({ id: 's1', title: 'First Chat', messages: MESSAGES_S1 });
      await flushPromises();

      await disconnect();
      fireEvent.click(screen.getByText('Second Chat'));

      // The fetch was NOT discarded: nothing transmitted yet, the
      // queue visibly holds it.
      expect(packetsOfType('SESSION_GET_REQUEST')).toHaveLength(0);
      expectQueuedPill();

      await reconnect();
      const gets = packetsOfType('SESSION_GET_REQUEST');
      expect(gets).toHaveLength(1);
      expect(gets[0].payload).toEqual({ session_id: 's2' });

      // The engine's fresh history (not the cache) is what renders.
      respondToLastGet({
        id: 's2', title: 'Second Chat',
        messages: [{ id: 'u9', role: 'user', content: 'other task', thoughts: [] },
                   { id: 'a9', role: 'assistant', content: 'second answer fresh', thoughts: [] }],
      });
      await flushPromises();
      expect(screen.getByText('second answer fresh')).toBeInTheDocument();
    });
  });

  describe('RUX-C-13/C-16 chat switch + offline states (Wave 3 slice 2)', () => {
    it('switching to an uncached session shows the switching state until its history lands', async () => {
      await seedActiveSession();

      fireEvent.click(screen.getByText('Second Chat'));

      // s2 has no cached copy: while its SESSION_GET is in flight
      // the pane must say it is loading — "No active session"
      // would claim the session does not exist.
      expect(screen.getByTestId('session-switch-loading')).toBeInTheDocument();
      expect(screen.queryByText(/No active session/)).not.toBeInTheDocument();

      respondToLastGet({
        id: 's2', title: 'Second Chat',
        messages: [{ id: 'u9', role: 'user', content: 'other task', thoughts: [] },
                   { id: 'a9', role: 'assistant', content: 'second answer', thoughts: [] }],
      });
      await flushPromises();
      expect(screen.queryByTestId('session-switch-loading')).not.toBeInTheDocument();
      expect(screen.getByText('second answer')).toBeInTheDocument();
    });

    it('a session that answers empty shows its genuine empty state', async () => {
      await seedActiveSession();

      fireEvent.click(screen.getByText('Second Chat'));
      expect(screen.getByTestId('session-switch-loading')).toBeInTheDocument();

      respondToLastGet({ id: 's2', title: 'Second Chat', messages: [] });
      await flushPromises();
      expect(screen.queryByTestId('session-switch-loading')).not.toBeInTheDocument();
      expect(screen.getByText(/No active session/)).toBeInTheDocument();
    });

    it('the chat shows the offline banner while disconnected and clears it on reconnect', async () => {
      await seedActiveSession();
      expect(screen.queryByTestId('chat-offline-banner')).not.toBeInTheDocument();

      await disconnect();
      expect(screen.getByTestId('chat-offline-banner')).toHaveTextContent(/Engine disconnected/);

      await reconnect();
      expect(screen.queryByTestId('chat-offline-banner')).not.toBeInTheDocument();
    });
  });

  describe('RUX-A-07 session delete', () => {
    it('offline: the delete is queued, the row stays until the engine confirms, and it flushes on reconnect', async () => {
      await seedActiveSession();
      await disconnect();

      const row = screen.getByText('Second Chat').closest('.session-item');
      fireEvent.click(within(row).getByTitle('Delete Session'));

      expect(packetsOfType('SESSION_DELETE_REQUEST')).toHaveLength(0);
      expectQueuedPill();
      // No optimistic removal: the row is still listed.
      expect(screen.getByText('Second Chat')).toBeInTheDocument();

      await reconnect();
      const deletes = packetsOfType('SESSION_DELETE_REQUEST');
      expect(deletes).toHaveLength(1);
      expect(deletes[0].payload).toEqual({ session_id: 's2' });

      emitEngineMessage({ type: 'SESSION_DELETE_RESPONSE', payload: { session_id: 's2' } });
      await flushPromises();
      expect(screen.queryByText('Second Chat')).not.toBeInTheDocument();
    });
  });

  describe('RUX-A-04 fork', () => {
    it('offline: a fork click is queued and flushes on reconnect', async () => {
      await seedActiveSession();
      await disconnect();

      fireEvent.click(screen.getAllByTestId('step-menu-btn')[0]);
      fireEvent.click(within(screen.getByTestId('step-menu')).getByTestId('fork-action'));

      expect(packetsOfType('SESSION_FORK_REQUEST')).toHaveLength(0);
      expectQueuedPill();

      await reconnect();
      const forks = packetsOfType('SESSION_FORK_REQUEST');
      expect(forks).toHaveLength(1);
      expect(forks[0].payload).toEqual({
        session_id: 's1', message_id: 'u1', step_index: null, new_title: null,
      });
    });
  });

  describe('RUX-A-03 rewind confirm + undo', () => {
    it('offline confirm keeps the modal open in its queued state; the undo queues the same way', async () => {
      await seedActiveSession();
      await seedManifest();

      // Open the confirm modal for the latest manifest point.
      fireEvent.click(screen.getAllByTestId('step-menu-btn')[3]);
      fireEvent.click(within(screen.getByTestId('step-menu')).getByTestId('rewind-action'));
      expect(screen.getByTestId('rewind-confirm-modal')).toBeInTheDocument();

      await disconnect();
      fireEvent.click(screen.getByTestId('rewind-confirm-btn'));

      // NOT closed as if completed: the modal is still open, in its
      // queued state, and nothing was transmitted.
      expect(screen.getByTestId('rewind-confirm-modal')).toBeInTheDocument();
      expect(screen.getByTestId('rewind-queued-note')).toBeInTheDocument();
      expect(screen.queryByTestId('rewind-confirm-btn')).not.toBeInTheDocument();
      expect(packetsOfType('TASK_REWIND_REQUEST')).toHaveLength(0);

      await reconnect();
      const rewinds = packetsOfType('TASK_REWIND_REQUEST');
      expect(rewinds).toHaveLength(1);
      expect(rewinds[0].payload).toEqual({
        session_id: 's1', task_id: 'task-2', step_index: 1, checkpoint_id: 'cp1111bbbb',
      });

      // The flushed rewind lands: the modal closes and the undo
      // window arms.
      emitEngineMessage({
        type: 'TASK_REWIND_RESPONSE',
        payload: {
          ok: true, session_id: 's1', rewound_to_step: 1,
          undo_expires_at: Date.now() / 1000 + 600,
          messages: MESSAGES_S1.slice(0, 3),
        },
      });
      await flushPromises();
      expect(screen.queryByTestId('rewind-confirm-modal')).not.toBeInTheDocument();
      expect(screen.getByTestId('rewind-undo-toast')).toBeInTheDocument();

      // Undo, offline: queued and flagged, never dropped.
      await disconnect();
      fireEvent.click(screen.getByTestId('rewind-undo-btn'));
      expect(screen.getByTestId('rewind-undo-toast')).toHaveTextContent('Undo queued');
      expect(screen.getByTestId('rewind-undo-btn')).toBeDisabled();
      expect(packetsOfType('TASK_REWIND_UNDO_REQUEST')).toHaveLength(0);

      await reconnect();
      const undos = packetsOfType('TASK_REWIND_UNDO_REQUEST');
      expect(undos).toHaveLength(1);
      expect(undos[0].payload).toEqual({ session_id: 's1' });
    });

    it('connected: confirm closes the modal immediately, as before', async () => {
      await seedActiveSession();
      await seedManifest();
      electronAPI.sendEngineMessage.mockClear();

      fireEvent.click(screen.getAllByTestId('step-menu-btn')[3]);
      fireEvent.click(within(screen.getByTestId('step-menu')).getByTestId('rewind-action'));
      fireEvent.click(screen.getByTestId('rewind-confirm-btn'));

      expect(packetsOfType('TASK_REWIND_REQUEST')).toHaveLength(1);
      expect(screen.queryByTestId('rewind-confirm-modal')).not.toBeInTheDocument();
      expect(screen.queryByTestId('rewind-queued-note')).not.toBeInTheDocument();
    });
  });

  describe('RUX-A-05 approval decisions', () => {
    const emitToolApproval = () => {
      emitEngineMessage({
        type: 'TOOL_APPROVAL_REQUEST',
        request_id: 'req-appr-1',
        payload: {
          tool_name: 'run_command',
          tool_args: { command: 'ls' },
          approval_key: 'key-appr-1',
          preview: null,
        },
      });
    };

    it('offline click: the decision is queued, the card shows it pending and disabled, and it flushes with the approval correlation ids', async () => {
      await connect();
      emitToolApproval();
      await flushPromises();
      expect(screen.getByText('Execution Approval Required')).toBeInTheDocument();

      await disconnect();
      fireEvent.click(screen.getByRole('button', { name: 'Approve Execution' }));

      // The card stays — but as a decided, pending card, not a live
      // one whose decision vanished.
      expect(screen.getByText('Execution Approval Required')).toBeInTheDocument();
      expect(screen.getAllByTestId('approval-decision-queued').length).toBeGreaterThan(0);
      expect(screen.getByRole('button', { name: 'Approve Execution' })).toBeDisabled();
      expect(packetsOfType('TOOL_APPROVAL_GRANTED')).toHaveLength(0);
      expectQueuedPill();

      await reconnect();
      const granted = packetsOfType('TOOL_APPROVAL_GRANTED');
      expect(granted).toHaveLength(1);
      expect(granted[0].request_id).toBe('req-appr-1');
      expect(granted[0].payload.approval_key).toBe('key-appr-1');
      expect(granted[0].payload.approved).toBe(true);
    });

    it('offline Ctrl+Enter: the keyboard path still decides — the decision queues instead of dying with the ref entry', async () => {
      await connect();
      emitEngineMessage({
        type: 'TOOL_APPROVAL_REQUEST',
        request_id: 'req-appr-2',
        payload: {
          tool_name: 'run_command',
          tool_args: { command: 'pwd' },
          approval_key: 'key-appr-2',
          preview: null,
        },
      });
      await flushPromises();

      await disconnect();
      fireEvent.keyDown(window, { key: 'Enter', ctrlKey: true });
      await flushPromises();

      expect(screen.getAllByTestId('approval-decision-queued').length).toBeGreaterThan(0);
      expect(packetsOfType('TOOL_APPROVAL_GRANTED')).toHaveLength(0);

      await reconnect();
      const granted = packetsOfType('TOOL_APPROVAL_GRANTED');
      expect(granted).toHaveLength(1);
      expect(granted[0].request_id).toBe('req-appr-2');
      expect(granted[0].payload.approval_key).toBe('key-appr-2');
    });

    it('connected click: sends immediately and removes the card, as before', async () => {
      await connect();
      emitToolApproval();
      await flushPromises();
      electronAPI.sendEngineMessage.mockClear();

      fireEvent.click(screen.getByRole('button', { name: 'Approve Execution' }));

      const granted = packetsOfType('TOOL_APPROVAL_GRANTED');
      expect(granted).toHaveLength(1);
      expect(granted[0].payload.approval_key).toBe('key-appr-1');
      expect(screen.queryByText('Execution Approval Required')).not.toBeInTheDocument();
      expect(screen.queryByTestId('approval-decision-queued')).not.toBeInTheDocument();
    });

    it('offline plugin approval: flushes with the PLUGIN_APPROVAL_REQUEST id the engine correlates on', async () => {
      await connect();
      emitEngineMessage({
        type: 'PLUGIN_APPROVAL_REQUEST',
        request_id: 'req-plugin-1',
        payload: { plugin_name: 'demo-plugin', plugin_details: { version: '1.0.0' } },
      });
      await flushPromises();
      expect(screen.getByText('Plugin Install Approval Required')).toBeInTheDocument();

      await disconnect();
      fireEvent.click(screen.getByRole('button', { name: 'Approve Install' }));
      expect(screen.getAllByTestId('approval-decision-queued').length).toBeGreaterThan(0);
      expect(packetsOfType('PLUGIN_APPROVAL_GRANTED')).toHaveLength(0);

      await reconnect();
      const granted = packetsOfType('PLUGIN_APPROVAL_GRANTED');
      expect(granted).toHaveLength(1);
      // Load-bearing: the engine pops the pending install by this id.
      expect(granted[0].request_id).toBe('req-plugin-1');
    });
  });
});
