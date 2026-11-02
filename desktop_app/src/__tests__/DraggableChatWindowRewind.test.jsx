import React from 'react';
import { render, screen, fireEvent, within } from '@testing-library/react';
import '@testing-library/jest-dom';
import DraggableChatWindow, { getRewindPointForMessage } from '../DraggableChatWindow';

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

// Epic 8, E8-1.1a: the step ⋯ menu surfaces the recorded rewind point
// for the CURRENT task's steps (label, checkpoint, files written so
// far). E8-1.1b makes "Rewind to here" live on those points (it hands
// the entry to onRewindRequest; App owns the confirm modal + engine
// call). Older tasks' steps keep the disabled current-task-only hint
// for rewind; fork (E8-1.2, covered in ForkSession.test.jsx) is live
// on every step instead.
describe('DraggableChatWindow rewind step menu (E8-1.1a/1.1b)', () => {
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

  // M12: the step menu must NOT vanish without a manifest — fork
  // from anywhere is promised on every step, and a forked/imported
  // session's inherited history has no manifest at all. Without one
  // the menu offers the message-boundary fork only: no rewind rows,
  // because no points were recorded.
  it('renders step menu buttons without a manifest, fork only', () => {
    const onForkRequest = jest.fn();
    renderChat({ messages, onForkRequest });
    expect(screen.getAllByTestId('step-menu-btn')).toHaveLength(4);

    fireEvent.click(screen.getAllByTestId('step-menu-btn')[0]);
    const menu = screen.getByTestId('step-menu');
    expect(within(menu).queryByTestId('rewind-action')).not.toBeInTheDocument();
    expect(within(menu).queryByTestId('rewind-point-meta')).not.toBeInTheDocument();
    const fork = within(menu).getByTestId('fork-action');
    expect(fork).not.toBeDisabled();
    fireEvent.click(fork);
    expect(onForkRequest).toHaveBeenCalledWith({ message_id: 'u1', step_index: null });
  });

  it('renders one step menu button per user/agent message with a manifest', () => {
    renderChat({ messages, rewindManifest: manifest });
    expect(screen.getAllByTestId('step-menu-btn')).toHaveLength(4);
  });

  it('current task user step shows the opening point metadata, rewind live', () => {
    renderChat({ messages, rewindManifest: manifest });
    fireEvent.click(screen.getAllByTestId('step-menu-btn')[2]);

    const menu = screen.getByTestId('step-menu');
    const meta = within(menu).getByTestId('rewind-point-meta');
    expect(meta).toHaveTextContent('Before this task began');
    expect(meta).toHaveTextContent('cp0000aa');
    expect(within(menu).getByTestId('rewind-action')).not.toBeDisabled();
    expect(within(menu).queryByTestId('rewind-hint')).not.toBeInTheDocument();
    // E8-1.2: fork is live on every step (see ForkSession.test.jsx).
    expect(within(menu).getByTestId('fork-action')).not.toBeDisabled();
    expect(within(menu).queryByTestId('fork-hint')).not.toBeInTheDocument();
  });

  it('current task agent step shows the latest point with files written so far', () => {
    renderChat({ messages, rewindManifest: manifest });
    fireEvent.click(screen.getAllByTestId('step-menu-btn')[3]);

    const menu = screen.getByTestId('step-menu');
    const meta = within(menu).getByTestId('rewind-point-meta');
    expect(meta).toHaveTextContent('After planner');
    expect(meta).toHaveTextContent('notes/out.txt');
    expect(within(menu).getByTestId('rewind-action')).not.toBeDisabled();
  });

  it('clicking Rewind hands the recorded entry to onRewindRequest and closes the menu', () => {
    const onRewindRequest = jest.fn();
    renderChat({ messages, rewindManifest: manifest, onRewindRequest });
    fireEvent.click(screen.getAllByTestId('step-menu-btn')[2]);
    fireEvent.click(within(screen.getByTestId('step-menu')).getByTestId('rewind-action'));

    expect(onRewindRequest).toHaveBeenCalledTimes(1);
    expect(onRewindRequest).toHaveBeenCalledWith(manifest.entries[0]);
    expect(screen.queryByTestId('step-menu')).not.toBeInTheDocument();
  });

  it('older task steps show no point metadata, only the current-task-only hint', () => {
    renderChat({ messages, rewindManifest: manifest });
    fireEvent.click(screen.getAllByTestId('step-menu-btn')[0]);

    const menu = screen.getByTestId('step-menu');
    expect(within(menu).queryByTestId('rewind-point-meta')).not.toBeInTheDocument();
    expect(within(menu).getByTestId('rewind-action')).toBeDisabled();
    expect(within(menu).getByTestId('rewind-hint')).toHaveTextContent(
      'Rewind is available for the current task only');
  });
});

