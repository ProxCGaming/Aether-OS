import React from 'react';
import { render, screen, act } from '@testing-library/react';
import LoadStateRow, { loadDisplay, LOAD_SLOW_NOTICE_MS, DEFAULT_SLOW_TEXT } from '../LoadStateRow';

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

describe('LoadStateRow slow notice (Wave 3 slice 2, mechanism 5)', () => {
  beforeEach(() => { jest.useFakeTimers(); });
  afterEach(() => { jest.useRealTimers(); });

  it('gains the slow line after LOAD_SLOW_NOTICE_MS of loading, and only then', () => {
    render(<LoadStateRow loadState="loading" isConnected testId="row" {...TEXTS} />);
    expect(screen.queryByTestId('row-slow')).not.toBeInTheDocument();
    act(() => { jest.advanceTimersByTime(LOAD_SLOW_NOTICE_MS - 1); });
    expect(screen.queryByTestId('row-slow')).not.toBeInTheDocument();
    act(() => { jest.advanceTimersByTime(1); });
    expect(screen.getByTestId('row-slow').textContent).toBe(DEFAULT_SLOW_TEXT);
    // The base copy is unchanged — the slow line is an addition,
    // never a state change or substitute data.
    expect(screen.getByTestId('row').dataset.loadDisplay).toBe('loading');
  });

  it('also applies while waiting offline, with custom copy when given', () => {
    render(
      <LoadStateRow loadState="loading" isConnected={false} testId="row" slowText="Still waiting." {...TEXTS} />,
    );
    act(() => { jest.advanceTimersByTime(LOAD_SLOW_NOTICE_MS); });
    expect(screen.getByTestId('row-slow').textContent).toBe('Still waiting.');
  });

  it('never appears for an error, however long it sits', () => {
    render(<LoadStateRow loadState="error" isConnected testId="row" {...TEXTS} />);
    act(() => { jest.advanceTimersByTime(LOAD_SLOW_NOTICE_MS * 3); });
    expect(screen.queryByTestId('row-slow')).not.toBeInTheDocument();
  });

  it('slowText={null} opts the surface out', () => {
    render(<LoadStateRow loadState="loading" isConnected testId="row" slowText={null} {...TEXTS} />);
    act(() => { jest.advanceTimersByTime(LOAD_SLOW_NOTICE_MS * 2); });
    expect(screen.queryByTestId('row-slow')).not.toBeInTheDocument();
  });
});
