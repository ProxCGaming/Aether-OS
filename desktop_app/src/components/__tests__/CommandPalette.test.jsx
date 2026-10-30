import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import CommandPalette, { filterPaletteItems } from '../CommandPalette';

const items = [
  { id: 'new-chat', kind: 'action', label: 'New chat', run: jest.fn() },
  { id: 'sess-1', kind: 'session', label: 'Plan the launch', run: jest.fn() },
  { id: 'model-gemini', kind: 'model', label: 'gemini-3.5-flash', hint: 'google_gemini', run: jest.fn() },
];

describe('filterPaletteItems (Epic 6, E6-03)', () => {
  it('matches subsequences across label and hint', () => {
    expect(filterPaletteItems(items, 'gemini').map((i) => i.id)).toEqual(['model-gemini']);
    expect(filterPaletteItems(items, 'launch').map((i) => i.id)).toEqual(['sess-1']);
    expect(filterPaletteItems(items, 'nwcht').map((i) => i.id)).toEqual(['new-chat']);
  });

  it('returns everything (capped) for an empty query and nothing for gibberish', () => {
    expect(filterPaletteItems(items, '')).toHaveLength(3);
    expect(filterPaletteItems(items, 'zzzz')).toHaveLength(0);
  });
});

describe('CommandPalette component', () => {
  it('runs the selected item on Enter and closes', () => {
    const onClose = jest.fn();
    const run = jest.fn();
    render(<CommandPalette open items={[{ id: 'a', kind: 'action', label: 'Do thing', run }]} onClose={onClose} />);
    const input = screen.getByLabelText('Search commands');
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(run).toHaveBeenCalledTimes(1);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('closes on Escape and renders nothing when closed', () => {
    const onClose = jest.fn();
    const { rerender } = render(<CommandPalette open items={items} onClose={onClose} />);
    fireEvent.keyDown(screen.getByLabelText('Search commands'), { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
    rerender(<CommandPalette open={false} items={items} onClose={onClose} />);
    expect(screen.queryByTestId('command-palette')).not.toBeInTheDocument();
  });
});