// Epic 8, E8-1.1b: the undo toast renders inside the chat window
// while App holds the staged rewind; its Undo button delegates back.
describe('DraggableChatWindow rewind undo toast (E8-1.1b)', () => {
  const messages = [
    { id: 'u1', role: 'user', content: 'task', isFinal: true, thoughts: [] },
  ];

  it('renders no toast without a staged rewind', () => {
    renderChat({ messages });
    expect(screen.queryByTestId('rewind-undo-toast')).not.toBeInTheDocument();
  });

  it('renders the toast and delegates Undo to onRewindUndo', () => {
    const onRewindUndo = jest.fn();
    renderChat({
      messages,
      rewindUndo: { sessionId: 's1', expiresAt: Date.now() + 5000 },
      onRewindUndo,
    });
    expect(screen.getByTestId('rewind-undo-toast')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('rewind-undo-btn'));
    expect(onRewindUndo).toHaveBeenCalledTimes(1);
  });
});

// Epic 8, M12: the session-header "Rewind / Fork…" step picker
// (PRD Item 1 entry points). The per-message menus can only ever
// name the first/final point; the picker lists the engine's ACTUAL
// manifest steps, and choosing one hands that exact entry (step
// identity included) to App — intermediate graph points included.
describe('DraggableChatWindow header step picker (M12)', () => {
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
      { step_index: 2, checkpoint_id: 'cp2222cccc', label: 'After coder', files_written: ['notes/out.txt', 'src/app.py'], timestamp: 102 },
      { step_index: 3, checkpoint_id: 'cp3333dddd', label: 'Task complete', files_written: ['notes/out.txt', 'src/app.py'], timestamp: 103 },
    ],
  };

  it('offers every manifest step, not just first and final', () => {
    renderChat({ messages, rewindManifest: manifest });
    fireEvent.click(screen.getByTestId('step-picker-btn'));

    const items = screen.getAllByTestId('step-picker-item');
    expect(items).toHaveLength(4);
    expect(items[0]).toHaveTextContent('Before this task began');
    expect(items[1]).toHaveTextContent('After planner');
    expect(items[2]).toHaveTextContent('After coder');
    expect(items[3]).toHaveTextContent('Task complete');
  });

  it('choosing a mid step rewinds with that exact entry', () => {
    const onRewindRequest = jest.fn();
    renderChat({ messages, rewindManifest: manifest, onRewindRequest });
    fireEvent.click(screen.getByTestId('step-picker-btn'));

    const items = screen.getAllByTestId('step-picker-item');
    fireEvent.click(within(items[2]).getByTestId('step-picker-rewind'));
    expect(onRewindRequest).toHaveBeenCalledTimes(1);
    expect(onRewindRequest).toHaveBeenCalledWith(manifest.entries[2]);
    expect(screen.queryByTestId('step-picker')).not.toBeInTheDocument();
  });

  it('choosing a mid step forks with that exact step_index', () => {
    const onForkRequest = jest.fn();
    renderChat({ messages, rewindManifest: manifest, onForkRequest });
    fireEvent.click(screen.getByTestId('step-picker-btn'));

    const items = screen.getAllByTestId('step-picker-item');
    fireEvent.click(within(items[2]).getByTestId('step-picker-fork'));
    expect(onForkRequest).toHaveBeenCalledTimes(1);
    expect(onForkRequest).toHaveBeenCalledWith({ message_id: null, step_index: 2 });
  });

  it('without a manifest the picker offers message-boundary forks', () => {
    const onForkRequest = jest.fn();
    renderChat({ messages, onForkRequest });
    fireEvent.click(screen.getByTestId('step-picker-btn'));

    const rows = screen.getAllByTestId('step-picker-message-item');
    expect(rows).toHaveLength(4);
    fireEvent.click(within(rows[1]).getByTestId('step-picker-fork-message'));
    expect(onForkRequest).toHaveBeenCalledWith({ message_id: 'a1', step_index: null });
  });
});

describe('getRewindPointForMessage mapping', () => {
  const entries = [
    { step_index: 0, checkpoint_id: 'a', label: 'Before this task began', files_written: [], timestamp: 1 },
    { step_index: 1, checkpoint_id: 'b', label: 'After planner', files_written: [], timestamp: 2 },
  ];
  const manifest = { session_id: 's', task_id: 't', entries };

  it('returns null without a manifest', () => {
    expect(getRewindPointForMessage([{ role: 'user' }], 0, null)).toBeNull();
  });

  it('maps the task-starting user message to entry 0 and later steps to the last entry', () => {
    const messages = [
      { role: 'user' }, { role: 'agent' }, { role: 'user' }, { role: 'agent' },
    ];
    expect(getRewindPointForMessage(messages, 2, manifest)).toEqual({ scope: 'current', entry: entries[0] });
    expect(getRewindPointForMessage(messages, 3, manifest)).toEqual({ scope: 'current', entry: entries[1] });
    expect(getRewindPointForMessage(messages, 0, manifest)).toEqual({ scope: 'older', entry: null });
    expect(getRewindPointForMessage(messages, 1, manifest)).toEqual({ scope: 'older', entry: null });
  });

  it('a steering user message does not start a new task', () => {
    const messages = [
      { role: 'user' }, { role: 'agent' }, { role: 'user', steering: true }, { role: 'agent' },
    ];
    expect(getRewindPointForMessage(messages, 2, manifest).scope).toBe('current');
    expect(getRewindPointForMessage(messages, 3, manifest).scope).toBe('current');
  });
});
