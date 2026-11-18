/** D4-1: the shared Capability Item shell — name + badges in the
 * header, actions on the right, the panel's detail as children,
 * and the load-error tone for invalid rows. */
import React from 'react';
import { render, screen } from '@testing-library/react';
import '@testing-library/jest-dom';
import CapabilityItem from '../CapabilityItem';

describe('CapabilityItem (D4-1)', () => {
  test('renders name, badges, description, actions and children', () => {
    render(
      <CapabilityItem
        name="read_file"
        badges={<span>builtin</span>}
        description="Reads a file"
        actions={<button>Policy</button>}
        testId="cap-item"
      >
        <div>detail body</div>
      </CapabilityItem>
    );
    expect(screen.getByText('read_file')).toBeInTheDocument();
    expect(screen.getByText('builtin')).toBeInTheDocument();
    expect(screen.getByText('Reads a file')).toBeInTheDocument();
    expect(screen.getByText('Policy')).toBeInTheDocument();
    expect(screen.getByText('detail body')).toBeInTheDocument();
    expect(screen.getByTestId('cap-item')).toHaveAttribute('data-capability-tone', 'default');
  });

  test('the error tone marks the row and tints its border', () => {
    render(
      <CapabilityItem name="broken-skill" tone="error" testId="cap-err">
        <div>could not parse</div>
      </CapabilityItem>
    );
    const row = screen.getByTestId('cap-err');
    expect(row).toHaveAttribute('data-capability-tone', 'error');
    expect(row.style.border).toContain('rgba(201, 42, 42, 0.3)');
    expect(screen.getByText('could not parse')).toBeInTheDocument();
  });

  test('badges, description, actions and children are all optional', () => {
    render(<CapabilityItem name="bare" />);
    expect(screen.getByText('bare')).toBeInTheDocument();
  });
});
