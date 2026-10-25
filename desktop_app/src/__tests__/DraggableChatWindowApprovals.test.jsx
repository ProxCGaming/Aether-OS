import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import DraggableChatWindow, { ToolActivityCard } from '../DraggableChatWindow';

// react-markdown is ESM-only; stub it for Jest (same as App.test.jsx).
jest.mock('react-markdown', () => ({
  __esModule: true,
  default: ({ children }) => <div data-testid="markdown-stub">{children}</div>,
}));

// jsdom has no scrollIntoView; stub it.
Element.prototype.scrollIntoView = jest.fn();

// jsdom has no WebSocket; the socket layer only needs the constants.
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

// Phase 10.3 D2: the approval cards are the trust boundary's UI — the
// buttons must call back with the exact approval id, decision, and
// approval_key, so the engine can match the decision to the pending
// request. A swapped or dropped argument would approve the wrong tool
// call (or approve when the user meant reject).
describe('DraggableChatWindow tool approval card (Phase 10.3 D2)', () => {
  const approvalMessage = {
    role: 'tool_approval',
    id: 'appr-1',
    tool_name: 'execute_shell',
    tool_args: { command: 'rm -rf /tmp/x' },
    approval_key: 'key-123',
  };

  it('renders the tool name and its arguments', () => {
    renderChat({ messages: [approvalMessage] });
    expect(screen.getByText('Execution Approval Required')).toBeInTheDocument();
    expect(screen.getByText('execute_shell')).toBeInTheDocument();
    expect(screen.getByText(/rm -rf \/tmp\/x/)).toBeInTheDocument();
  });

  it('Approve calls onApproveTool with (id, true, approval_key)', () => {
    const { handlers } = renderChat({ messages: [approvalMessage] });
    fireEvent.click(screen.getByRole('button', { name: 'Approve Execution' }));
    expect(handlers.onApproveTool).toHaveBeenCalledTimes(1);
    expect(handlers.onApproveTool).toHaveBeenCalledWith('appr-1', true, 'key-123');
  });

  it('Reject calls onApproveTool with (id, false, approval_key)', () => {
    const { handlers } = renderChat({ messages: [approvalMessage] });
    fireEvent.click(screen.getByRole('button', { name: 'Reject Request' }));
    expect(handlers.onApproveTool).toHaveBeenCalledTimes(1);
    expect(handlers.onApproveTool).toHaveBeenCalledWith('appr-1', false, 'key-123');
  });

  it('does not throw when no onApproveTool handler is wired', () => {
    renderChat({ messages: [approvalMessage], onApproveTool: undefined });
    expect(() => {
      fireEvent.click(screen.getByRole('button', { name: 'Approve Execution' }));
    }).not.toThrow();
  });
});

describe('DraggableChatWindow plugin approval card (Phase 10.3 D2)', () => {
  const pluginMessage = {
    role: 'plugin_approval',
    id: 'plug-1',
    plugin_name: 'weather-plugin',
    plugin_details: { version: '1.0.0', source: 'registry' },
  };

  it('Approve Install calls onApprovePlugin with (id, true)', () => {
    const { handlers } = renderChat({ messages: [pluginMessage] });
    expect(screen.getByText('Plugin Install Approval Required')).toBeInTheDocument();
    expect(screen.getByText('weather-plugin')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Approve Install' }));
    expect(handlers.onApprovePlugin).toHaveBeenCalledTimes(1);
    expect(handlers.onApprovePlugin).toHaveBeenCalledWith('plug-1', true);
  });

  it('Reject calls onApprovePlugin with (id, false)', () => {
    const { handlers } = renderChat({ messages: [pluginMessage] });
    fireEvent.click(screen.getByRole('button', { name: 'Reject Request' }));
    expect(handlers.onApprovePlugin).toHaveBeenCalledTimes(1);
    expect(handlers.onApprovePlugin).toHaveBeenCalledWith('plug-1', false);
  });
});

describe('ToolActivityCard pending approval (Phase 10.3 D2)', () => {
  const payload = {
    tool_name: 'write_file',
    tool_args: { path: '/tmp/x.txt', content: 'hi' },
    status: 'pending',
  };
  const pendingApproval = { id: 'appr-2', approval_key: 'k2', tool_name: 'write_file' };

  it('shows "approval required" and both decision buttons while pending with approval', () => {
    render(<ToolActivityCard payload={payload} pendingApproval={pendingApproval} onApproveTool={jest.fn()} />);
    expect(screen.getByText('approval required')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Approve & Execute' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Reject Request' })).toBeInTheDocument();
  });

  it('Approve & Execute calls onApproveTool with (id, true, approval_key)', () => {
    const onApproveTool = jest.fn();
    render(<ToolActivityCard payload={payload} pendingApproval={pendingApproval} onApproveTool={onApproveTool} />);
    fireEvent.click(screen.getByRole('button', { name: 'Approve & Execute' }));
    expect(onApproveTool).toHaveBeenCalledTimes(1);
    expect(onApproveTool).toHaveBeenCalledWith('appr-2', true, 'k2');
  });

  it('Reject Request calls onApproveTool with (id, false, approval_key)', () => {
    const onApproveTool = jest.fn();
    render(<ToolActivityCard payload={payload} pendingApproval={pendingApproval} onApproveTool={onApproveTool} />);
    fireEvent.click(screen.getByRole('button', { name: 'Reject Request' }));
    expect(onApproveTool).toHaveBeenCalledTimes(1);
    expect(onApproveTool).toHaveBeenCalledWith('appr-2', false, 'k2');
  });

  it('a pending tool WITHOUT a pending approval shows no approval buttons', () => {
    render(<ToolActivityCard payload={payload} pendingApproval={null} onApproveTool={jest.fn()} />);
    expect(screen.queryByText('approval required')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Approve & Execute' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Reject Request' })).not.toBeInTheDocument();
  });

  it('returns null for a missing payload', () => {
    const { container } = render(<ToolActivityCard payload={null} />);
    expect(container.firstChild).toBeNull();
  });
});
