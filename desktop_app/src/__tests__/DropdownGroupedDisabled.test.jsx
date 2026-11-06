import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import Dropdown from '../Dropdown';

// Wave 3, D2-19: the flat option path honours `disabled` (guarded
// click + disabled attribute + dimmed styling); the grouped path
// called onChange unconditionally. Both paths must honour the
// component's own contract.
describe('Dropdown grouped items (D2-19)', () => {
  const groupedOptions = [
    {
      group: 'Provider A',
      items: [
        { value: 'a:one', label: 'Model One' },
        { value: 'a:two', label: 'Model Two', disabled: true },
      ],
    },
  ];

  test('a disabled grouped item is not selectable and carries the disabled attribute', () => {
    const onChange = jest.fn();
    render(<Dropdown value="a:one" onChange={onChange} options={groupedOptions} />);
    fireEvent.click(screen.getByText('Model One'));
    const disabledItem = screen.getByText('Model Two').closest('button');
    expect(disabledItem).toBeDisabled();
    fireEvent.click(disabledItem);
    expect(onChange).not.toHaveBeenCalled();
  });

  test('an enabled grouped item still selects and closes', () => {
    const onChange = jest.fn();
    render(<Dropdown value="" onChange={onChange} options={groupedOptions} placeholder="Pick" />);
    fireEvent.click(screen.getByText('Pick'));
    fireEvent.click(screen.getByText('Model One'));
    expect(onChange).toHaveBeenCalledWith('a:one');
  });
});
