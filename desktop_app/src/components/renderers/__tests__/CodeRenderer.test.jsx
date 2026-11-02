import React from 'react';
import { render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';
import CodeRenderer from '../CodeRenderer';

// CodeRenderer: the artifact code view — source text split into
// lines, a line-number gutter, and lightweight token highlighting
// (keywords, strings, comments, numbers get their own spans).

describe('CodeRenderer', () => {
  it('renders every line of the content', () => {
    const code = "def hello():\n    return 'world'";
    render(<CodeRenderer content={code} />);
    expect(screen.getByTestId('code-renderer')).toBeInTheDocument();
    expect(screen.getByText('def')).toBeInTheDocument();
    expect(screen.getByText(/hello/)).toBeInTheDocument();
    expect(screen.getByText('return')).toBeInTheDocument();
    expect(screen.getByText("'world'")).toBeInTheDocument();
  });

  it('shows a line-number gutter matching the line count', () => {
    const code = 'alpha = one\nbeta = two\ngamma = three';
    render(<CodeRenderer content={code} />);
    expect(screen.getByText('1')).toBeInTheDocument();
    expect(screen.getByText('2')).toBeInTheDocument();
    expect(screen.getByText('3')).toBeInTheDocument();
    expect(screen.queryByText('4')).toBeNull();
    expect(screen.getByText(/alpha/)).toBeInTheDocument();
    expect(screen.getByText(/gamma/)).toBeInTheDocument();
  });

  it('keeps comment text visible as part of its line', () => {
    const code = '# configure the worker\nvalue = 1';
    render(<CodeRenderer content={code} />);
    expect(screen.getByText('# configure the worker')).toBeInTheDocument();
    expect(screen.getByText(/value/)).toBeInTheDocument();
  });

  it('renders an empty line gutter for empty content instead of crashing', () => {
    render(<CodeRenderer content="" />);
    expect(screen.getByTestId('code-renderer')).toBeInTheDocument();
    expect(screen.getByText('1')).toBeInTheDocument();
    expect(screen.queryByText('2')).toBeNull();
  });

  it('renders with no props at all (defaults)', () => {
    render(<CodeRenderer />);
    expect(screen.getByTestId('code-renderer')).toBeInTheDocument();
  });
});
