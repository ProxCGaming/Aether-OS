/** S-8 (FF1-10): the main window's detached-chat panel — the
 * state explains itself in the content area and the primary
 * Re-attach action runs the existing re-attach path. */
import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import DetachedChatPanel from '../DetachedChatPanel';

describe('DetachedChatPanel (S-8)', () => {
  test('idle: explains the detached state and Re-attach fires the handler', () => {
    const onReattach = jest.fn();
    render(<DetachedChatPanel isThinking={false} onReattach={onReattach} />);
    expect(screen.getByText('Chat is in its own window')).toBeInTheDocument();
    expect(screen.getByText(/open in a separate window/)).toBeInTheDocument();
    expect(screen.getByTestId('detached-chat-status')).toHaveTextContent('Detached chat is idle');
    fireEvent.click(screen.getByText('Re-attach chat'));
    expect(onReattach).toHaveBeenCalledTimes(1);
  });

  test('thinking: the running status is stated in the panel, not just the title bar', () => {
    render(<DetachedChatPanel isThinking onReattach={jest.fn()} />);
    expect(screen.getByText(/A task is running in the detached chat/)).toBeInTheDocument();
    expect(screen.getByTestId('detached-chat-status')).toHaveTextContent('Task running in the detached chat');
  });
});
