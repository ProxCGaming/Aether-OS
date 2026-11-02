import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom';
import App from '../App';

// E6-F06 (App seam): the main NavItem was a mouse-only DIV — no
// tabIndex, no button role, no keyboard handler — so core tab
// navigation could not be focused or activated from the keyboard.
// These tests pin focusability, Enter/Space activation identical to
// click, and the accessible name; mouse click and the Ctrl+K palette
// are pinned as controls. Harness mirrors AppOverlayMounts.test.jsx
// (real SocketProvider over a mocked electronAPI).

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

describe('App main navigation is keyboard-operable (E6-F06)', () => {
  let engineListeners;

  beforeEach(() => {
    engineListeners = [];
    window.electronAPI = {
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
      onChatDetached: jest.fn(() => jest.fn()),
      onChatAttached: jest.fn(() => jest.fn()),
    };
  });

  afterEach(() => {
    delete window.electronAPI;
  });

  const flushPromises = async () => {
    await act(async () => {});
  };

  it('Projects nav item is focusable and Enter navigates to Projects', async () => {
    render(<App />);
    await flushPromises();

    const item = screen.getByText('Projects');
    // Audit repro shape: rendered as DIV / tabIndex -1, focus failed.
    expect(item).toHaveAttribute('role', 'button');
    expect(item.tabIndex).toBe(0);
    item.focus();
    expect(document.activeElement).toBe(item);

    fireEvent.keyDown(item, { key: 'Enter' });
    expect(screen.getByText('Workspaces')).toBeInTheDocument();
  });

  it('Space activates a nav item exactly like a click', async () => {
    render(<App />);
    await flushPromises();

    const item = screen.getByText('Projects');
    item.focus();
    expect(document.activeElement).toBe(item);

    fireEvent.keyDown(item, { key: ' ' });
    expect(screen.getByText('Workspaces')).toBeInTheDocument();
  });

  it('nav items expose an accessible button name', async () => {
    render(<App />);
    await flushPromises();

    expect(screen.getByRole('button', { name: 'Projects' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Settings' })).toBeInTheDocument();
  });

  it('control: mouse click still navigates to Projects', async () => {
    render(<App />);
    await flushPromises();

    fireEvent.click(screen.getByText('Projects'));
    expect(screen.getByText('Workspaces')).toBeInTheDocument();
  });

  it('control: Ctrl+K still opens the command palette', async () => {
    render(<App />);
    await flushPromises();

    fireEvent.keyDown(window, { key: 'k', ctrlKey: true });
    expect(screen.getByTestId('command-palette')).toBeInTheDocument();
  });
});
