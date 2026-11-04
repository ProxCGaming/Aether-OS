/** Wave 3 slice 2 — RUX-C-13 + RUX-C-16.
 * C-13: switching to an uncached session showed "No active
 * session" while its history was in flight (and forever if the
 * switch happened offline). The chat pane now has a switching
 * state keyed to App's pending switch; only an answered session
 * may render the genuine empty text.
 * C-16: the only in-chat sign of a dead engine was an 8px red
 * dot. A slim banner now states the connection fact in the chat
 * area and clears itself on reconnect. */
import React from 'react';
import { render, screen, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import DraggableChatWindow from '../DraggableChatWindow';

jest.mock('react-markdown', () => ({
  __esModule: true,
  default: ({ children }) => <div data-testid="markdown-stub">{children}</div>,
}));

Element.prototype.scrollIntoView = jest.fn();
global.WebSocket = { OPEN: 1, CLOSED: 3, CONNECTING: 0, CLOSING: 2 };

const baseProps = {
  messages: [],
  onSendMessage: jest.fn(),
  onStopTask: jest.fn(),
  isThinking: false,
  providers: [],
};

describe('session switching state (RUX-C-13)', () => {
  it('a pending switch renders the loading state, not "No active session"', () => {
    render(<DraggableChatWindow {...baseProps} status="Online" sessionSwitchPending />);
    expect(screen.getByTestId('session-switch-loading')).toHaveTextContent('Loading session…');
    expect(screen.queryByText(/No active session/)).not.toBeInTheDocument();
  });

  it('a pending switch while offline reads as waiting for the engine', () => {
    render(
      <DraggableChatWindow {...baseProps} status="Offline / Engine Disconnected" sessionSwitchPending />,
    );
    expect(screen.getByTestId('session-switch-loading')).toHaveTextContent(/Waiting for the engine/);
    expect(screen.queryByText(/No active session/)).not.toBeInTheDocument();
  });

  it('an answered session (no longer pending) with no messages shows the genuine empty state', () => {
    render(<DraggableChatWindow {...baseProps} status="Online" sessionSwitchPending={false} />);
    expect(screen.queryByTestId('session-switch-loading')).not.toBeInTheDocument();
    expect(screen.getByText(/No active session/)).toBeInTheDocument();
  });

  it('a switch that outlasts the slow interval gains the taking-longer line', () => {
    jest.useFakeTimers();
    try {
      render(<DraggableChatWindow {...baseProps} status="Online" sessionSwitchPending />);
      expect(screen.queryByText(/taking longer than usual/)).not.toBeInTheDocument();
      act(() => { jest.advanceTimersByTime(20000); });
      expect(screen.getByText(/taking longer than usual/)).toBeInTheDocument();
    } finally {
      jest.useRealTimers();
    }
  });
});

describe('chat offline banner (RUX-C-16)', () => {
  it('appears while disconnected and states the connection fact', () => {
    render(<DraggableChatWindow {...baseProps} status="Offline / Engine Disconnected" />);
    const banner = screen.getByTestId('chat-offline-banner');
    expect(banner).toHaveTextContent(/Engine disconnected/);
    expect(banner).toHaveTextContent(/last known state/);
  });

  it('is absent while online — it clears itself on reconnect', () => {
    const { rerender } = render(
      <DraggableChatWindow {...baseProps} status="Offline / Engine Disconnected" />,
    );
    expect(screen.getByTestId('chat-offline-banner')).toBeInTheDocument();
    rerender(<DraggableChatWindow {...baseProps} status="Online" />);
    expect(screen.queryByTestId('chat-offline-banner')).not.toBeInTheDocument();
  });
});
