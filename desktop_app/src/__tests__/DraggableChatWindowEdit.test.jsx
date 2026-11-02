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
      onDetach={jest.fn()}
      isFloating={false}
      initialInput=""
      slashCommands={[]}
      isSidebarCollapsed={false}
      {...props}
    />
  );
}

// Edit-and-resend hardening: the button must address the exact message
// that was clicked — including when the same text was sent twice — and
// load that text into the composer.
describe('DraggableChatWindow edit-and-resend button', () => {
  const first = { id: 'u1', role: 'user', content: 'same text', isFinal: true, thoughts: [] };
  const second = { id: 'u2', role: 'user', content: 'same text', isFinal: true, thoughts: [] };
  const messages = [
    first,
    { id: 'a1', role: 'agent', content: 'first answer', isFinal: true, thoughts: [] },
    second,
    { id: 'a2', role: 'agent', content: 'second answer', isFinal: true, thoughts: [] },
  ];

  it('renders one labelled edit button per user message and none for the agent', () => {
    renderChat({ messages, onEditMessage: jest.fn() });
    expect(screen.getAllByRole('button', { name: 'Edit and resend this message' })).toHaveLength(2);
  });

  it('clicking the second duplicate reports that exact message and its index', () => {
    const onEditMessage = jest.fn();
    renderChat({ messages, onEditMessage });

    const buttons = screen.getAllByRole('button', { name: 'Edit and resend this message' });
    expect(buttons[1].getAttribute('type')).toBe('button');
    fireEvent.click(buttons[1]);

    expect(onEditMessage).toHaveBeenCalledTimes(1);
    expect(onEditMessage).toHaveBeenCalledWith(second, 2);
    expect(screen.getByPlaceholderText('Message Aether...')).toHaveValue('same text');
  });

  it('clicking the first duplicate reports index 0', () => {
    const onEditMessage = jest.fn();
    renderChat({ messages, onEditMessage });

    fireEvent.click(screen.getAllByRole('button', { name: 'Edit and resend this message' })[0]);

    expect(onEditMessage).toHaveBeenCalledWith(first, 0);
  });
});
