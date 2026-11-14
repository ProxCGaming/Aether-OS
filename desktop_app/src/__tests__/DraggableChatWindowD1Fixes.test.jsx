import React from 'react';
import { render, screen, fireEvent, act, within } from '@testing-library/react';
import '@testing-library/jest-dom';
import DraggableChatWindow, { PayloadDrawer, ThinkingAccordion } from '../DraggableChatWindow';

// Wave 3, D1 (DraggableChatWindow.jsx) fixes — failing-first
// evidence for the Pass A D1-C cards (all Pass B CONFIRMED).
// D1-C2 is a DUP of quality-lane Q-11 and is deliberately not
// covered here (single owner: Q-11).

jest.mock('react-markdown', () => ({
  __esModule: true,
  default: ({ children }) => <div data-testid="markdown-stub">{children}</div>,
}));

Element.prototype.scrollIntoView = jest.fn();
global.WebSocket = { OPEN: 1, CLOSED: 3, CONNECTING: 0, CLOSING: 2 };

function renderChat(props = {}) {
  const handlers = {
    onSendMessage: jest.fn(),
    onStopTask: jest.fn(),
    onApproveTool: jest.fn(),
    onApprovePlugin: jest.fn(),
    onChangeModel: jest.fn(),
    onDetach: jest.fn(),
    ...props.handlers,
  };
  delete props.handlers;
  const utils = render(
    <DraggableChatWindow
      messages={[]}
      status="Online"
      isThinking={false}
      providers={[]}
      isFloating={false}
      initialInput=""
      slashCommands={[]}
      isSidebarCollapsed={false}
      {...handlers}
      {...props}
    />
  );
  return { ...utils, handlers };
}

const flushPromises = async () => {
  await act(async () => {});
};

function mockClipboard({ writeImpl, readText = '' } = {}) {
  const clipboard = {
    writeText: jest.fn(writeImpl || (() => Promise.resolve())),
    readText: jest.fn(() => Promise.resolve(readText)),
  };
  Object.defineProperty(window.navigator, 'clipboard', {
    configurable: true,
    value: clipboard,
  });
  return clipboard;
}

describe('D1-C1 approval cards bind to approvals by identity, not tool name', () => {
  it('two pending calls to the same tool approve their OWN approval, in order', () => {
    const agentMessage = {
      role: 'assistant',
      id: 'agent-1',
      content: 'Working on it.',
      isFinal: false,
      thoughts: [
        { type: 'TOOL_ACTIVITY', payload: { tool_name: 'read_file', tool_args: { path: '/a.txt' }, status: 'pending' } },
        { type: 'TOOL_ACTIVITY', payload: { tool_name: 'read_file', tool_args: { path: '/b.txt' }, status: 'pending' } },
      ],
    };
    const approval1 = {
      role: 'tool_approval', id: 'appr-1', tool_name: 'read_file',
      tool_args: { path: '/a.txt' }, approval_key: 'key-1',
    };
    const approval2 = {
      role: 'tool_approval', id: 'appr-2', tool_name: 'read_file',
      tool_args: { path: '/b.txt' }, approval_key: 'key-2',
    };
    const { handlers, container } = renderChat({
      messages: [agentMessage, approval1, approval2],
    });

    const cards = container.querySelectorAll('.tool-activity-card');
    expect(cards).toHaveLength(2);

    // The second card displays /b.txt's call: approving from it must
    // approve the SECOND approval (key-2), not the first.
    fireEvent.click(within(cards[1]).getByRole('button', { name: 'Approve & Execute' }));
    expect(handlers.onApproveTool).toHaveBeenCalledTimes(1);
    expect(handlers.onApproveTool).toHaveBeenCalledWith('appr-2', true, 'key-2');

    // The first card still owns the first approval.
    fireEvent.click(within(cards[0]).getByRole('button', { name: 'Approve & Execute' }));
    expect(handlers.onApproveTool).toHaveBeenCalledTimes(2);
    expect(handlers.onApproveTool).toHaveBeenLastCalledWith('appr-1', true, 'key-1');
  });
});

