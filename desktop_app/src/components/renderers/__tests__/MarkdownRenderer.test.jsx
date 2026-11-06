import React from 'react';
import { render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';

// react-markdown is ESM-only; stub it for Jest (repo convention).
// The stub renders the raw markdown it receives, which is exactly
// what these tests pin: MarkdownRenderer owns the styled wrapper
// and hands the artifact content to ReactMarkdown untouched. The
// stub also records the props it received so the D3-16 wiring test
// can pin the plugin contract. remark-gfm is ESM-only too; Jest
// maps it to __mocks__/remarkGfmMock.js (see jest.config.cjs),
// whose default export is the sentinel the wiring test recognises.
const mockMarkdownProps = { current: null };
jest.mock('react-markdown', () => ({
  __esModule: true,
  default: (props) => {
    mockMarkdownProps.current = props;
    return <div data-testid="markdown-stub">{props.children}</div>;
  },
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

describe('MarkdownRenderer GFM (D3-16)', () => {
  // Jest cannot load the real ESM markdown stack (see the stub note
  // above), so this pins the wiring contract: MarkdownRenderer must
  // hand ReactMarkdown the remark-gfm plugin, which is what makes
  // GFM tables/task-lists render instead of literal pipes. The real
  // render was verified end-to-end with the installed packages
  // (node + react-dom/server) when the plugin was added.
  it('passes the remark-gfm plugin to ReactMarkdown', () => {
    const remarkGfm = require('remark-gfm').default;
    render(<MarkdownRenderer content="| A | B |\n| --- | --- |\n| 1 | 2 |" />);
    expect(mockMarkdownProps.current).not.toBeNull();
    expect(mockMarkdownProps.current.remarkPlugins).toEqual([remarkGfm]);
  });
});
