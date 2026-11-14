/**
 * DesktopControlCard tests (S20).
 *
 * The card is the live control surface for the desktop lane in a
 * chat window: Pause / Resume, Take control / Return control,
 * and Stop (which stops desktop actions for the task WITHOUT
 * killing the task — the copy says so). It renders from the
 * control object DraggableChatWindow derives from the engine's
 * DESKTOP_CONTROL_RESPONSE plus its own optimistic state; these
 * tests pin the card component itself: states, banners, and the
 * exact action strings the buttons report.
 */
import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import { DesktopControlCard } from '../DraggableChatWindow';

// react-markdown is ESM-only; stub it for Jest (same as the
// sibling DraggableChatWindow suites).
jest.mock('react-markdown', () => ({
  __esModule: true,
  default: ({ children }) => <div data-testid="markdown-stub">{children}</div>,
}));

Element.prototype.scrollIntoView = jest.fn();
global.WebSocket = { OPEN: 1, CLOSED: 3, CONNECTING: 0, CLOSING: 2 };

describe('DesktopControlCard (S20)', () => {
  test('active state offers Pause, Take control and Stop', () => {
    const onControl = jest.fn();
    render(<DesktopControlCard control={{ controlState: 'agent' }} onControl={onControl} />);
    expect(screen.getByTestId('desktop-control-card')).toBeInTheDocument();
    expect(screen.getByText('Active')).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('desktop-pause-btn'));
    expect(onControl).toHaveBeenCalledWith('pause');
    fireEvent.click(screen.getByTestId('desktop-take-control-btn'));
    expect(onControl).toHaveBeenCalledWith('take_control');
    fireEvent.click(screen.getByTestId('desktop-stop-btn'));
    expect(onControl).toHaveBeenCalledWith('stop');
  });

  test('paused state offers Resume instead of Pause', () => {
    const onControl = jest.fn();
    render(<DesktopControlCard control={{ controlState: 'paused' }} onControl={onControl} />);
    expect(screen.getByText('Paused')).toBeInTheDocument();
    expect(screen.queryByTestId('desktop-pause-btn')).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId('desktop-resume-btn'));
    expect(onControl).toHaveBeenCalledWith('resume');
  });

  test('user-in-control state explains itself and offers Return control', () => {
    const onControl = jest.fn();
    render(<DesktopControlCard control={{ controlState: 'user' }} onControl={onControl} />);
    expect(screen.getByText('User in control')).toBeInTheDocument();
    expect(
      screen.getByText(/You have control of the desktop\. Aether is waiting/)
    ).toBeInTheDocument();
    fireEvent.click(screen.getByTestId('desktop-return-control-btn'));
    expect(onControl).toHaveBeenCalledWith('return_control');
  });

  test('stopped-for-this-task disables the lane buttons and says the task continues', () => {
    const onControl = jest.fn();
    render(
      <DesktopControlCard
        control={{ controlState: 'agent', taskStopped: true }}
        onControl={onControl}
      />
    );
    expect(screen.getByText('Stopped for this task')).toBeInTheDocument();
    expect(
      screen.getByText(/The task itself continues in chat/)
    ).toBeInTheDocument();
    expect(screen.getByTestId('desktop-pause-btn')).toBeDisabled();
    expect(screen.getByTestId('desktop-take-control-btn')).toBeDisabled();
    expect(screen.getByTestId('desktop-stop-btn')).toBeDisabled();
  });
});