describe('D1-C3 context-menu Cut/Paste write into the field the menu opened on', () => {
  const approvalMessage = {
    role: 'tool_approval',
    id: 'appr-1',
    tool_name: 'execute_shell',
    tool_args: { command: 'echo hello world' },
    approval_key: 'key-1',
  };

  const openArgsEditor = async () => {
    renderChat({ messages: [approvalMessage] });
    fireEvent.click(screen.getByRole('button', { name: 'Edit arguments' }));
    const editor = await screen.findByTestId('approval-args-editor');
    return editor;
  };

  it('Cut inside the approval-args editor cuts THERE, not into the composer draft', async () => {
    mockClipboard();
    const editor = await openArgsEditor();
    const composer = screen.getByPlaceholderText('Message Aether...');
    const value = editor.value;
    const start = value.indexOf('hello');
    expect(start).toBeGreaterThan(-1);
    editor.setSelectionRange(start, start + 'hello'.length);
    fireEvent.contextMenu(editor);
    fireEvent.click(screen.getByText('Cut'));
    await flushPromises();

    expect(editor.value).toBe(value.slice(0, start) + value.slice(start + 'hello'.length));
    expect(composer.value).toBe('');
  });

  it('Paste inside the approval-args editor pastes THERE, not into the composer draft', async () => {
    mockClipboard({ readText: 'PASTED' });
    const editor = await openArgsEditor();
    const composer = screen.getByPlaceholderText('Message Aether...');
    const value = editor.value;
    const at = value.indexOf('hello');
    editor.setSelectionRange(at, at);
    fireEvent.contextMenu(editor);
    fireEvent.click(screen.getByText('Paste'));
    await flushPromises();

    expect(editor.value).toBe(value.slice(0, at) + 'PASTED' + value.slice(at));
    expect(composer.value).toBe('');
  });
});

describe('D1-C4 outside-click + Escape dismissal are independent per popover', () => {
  it('opening the context popover closes the step picker (its close is not nested inside other checks)', async () => {
    renderChat({ messages: [] });
    fireEvent.click(screen.getByTestId('step-picker-btn'));
    expect(screen.getByTestId('step-picker')).toBeInTheDocument();

    fireEvent.click(screen.getByTestId('context-header-btn'));
    expect(screen.getByTestId('context-bar-panel')).toBeInTheDocument();
    expect(screen.queryByTestId('step-picker')).not.toBeInTheDocument();
  });

  it('Escape closes the context popover and the step picker too', async () => {
    renderChat({ messages: [] });
    fireEvent.click(screen.getByTestId('context-header-btn'));
    expect(screen.getByTestId('context-bar-panel')).toBeInTheDocument();
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.queryByTestId('context-bar-panel')).not.toBeInTheDocument();

    fireEvent.click(screen.getByTestId('step-picker-btn'));
    expect(screen.getByTestId('step-picker')).toBeInTheDocument();
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.queryByTestId('step-picker')).not.toBeInTheDocument();
  });
});

describe('D1-C5 auto-scroll respects a user who scrolled up', () => {
  const baseMessages = [
    { id: 'm1', role: 'user', content: 'one', isFinal: true },
    { id: 'm2', role: 'assistant', content: 'two', isFinal: true },
  ];

  const setFeedMetrics = (feed, { scrollHeight, clientHeight, scrollTop }) => {
    Object.defineProperty(feed, 'scrollHeight', { value: scrollHeight, configurable: true });
    Object.defineProperty(feed, 'clientHeight', { value: clientHeight, configurable: true });
    Object.defineProperty(feed, 'scrollTop', { value: scrollTop, configurable: true, writable: true });
  };

  it('new messages do not drag a scrolled-up reader back to the bottom, and do follow when at the bottom', () => {
    const scrollIntoView = Element.prototype.scrollIntoView;
    scrollIntoView.mockClear();
    const { container, rerender, handlers } = renderChat({ messages: baseMessages });
    const feed = screen.getByText('one').closest('.chat-scroll');
    expect(feed).not.toBeNull();

    // The user scrolls up to read history.
    setFeedMetrics(feed, { scrollHeight: 2000, clientHeight: 400, scrollTop: 0 });
    fireEvent.scroll(feed);
    scrollIntoView.mockClear();

    const withThird = [...baseMessages, { id: 'm3', role: 'assistant', content: 'three', isFinal: true }];
    rerender(
      <DraggableChatWindow
        messages={withThird}
        status="Online"
        isThinking={false}
        providers={[]}
        isFloating={false}
        initialInput=""
        slashCommands={[]}
        isSidebarCollapsed={false}
        {...handlers}
      />
    );
    expect(scrollIntoView).not.toHaveBeenCalled();

    // Back at the bottom: streaming updates follow again.
    setFeedMetrics(feed, { scrollHeight: 2000, clientHeight: 400, scrollTop: 1600 });
    fireEvent.scroll(feed);
    scrollIntoView.mockClear();
    const withFourth = [...withThird, { id: 'm4', role: 'assistant', content: 'four', isFinal: true }];
    rerender(
      <DraggableChatWindow
        messages={withFourth}
        status="Online"
        isThinking={false}
        providers={[]}
        isFloating={false}
        initialInput=""
        slashCommands={[]}
        isSidebarCollapsed={false}
        {...handlers}
      />
    );
    expect(scrollIntoView).toHaveBeenCalled();
    expect(container).toBeTruthy();
  });
});

