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
// call). Fork stays honestly disabled until E8-1.2, and older tasks'
// steps keep the disabled current-task-only hint.
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

  it('renders no step menu buttons without a manifest', () => {
    renderChat({ messages });
    expect(screen.queryAllByTestId('step-menu-btn')).toHaveLength(0);
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
    expect(within(menu).getByTestId('fork-action')).toBeDisabled();
    expect(within(menu).getByTestId('fork-hint')).toHaveTextContent('E8-1.2');
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
