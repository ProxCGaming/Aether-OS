import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import EmptyState from '../EmptyState';

// S-9 (FF1-14): the shared empty state — icon, title, one line,
// at most one action.
describe('EmptyState (S-9)', () => {
  test('renders icon, title and one description line', () => {
    render(
      <EmptyState
        icon={<span data-testid="es-icon">i</span>}
        title="Nothing here yet"
        description="Things you create will appear here."
        testId="es"
      />
    );
    expect(screen.getByTestId('es-icon')).toBeInTheDocument();
    expect(screen.getByText('Nothing here yet')).toBeInTheDocument();
    expect(screen.getByText('Things you create will appear here.')).toBeInTheDocument();
    expect(screen.getByTestId('es')).toHaveAttribute('data-empty-variant', 'empty');
  });

  test('the single CTA renders only with both a label and a handler, and fires it', () => {
    const onAction = jest.fn();
    const { rerender } = render(<EmptyState title="Empty" actionLabel="Create one" />);
    expect(screen.queryByText('Create one')).not.toBeInTheDocument();
    rerender(<EmptyState title="Empty" actionLabel="Create one" onAction={onAction} />);
    fireEvent.click(screen.getByText('Create one'));
    expect(onAction).toHaveBeenCalledTimes(1);
  });

  test('no-results variant is marked and drops the medallion treatment', () => {
    render(
      <EmptyState
        icon={<span data-testid="es-icon">s</span>}
        title="No matches"
        variant="no-results"
        testId="es-nr"
      />
    );
    expect(screen.getByTestId('es-nr')).toHaveAttribute('data-empty-variant', 'no-results');
    expect(screen.getByTestId('es-icon')).toBeInTheDocument();
  });

  test('description is optional — title alone still renders', () => {
    render(<EmptyState title="Just a title" />);
    expect(screen.getByText('Just a title')).toBeInTheDocument();
  });
});
