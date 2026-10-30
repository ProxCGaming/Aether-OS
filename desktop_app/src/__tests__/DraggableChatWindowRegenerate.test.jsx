import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import DraggableChatWindow from '../DraggableChatWindow';

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
      onEditMessage={jest.fn()}
      onRegenerate={jest.fn()}
      planMode={false}
      onTogglePlanMode={jest.fn()}
      onDetach={jest.fn()}
      isFloating={false}
      initialInput=""
      slashCommands={[]}
      isSidebarCollapsed={false}
      {...props}
    />
  );
}

// Epic 1 UI batch: regenerate lives on the last final agent bubble
// only (E1-08); the plan-mode toggle sits by the composer (E1-09 UI).
describe('DraggableChatWindow regenerate + plan toggle (Epic 1 UI batch)', () => {
  const exchange = [
    { id: 'u1', role: 'user', content: 'first question', isFinal: true, thoughts: [] },
    { id: 'a1', role: 'agent', content: 'first answer', isFinal: true, thoughts: [] },
    { id: 'u2', role: 'user', content: 'second question', isFinal: true, thoughts: [] },
    { id: 'a2', role: 'agent', content: 'second answer', isFinal: true, thoughts: [] },
  ];

  it('shows Regenerate only on the last final agent message and reports clicks', () => {
    const onRegenerate = jest.fn();
    renderChat({ messages: exchange, onRegenerate });

    const buttons = screen.getAllByRole('button', { name: 'Regenerate response' });
    expect(buttons).toHaveLength(1);
    fireEvent.click(buttons[0]);
    expect(onRegenerate).toHaveBeenCalledTimes(1);
  });

  it('offers Regenerate on a failed/error final bubble too', () => {
    const messages = [
      { id: 'u1', role: 'user', content: 'break please', isFinal: true, thoughts: [] },
      { id: 'a1', role: 'agent', content: '**Task Failed**: boom', isFinal: true, hasError: true, thoughts: [] },
    ];
    renderChat({ messages });
    expect(screen.getByRole('button', { name: 'Regenerate response' })).toBeInTheDocument();
  });

  it('hides Regenerate while a task is running', () => {
    renderChat({ messages: exchange, isThinking: true });
    expect(screen.queryByRole('button', { name: 'Regenerate response' })).not.toBeInTheDocument();
  });

  it('hides Regenerate when no handler is wired', () => {
    renderChat({ messages: exchange, onRegenerate: undefined });
    expect(screen.queryByRole('button', { name: 'Regenerate response' })).not.toBeInTheDocument();
  });

  it('renders the plan toggle with its tooltip and pressed state, and reports clicks', () => {
    const onTogglePlanMode = jest.fn();
    renderChat({ planMode: true, onTogglePlanMode });

    const toggle = screen.getByRole('button', { name: 'Toggle plan mode' });
    expect(toggle).toHaveTextContent('Plan');
    expect(toggle).toHaveAttribute('aria-pressed', 'true');
    expect(toggle).toHaveAttribute('title', 'Plan mode: agents only use read-only tools');
    expect(toggle).toHaveClass('active');

    fireEvent.click(toggle);
    expect(onTogglePlanMode).toHaveBeenCalledTimes(1);
  });

  it('renders the plan toggle unpressed when plan mode is off', () => {
    renderChat({ planMode: false });
    const toggle = screen.getByRole('button', { name: 'Toggle plan mode' });
    expect(toggle).toHaveAttribute('aria-pressed', 'false');
    expect(toggle).not.toHaveClass('active');
  });
});