describe('D1-C6 browser card never presents an unconfirmed control state as settled', () => {
  it('an optimistic Stop reverts when the engine never confirms it', () => {
    jest.useFakeTimers();
    try {
      const onBrowserControl = jest.fn();
      renderChat({
        messages: [],
        browserSession: { controlState: 'agent', currentUrl: 'https://example.com', challenge: null },
        handlers: { onBrowserControl },
      });
      const card = screen.getByTestId('browser-task-card');
      expect(within(card).getByText('Active')).toBeInTheDocument();

      fireEvent.click(screen.getByTestId('browser-stop-btn'));
      expect(onBrowserControl).toHaveBeenCalledWith('stop');
      // Immediate feedback is fine...
      expect(within(card).getByText('Stopped')).toBeInTheDocument();

      // ...but with no engine confirmation it must not stick forever.
      act(() => {
        jest.advanceTimersByTime(6000);
      });
      expect(within(card).getByText('Active')).toBeInTheDocument();
      expect(within(card).queryByText('Stopped')).not.toBeInTheDocument();
    } finally {
      jest.useRealTimers();
    }
  });

  it('an engine-confirmed state is what the card settles on', () => {
    jest.useFakeTimers();
    try {
      const onBrowserControl = jest.fn();
      const { rerender, handlers } = renderChat({
        messages: [],
        browserSession: { controlState: 'agent', currentUrl: 'https://example.com', challenge: null },
        handlers: { onBrowserControl },
      });
      fireEvent.click(screen.getByTestId('browser-pause-btn'));
      expect(onBrowserControl).toHaveBeenCalledWith('pause');

      // The engine confirms: the session prop changes to paused.
      rerender(
        <DraggableChatWindow
          messages={[]}
          status="Online"
          isThinking={false}
          providers={[]}
          isFloating={false}
          initialInput=""
          slashCommands={[]}
          isSidebarCollapsed={false}
          browserSession={{ controlState: 'paused', currentUrl: 'https://example.com', challenge: null }}
          {...handlers}
        />
      );
      const card = screen.getByTestId('browser-task-card');
      expect(within(card).getByText('Paused')).toBeInTheDocument();
      act(() => {
        jest.advanceTimersByTime(6000);
      });
      expect(within(card).getByText('Paused')).toBeInTheDocument();
    } finally {
      jest.useRealTimers();
    }
  });
});

describe('D1-C8 PayloadDrawer Copy only claims success after the clipboard resolves', () => {
  it('a rejected clipboard write shows no "Copied" confirmation', async () => {
    mockClipboard({ writeImpl: () => Promise.reject(new Error('permission denied')) });
    render(<PayloadDrawer title="Payload" data={{ a: 1 }} />);
    fireEvent.click(screen.getByRole('button', { name: /Copy/ }));
    await flushPromises();
    expect(screen.queryByText('Copied')).not.toBeInTheDocument();
  });

  it('a resolved clipboard write shows "Copied"', async () => {
    mockClipboard();
    render(<PayloadDrawer title="Payload" data={{ a: 1 }} />);
    fireEvent.click(screen.getByRole('button', { name: /Copy/ }));
    await flushPromises();
    expect(screen.getByText('Copied')).toBeInTheDocument();
  });
});

