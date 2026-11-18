/**
 * S-6 (FF1-08) return-to-origin + S-8 (FF1-10) detached panel,
 * driven through the real App:
 * - closing Settings returns to the tab the user came from
 *   (Dashboard), not always to Chat; from Chat it still returns
 *   to Chat;
 * - while the chat is detached, the Chat tab's content area shows
 *   the designed detached panel, and its Re-attach button runs
 *   the existing re-attach path.
 */
import React from 'react';
import { render, screen, fireEvent, act, within } from '@testing-library/react';
import '@testing-library/jest-dom';
import App from '../App';

// The 3D force graph needs WebGL; replace with a stub for jsdom.
jest.mock('react-force-graph-3d', () => ({
  __esModule: true,
  default: () => <div data-testid="force-graph-stub" />,
}));

// react-markdown is ESM-only; stub it for Jest.
jest.mock('react-markdown', () => ({
  __esModule: true,
  default: ({ children }) => <div data-testid="markdown-stub">{children}</div>,
}));

// jsdom has no scrollIntoView; stub it.
Element.prototype.scrollIntoView = jest.fn();

// jsdom has no WebSocket; the socket layer only needs the constants.
global.WebSocket = { OPEN: 1, CLOSED: 3, CONNECTING: 0, CLOSING: 2 };

describe('App Settings return-to-origin and detached panel (S-6/S-8)', () => {
  let engineListeners;
  let electronAPI;
  let detachedCallback;

  beforeEach(() => {
    window.history.pushState({}, '', '/');
    window.localStorage.clear();
    engineListeners = [];
    detachedCallback = null;
    electronAPI = {
      sendEngineMessage: jest.fn(),
      onEngineMessage: jest.fn((cb) => {
        engineListeners.push(cb);
        return () => {
          const idx = engineListeners.indexOf(cb);
          if (idx !== -1) engineListeners.splice(idx, 1);
        };
      }),
      getWsStatus: jest.fn(() => Promise.resolve(false)),
      getMemoryUsage: jest.fn(() => Promise.resolve(128)),
      onChatDetached: jest.fn((cb) => { detachedCallback = cb; return jest.fn(); }),
      onChatAttached: jest.fn(() => jest.fn()),
      openFloatingChat: jest.fn(() => Promise.resolve()),
      closeFloatingChat: jest.fn(() => Promise.resolve()),
      setFloatingTransferState: jest.fn(() => Promise.resolve(true)),
      getFloatingTransferState: jest.fn(() => Promise.resolve(null)),
      clearFloatingTransferState: jest.fn(() => Promise.resolve()),
    };
    window.electronAPI = electronAPI;
  });

  afterEach(() => {
    delete window.electronAPI;
    window.history.pushState({}, '', '/');
  });

  const flushPromises = async () => {
    await act(async () => {});
  };

  const renderConnectedApp = async () => {
    render(<App />);
    await flushPromises();
    act(() => {
      [...engineListeners].forEach((cb) => cb(JSON.stringify({ type: '_ws_status', status: 'connected' })));
    });
    await flushPromises();
  };

  const navClick = (label) => {
    fireEvent.click(within(screen.getByRole('navigation')).getByText(label));
  };

  it('closing Settings returns to Dashboard when that is where the user came from', async () => {
    await renderConnectedApp();
    navClick('Dashboard');
    expect(screen.getByText('System Dashboard')).toBeInTheDocument();

    navClick('Settings');
    expect(screen.getByLabelText('Close Settings')).toBeInTheDocument();
    expect(screen.queryByText('System Dashboard')).not.toBeInTheDocument();

    fireEvent.click(screen.getByLabelText('Close Settings'));
    expect(screen.getByText('System Dashboard')).toBeInTheDocument();
    expect(screen.queryByLabelText('Close Settings')).not.toBeInTheDocument();
  });

  it('closing Settings still returns to Chat when the user came from Chat', async () => {
    await renderConnectedApp();
    expect(screen.getByPlaceholderText('Message Aether...')).toBeInTheDocument();

    navClick('Settings');
    expect(screen.getByLabelText('Close Settings')).toBeInTheDocument();

    fireEvent.click(screen.getByLabelText('Close Settings'));
    expect(screen.getByPlaceholderText('Message Aether...')).toBeInTheDocument();
  });

  it('the origin is the latest entry tab: Projects, then Settings, closes back to Projects', async () => {
    await renderConnectedApp();
    navClick('Projects');
    navClick('Settings');
    fireEvent.click(screen.getByLabelText('Close Settings'));
    // The Projects view is back (its tab content), Settings is gone.
    expect(screen.queryByLabelText('Close Settings')).not.toBeInTheDocument();
    expect(within(screen.getByRole('navigation')).getByText('Projects')).toBeInTheDocument();
    // Chat was not forced: the composer is not what we returned to.
    expect(screen.queryByPlaceholderText('Message Aether...')).not.toBeInTheDocument();
  });

  it('while detached, the content area shows the designed panel and Re-attach runs the existing path', async () => {
    await renderConnectedApp();
    expect(screen.queryByTestId('detached-chat-panel')).not.toBeInTheDocument();

    act(() => { detachedCallback(); });
    await flushPromises();

    expect(screen.getByTestId('detached-chat-panel')).toBeInTheDocument();
    expect(screen.getByText('Chat is in its own window')).toBeInTheDocument();
    expect(screen.getByTestId('detached-chat-status')).toHaveTextContent('Detached chat is idle');
    // The chat window itself is not rendered in the main window.
    expect(screen.queryByPlaceholderText('Message Aether...')).not.toBeInTheDocument();

    fireEvent.click(screen.getByText('Re-attach chat'));
    await flushPromises();
    await flushPromises();

    expect(electronAPI.closeFloatingChat).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId('detached-chat-panel')).not.toBeInTheDocument();
    expect(screen.getByPlaceholderText('Message Aether...')).toBeInTheDocument();
  });
});
