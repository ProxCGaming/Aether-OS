import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import SnipOverlay from '../SnipOverlay';

// SnipOverlay (Epic 8 snip tool): a full-screen region picker. The
// stable core pinned here is the selection/cancel flow — open the
// overlay, drag a rectangle, hand the captured region to onCapture;
// bail out via the Cancel button or Escape without capturing.
// Deliberately NOT pinned: the capture-failure error banner and any
// hotkey configuration (later refinements, not the original core).

describe('SnipOverlay', () => {
  const captureScreen = jest.fn();

  beforeEach(() => {
    captureScreen.mockReset();
    captureScreen.mockResolvedValue('data:image/png;base64,FAKEPNGDATA');
    window.electronAPI = { captureScreen };
  });

  afterEach(() => {
    delete window.electronAPI;
  });

  const baseProps = {
    isOpen: true,
    onCapture: jest.fn(),
    onCancel: jest.fn(),
  };

  it('renders nothing while closed', () => {
    const { container } = render(<SnipOverlay {...baseProps} isOpen={false} />);
    expect(container.firstChild).toBeNull();
    expect(screen.queryByTestId('snip-overlay')).toBeNull();
  });

  it('shows the overlay with its drag instructions when open', () => {
    render(<SnipOverlay {...baseProps} />);
    expect(screen.getByTestId('snip-overlay')).toBeInTheDocument();
    expect(screen.getByText(/Drag to select screen region/)).toBeInTheDocument();
    expect(screen.getByTestId('snip-cancel-btn')).toBeInTheDocument();
  });

  it('cancel button fires onCancel and never captures', () => {
    const onCancel = jest.fn();
    const onCapture = jest.fn();
    render(<SnipOverlay {...baseProps} onCancel={onCancel} onCapture={onCapture} />);
    fireEvent.click(screen.getByTestId('snip-cancel-btn'));
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onCapture).not.toHaveBeenCalled();
    expect(captureScreen).not.toHaveBeenCalled();
  });

  it('Escape key cancels the snip', () => {
    const onCancel = jest.fn();
    render(<SnipOverlay {...baseProps} onCancel={onCancel} />);
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it('dragging draws the selection rectangle with its dimensions', () => {
    render(<SnipOverlay {...baseProps} />);
    const overlay = screen.getByTestId('snip-overlay');
    fireEvent.mouseDown(overlay, { button: 0, clientX: 100, clientY: 100 });
    fireEvent.mouseMove(overlay, { clientX: 220, clientY: 180 });
    const box = screen.getByTestId('snip-selection-box');
    expect(box).toBeInTheDocument();
    expect(box).toHaveStyle({ left: '100px', top: '100px', width: '120px', height: '80px' });
    expect(box).toHaveTextContent('120 × 80');
  });

  it('releasing the drag captures exactly the dragged bounds', async () => {
    const onCapture = jest.fn();
    render(<SnipOverlay {...baseProps} onCapture={onCapture} />);
    const overlay = screen.getByTestId('snip-overlay');
    fireEvent.mouseDown(overlay, { button: 0, clientX: 100, clientY: 100 });
    fireEvent.mouseMove(overlay, { clientX: 220, clientY: 180 });
    await act(async () => { fireEvent.mouseUp(overlay); });

    expect(captureScreen).toHaveBeenCalledTimes(1);
    expect(captureScreen).toHaveBeenCalledWith({ x: 100, y: 100, width: 120, height: 80 });
    expect(onCapture).toHaveBeenCalledTimes(1);
    const payload = onCapture.mock.calls[0][0];
    expect(payload.data).toBe('data:image/png;base64,FAKEPNGDATA');
    expect(payload.mime_type).toBe('image/png');
    expect(payload.bounds).toEqual({ x: 100, y: 100, width: 120, height: 80 });
  });

  it('a drag smaller than the minimum region captures nothing', async () => {
    const onCapture = jest.fn();
    render(<SnipOverlay {...baseProps} onCapture={onCapture} />);
    const overlay = screen.getByTestId('snip-overlay');
    fireEvent.mouseDown(overlay, { button: 0, clientX: 50, clientY: 50 });
    fireEvent.mouseMove(overlay, { clientX: 52, clientY: 53 });
    await act(async () => { fireEvent.mouseUp(overlay); });
    expect(captureScreen).not.toHaveBeenCalled();
    expect(onCapture).not.toHaveBeenCalled();
  });

  it('a right-click drag never starts a selection', () => {
    render(<SnipOverlay {...baseProps} />);
    const overlay = screen.getByTestId('snip-overlay');
    fireEvent.mouseDown(overlay, { button: 2, clientX: 100, clientY: 100 });
    fireEvent.mouseMove(overlay, { clientX: 220, clientY: 180 });
    expect(screen.queryByTestId('snip-selection-box')).toBeNull();
  });
});
