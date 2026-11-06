import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import DataRenderer from '../DataRenderer';
import DiffRenderer from '../DiffRenderer';
import CodeRenderer from '../CodeRenderer';
import ImageViewer from '../ImageViewer';

// Wave 3 (D3 chunk) — renderer correctness fixes. Each test pins a
// defect from the blow-up ledger (passA-D3) that the pre-existing
// suites did not cover.

describe('D3-01 — DataRenderer routes by the declared format, not content sniffing', () => {
  it('renders a CSV table for csv content whose first cell starts with "["', () => {
    const csv = '[1,2],count\n[3,4],other';
    render(<DataRenderer content={csv} format="csv" />);
    expect(screen.getByTestId('csv-table')).toBeInTheDocument();
    expect(screen.queryByText(/Invalid JSON/)).not.toBeInTheDocument();
    expect(screen.getByText('[1')).toBeInTheDocument();
    expect(screen.getByText('other')).toBeInTheDocument();
  });

  it('still detects a JSON array when no format is declared', () => {
    render(<DataRenderer content={'["a", "b"]'} format="" />);
    expect(screen.getByTestId('json-tree')).toBeInTheDocument();
  });
});

describe('D3-14 — DiffRenderer: ---/+++ are meta only in the file header', () => {
  it('classifies body lines whose content begins -- / ++ as removed / added', () => {
    const diff = [
      '@@ -5,2 +5,2 @@',
      ' ctx',
      '---gone',
      '+++here',
    ].join('\n');
    render(<DiffRenderer diff={diff} />);
    expect(screen.getByText('---gone')).toHaveClass('diff-line-removed');
    expect(screen.getByText('+++here')).toHaveClass('diff-line-added');
  });

  it('keeps line numbers in step when body lines begin -- / ++', () => {
    const diff = [
      '@@ -5,2 +5,2 @@',
      ' ctx',
      '---gone',
      '+++here',
    ].join('\n');
    render(<DiffRenderer diff={diff} />);
    // ctx is old/new 5; the removed line is old 6, the added line new 6.
    expect(screen.getAllByText('6').length).toBeGreaterThanOrEqual(2);
  });

  it('still treats the second file header of a multi-file diff as meta', () => {
    const diff = [
      'diff --git a/x b/x',
      '--- a/x',
      '+++ b/x',
      '@@ -1,1 +1,1 @@',
      '-a',
      '+b',
      'diff --git a/y b/y',
      '--- a/y',
      '+++ b/y',
      '@@ -1,1 +1,1 @@',
      '-c',
      '+d',
    ].join('\n');
    render(<DiffRenderer diff={diff} />);
    expect(screen.getByText('--- a/y')).toHaveClass('diff-line-meta');
    expect(screen.getByText('+++ b/y')).toHaveClass('diff-line-meta');
  });
});

describe('D3-15 — CodeRenderer honours its language contract', () => {
  it('does not treat # as a comment in JavaScript content', () => {
    render(<CodeRenderer content={'# heading\nconst x = 1;'} fileName="app.js" />);
    // Pre-fix the whole '# heading' line was one comment span.
    expect(screen.queryByText('# heading')).not.toBeInTheDocument();
    expect(screen.getByText(/const/)).toBeInTheDocument();
  });

  it('does not treat # as a comment in CSS content (an id selector is not a comment)', () => {
    render(<CodeRenderer content="#header { margin: 0; }" fileName="styles.css" />);
    expect(screen.queryByText('#header { margin: 0; }')).not.toBeInTheDocument();
  });

  it('treats # as a comment in Python content (via fileName)', () => {
    render(<CodeRenderer content="# configure the worker" fileName="main.py" />);
    expect(screen.getByText('# configure the worker')).toBeInTheDocument();
  });

  it('treats # as a comment when the language prop says python', () => {
    render(<CodeRenderer content="# configure the worker" language="python" />);
    expect(screen.getByText('# configure the worker')).toBeInTheDocument();
  });

  it('does not treat # as a comment when the language prop says javascript', () => {
    render(<CodeRenderer content="# configure the worker" language="javascript" />);
    expect(screen.queryByText('# configure the worker')).not.toBeInTheDocument();
  });

  it('caps the rendered lines and says how many were not shown', () => {
    const big = Array.from({ length: 3000 }, (_, i) => `line ${i + 1}`).join('\n');
    render(<CodeRenderer content={big} fileName="big.txt" />);
    expect(screen.getByText(/lines? not shown/i)).toBeInTheDocument();
    expect(screen.queryByText('3000')).not.toBeInTheDocument();
  });
});

describe('D3-12 — ImageViewer zoom grows the layout (pannable) and load failure is visible', () => {
  const SRC = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==';

  it('applies zoom as a layout size, not a transform, so overflow can scroll', () => {
    render(<ImageViewer src={SRC} alt="Zoomable" />);
    const img = screen.getByAltText('Zoomable');
    fireEvent.click(screen.getByTestId('zoom-in'));
    fireEvent.click(screen.getByTestId('zoom-in'));
    fireEvent.click(screen.getByTestId('zoom-in'));
    fireEvent.click(screen.getByTestId('zoom-in'));
    expect(screen.getByTestId('zoom-level')).toHaveTextContent('200%');
    // A transform scale leaves the layout box at 100% — the overflow
    // has no scroll range and the edges are unreachable. The zoomed
    // size must be real layout size.
    expect(img.style.width).toBe('200%');
    expect(img.style.transform).toBe('');
  });

  it('shows a visible error state when the image fails to load', () => {
    render(<ImageViewer src="data:image/png;base64,broken" alt="Broken" />);
    fireEvent.error(screen.getByAltText('Broken'));
    expect(screen.getByTestId('image-load-error')).toBeInTheDocument();
    expect(screen.queryByAltText('Broken')).not.toBeInTheDocument();
  });

  it('clears the error state when a new src arrives', () => {
    const { rerender } = render(<ImageViewer src="data:image/png;base64,broken" alt="Swap" />);
    fireEvent.error(screen.getByAltText('Swap'));
    expect(screen.getByTestId('image-load-error')).toBeInTheDocument();
    rerender(<ImageViewer src={SRC} alt="Swap" />);
    expect(screen.queryByTestId('image-load-error')).not.toBeInTheDocument();
    expect(screen.getByAltText('Swap')).toBeInTheDocument();
  });
});
