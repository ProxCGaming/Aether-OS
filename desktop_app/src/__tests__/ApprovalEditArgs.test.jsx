import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import DraggableChatWindow, { ToolActivityCard } from '../DraggableChatWindow';
import App from '../App';

// react-markdown is ESM-only; stub it for Jest (same as App.test.jsx).
jest.mock('react-markdown', () => ({
  __esModule: true,
  default: ({ children }) => <div data-testid="markdown-stub">{children}</div>,
}));

// The 3D force graph needs WebGL; replace with a stub for jsdom.
jest.mock('react-force-graph-3d', () => ({
  __esModule: true,
  default: () => <div data-testid="force-graph-stub" />,
}));

// jsdom has no scrollIntoView; stub it.
Element.prototype.scrollIntoView = jest.fn();

// jsdom has no WebSocket; the socket layer only needs the constants.
global.WebSocket = { OPEN: 1, CLOSED: 3, CONNECTING: 0, CLOSING: 2 };

// Epic 8, E8-1.3: the approval drawer lets the user edit argument
// values before approving. A real edit rides onApproveTool as the 5th
// argument (editedArgs); an untouched or unchanged editor keeps the
// exact legacy call shape. The executed tool card wears an
// "edited by you" pill (data-testid="tool-edited-badge").
describe('Approval argument editing (Epic 8, E8-1.3)', () => {
  const approvalMessage = {
    role: 'tool_approval',
    id: 'appr-1',
    tool_name: 'write_file',
    tool_args: { path: '/tmp/x.txt', content: 'hello' },
    approval_key: 'key-123',
  };

  function renderChat(props = {}) {
    const handlers = {
      onSendMessage: jest.fn(),
      onStopTask: jest.fn(),
      onApproveTool: jest.fn(),
      onApprovePlugin: jest.fn(),
      onChangeModel: jest.fn(),
      onDetach: jest.fn(),
      ...(props.handlers || {}),
    };
    const rest = { ...props };
    delete rest.handlers;
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
        {...rest}
      />
    );
    return { ...utils, handlers };
  }

  it('offers an argument editor prefilled with the proposed args', () => {
    renderChat({ messages: [approvalMessage] });
    fireEvent.click(screen.getByTestId('approval-edit-args-btn'));
    const editor = screen.getByTestId('approval-args-editor');
    expect(editor).toBeInTheDocument();
    expect(JSON.parse(editor.value)).toEqual({ path: '/tmp/x.txt', content: 'hello' });
  });

  it('a real edit rides the approve call as editedArgs', () => {
    const { handlers } = renderChat({ messages: [approvalMessage] });
    fireEvent.click(screen.getByTestId('approval-edit-args-btn'));
    fireEvent.change(screen.getByTestId('approval-args-editor'), {
      target: { value: JSON.stringify({ path: '/tmp/x.txt', content: 'edited by hand' }) },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Approve Execution' }));
    expect(handlers.onApproveTool).toHaveBeenCalledTimes(1);
    expect(handlers.onApproveTool).toHaveBeenCalledWith(
      'appr-1', true, 'key-123', undefined,
      { path: '/tmp/x.txt', content: 'edited by hand' });
  });

  it('invalid JSON blocks the approve and shows an error', () => {
    const { handlers } = renderChat({ messages: [approvalMessage] });
    fireEvent.click(screen.getByTestId('approval-edit-args-btn'));
    fireEvent.change(screen.getByTestId('approval-args-editor'), {
      target: { value: '{not json' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Approve Execution' }));
    expect(screen.getByTestId('approval-args-error')).toBeInTheDocument();
    expect(handlers.onApproveTool).not.toHaveBeenCalled();
  });

  it('an opened-but-unchanged editor keeps the legacy approve call', () => {
    const { handlers } = renderChat({ messages: [approvalMessage] });
    fireEvent.click(screen.getByTestId('approval-edit-args-btn'));
    fireEvent.click(screen.getByRole('button', { name: 'Approve Execution' }));
    expect(handlers.onApproveTool).toHaveBeenCalledTimes(1);
    expect(handlers.onApproveTool).toHaveBeenCalledWith('appr-1', true, 'key-123');
  });

  it('the drawer card (ToolActivityCard) edits ride its approve too', () => {
    const onApproveTool = jest.fn();
    render(
      <ToolActivityCard
        payload={{ tool_name: 'write_file', tool_args: { path: '/tmp/x.txt', content: 'hello' }, status: 'pending' }}
        pendingApproval={{ id: 'appr-9', approval_key: 'key-9', tool_args: { path: '/tmp/x.txt', content: 'hello' } }}
        onApproveTool={onApproveTool}
      />
    );
    fireEvent.click(screen.getByTestId('approval-edit-args-btn'));
    fireEvent.change(screen.getByTestId('approval-args-editor'), {
      target: { value: JSON.stringify({ path: '/tmp/x.txt', content: 'drawer edit' }) },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Approve & Execute' }));
    expect(onApproveTool).toHaveBeenCalledTimes(1);
    expect(onApproveTool).toHaveBeenCalledWith(
      'appr-9', true, 'key-9', undefined,
      { path: '/tmp/x.txt', content: 'drawer edit' });
  });

  it('the executed tool card shows the "edited by you" pill only when edited', () => {
    const base = {
      tool_name: 'write_file',
      tool_args: { path: '/tmp/x.txt', content: 'edited by hand' },
      status: 'completed',
      result: 'ok',
    };
    const { rerender } = render(<ToolActivityCard payload={{ ...base, edited: true }} />);
    const badge = screen.getByTestId('tool-edited-badge');
    expect(badge).toBeInTheDocument();
    expect(badge).toHaveTextContent('edited by you');

    rerender(<ToolActivityCard payload={{ ...base }} />);
    expect(screen.queryByTestId('tool-edited-badge')).not.toBeInTheDocument();
  });
});

// App level: the edited args must reach the wire as
// TOOL_APPROVAL_GRANTED payload.edited_args; an unedited approval must
// not carry the key at all (legacy packet shape).
describe('App approval edited_args wire format (Epic 8, E8-1.3)', () => {
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

  const packetsOfType = (type) =>
    electronAPI.sendEngineMessage.mock.calls
      .map(([raw]) => JSON.parse(raw))
      .filter((p) => p.type === type);

  const emitApproval = () => {
    emitEngineMessage({
      type: 'TOOL_APPROVAL_REQUEST',
      request_id: 'req-e8-1',
      payload: {
        tool_name: 'write_file',
        tool_args: { path: '/tmp/x.txt', content: 'hello' },
        approval_key: 'key-e8-1',
      },
    });
  };

  it('sends edited_args on TOOL_APPROVAL_GRANTED after an edit', async () => {
    await connect();
    emitApproval();
    fireEvent.click(screen.getByTestId('approval-edit-args-btn'));
    fireEvent.change(screen.getByTestId('approval-args-editor'), {
      target: { value: JSON.stringify({ path: '/tmp/x.txt', content: 'changed' }) },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Approve Execution' }));
    const granted = packetsOfType('TOOL_APPROVAL_GRANTED');
    expect(granted).toHaveLength(1);
    expect(granted[0].payload.edited_args).toEqual({ path: '/tmp/x.txt', content: 'changed' });
    expect(granted[0].payload.approval_key).toBe('key-e8-1');
  });

  it('omits edited_args entirely for an unedited approval', async () => {
    await connect();
    emitApproval();
    fireEvent.click(screen.getByRole('button', { name: 'Approve Execution' }));
    const granted = packetsOfType('TOOL_APPROVAL_GRANTED');
    expect(granted).toHaveLength(1);
    expect(granted[0].payload).not.toHaveProperty('edited_args');
  });
});
