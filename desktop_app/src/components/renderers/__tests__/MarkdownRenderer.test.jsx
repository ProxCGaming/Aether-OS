import React from 'react';
import { render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';

// react-markdown is ESM-only; stub it for Jest (repo convention).
// The stub renders the raw markdown it receives, which is exactly
// what these tests pin: MarkdownRenderer owns the styled wrapper
// and hands the artifact content to ReactMarkdown untouched.
jest.mock('react-markdown', () => ({
  __esModule: true,
  default: ({ children }) => <div data-testid="markdown-stub">{children}</div>,
}));

import MarkdownRenderer from '../MarkdownRenderer';

describe('MarkdownRenderer', () => {
  it('renders the styled markdown wrapper', () => {
    render(<MarkdownRenderer content="# Title" />);
    const wrapper = screen.getByTestId('markdown-renderer');
    expect(wrapper).toBeInTheDocument();
    expect(wrapper).toHaveClass('markdown-content');
  });

  it('hands the full markdown content to the markdown renderer', () => {
    const md = '# Document Title\n\nThis is a paragraph.';
    render(<MarkdownRenderer content={md} />);
    expect(screen.getByTestId('markdown-stub')).toBeInTheDocument();
    expect(screen.getByText(/Document Title/)).toBeInTheDocument();
    expect(screen.getByText(/This is a paragraph/)).toBeInTheDocument();
  });

  it('passes multi-block content through intact', () => {
    const md = '## Section\n\n- first item\n- second item\n\nSome closing text.';
    render(<MarkdownRenderer content={md} />);
    expect(screen.getByText(/Section/)).toBeInTheDocument();
    expect(screen.getByText(/first item/)).toBeInTheDocument();
    expect(screen.getByText(/second item/)).toBeInTheDocument();
    expect(screen.getByText(/closing text/)).toBeInTheDocument();
  });

  it('renders an empty document without crashing', () => {
    render(<MarkdownRenderer content="" />);
    expect(screen.getByTestId('markdown-renderer')).toBeInTheDocument();
    expect(screen.getByTestId('markdown-stub')).toBeEmptyDOMElement();
  });

  it('renders with no props at all (defaults)', () => {
    render(<MarkdownRenderer />);
    expect(screen.getByTestId('markdown-renderer')).toBeInTheDocument();
  });
});
