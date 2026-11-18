/** D4-1: the shared search/filter toolbar — one chrome for every
 * capability panel: search input, the five chips with counts,
 * the result count and clear-all. */
import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import CapabilityFilterBar from '../CapabilityFilterBar';

function baseProps(overrides = {}) {
  return {
    query: '',
    onQueryChange: jest.fn(),
    filter: 'all',
    onFilterChange: jest.fn(),
    counts: { all: 4, enabled: 2, disabled: 1, error: 1, updates: 0 },
    resultCount: 4,
    totalCount: 4,
    ...overrides,
  };
}

describe('CapabilityFilterBar (D4-1)', () => {
  test('renders the search input, all five chips with counts, and the result count', () => {
    render(<CapabilityFilterBar {...baseProps()} />);
    expect(screen.getByPlaceholderText('Search by name…')).toBeInTheDocument();
    expect(screen.getByText('All · 4')).toBeInTheDocument();
    expect(screen.getByText('Enabled · 2')).toBeInTheDocument();
    expect(screen.getByText('Disabled · 1')).toBeInTheDocument();
    expect(screen.getByText('Error · 1')).toBeInTheDocument();
    expect(screen.getByText('Updates · 0')).toBeInTheDocument();
    expect(screen.getByText('Showing 4 of 4')).toBeInTheDocument();
    // Nothing to clear while no search/filter is active.
    expect(screen.queryByText('Clear search & filters')).not.toBeInTheDocument();
  });

  test('typing fires onQueryChange and clicking a chip fires onFilterChange', () => {
    const props = baseProps();
    render(<CapabilityFilterBar {...props} />);
    fireEvent.change(screen.getByPlaceholderText('Search by name…'), { target: { value: 'file' } });
    expect(props.onQueryChange).toHaveBeenCalledWith('file');
    fireEvent.click(screen.getByText('Disabled · 1'));
    expect(props.onFilterChange).toHaveBeenCalledWith('disabled');
  });

  test('the active chip is pressed; clear-all appears while filtering and resets both', () => {
    const props = baseProps({ query: 'file', filter: 'disabled', resultCount: 1 });
    render(<CapabilityFilterBar {...props} />);
    expect(screen.getByText('Disabled · 1')).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByText('All · 4')).toHaveAttribute('aria-pressed', 'false');
    expect(screen.getByText('Showing 1 of 4')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Clear search & filters'));
    expect(props.onQueryChange).toHaveBeenCalledWith('');
    expect(props.onFilterChange).toHaveBeenCalledWith('all');
  });
});
