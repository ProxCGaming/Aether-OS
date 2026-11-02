import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import UndoToast from '../components/UndoToast';

// Epic 8, E8-1.1b: the 5-second undo toast (grill Q3). After a
// rewind, the discarded steps live for exactly this window; the
// toast counts it down and offers the one-click restore.
describe('UndoToast (E8-1.1b)', () => {
  beforeEach(() => {
    jest.useFakeTimers();
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  it('shows the countdown and fires onUndo from the button', () => {
    const onUndo = jest.fn();
    render(<UndoToast expiresAt={Date.now() + 5000} onUndo={onUndo} onExpire={jest.fn()} />);
    const toast = screen.getByTestId('rewind-undo-toast');
    expect(toast).toHaveTextContent('5s');
    fireEvent.click(screen.getByTestId('rewind-undo-btn'));
    expect(onUndo).toHaveBeenCalledTimes(1);
  });

  it('counts down and expires exactly once', () => {
    const onExpire = jest.fn();
    render(<UndoToast expiresAt={Date.now() + 5000} onUndo={jest.fn()} onExpire={onExpire} />);
    act(() => { jest.advanceTimersByTime(2000); });
    expect(screen.getByTestId('rewind-undo-toast')).toHaveTextContent('3s');
    expect(onExpire).not.toHaveBeenCalled();
    act(() => { jest.advanceTimersByTime(3000); });
    expect(onExpire).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId('rewind-undo-toast')).not.toBeInTheDocument();
  });
});
