import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import CommandPalette, {
  filterPaletteItems,
  groupPaletteItems,
  paletteGroupFor,
  scorePaletteMatch,
} from '../CommandPalette';

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

describe('CommandPalette offline note (Lane C grouped S4)', () => {
  it('shows no offline note while connected (the default)', () => {
    render(<CommandPalette open items={items} onClose={jest.fn()} />);
    expect(screen.queryByTestId('palette-offline-note')).not.toBeInTheDocument();
    render(<CommandPalette open items={items} onClose={jest.fn()} isConnected />);
    expect(screen.queryByTestId('palette-offline-note')).not.toBeInTheDocument();
  });

  it('while disconnected, a slim note says items may be stale — and items still run', () => {
    const run = jest.fn();
    const onClose = jest.fn();
    render(
      <CommandPalette
        open
        items={[{ id: 'a', kind: 'action', label: 'Do thing', run }]}
        onClose={onClose}
        isConnected={false}
      />
    );
    const note = screen.getByTestId('palette-offline-note');
    expect(note).toHaveTextContent(/Engine disconnected/);
    expect(note).toHaveTextContent(/stale/);
    // Item behaviour is unchanged: Enter still runs the selection.
    fireEvent.keyDown(screen.getByLabelText('Search commands'), { key: 'Enter' });
    expect(run).toHaveBeenCalledTimes(1);
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

describe('CommandPalette search input focus ring (E6-F16)', () => {
  it('the palette search input carries the shared focus-ring class', () => {
    render(<CommandPalette open items={items} onClose={jest.fn()} />);
    const input = screen.getByLabelText('Search commands');
    expect(input.classList.contains('aether-search-input')).toBe(true);
    expect(input.style.outline).toBe('');
  });
});

describe('palette scoring (S-3: word-start / contiguous / recency)', () => {
  it('a word-start verbatim match outranks a scattered subsequence match', () => {
    const voice = { id: 'v', kind: 'settings', label: 'Settings: Voice Tuning', hint: 'Settings section' };
    const stray = { id: 's', kind: 'session', label: 'Verbose omnichannel notes' };
    expect(scorePaletteMatch(voice, 'voice')).toBeGreaterThan(scorePaletteMatch(stray, 'voice'));
    expect(filterPaletteItems([stray, voice], 'voice').map((i) => i.id)).toEqual(['v', 's']);
  });

  it('a contiguous match outranks the same letters spread out', () => {
    const tight = { id: 't', kind: 'action', label: 'Newchat' };
    const loose = { id: 'l', kind: 'action', label: 'New old chat' };
    expect(scorePaletteMatch(tight, 'newchat')).toBeGreaterThan(scorePaletteMatch(loose, 'newchat'));
  });

  it('recent use breaks an otherwise even ranking, most recent first', () => {
    const a = { id: 'a', kind: 'session', label: 'Chat alpha' };
    const b = { id: 'b', kind: 'session', label: 'Chat beta' };
    expect(filterPaletteItems([a, b], 'chat').map((i) => i.id)).toEqual(['a', 'b']);
    expect(filterPaletteItems([a, b], 'chat', ['b']).map((i) => i.id)).toEqual(['b', 'a']);
  });

  it('is deterministic: equal scores keep host order across runs', () => {
    const many = [
      { id: 'm1', kind: 'model', label: 'gpt-x', hint: 'openai' },
      { id: 'm2', kind: 'model', label: 'gpt-x', hint: 'openai' },
      { id: 'm3', kind: 'model', label: 'gpt-x', hint: 'openai' },
    ];
    const first = filterPaletteItems(many, 'gpt').map((i) => i.id);
    expect(first).toEqual(['m1', 'm2', 'm3']);
    expect(filterPaletteItems(many, 'gpt').map((i) => i.id)).toEqual(first);
  });

  it('no match scores null and filters out', () => {
    expect(scorePaletteMatch(items[0], 'zzzz')).toBeNull();
  });
});

describe('palette grouping (S-3)', () => {
  it('derives groups from kind; Go to actions are navigation', () => {
    expect(paletteGroupFor({ kind: 'action', label: 'New chat' })).toBe('Actions');
    expect(paletteGroupFor({ kind: 'action', label: 'Go to Dashboard' })).toBe('Go to');
    expect(paletteGroupFor({ kind: 'settings', label: 'Settings: Voice' })).toBe('Settings');
    expect(paletteGroupFor({ kind: 'session', label: 'A chat' })).toBe('Sessions');
    expect(paletteGroupFor({ kind: 'model', label: 'gpt' })).toBe('Models');
    expect(paletteGroupFor({ kind: 'model', label: 'gpt', group: 'Custom' })).toBe('Custom');
  });

  it('groups render in the fixed order and cap per group, not flat', () => {
    const settings = Array.from({ length: 8 }, (_, i) => ({
      id: `settings-${i}`, kind: 'settings', label: `Settings: Section ${i}`, run: jest.fn(),
    }));
    const grouped = groupPaletteItems([...settings, ...items]);
    expect(grouped.map((g) => g.name)).toEqual(['Actions', 'Settings', 'Sessions', 'Models']);
    expect(grouped.find((g) => g.name === 'Settings').items).toHaveLength(6);
    expect(grouped.find((g) => g.name === 'Actions').items).toHaveLength(1);
  });
});

describe('CommandPalette navigation surface (S-3)', () => {
  it('renders group headers, icons, hints and the footer key hints', () => {
    render(<CommandPalette open items={items} onClose={jest.fn()} />);
    expect(screen.getByText('Actions')).toBeInTheDocument();
    expect(screen.getByText('Sessions')).toBeInTheDocument();
    expect(screen.getByText('Models')).toBeInTheDocument();
    const option = screen.getByTestId('palette-item-model-gemini');
    expect(option.querySelector('svg')).not.toBeNull();
    expect(option).toHaveTextContent('google_gemini');
    expect(screen.getByText('↑↓ Navigate')).toBeInTheDocument();
    expect(screen.getByText('Enter Run')).toBeInTheDocument();
    expect(screen.getByText('Esc Close')).toBeInTheDocument();
  });

  it('exposes the ARIA combobox pattern and tracks the active option', () => {
    render(<CommandPalette open items={items} onClose={jest.fn()} />);
    const input = screen.getByLabelText('Search commands');
    expect(input).toHaveAttribute('role', 'combobox');
    expect(input).toHaveAttribute('aria-expanded', 'true');
    expect(input).toHaveAttribute('aria-controls', 'palette-listbox');
    expect(input).toHaveAttribute('aria-activedescendant', 'palette-option-new-chat');
    expect(screen.getByRole('listbox')).toBeInTheDocument();
    expect(screen.getByTestId('palette-item-new-chat')).toHaveAttribute('role', 'option');
    expect(screen.getByTestId('palette-item-new-chat')).toHaveAttribute('aria-selected', 'true');
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    expect(input).toHaveAttribute('aria-activedescendant', 'palette-option-sess-1');
    expect(screen.getByTestId('palette-item-sess-1')).toHaveAttribute('aria-selected', 'true');
    const dialog = screen.getByTestId('command-palette').firstChild;
    expect(dialog).toHaveAttribute('aria-modal', 'true');
  });

  it('caps each group at 6 in the rendered list', () => {
    const settings = Array.from({ length: 9 }, (_, i) => ({
      id: `settings-${i}`, kind: 'settings', label: `Settings: Section ${i}`, run: jest.fn(),
    }));
    render(<CommandPalette open items={settings} onClose={jest.fn()} />);
    expect(screen.getAllByRole('option')).toHaveLength(6);
  });

  it('gibberish gets the shared no-results state with a one-click recovery', () => {
    render(<CommandPalette open items={items} onClose={jest.fn()} />);
    const input = screen.getByLabelText('Search commands');
    fireEvent.change(input, { target: { value: 'zzzz' } });
    const empty = screen.getByTestId('palette-no-results');
    expect(empty).toHaveAttribute('data-empty-variant', 'no-results');
    expect(empty).toHaveTextContent('No matches');
    fireEvent.click(screen.getByText('Clear search'));
    expect(input).toHaveValue('');
    expect(screen.getByTestId('palette-item-new-chat')).toBeInTheDocument();
  });

  it('an item run from the palette leads as a recent on the next open, unduplicated', () => {
    const onClose = jest.fn();
    const run = jest.fn();
    const withRuns = items.map((i) => ({ ...i, run: i.id === 'sess-1' ? run : jest.fn() }));
    const { rerender } = render(<CommandPalette open items={withRuns} onClose={onClose} />);
    fireEvent.click(screen.getByTestId('palette-item-sess-1'));
    expect(run).toHaveBeenCalledTimes(1);
    rerender(<CommandPalette open={false} items={withRuns} onClose={onClose} />);
    rerender(<CommandPalette open items={withRuns} onClose={onClose} />);
    expect(screen.getByText('Recent')).toBeInTheDocument();
    expect(screen.getAllByTestId('palette-item-sess-1')).toHaveLength(1);
    const options = screen.getAllByRole('option');
    expect(options[0]).toHaveAttribute('data-testid', 'palette-item-sess-1');
  });
});
