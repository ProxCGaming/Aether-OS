import React from 'react';
import { render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';
import FitBadge, { LoadStateBadge } from '../FitBadge';

// Spec D (FF3-08): the shared fit / load-state badges. Fit is
// judged engine-side and carried on the payload; an unjudged
// row (no fit, unknown tier) and a row whose backend cannot
// report residency (no `loaded` field) render NOTHING — the
// badges never guess.

describe('FitBadge', () => {
  it('renders the three tiers with the shared vocabulary', () => {
    const { rerender } = render(
      <FitBadge fit={{ tier: 'comfortable', reason: 'Fits.' }} testId="fit" />
    );
    expect(screen.getByTestId('fit')).toHaveTextContent('Comfortable');
    expect(screen.getByTestId('fit')).toHaveAttribute('title', 'Fits.');
    rerender(<FitBadge fit={{ tier: 'tight', reason: 'Snug.' }} testId="fit" />);
    expect(screen.getByTestId('fit')).toHaveTextContent('Tight');
    rerender(<FitBadge fit={{ tier: 'too_large', reason: 'Nope.' }} testId="fit" />);
    expect(screen.getByTestId('fit')).toHaveTextContent('Too large');
  });

  it('renders nothing when the row is unjudged', () => {
    const { container, rerender } = render(<FitBadge fit={null} testId="fit" />);
    expect(container).toBeEmptyDOMElement();
    rerender(<FitBadge fit={undefined} testId="fit" />);
    expect(container).toBeEmptyDOMElement();
    rerender(<FitBadge fit={{ tier: 'enormous' }} testId="fit" />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe('LoadStateBadge', () => {
  it('distinguishes Loaded from On disk', () => {
    const { rerender } = render(<LoadStateBadge loaded testId="load-state" />);
    expect(screen.getByTestId('load-state')).toHaveTextContent('Loaded');
    rerender(<LoadStateBadge loaded={false} testId="load-state" />);
    expect(screen.getByTestId('load-state')).toHaveTextContent('On disk');
  });

  it('omits entirely when the backend did not report residency', () => {
    const { container } = render(<LoadStateBadge loaded={undefined} testId="load-state" />);
    expect(container).toBeEmptyDOMElement();
  });
});
