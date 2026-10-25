import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import DraggableChatWindow from '../DraggableChatWindow';

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

// Phase 10.3 C2: DraggableChatWindow's isMinimized state used to be
// settable by nothing — the state existed, the control did not. The header
// now has a real minimise/restore toggle: minimising collapses the feed
// and input down to the header bar, restoring brings them back.
describe('DraggableChatWindow minimise toggle (Phase 10.3 C2)', () => {
  it('minimising hides the feed and input; restoring brings them back', () => {
    renderChat();

    // Expanded by default: the composer is visible.
    expect(screen.getByPlaceholderText('Message Aether...')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Minimise chat' })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Minimise chat' }));

    // Minimised: feed + input are gone, only the header bar remains, and
    // the toggle now offers to restore.
    expect(screen.queryByPlaceholderText('Message Aether...')).not.toBeInTheDocument();
    expect(screen.queryByText('No active session. Open a project or start a new chat.')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Minimise chat' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Restore chat' })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Restore chat' }));

    expect(screen.getByPlaceholderText('Message Aether...')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Minimise chat' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Restore chat' })).not.toBeInTheDocument();
  });

  it('the toggle works in the floating window too', () => {
    renderChat({ isFloating: true });

    fireEvent.click(screen.getByRole('button', { name: 'Minimise chat' }));
    expect(screen.queryByPlaceholderText('Message Aether...')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Restore chat' })).toBeInTheDocument();
  });
});
