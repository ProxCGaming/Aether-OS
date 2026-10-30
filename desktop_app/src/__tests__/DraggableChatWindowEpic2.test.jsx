import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import DraggableChatWindow, { ToolActivityCard, ApprovalPreview } from '../DraggableChatWindow';

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

const DIFF = [
  '--- a/src/app.py',
  '+++ b/src/app.py',
  '@@ -1,3 +1,3 @@',
  ' context line',
  '-old line',
  '+new line',
].join('\n');

// Epic 2 UI batch: approval previews (E2-03 diff / error note) and
// scoped approvals (E2-09 remember file/folder). The scoped buttons may
// only appear when the pending call carries a string `path`, and the
// plain approve must keep the exact legacy 3-argument callback shape —
// the Phase 10.3 D2 tests pin it.
describe('ToolActivityCard approval preview (Epic 2)', () => {
  const payload = {
    tool_name: 'edit_file',
    tool_args: { path: '/tmp/proj/app.py' },
    status: 'pending',
  };

  it('renders the diff with add/del lines coloured differently', () => {
    const pendingApproval = {
      id: 'appr-d1', approval_key: 'kd1', tool_name: 'edit_file',
      tool_args: { path: '/tmp/proj/app.py' },
      preview: { diff: DIFF },
    };
    render(<ToolActivityCard payload={payload} pendingApproval={pendingApproval} onApproveTool={jest.fn()} />);
    expect(screen.getByTestId('approval-diff-preview')).toBeInTheDocument();
    const add = screen.getByText('+new line');
    const del = screen.getByText('-old line');
    const hunk = screen.getByText('@@ -1,3 +1,3 @@');
    expect(add.className).toContain('diff-line-add');
    expect(del.className).toContain('diff-line-del');
    expect(hunk.className).toContain('diff-line-hunk');
    expect(add.style.color).not.toBe(del.style.color);
    expect(add.style.color).toBe('rgb(74, 222, 128)');
    expect(del.style.color).toBe('rgb(248, 113, 113)');
  });

  it('renders preview.error as a warning note instead of a diff', () => {
    const pendingApproval = {
      id: 'appr-d2', approval_key: 'kd2', tool_name: 'edit_file',
      tool_args: { path: '/tmp/proj/app.py' },
      preview: { error: 'file changed since the preview was requested' },
    };
    render(<ToolActivityCard payload={payload} pendingApproval={pendingApproval} onApproveTool={jest.fn()} />);
    expect(screen.queryByTestId('approval-diff-preview')).not.toBeInTheDocument();
    expect(screen.getByTestId('approval-preview-error')).toBeInTheDocument();
    expect(screen.getByText(/file changed since the preview was requested/)).toBeInTheDocument();
  });

  it('no preview → no preview UI, approval box unchanged', () => {
    const pendingApproval = { id: 'appr-d3', approval_key: 'kd3', tool_name: 'edit_file' };
    render(<ToolActivityCard payload={payload} pendingApproval={pendingApproval} onApproveTool={jest.fn()} />);
    expect(screen.queryByTestId('approval-diff-preview')).not.toBeInTheDocument();
    expect(screen.queryByTestId('approval-preview-error')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Approve & Execute' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Reject Request' })).toBeInTheDocument();
  });

  it('ApprovalPreview renders nothing for a null preview', () => {
    const { container } = render(<ApprovalPreview preview={null} />);
    expect(container.firstChild).toBeNull();
  });
});

