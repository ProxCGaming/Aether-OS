import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import {
  getNodeStyle,
  PayloadDrawer,
  ApprovalPreview,
  approvalScopePath,
  getRewindPointForMessage,
  ToolActivityCard,
  ThinkingAccordion,
} from '../ChatCards';
import { parseApprovalEditedArgs, browserEnvelopeGrant } from '../approvalLogic';

// Wave 5 (Annex C row 6): the transcript-rendering cards moved
// out of DraggableChatWindow into ChatCards. The window re-exports
// these names (covered by the DraggableChatWindow suites); this
// file pins the module at its own path, including the D1-C1
// call-order approval binding inside ThinkingAccordion.

describe('getNodeStyle', () => {
  test('maps node names to their badge identities', () => {
    expect(getNodeStyle('supervisor').name).toBe('SUPERVISOR');
    expect(getNodeStyle('planner').name).toBe('PLANNER');
    expect(getNodeStyle('coder').color).toBe('#6ee7b7');
    expect(getNodeStyle('researcher').name).toBe('RESEARCHER');
    expect(getNodeStyle('execute_tool').name).toBe('EXECUTE_TOOL');
    expect(getNodeStyle('something-else').name).toBe('SOMETHING-ELSE');
    expect(getNodeStyle('').name).toBe('NODE');
  });
});

