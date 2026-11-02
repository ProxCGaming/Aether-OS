import React from 'react';
import { render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';
import DiffRenderer from '../DiffRenderer';

// DiffRenderer: a unified diff with meta lines, hunk headers, and
// added / removed / context lines tracked against old and new line
// numbers parsed from the hunk headers.

describe('DiffRenderer', () => {
  const diff = [
    '--- a/file.py',
    '+++ b/file.py',
    '@@ -1,2 +1,2 @@',
    '-old_line',
    '+new_line',
    ' context_line',
  ].join('\n');

  it('renders meta, hunk, added, removed and context lines with their classes', () => {
    render(<DiffRenderer diff={diff} />);
    expect(screen.getByTestId('diff-renderer')).toBeInTheDocument();
    expect(screen.getByText('--- a/file.py')).toHaveClass('diff-line-meta');
    expect(screen.getByText('+++ b/file.py')).toHaveClass('diff-line-meta');
    expect(screen.getByText('@@ -1,2 +1,2 @@')).toHaveClass('diff-hunk-header');
    expect(screen.getByText('-old_line')).toHaveClass('diff-line-removed');
    expect(screen.getByText('+new_line')).toHaveClass('diff-line-added');
    expect(screen.getByText('context_line', { exact: false })).toBeInTheDocument();
  });

  it('numbers lines from the hunk header offsets', () => {
    const offsetDiff = [
      '@@ -10,1 +20,1 @@',
      ' plain context',
    ].join('\n');
    render(<DiffRenderer diff={offsetDiff} />);
    // The context line is old line 10 and new line 20.
    expect(screen.getByText('10')).toBeInTheDocument();
    expect(screen.getByText('20')).toBeInTheDocument();
  });

  it('advances old and new counters independently across a hunk', () => {
    const seqDiff = [
      '@@ -1,3 +1,3 @@',
      ' first',
      '-gone',
      '+here',
      ' last',
    ].join('\n');
    render(<DiffRenderer diff={seqDiff} />);
    // Old side reaches 3 (first/gone/last), new side reaches 3
    // (first/here/last); both counters must appear.
    expect(screen.getAllByText('3').length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText('-gone')).toHaveClass('diff-line-removed');
    expect(screen.getByText('+here')).toHaveClass('diff-line-added');
  });

  it('renders an empty container for an empty diff', () => {
    render(<DiffRenderer diff="" />);
    const el = screen.getByTestId('diff-renderer');
    expect(el).toBeInTheDocument();
    expect(el.querySelectorAll('.diff-line')).toHaveLength(0);
  });

  it('renders with no props at all (defaults)', () => {
    render(<DiffRenderer />);
    expect(screen.getByTestId('diff-renderer')).toBeInTheDocument();
  });
});
