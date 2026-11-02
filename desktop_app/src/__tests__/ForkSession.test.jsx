import React from 'react';
import { render, screen, fireEvent, within, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import DraggableChatWindow from '../DraggableChatWindow';
import App from '../App';

// Stub 3D force graph for jsdom (same as SessionExportImport.test.jsx).
jest.mock('react-force-graph-3d', () => ({
  __esModule: true,
  default: () => <div data-testid="force-graph-stub" />,
}));

// react-markdown is ESM-only; stub it for Jest (same as the sibling
// DraggableChatWindow*.test.jsx files).
jest.mock('react-markdown', () => ({
  __esModule: true,
  default: ({ children }) => <div data-testid="markdown-stub">{children}</div>,
}));

// jsdom has no scrollIntoView; the chat window calls it on new messages.
Element.prototype.scrollIntoView = jest.fn();

// jsdom has no WebSocket; the socket layer only needs the constants.
global.WebSocket = { OPEN: 1, CLOSED: 3, CONNECTING: 0, CLOSING: 2 };

function renderChat(props = {}) {
  return render(
    <DraggableChatWindow
      messages={[]}
      onSendMessage={jest.fn()}
      onStopTask={jest.fn()}
      status="Online"
      isThinking={false}
      onApproveTool={jest.fn()}
      onApprovePlugin={jest.fn()}
      providers={[]}
      onChangeModel={jest.fn()}
      onDetach={jest.fn()}
      isFloating={false}
      initialInput=""
      slashCommands={[]}
      isSidebarCollapsed={false}
      {...props}
    />
  );
}

// Epic 8, E8-1.2: "Fork from here" is live on EVERY step — the last
// task's steps fork by manifest step_index (the engine copies the
// checkpoint thread up to that point), older tasks' steps fork by
// message id. This is the deliberate contrast with rewind in the
// same menu: rewind stays current-task-only, fork does not.
describe('DraggableChatWindow fork step menu (E8-1.2)', () => {
  const messages = [
    { id: 'u1', role: 'user', content: 'first task', isFinal: true, thoughts: [] },
    { id: 'a1', role: 'agent', content: 'first answer', isFinal: true, thoughts: [] },
    { id: 'u2', role: 'user', content: 'second task', isFinal: true, thoughts: [] },
    { id: 'a2', role: 'agent', content: 'second answer', isFinal: true, thoughts: [] },
  ];
  const manifest = {
    session_id: 's1',
    task_id: 'task-2',
    entries: [
      { step_index: 0, checkpoint_id: 'cp0000aaaa', label: 'Before this task began', files_written: [], timestamp: 100 },
      { step_index: 1, checkpoint_id: 'cp1111bbbb', label: 'After planner', files_written: ['notes/out.txt'], timestamp: 101 },
    ],
  };

  it('fork is live on an older task step (message boundary) where rewind is disabled', () => {
    const onForkRequest = jest.fn();
    renderChat({ messages, rewindManifest: manifest, onForkRequest });
    fireEvent.click(screen.getAllByTestId('step-menu-btn')[0]);

    const menu = screen.getByTestId('step-menu');
    expect(within(menu).getByTestId('rewind-action')).toBeDisabled();
    const fork = within(menu).getByTestId('fork-action');
    expect(fork).not.toBeDisabled();
    expect(within(menu).queryByTestId('fork-hint')).not.toBeInTheDocument();

    fireEvent.click(fork);
    expect(onForkRequest).toHaveBeenCalledTimes(1);
    expect(onForkRequest).toHaveBeenCalledWith({ message_id: 'u1', step_index: null });
    expect(screen.queryByTestId('step-menu')).not.toBeInTheDocument();
  });

  it('fork on the current task opening step sends its step_index', () => {
    const onForkRequest = jest.fn();
    renderChat({ messages, rewindManifest: manifest, onForkRequest });
    fireEvent.click(screen.getAllByTestId('step-menu-btn')[2]);

    fireEvent.click(within(screen.getByTestId('step-menu')).getByTestId('fork-action'));
    expect(onForkRequest).toHaveBeenCalledWith({ message_id: 'u2', step_index: 0 });
  });

  it('fork on the current task latest step sends the final step_index', () => {
    const onForkRequest = jest.fn();
    renderChat({ messages, rewindManifest: manifest, onForkRequest });
    fireEvent.click(screen.getAllByTestId('step-menu-btn')[3]);

    const fork = within(screen.getByTestId('step-menu')).getByTestId('fork-action');
    expect(fork).not.toBeDisabled();
    fireEvent.click(fork);
    expect(onForkRequest).toHaveBeenCalledWith({ message_id: 'a2', step_index: 1 });
  });
});

// App owns the wire: a fork request goes out as SESSION_FORK_REQUEST
// naming the active session, and a successful SESSION_FORK_RESPONSE
// refreshes the session list and switches into the new session.
describe('App session fork wiring (E8-1.2)', () => {
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

  const connectApp = async () => {
    render(<App />);
    await flushPromises();
    emitEngineMessage({ type: '_ws_status', status: 'connected' });
    await flushPromises();
  };

  const sentPackets = () =>
    electronAPI.sendEngineMessage.mock.calls.map(([raw]) => JSON.parse(raw));

  it('handles SESSION_FORK_RESPONSE success by refreshing the list and switching to the fork', async () => {
    await connectApp();
    electronAPI.sendEngineMessage.mockClear();

    emitEngineMessage({
      type: 'SESSION_FORK_RESPONSE',
      payload: {
        ok: true,
        new_session_id: 'fork-sess-1',
        source_session_id: 'sess-1',
        title: 'Fork of Chat',
        messages_copied: 3,
        error: null,
      },
    });
    await flushPromises();

    const packets = sentPackets();
    expect(packets.some((p) => p.type === 'SESSION_LIST_REQUEST')).toBe(true);
    const getReq = packets.find((p) => p.type === 'SESSION_GET_REQUEST');
    expect(getReq).toBeDefined();
    expect(getReq.payload.session_id).toBe('fork-sess-1');
  });

  it('renders the engine-returned retained history after a rewind (M12)', async () => {
    await connectApp();
    // Land in a session and load its stored history.
    emitEngineMessage({
      type: 'SESSION_LIST_RESPONSE',
      payload: { sessions: [{ id: 'sess-1', title: 'Chat' }] },
    });
    await flushPromises();
    const getReq = sentPackets().find((p) => p.type === 'SESSION_GET_REQUEST');
    expect(getReq).toBeDefined();
    const stored = [
      { id: 'u1', role: 'user', content: 'first task', thoughts: [] },
      { id: 'a1', role: 'assistant', content: 'first answer', thoughts: [] },
      { id: 'u2', role: 'user', content: 'second task', thoughts: [] },
      { id: 'a2', role: 'assistant', content: 'second answer', thoughts: [] },
    ];
    emitEngineMessage({
      type: 'SESSION_GET_RESPONSE',
      request_id: getReq.request_id,
      payload: { session: { id: 'sess-1', title: 'Chat', messages: stored } },
    });
    await flushPromises();
    expect(screen.getByText('second answer')).toBeInTheDocument();

    // A rewind to the FINAL point discards no stored messages: the
    // engine's retained history still holds the response, and the
    // desktop must render exactly that — not its own structural cut
    // (which used to remove the response anyway).
    emitEngineMessage({
      type: 'TASK_REWIND_RESPONSE',
      payload: {
        ok: true,
        session_id: 'sess-1',
        rewound_to_step: 2,
        undo_expires_at: Date.now() / 1000 + 3600,
        files_not_reverted: [],
        error: null,
        messages: stored,
      },
    });
    await flushPromises();
    expect(screen.getByText('second answer')).toBeInTheDocument();
    expect(screen.getByText('second task')).toBeInTheDocument();

    // A rewind to step 0 renders the engine's prefix verbatim.
    emitEngineMessage({
      type: 'TASK_REWIND_RESPONSE',
      payload: {
        ok: true,
        session_id: 'sess-1',
        rewound_to_step: 0,
        undo_expires_at: Date.now() / 1000 + 3600,
        files_not_reverted: [],
        error: null,
        messages: stored.slice(0, 2),
      },
    });
    await flushPromises();
    expect(screen.queryByText('second task')).not.toBeInTheDocument();
    expect(screen.queryByText('second answer')).not.toBeInTheDocument();
    expect(screen.getByText('first answer')).toBeInTheDocument();
  });

  it('handles SESSION_FORK_RESPONSE failure without switching sessions', async () => {
    await connectApp();
    electronAPI.sendEngineMessage.mockClear();

    emitEngineMessage({
      type: 'SESSION_FORK_RESPONSE',
      payload: {
        ok: false,
        new_session_id: '',
        source_session_id: 'sess-1',
        title: '',
        messages_copied: 0,
        error: 'Fork point not found for this session.',
      },
    });
    await flushPromises();

    const packets = sentPackets();
    expect(packets.some((p) => p.type === 'SESSION_GET_REQUEST')).toBe(false);
  });
});
