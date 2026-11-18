import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import ChatTranscript from '../ChatTranscript';

// react-markdown is ESM-only; stub it for Jest (same as App.test.jsx and
// the sibling DraggableChatWindow*.test.jsx files).
jest.mock('react-markdown', () => ({
  __esModule: true,
  default: ({ children }) => <div data-testid="markdown-stub">{children}</div>,
}));

// Wave 5 (Annex C row 6, transcript seam): the message-list
// rendering extracted from DraggableChatWindow — pinned here at
// its own path. The window suites cover the same rows through
// the composed component.

function makeProps(overrides = {}) {
  return {
    messages: [],
    isThinking: false,
    sessionSwitchPending: false,
    chatOffline: false,
    switchSlow: false,
    voiceState: null,
    feedScrollRef: React.createRef(),
    onFeedScroll: jest.fn(),
    messagesEndRef: React.createRef(),
    rewindManifest: null,
    onRewindRequest: jest.fn(),
    onForkRequest: jest.fn(),
    approvalArgEdits: {},
    setApprovalArgEdits: jest.fn(),
    browserEnvelopeGrants: {},
    setBrowserEnvelopeGrants: jest.fn(),
    onEnvelopeGrantChange: jest.fn(),
    onApproveTool: jest.fn(),
    onApprovePlugin: jest.fn(),
    approveToolWithEdits: jest.fn(),
    onStopTask: jest.fn(),
    onOpenArtifact: jest.fn(),
    onEditMessage: jest.fn(),
    setInput: jest.fn(),
    textareaRef: React.createRef(),
    openStepMenu: null,
    setOpenStepMenu: jest.fn(),
    playingMessageId: null,
    readAloudError: null,
    onToggleReadAloud: jest.fn(),
    onRegenerate: jest.fn(),
    ...overrides,
  };
}

