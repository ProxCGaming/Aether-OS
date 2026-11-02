import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom';
import Dropdown from '../Dropdown';

const FLAT_OPTIONS = [
  { value: 'gpt-4', label: 'GPT-4' },
  { value: 'claude', label: 'Claude Sonnet' },
  { value: 'local', label: 'Local Model', disabled: true },
];

const GROUPED_OPTIONS = [
  {
    group: 'OpenAI',
    items: [
      { value: 'gpt-4', label: 'GPT-4' },
      { value: 'gpt-3', label: 'GPT-3.5' },
    ],
  },
  {
    group: 'Anthropic',
    items: [{ value: 'claude', label: 'Claude Sonnet' }],
  },
];

// Phase 10.3 D2: Dropdown had zero tests. It is the model/provider
// picker, so a broken selection silently changes which model answers.
describe('Dropdown (Phase 10.3 D2)', () => {
  it('shows the placeholder when nothing is selected', () => {
    render(<Dropdown value={null} onChange={jest.fn()} options={FLAT_OPTIONS} />);
    expect(screen.getByText('Select...')).toBeInTheDocument();
    expect(screen.queryByText('GPT-4')).not.toBeInTheDocument();
  });

  it('shows the selected option label instead of the placeholder', () => {
    render(<Dropdown value="claude" onChange={jest.fn()} options={FLAT_OPTIONS} />);
    expect(screen.getByText('Claude Sonnet')).toBeInTheDocument();
    expect(screen.queryByText('Select...')).not.toBeInTheDocument();
  });

  it('opens on click and lists every option', () => {
    render(<Dropdown value={null} onChange={jest.fn()} options={FLAT_OPTIONS} />);
    fireEvent.click(screen.getByText('Select...'));
    expect(screen.getByRole('button', { name: 'GPT-4' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Claude Sonnet' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Local Model' })).toBeInTheDocument();
  });

  it('clicking an option calls onChange with its value and closes the menu', () => {
    const onChange = jest.fn();
    render(<Dropdown value={null} onChange={onChange} options={FLAT_OPTIONS} />);
    fireEvent.click(screen.getByText('Select...'));
    fireEvent.click(screen.getByRole('button', { name: 'Claude Sonnet' }));
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith('claude');
    // Menu closed: the option buttons are gone.
    expect(screen.queryByRole('button', { name: 'GPT-4' })).not.toBeInTheDocument();
  });

  it('a disabled option cannot be selected', () => {
    const onChange = jest.fn();
    render(<Dropdown value={null} onChange={onChange} options={FLAT_OPTIONS} />);
    fireEvent.click(screen.getByText('Select...'));
    const disabledBtn = screen.getByRole('button', { name: 'Local Model' });
    expect(disabledBtn).toBeDisabled();
    fireEvent.click(disabledBtn);
    expect(onChange).not.toHaveBeenCalled();
  });

  it('clicking outside the dropdown closes the menu', () => {
    render(<Dropdown value={null} onChange={jest.fn()} options={FLAT_OPTIONS} />);
    fireEvent.click(screen.getByText('Select...'));
    expect(screen.getByRole('button', { name: 'GPT-4' })).toBeInTheDocument();
    fireEvent.mouseDown(document.body);
    expect(screen.queryByRole('button', { name: 'GPT-4' })).not.toBeInTheDocument();
  });

  it('grouped options render group headers and select items inside them', () => {
    const onChange = jest.fn();
    render(<Dropdown value={null} onChange={onChange} options={GROUPED_OPTIONS} />);
    fireEvent.click(screen.getByText('Select...'));
    expect(screen.getByText('OpenAI')).toBeInTheDocument();
    expect(screen.getByText('Anthropic')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'GPT-3.5' }));
    expect(onChange).toHaveBeenCalledWith('gpt-3');
  });

  it('finds the selected label inside a group', () => {
    render(<Dropdown value="gpt-3" onChange={jest.fn()} options={GROUPED_OPTIONS} />);
    expect(screen.getByText('GPT-3.5')).toBeInTheDocument();
  });

  it('align=right anchors the menu to the right edge', () => {
    const { container } = render(
      <Dropdown value={null} onChange={jest.fn()} options={FLAT_OPTIONS} align="right" />
    );
    fireEvent.click(screen.getByText('Select...'));
    const menu = container.querySelector('.ios-glass');
    expect(menu).not.toBeNull();
    expect(menu.style.right).toBe('0px');
  });
});