describe('D1-C9 message rows keyed by message id survive list cuts', () => {
  it('cutting the first message does not hand its accordion state to the next message', () => {
    const m1 = {
      id: 'm1', role: 'assistant', content: 'one', isFinal: true,
      thoughts: [{ node: 'coder', text: 'thought alpha' }],
    };
    const m2 = {
      id: 'm2', role: 'assistant', content: 'two', isFinal: true,
      thoughts: [{ node: 'coder', text: 'thought beta' }],
    };
    const { rerender, handlers } = renderChat({ messages: [m1, m2] });

    // Expand the FIRST message's pipeline; its thought shows.
    const headers = screen.getAllByText(/Execution Pipeline/);
    fireEvent.click(headers[0]);
    expect(screen.getByText('thought alpha')).toBeInTheDocument();

    // The list is cut (edit-and-resend / rewind): m1 is gone.
    rerender(
      <DraggableChatWindow
        messages={[m2]}
        status="Online"
        isThinking={false}
        providers={[]}
        isFloating={false}
        initialInput=""
        slashCommands={[]}
        isSidebarCollapsed={false}
        {...handlers}
      />
    );
    // m2's accordion keeps its OWN (collapsed) state.
    expect(screen.queryByText('thought beta')).not.toBeInTheDocument();
  });
});

describe('D1-C10 thought visibility is not decided by a hard-coded greeting string', () => {
  it('a thought that merely starts with the greeting text still renders', () => {
    render(
      <ThinkingAccordion
        thoughts={[{ node: 'coder', text: 'Hello! The current time budget for this step is two calls.' }]}
        isThinking={false}
        hasError={false}
      />
    );
    const header = screen.getByText(/Execution Pipeline/);
    fireEvent.click(header);
    expect(screen.getByText(/Hello! The current time budget/)).toBeInTheDocument();
  });

  // S12 replaced the old "structural supervisor-delegation filter"
  // test: it pinned a render-time drop of '[Supervisor] ->
  // Delegating' text thoughts. Since S12 the engine emits
  // delegation ONLY as a structured NODE_ACTIVITY event — the text
  // form no longer exists anywhere in the stream, so the drop was
  // removed and visibility is fully structural. What is pinned now:
  // the NODE_ACTIVITY handoff renders its card (with the S4
  // delegation ordinal), and plain text thoughts — even a
  // supervisor's — render exactly as written.
  it('S12: a NODE_ACTIVITY delegation renders its handoff card and count', () => {
    render(
      <ThinkingAccordion
        thoughts={[
          {
            type: 'NODE_ACTIVITY',
            payload: {
              from_node: 'supervisor',
              to_node: 'coder',
              action: 'delegation',
              reason: 'build it',
              delegation_count: 2,
            },
          },
        ]}
        isThinking={false}
        hasError={false}
      />
    );
    const header = screen.getByText(/Execution Pipeline/);
    fireEvent.click(header);
    expect(screen.getByText('CODER')).toBeInTheDocument();
    expect(screen.getByText('Delegation 2')).toBeInTheDocument();
  });

  it('S12: a plain supervisor text thought renders as written (no prefix drops remain)', () => {
    render(
      <ThinkingAccordion
        thoughts={[{ node: 'supervisor', text: 'Working through the plan step by step.' }]}
        isThinking={false}
        hasError={false}
      />
    );
    const header = screen.getByText(/Execution Pipeline/);
    fireEvent.click(header);
    expect(screen.getByText(/Working through the plan step by step/)).toBeInTheDocument();
  });
});

describe('D1-C11 one mic release fires dictation-stop once', () => {
  it('mouseUp followed by mouseLeave (pointer slides off) stops dictation a single time', () => {
    const onDictationStart = jest.fn();
    const onDictationStop = jest.fn();
    renderChat({ messages: [], handlers: { onDictationStart, onDictationStop } });
    const micBtn = screen.getByTestId('composer-mic-btn');
    fireEvent.mouseDown(micBtn);
    expect(onDictationStart).toHaveBeenCalledTimes(1);
    fireEvent.mouseUp(micBtn);
    fireEvent.mouseLeave(micBtn);
    expect(onDictationStop).toHaveBeenCalledTimes(1);
  });
});
