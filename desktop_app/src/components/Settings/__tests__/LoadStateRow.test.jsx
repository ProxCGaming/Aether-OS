import React from 'react';
import { render, screen } from '@testing-library/react';
import LoadStateRow, { loadDisplay } from '../LoadStateRow';

const TEXTS = {
  loadingText: 'Loading credentials…',
  offlineText: 'Waiting for the engine — credentials will load when it reconnects.',
  errorText: "Couldn't load credentials.",
};

describe('loadDisplay', () => {
  it('ready renders nothing regardless of connection', () => {
    expect(loadDisplay('ready', false)).toBeNull();
    expect(loadDisplay('ready', true)).toBeNull();
  });

  it('error wins regardless of connection', () => {
    expect(loadDisplay('error', false)).toBe('error');
    expect(loadDisplay('error', true)).toBe('error');
  });

  it('loading renders as offline when disconnected, loading when connected', () => {
    expect(loadDisplay('loading', false)).toBe('offline');
    expect(loadDisplay('loading', true)).toBe('loading');
    // Unknown connection (panel mounted outside the socket context)
    // still reads as loading, never as empty.
    expect(loadDisplay('loading', null)).toBe('loading');
  });
});

describe('LoadStateRow', () => {
  it('renders nothing when ready', () => {
    const { container } = render(
      <LoadStateRow loadState="ready" isConnected={false} testId="row" {...TEXTS} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it('shows the loading text while connected', () => {
    render(<LoadStateRow loadState="loading" isConnected testId="row" {...TEXTS} />);
    const row = screen.getByTestId('row');
    expect(row.dataset.loadDisplay).toBe('loading');
    expect(row.textContent).toBe(TEXTS.loadingText);
  });

  it('shows the waiting-for-engine text while disconnected', () => {
    render(<LoadStateRow loadState="loading" isConnected={false} testId="row" {...TEXTS} />);
    const row = screen.getByTestId('row');
    expect(row.dataset.loadDisplay).toBe('offline');
    expect(row.textContent).toBe(TEXTS.offlineText);
  });

  it('shows the error text on a failed load', () => {
    render(<LoadStateRow loadState="error" isConnected testId="row" {...TEXTS} />);
    const row = screen.getByTestId('row');
    expect(row.dataset.loadDisplay).toBe('error');
    expect(row.textContent).toBe(TEXTS.errorText);
  });
});