describe('ChatTranscript', () => {
  test('empty feed shows the taught landing; a pending switch shows loading instead (RUX-C-13)', () => {
    const { rerender } = render(<ChatTranscript {...makeProps()} />);
    // S-5: the genuine empty state is the taught landing — the
    // shared EmptyState plus clickable suggested first tasks.
    expect(screen.getByTestId('chat-empty-landing')).toBeInTheDocument();
    expect(screen.getByTestId('chat-empty-state')).toBeInTheDocument();
    expect(screen.getAllByTestId('suggested-task-chip')).toHaveLength(4);
    rerender(<ChatTranscript {...makeProps({ sessionSwitchPending: true })} />);
    expect(screen.getByTestId('session-switch-loading')).toHaveTextContent('Loading session…');
    expect(screen.queryByTestId('chat-empty-landing')).not.toBeInTheDocument();
    rerender(<ChatTranscript {...makeProps({ sessionSwitchPending: true, chatOffline: true })} />);
    expect(screen.getByTestId('session-switch-loading')).toHaveTextContent('Waiting for the engine');
  });

  test('a suggestion chip sends its exact text through the send path', () => {
    const onSuggestedTask = jest.fn();
    render(<ChatTranscript {...makeProps({ onSuggestedTask })} />);
    const chip = screen.getAllByTestId('suggested-task-chip')[0];
    fireEvent.click(chip);
    expect(onSuggestedTask).toHaveBeenCalledTimes(1);
    expect(onSuggestedTask).toHaveBeenCalledWith(chip.textContent);
  });

  test('without a wired send, a suggestion chip fills the composer instead of dropping the task', () => {
    const setInput = jest.fn();
    render(<ChatTranscript {...makeProps({ setInput })} />);
    const chip = screen.getAllByTestId('suggested-task-chip')[1];
    fireEvent.click(chip);
    expect(setInput).toHaveBeenCalledWith(chip.textContent);
  });

  test('system, user and agent messages render in order with their content', () => {
    const messages = [
      { id: 'm1', role: 'system', content: 'Session note' },
      { id: 'm2', role: 'user', content: 'Hello there' },
      { id: 'm3', role: 'agent', content: 'General reply', isFinal: true },
    ];
    render(<ChatTranscript {...makeProps({ messages })} />);
    expect(screen.getByText('Session note')).toBeInTheDocument();
    expect(screen.getByText('Hello there')).toBeInTheDocument();
    expect(screen.getByText('General reply')).toBeInTheDocument();
    expect(document.querySelector('[data-message-id="m2"]')).not.toBeNull();
  });

  test('queued user messages carry the queued indicator', () => {
    const messages = [{ id: 'm1', role: 'user', content: 'later', queued: true }];
    render(<ChatTranscript {...makeProps({ messages })} />);
    expect(screen.getByTestId('queued-indicator')).toHaveTextContent('Queued');
  });

  test('tool approval rows: approve goes through approveToolWithEdits, reject through onApproveTool', () => {
    const onApproveTool = jest.fn();
    const approveToolWithEdits = jest.fn();
    const messages = [{
      id: 'appr-1', role: 'tool_approval', tool_name: 'run_shell',
      tool_args: { command: 'ls' }, approval_key: 'key-1',
    }];
    render(<ChatTranscript {...makeProps({ messages, onApproveTool, approveToolWithEdits })} />);
    expect(screen.getByText('Execution Approval Required')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Approve Execution'));
    expect(approveToolWithEdits).toHaveBeenCalledWith(messages[0]);
    expect(onApproveTool).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText('Reject Request'));
    expect(onApproveTool).toHaveBeenCalledWith('appr-1', false, 'key-1');
  });

  test('plugin approval rows go through onApprovePlugin', () => {
    const onApprovePlugin = jest.fn();
    const messages = [{
      id: 'plug-1', role: 'plugin_approval', plugin_name: 'demo-plugin', plugin_details: { version: '1.0' },
    }];
    render(<ChatTranscript {...makeProps({ messages, onApprovePlugin })} />);
    expect(screen.getByText('Plugin Install Approval Required')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Approve Install'));
    expect(onApprovePlugin).toHaveBeenCalledWith('plug-1', true);
  });

  test('step menu forks from the message and edit resends through the window handlers', () => {
    const props = makeProps({
      messages: [{ id: 'm1', role: 'user', content: 'fork me' }],
      openStepMenu: 0,
    });
    render(<ChatTranscript {...props} />);
    expect(screen.getByTestId('step-menu')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('fork-action'));
    expect(props.onForkRequest).toHaveBeenCalledWith({ message_id: 'm1', step_index: null });
    fireEvent.click(screen.getByLabelText('Edit and resend this message'));
    expect(props.onEditMessage).toHaveBeenCalledWith(props.messages[0], 0);
    expect(props.setInput).toHaveBeenCalledWith('fork me');
  });

  test('read-aloud button forwards the message and its index', () => {
    const props = makeProps({
      messages: [{ id: 'm9', role: 'agent', content: 'read me', isFinal: true }],
    });
    render(<ChatTranscript {...props} />);
    fireEvent.click(screen.getByTestId('read-aloud-btn-m9'));
    expect(props.onToggleReadAloud).toHaveBeenCalledWith(props.messages[0], 0);
  });

  test('regenerate shows only on the last final agent bubble and never mid-task', () => {
    const messages = [
      { id: 'a1', role: 'agent', content: 'first', isFinal: true },
      { id: 'a2', role: 'agent', content: 'second', isFinal: true },
    ];
    const props = makeProps({ messages });
    render(<ChatTranscript {...props} />);
    const regen = screen.getAllByLabelText('Regenerate response');
    expect(regen).toHaveLength(1);
    fireEvent.click(regen[0]);
    expect(props.onRegenerate).toHaveBeenCalledTimes(1);
  });

  test('voice live mirror renders partial and assistant bubbles', () => {
    const voiceState = { partialText: 'speaking words', assistantText: 'replying words' };
    render(<ChatTranscript {...makeProps({ voiceState })} />);
    expect(screen.getByTestId('voice-partial-bubble')).toHaveTextContent('speaking words');
    expect(screen.getByTestId('voice-assistant-bubble')).toHaveTextContent('replying words');
  });

  test('agent rows render the thinking accordion for in-flight messages', () => {
    const messages = [{
      id: 'a1', role: 'agent', content: '', isFinal: false,
      thoughts: [{ type: 'TOOL_ACTIVITY', payload: { tool_name: 'read_file', tool_args: {}, status: 'completed', result: 'ok' } }],
    }];
    render(<ChatTranscript {...makeProps({ messages, isThinking: true })} />);
    expect(screen.getByText('Agent is thinking...')).toBeInTheDocument();
    expect(screen.getByText('read_file')).toBeInTheDocument();
  });
});
