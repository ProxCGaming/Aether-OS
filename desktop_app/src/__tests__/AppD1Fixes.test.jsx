import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import App from '../App';

// Wave 3, D1 (App.jsx) fixes — failing-first evidence for the Pass A
// D1-A cards, Pass B verdicts applied (A7 downgraded to S4: honour
// the send disposition in handleStopTask; the facade already holds a
// queued CANCEL_TASK for reconfirmation).
//
// The harness mirrors AppActionSafeSend: the real SocketProvider
// inside App, driven through a mock electronAPI.

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

describe('App D1 fixes', () => {
  let engineListeners;
  let electronAPI;
  let mockAudioContext;

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

    const mockSource = {
      connect: jest.fn(),
      start: jest.fn(),
      stop: jest.fn(),
      onended: null,
    };
    mockAudioContext = {
      currentTime: 0,
      destination: {},
      createBufferSource: jest.fn(() => mockSource),
      decodeAudioData: jest.fn(() => Promise.resolve({ duration: 1 })),
      close: jest.fn(),
    };
    window.AudioContext = jest.fn(() => mockAudioContext);
    window.webkitAudioContext = window.AudioContext;
  });

  afterEach(() => {
    delete window.electronAPI;
    delete window.AudioContext;
    delete window.webkitAudioContext;
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

  const connect = async (ui) => {
    const utils = render(ui || <App />);
    await flushPromises();
    emitEngineMessage({ type: '_ws_status', status: 'connected' });
    await flushPromises();
    return utils;
  };

  const sentPackets = () =>
    electronAPI.sendEngineMessage.mock.calls
      .map(([raw]) => { try { return JSON.parse(raw); } catch { return null; } })
      .filter(Boolean);

  const packetsOfType = (type) => sentPackets().filter((p) => p.type === type);

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

  // Land in s1 ("First Chat") with its history rendered; s2 ("Second
  // Chat") is also listed in the sidebar.
  const seedActiveSession = async (ui) => {
    const utils = await connect(ui);
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
    return utils;
  };

  const sendComposerMessage = async (text) => {
    const textarea = screen.getByPlaceholderText('Message Aether...');
    fireEvent.change(textarea, { target: { value: text } });
    fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: false });
    await flushPromises();
  };

  describe('D1-A1 stale closures in the once-registered handler', () => {
    it('SESSION_GET_RESPONSE merges back the in-flight streaming bubble after a switch away and back', async () => {
      await seedActiveSession();
      await sendComposerMessage('stream something');
      // The engine echoes the task start; the task session is s1.
      emitEngineMessage({
        type: 'START_TASK',
        request_id: 'task-req-1',
        payload: { task_id: 't1', session_id: 's1' },
      });
      await flushPromises();

      // Switch to s2 and back to s1 while the task is still running.
      fireEvent.click(screen.getByText('Second Chat'));
      await flushPromises();
      respondToLastGet({ id: 's2', title: 'Second Chat', messages: MESSAGES_S2 });
      await flushPromises();
      expect(screen.getByText('other answer')).toBeInTheDocument();

      fireEvent.click(screen.getByText('First Chat'));
      await flushPromises();
      // The stored history has no trace of the still-streaming bubble.
      respondToLastGet({ id: 's1', title: 'First Chat', messages: MESSAGES_S1 });
      await flushPromises();

      // The bubble must survive the reload (merge-back), not vanish.
      expect(screen.getByText('Agent is thinking...')).toBeInTheDocument();
    });

    it('ARTIFACT_RECORDED appends live to the canvas of the session opened after mount', async () => {
      await seedActiveSession();
      expect(screen.getByTestId('artifacts-badge')).toHaveTextContent('0');
      emitEngineMessage({
        type: 'ARTIFACT_RECORDED',
        payload: {
          session_id: 's1',
          artifact_id: 'art-1',
          task_id: 't1',
          kind: 'file',
          title: 'notes.txt',
          path: '/tmp/notes.txt',
          created_at: 1,
        },
      });
      await flushPromises();
      expect(screen.getByTestId('artifacts-badge')).toHaveTextContent('1');
    });

    it('ARTIFACT_LIST_RESPONSE restores the canvas selection saved for the CURRENT session', async () => {
      await seedActiveSession();
      localStorage.setItem('aether_canvas_selected_s1', 'art-9');
      electronAPI.sendEngineMessage.mockClear();
      emitEngineMessage({
        type: 'ARTIFACT_LIST_RESPONSE',
        request_id: 'list-1',
        payload: { artifacts: [{ id: 'art-9', title: 'kept.txt' }] },
      });
      await flushPromises();
      const gets = packetsOfType('ARTIFACT_GET_REQUEST');
      expect(gets.some((p) => p.payload && p.payload.artifact_id === 'art-9')).toBe(true);
      localStorage.removeItem('aether_canvas_selected_s1');
    });
  });

  describe('D1-A1/A2 SESSION_DELETE_RESPONSE', () => {
    it('deleting the viewed session switches to the next one, exactly once, with no side effects inside the state updater', async () => {
      await seedActiveSession(
        <React.StrictMode>
          <App />
        </React.StrictMode>
      );
      const deleteBtn = screen.getAllByTitle('Delete Session')[0];
      fireEvent.click(deleteBtn);
      await flushPromises();
      expect(packetsOfType('SESSION_DELETE_REQUEST')).toHaveLength(1);

      electronAPI.sendEngineMessage.mockClear();
      emitEngineMessage({
        type: 'SESSION_DELETE_RESPONSE',
        payload: { session_id: 's1' },
      });
      await flushPromises();

      // The view switches to s2: exactly one history fetch for it,
      // even under StrictMode's double-invoked updaters.
      const gets = packetsOfType('SESSION_GET_REQUEST')
        .filter((p) => p.payload && p.payload.session_id === 's2');
      expect(gets).toHaveLength(1);
      respondToLastGet({ id: 's2', title: 'Second Chat', messages: MESSAGES_S2 });
      await flushPromises();
      expect(screen.getByText('other answer')).toBeInTheDocument();
      expect(screen.queryByText('First Chat')).not.toBeInTheDocument();
    });
  });

  describe('D1-A3 export outcome is rendered', () => {
    const exportAsJson = async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Export Session' }));
      fireEvent.click(screen.getByText('JSON (.json)'));
      await flushPromises();
      expect(packetsOfType('SESSION_EXPORT_REQUEST').length).toBeGreaterThan(0);
    };

    it('an export refusal shows a visible error instead of silence', async () => {
      await seedActiveSession();
      await exportAsJson();
      emitEngineMessage({
        type: 'SESSION_EXPORT_RESPONSE',
        payload: { error: 'disk is full' },
      });
      await flushPromises();
      expect(screen.getByTestId('session-export-status')).toHaveTextContent('Export failed: disk is full');
    });

    it('a successful export shows a confirmation', async () => {
      await seedActiveSession();
      await exportAsJson();
      emitEngineMessage({
        type: 'SESSION_EXPORT_RESPONSE',
        payload: { filename: 'chat.json', format: 'json' },
      });
      await flushPromises();
      expect(screen.getByTestId('session-export-status')).toHaveTextContent('Exported chat.json');
    });
  });

  describe('D1-A4 read-aloud end + decode failures', () => {
    const startReadAloud = async () => {
      emitEngineMessage({
        type: 'TASK_COMPLETED',
        payload: { session_id: 's1', task_id: 't1', result: 'Done speaking.' },
      });
      await flushPromises();
      const readBtn = screen.getByTestId(/read-aloud-btn-/);
      fireEvent.click(readBtn);
      await flushPromises();
      const reqs = packetsOfType('READ_ALOUD_START_REQUEST');
      expect(reqs).toHaveLength(1);
      const sentMsgId = reqs[0].payload.message_id;
      emitEngineMessage({
        type: 'READ_ALOUD_START_RESPONSE',
        payload: { ok: true, message_id: sentMsgId },
      });
      await flushPromises();
      expect(readBtn).toHaveAttribute('aria-pressed', 'true');
      return { readBtn, sentMsgId };
    };

    it('the final chunk returns the read-aloud button to idle', async () => {
      await seedActiveSession();
      const { readBtn, sentMsgId } = await startReadAloud();
      emitEngineMessage({
        type: 'READ_ALOUD_CHUNK',
        payload: { message_id: sentMsgId, is_final: true },
      });
      await flushPromises();
      expect(readBtn).toHaveAttribute('aria-pressed', 'false');
      expect(readBtn).toHaveAttribute('title', 'Read aloud');
    });

    it('a chunk that fails to decode surfaces an error instead of an unhandled rejection', async () => {
      await seedActiveSession();
      mockAudioContext.decodeAudioData.mockImplementation(() => Promise.reject(new Error('bad audio')));
      const { sentMsgId } = await startReadAloud();
      const b64Wav = window.btoa('RIFF....WAVEfmt ');
      emitEngineMessage({
        type: 'READ_ALOUD_CHUNK',
        payload: { message_id: sentMsgId, audio_chunk: b64Wav, is_final: false },
      });
      await flushPromises();
      await flushPromises();
      // The banner renders once per matching bubble surface; the
      // card's requirement is that the failure is surfaced at all
      // (previously: an unhandled rejection and silence).
      const banners = screen.getAllByTestId('read-aloud-error');
      expect(banners.length).toBeGreaterThan(0);
      expect(banners[0]).toHaveTextContent(/could not be decoded/i);
    });
  });

  describe('D1-A5 approval shortcuts die with their task', () => {
    it('Ctrl+Enter acts on a live approval but not on one orphaned by a failed task', async () => {
      await seedActiveSession();
      await sendComposerMessage('do the thing');

      emitEngineMessage({
        type: 'TOOL_APPROVAL_REQUEST',
        request_id: 'appr-1',
        payload: { tool_name: 'execute_shell', tool_args: { command: 'ls' }, approval_key: 'key-1' },
      });
      await flushPromises();
      fireEvent.keyDown(window, { key: 'Enter', ctrlKey: true });
      await flushPromises();
      expect(packetsOfType('TOOL_APPROVAL_GRANTED')).toHaveLength(1);
      expect(packetsOfType('TOOL_APPROVAL_GRANTED')[0].payload.approval_key).toBe('key-1');

      // A second approval arrives, then its task fails undecided.
      emitEngineMessage({
        type: 'TOOL_APPROVAL_REQUEST',
        request_id: 'appr-2',
        payload: { tool_name: 'execute_shell', tool_args: { command: 'pwd' }, approval_key: 'key-2' },
      });
      await flushPromises();
      emitEngineMessage({
        type: 'TASK_FAILED',
        payload: { task_id: 't1', session_id: 's1', error: 'boom' },
      });
      await flushPromises();

      fireEvent.keyDown(window, { key: 'Enter', ctrlKey: true });
      await flushPromises();
      // The stale approval must NOT be granted in a later context.
      expect(packetsOfType('TOOL_APPROVAL_GRANTED')).toHaveLength(1);
    });
  });

  describe('D1-A6 tool results route to the call that produced them', () => {
    it('the first result finalises the FIRST pending call of that tool, not the last', async () => {
      await seedActiveSession();
      await sendComposerMessage('read both files');
      const activity = (args, status, result) => ({
        type: 'TOOL_ACTIVITY',
        payload: {
          tool_name: 'read_file',
          tool_args: args,
          status,
          ...(result ? { result } : {}),
          node: 'coder',
        },
      });
      emitEngineMessage(activity({ path: '/a.txt' }, 'pending'));
      emitEngineMessage(activity({ path: '/b.txt' }, 'pending'));
      await flushPromises();
      emitEngineMessage(activity({ path: '/a.txt' }, 'completed', 'A-RESULT'));
      await flushPromises();

      const cards = document.querySelectorAll('.tool-activity-card');
      expect(cards).toHaveLength(2);
      expect(cards[0].textContent).toContain('A-RESULT');
      expect(cards[1].textContent).not.toContain('A-RESULT');
      expect(cards[1].textContent).toContain('awaiting output payload');
    });
  });

  describe('D1-A8 fork / rewind refusals are visible', () => {
    it('a fork refusal appears in the chat instead of only the console', async () => {
      await seedActiveSession();
      emitEngineMessage({
        type: 'SESSION_FORK_RESPONSE',
        payload: { ok: false, error: 'fork is not available for this session' },
      });
      await flushPromises();
      expect(screen.getByText(/Fork refused: fork is not available for this session/)).toBeInTheDocument();
    });

    it('a rewind refusal appears in the chat instead of only the console', async () => {
      await seedActiveSession();
      emitEngineMessage({
        type: 'TASK_REWIND_RESPONSE',
        payload: { session_id: 's1', ok: false, error: 'no checkpoint recorded' },
      });
      await flushPromises();
      expect(screen.getByText(/Rewind refused: no checkpoint recorded/)).toBeInTheDocument();
    });
  });

  describe('D1-A9 payload-less frames do not kill dispatch', () => {
    it('hello / SESSION_LIST_RESPONSE / TASK_PROGRESS without a payload are survived', async () => {
      render(<App />);
      await flushPromises();
      expect(() => emitEngineMessage({ type: 'hello' })).not.toThrow();
      await flushPromises();
      expect(screen.getByText('Online / Agent')).toBeInTheDocument();
      expect(() => emitEngineMessage({ type: 'SESSION_LIST_RESPONSE' })).not.toThrow();
      expect(() => emitEngineMessage({ type: 'TASK_PROGRESS' })).not.toThrow();
      await flushPromises();
      // The handler is still alive for well-formed frames afterwards.
      emitEngineMessage({
        type: 'SESSION_LIST_RESPONSE',
        payload: { sessions: [{ id: 's1', title: 'First Chat' }] },
      });
      await flushPromises();
      expect(screen.getByText('First Chat')).toBeInTheDocument();
    });
  });
});
