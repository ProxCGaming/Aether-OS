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

// Full Furnish Spec C (FF3-07): the opt-in picker upgrades — item
// meta lines + badges, and the searchable (Wave 3) pattern. All
// off by default; the suite above pins the unchanged behaviour.
describe('Dropdown (Spec C picker upgrades)', () => {
  const META_OPTIONS = [
    { value: 'a', label: 'Model A', meta: 'Vision · Tools · $$' },
    { value: 'b', label: 'Model B', badge: { text: 'Unusable', tone: 'danger' } },
    { value: 'c', label: 'Model C' },
  ];

  it('renders an item meta line and badge only when present', () => {
    render(<Dropdown value={null} onChange={jest.fn()} options={META_OPTIONS} />);
    fireEvent.click(screen.getByText('Select...'));
    expect(screen.getByText('Vision · Tools · $$')).toBeInTheDocument();
    const badge = screen.getByText('Unusable');
    expect(badge).toBeInTheDocument();
    expect(badge).toHaveAttribute('data-badge', 'danger');
    // Model C carries neither: its row is the bare label.
    expect(screen.getByRole('button', { name: 'Model C' })).toBeInTheDocument();
  });

  it('a badged item is still selectable (badge, not a block)', () => {
    const onChange = jest.fn();
    render(<Dropdown value={null} onChange={onChange} options={META_OPTIONS} />);
    fireEvent.click(screen.getByText('Select...'));
    fireEvent.click(screen.getByText('Model B'));
    expect(onChange).toHaveBeenCalledWith('b');
  });

  it('no search input unless searchable', () => {
    render(<Dropdown value={null} onChange={jest.fn()} options={FLAT_OPTIONS} testId="dd" />);
    fireEvent.click(screen.getByText('Select...'));
    expect(screen.queryByTestId('dd-search')).not.toBeInTheDocument();
  });

  it('searchable filters items by label and group name', () => {
    render(
      <Dropdown value={null} onChange={jest.fn()} options={GROUPED_OPTIONS} searchable testId="dd" />
    );
    fireEvent.click(screen.getByText('Select...'));
    fireEvent.change(screen.getByTestId('dd-search'), { target: { value: 'claude' } });
    expect(screen.queryByRole('button', { name: 'GPT-4' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Claude Sonnet' })).toBeInTheDocument();
    // A provider (group) name keeps that provider's whole group.
    fireEvent.change(screen.getByTestId('dd-search'), { target: { value: 'openai' } });
    expect(screen.getByRole('button', { name: 'GPT-4' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'GPT-3.5' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Claude Sonnet' })).not.toBeInTheDocument();
  });

  it('a fruitless search shows the no-results state and Clear search restores the list', () => {
    render(
      <Dropdown value={null} onChange={jest.fn()} options={GROUPED_OPTIONS} searchable testId="dd" noResultsTitle="No matching models" />
    );
    fireEvent.click(screen.getByText('Select...'));
    fireEvent.change(screen.getByTestId('dd-search'), { target: { value: 'zzz' } });
    expect(screen.getByTestId('dd-no-results')).toBeInTheDocument();
    expect(screen.getByText('No matching models')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Clear search'));
    expect(screen.queryByTestId('dd-no-results')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'GPT-4' })).toBeInTheDocument();
  });

  it('Enter in the search input picks the first visible enabled item', () => {
    const onChange = jest.fn();
    render(
      <Dropdown value={null} onChange={onChange} options={GROUPED_OPTIONS} searchable testId="dd" />
    );
    fireEvent.click(screen.getByText('Select...'));
    const input = screen.getByTestId('dd-search');
    fireEvent.change(input, { target: { value: 'claude' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(onChange).toHaveBeenCalledWith('claude');
  });

  it('closing the menu forgets the filter', () => {
    render(
      <Dropdown value={null} onChange={jest.fn()} options={GROUPED_OPTIONS} searchable testId="dd" />
    );
    fireEvent.click(screen.getByText('Select...'));
    fireEvent.change(screen.getByTestId('dd-search'), { target: { value: 'claude' } });
    expect(screen.queryByRole('button', { name: 'GPT-4' })).not.toBeInTheDocument();
    fireEvent.mouseDown(document.body);
    fireEvent.click(screen.getByText('Select...'));
    expect(screen.getByTestId('dd-search')).toHaveValue('');
    expect(screen.getByRole('button', { name: 'GPT-4' })).toBeInTheDocument();
  });
});
