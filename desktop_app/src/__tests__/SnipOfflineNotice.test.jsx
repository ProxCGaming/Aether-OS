import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import DraggableChatWindow from '../DraggableChatWindow';

// Lane C grouped S4: after capture, a snip rides the normal chat
// send path — whose only offline sign was the generic "Queued" tag
// on the eventual bubble. A snip captured while the engine is down
// now gets its own one-line notice at the moment of capture (the
// send itself already queues, Wave 2). Capture while connected
// stays silent, exactly as before.

jest.mock('react-markdown', () => ({
  __esModule: true,
  default: ({ children }) => <div data-testid="markdown-stub">{children}</div>,
}));

Element.prototype.scrollIntoView = jest.fn();
global.WebSocket = { OPEN: 1, CLOSED: 3, CONNECTING: 0, CLOSING: 2 };

describe('Snip capture offline notice (grouped S4)', () => {
  let captureScreen;

  beforeEach(() => {
    captureScreen = jest.fn(async () => 'data:image/png;base64,FAKEPNGDATA');
    window.electronAPI = { captureScreen };
  });

  afterEach(() => {
    delete window.electronAPI;
  });

  const captureSnip = async () => {
    fireEvent.click(screen.getByTestId('snip-ask-btn'));
    const overlay = screen.getByTestId('snip-overlay');
    fireEvent.mouseDown(overlay, { button: 0, clientX: 100, clientY: 100 });
    fireEvent.mouseMove(overlay, { clientX: 220, clientY: 180 });
    await act(async () => {
      fireEvent.mouseUp(overlay);
    });
    expect(captureScreen).toHaveBeenCalledTimes(1);
    // The snip landed in the composer either way.
    expect(screen.getByTestId('composer-attachment-thumbnail')).toBeInTheDocument();
  };

  it('captured while the engine is down: the snip-specific notice shows', async () => {
    render(
      <DraggableChatWindow
        messages={[]}
        sessionId="s1"
        status="Offline / Engine Disconnected"
        onSendMessage={jest.fn()}
      />
    );
    await captureSnip();
    expect(screen.getByText('Snip captured — it will send when the engine reconnects.')).toBeInTheDocument();
  });

  it('captured while connected: no offline notice, as before', async () => {
    render(
      <DraggableChatWindow
        messages={[]}
        sessionId="s1"
        status="Online"
        onSendMessage={jest.fn()}
      />
    );
    await captureSnip();
    expect(screen.queryByText(/will send when the engine reconnects/)).not.toBeInTheDocument();
  });
});