describe('PayloadDrawer', () => {
  test('renders collapsed with a param count, expands on header click', () => {
    render(<PayloadDrawer title="Request" data={{ a: 1, b: 2 }} />);
    expect(screen.getByText('Request')).toBeInTheDocument();
    expect(screen.getByText('2 params')).toBeInTheDocument();
    expect(screen.queryByText(/"a": 1/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByText('Request'));
    expect(screen.getByText(/"a": 1/)).toBeInTheDocument();
  });

  test('copy button writes the formatted payload (D1-C8: awaited)', async () => {
    const writeText = jest.fn().mockResolvedValue(undefined);
    Object.defineProperty(window.navigator, 'clipboard', { value: { writeText }, configurable: true });
    render(<PayloadDrawer title="Request" data={{ a: 1 }} defaultOpen />);
    fireEvent.click(screen.getByText('Copy'));
    expect(await screen.findByText('Copied')).toBeInTheDocument();
    expect(writeText).toHaveBeenCalledWith('{\n  "a": 1\n}');
  });
});

describe('ApprovalPreview', () => {
  test('renders diff lines with add/del classes, and the error variant', () => {
    const { rerender } = render(<ApprovalPreview preview={{ diff: '+added\n-removed\n context' }} />);
    const diff = screen.getByTestId('approval-diff-preview');
    expect(diff.querySelector('.diff-line-add')).toHaveTextContent('+added');
    expect(diff.querySelector('.diff-line-del')).toHaveTextContent('-removed');
    rerender(<ApprovalPreview preview={{ error: 'too large' }} />);
    expect(screen.getByTestId('approval-preview-error')).toHaveTextContent('No preview available: too large');
    rerender(<ApprovalPreview preview={null} />);
    expect(screen.queryByTestId('approval-diff-preview')).not.toBeInTheDocument();
  });
});

describe('approval helpers', () => {
  test('approvalScopePath returns the target path or null', () => {
    expect(approvalScopePath({ path: '/tmp/x.txt' })).toBe('/tmp/x.txt');
    expect(approvalScopePath({})).toBeNull();
    expect(approvalScopePath(null)).toBeNull();
  });

  test('parseApprovalEditedArgs distinguishes unchanged / edited / invalid', () => {
    expect(parseApprovalEditedArgs('{"a": 1}', { a: 1 })).toEqual({ editedArgs: null, error: null });
    expect(parseApprovalEditedArgs('{"a": 2}', { a: 1 })).toEqual({ editedArgs: { a: 2 }, error: null });
    expect(parseApprovalEditedArgs('not json', {})).toEqual({ editedArgs: null, error: 'Arguments must be valid JSON.' });
    expect(parseApprovalEditedArgs('[1,2]', {})).toEqual({ editedArgs: null, error: 'Arguments must be a JSON object.' });
  });

  test('browserEnvelopeGrant never grants payments and only when checked', () => {
    const conf = { category: 'navigation', site_host: 'example.com' };
    expect(browserEnvelopeGrant(conf, true)).toEqual({ grant: true });
    expect(browserEnvelopeGrant(conf, false)).toBeUndefined();
    expect(browserEnvelopeGrant({ category: 'payments' }, true)).toBeUndefined();
    expect(browserEnvelopeGrant(null, true)).toBeUndefined();
  });

  test('getRewindPointForMessage maps messages onto the manifest trail', () => {
    const messages = [
      { role: 'user' },
      { role: 'agent' },
      { role: 'user' },
      { role: 'agent' },
    ];
    const manifest = { entries: [{ step_index: 0 }, { step_index: 1 }] };
    expect(getRewindPointForMessage(messages, 0, null)).toBeNull();
    expect(getRewindPointForMessage(messages, 1, manifest)).toEqual({ scope: 'older', entry: null });
    expect(getRewindPointForMessage(messages, 2, manifest)).toEqual({ scope: 'current', entry: { step_index: 0 } });
    expect(getRewindPointForMessage(messages, 3, manifest)).toEqual({ scope: 'current', entry: { step_index: 1 } });
  });
});

describe('ToolActivityCard approval surface', () => {
  const payload = { tool_name: 'run_shell', tool_args: { command: 'ls' }, status: 'pending' };
  const pendingApproval = { id: 'appr-1', approval_key: 'key-1', tool_args: { command: 'ls' } };

  test('approve fires the legacy 3-argument contract; reject too', () => {
    const onApproveTool = jest.fn();
    render(<ToolActivityCard payload={payload} pendingApproval={pendingApproval} onApproveTool={onApproveTool} />);
    expect(screen.getByText('Execution Approval Required')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Approve & Execute'));
    expect(onApproveTool).toHaveBeenCalledWith('appr-1', true, 'key-1');
    fireEvent.click(screen.getByText('Reject Request'));
    expect(onApproveTool).toHaveBeenCalledWith('appr-1', false, 'key-1');
  });

  test('without a pending approval a pending tool shows the awaiting line', () => {
    render(<ToolActivityCard payload={payload} />);
    expect(screen.getByText(/Executing tool in sandbox/)).toBeInTheDocument();
  });
});

describe('ThinkingAccordion D1-C1 binding', () => {
  test('two pending same-tool cards bind approvals in call order', () => {
    const thoughts = [
      { type: 'TOOL_ACTIVITY', payload: { tool_name: 'edit_file', tool_args: { path: '/a.txt' }, status: 'pending' } },
      { type: 'TOOL_ACTIVITY', payload: { tool_name: 'edit_file', tool_args: { path: '/b.txt' }, status: 'pending' } },
    ];
    const pendingApprovals = [
      { id: 'appr-A', approval_key: 'key-A', tool_name: 'edit_file', tool_args: { path: '/a.txt' } },
      { id: 'appr-B', approval_key: 'key-B', tool_name: 'edit_file', tool_args: { path: '/b.txt' } },
    ];
    const onApproveTool = jest.fn();
    render(
      <ThinkingAccordion
        thoughts={thoughts}
        isThinking
        hasError={false}
        onStopTask={jest.fn()}
        onApproveTool={onApproveTool}
        pendingApprovals={pendingApprovals}
      />
    );
    const approveButtons = screen.getAllByText('Approve & Execute');
    expect(approveButtons).toHaveLength(2);
    // The second card displays call B's arguments and must approve call B.
    fireEvent.click(approveButtons[1]);
    expect(onApproveTool).toHaveBeenCalledWith('appr-B', true, 'key-B');
    fireEvent.click(approveButtons[0]);
    expect(onApproveTool).toHaveBeenCalledWith('appr-A', true, 'key-A');
  });

  test('renders nothing for an empty finished thought list', () => {
    const { container } = render(
      <ThinkingAccordion thoughts={[]} isThinking={false} hasError={false} onStopTask={jest.fn()} />
    );
    expect(container.firstChild).toBeNull();
  });
});
