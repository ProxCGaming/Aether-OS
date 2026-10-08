import React from 'react';
import { render, screen, fireEvent, act } from '@testing-library/react';
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

describe('App', () => {
  it('renders the main navigation', () => {
    render(<App />);
    expect(screen.getByText('Chat')).toBeInTheDocument();
    expect(screen.getByText('Dashboard')).toBeInTheDocument();
    expect(screen.getByText('Projects')).toBeInTheDocument();
    expect(screen.getByText('Settings')).toBeInTheDocument();
  });

  it('switches to the Dashboard tab and back via Start New Workflow', () => {
    render(<App />);

    fireEvent.click(screen.getByText('Dashboard'));
    expect(screen.getByText('System Dashboard')).toBeInTheDocument();

    // Phase 9, B3: the button must actually do something (navigates to Chat).
    fireEvent.click(screen.getByText('Start New Workflow'));
    expect(screen.queryByText('System Dashboard')).not.toBeInTheDocument();
  });

  it('renders the decorative knowledge graph background without fabricated nodes', () => {
    // Phase 9, B3: no random ambient nodes simulating a populated network.
    const { container } = render(<App />);
    expect(screen.getByTestId('force-graph-stub')).toBeInTheDocument();
    expect(container.textContent).not.toMatch(/amb_\d+/);
  });
});