describe('ToolActivityCard scoped approvals (Epic 2)', () => {
  const payload = {
    tool_name: 'edit_file',
    tool_args: { path: '/tmp/proj/app.py' },
    status: 'pending',
  };
  const pendingApproval = {
    id: 'appr-s1', approval_key: 'ks1', tool_name: 'edit_file',
    tool_args: { path: '/tmp/proj/app.py' },
  };

  it('offers remember file/folder when the call has a path', () => {
    render(<ToolActivityCard payload={payload} pendingApproval={pendingApproval} onApproveTool={jest.fn()} />);
    expect(screen.getByRole('button', { name: 'Approve + remember file' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Approve + remember folder' })).toBeInTheDocument();
  });

  it('"remember folder" calls onApproveTool with scope "folder"', () => {
    const onApproveTool = jest.fn();
    render(<ToolActivityCard payload={payload} pendingApproval={pendingApproval} onApproveTool={onApproveTool} />);
    fireEvent.click(screen.getByRole('button', { name: 'Approve + remember folder' }));
    expect(onApproveTool).toHaveBeenCalledTimes(1);
    expect(onApproveTool).toHaveBeenCalledWith('appr-s1', true, 'ks1', 'folder');
  });

  it('"remember file" calls onApproveTool with scope "file"', () => {
    const onApproveTool = jest.fn();
    render(<ToolActivityCard payload={payload} pendingApproval={pendingApproval} onApproveTool={onApproveTool} />);
    fireEvent.click(screen.getByRole('button', { name: 'Approve + remember file' }));
    expect(onApproveTool).toHaveBeenCalledTimes(1);
    expect(onApproveTool).toHaveBeenCalledWith('appr-s1', true, 'ks1', 'file');
  });

  it('plain Approve & Execute keeps the legacy 3-argument call (no scope)', () => {
    const onApproveTool = jest.fn();
    render(<ToolActivityCard payload={payload} pendingApproval={pendingApproval} onApproveTool={onApproveTool} />);
    fireEvent.click(screen.getByRole('button', { name: 'Approve & Execute' }));
    expect(onApproveTool).toHaveBeenCalledTimes(1);
    expect(onApproveTool).toHaveBeenCalledWith('appr-s1', true, 'ks1');
    expect(onApproveTool.mock.calls[0]).toHaveLength(3);
  });

  it('no scope buttons when the pending call has no path argument', () => {
    const shellPayload = {
      tool_name: 'execute_shell',
      tool_args: { command: 'ls' },
      status: 'pending',
    };
    const shellApproval = {
      id: 'appr-s2', approval_key: 'ks2', tool_name: 'execute_shell',
      tool_args: { command: 'ls' },
    };
    render(<ToolActivityCard payload={shellPayload} pendingApproval={shellApproval} onApproveTool={jest.fn()} />);
    expect(screen.queryByRole('button', { name: 'Approve + remember file' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Approve + remember folder' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Approve & Execute' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Reject Request' })).toBeInTheDocument();
  });
});

describe('Standalone approval card preview + scope (Epic 2)', () => {
  const editMessage = {
    role: 'tool_approval',
    id: 'appr-m1',
    tool_name: 'edit_file',
    tool_args: { path: '/tmp/proj/app.py' },
    approval_key: 'km1',
    preview: { diff: DIFF },
  };

  it('renders the diff preview in the message-list approval card', () => {
    renderChat({ messages: [editMessage] });
    expect(screen.getByTestId('approval-diff-preview')).toBeInTheDocument();
    expect(screen.getByText('+new line')).toBeInTheDocument();
    expect(screen.getByText('-old line')).toBeInTheDocument();
  });

  it('renders the preview error note in the message-list approval card', () => {
    renderChat({ messages: [{ ...editMessage, preview: { error: 'preview build failed' } }] });
    expect(screen.getByTestId('approval-preview-error')).toBeInTheDocument();
    expect(screen.getByText(/preview build failed/)).toBeInTheDocument();
  });

  it('"remember folder" in the standalone card passes scope through', () => {
    const { handlers } = renderChat({ messages: [editMessage] });
    fireEvent.click(screen.getByRole('button', { name: 'Approve + remember folder' }));
    expect(handlers.onApproveTool).toHaveBeenCalledTimes(1);
    expect(handlers.onApproveTool).toHaveBeenCalledWith('appr-m1', true, 'km1', 'folder');
  });

  it('a call without a path shows no scope buttons in the standalone card', () => {
    const shellMessage = {
      role: 'tool_approval',
      id: 'appr-m2',
      tool_name: 'execute_shell',
      tool_args: { command: 'ls -la' },
      approval_key: 'km2',
    };
    renderChat({ messages: [shellMessage] });
    expect(screen.queryByRole('button', { name: 'Approve + remember file' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Approve + remember folder' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Approve Execution' })).toBeInTheDocument();
  });
});
