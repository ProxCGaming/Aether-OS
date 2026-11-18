/** D4-1: the shared capability filter model — the chips and the
 * search must mean the same thing on every capability panel. */
import { CAPABILITY_FILTERS, capabilityFilterCounts, filterCapabilityItems } from '../capabilityFilter';

const ENTRIES = [
  { item: { id: 1 }, name: 'read_file', state: 'enabled', hasUpdate: false },
  { item: { id: 2 }, name: 'write_file', state: 'disabled', hasUpdate: false },
  { item: { id: 3 }, name: 'broken-skill', state: 'error', hasUpdate: false },
  { item: { id: 4 }, name: 'old-plugin', state: 'enabled', hasUpdate: true },
];

describe('capabilityFilter (D4-1)', () => {
  test('the chip set is the specced five, in order', () => {
    expect(CAPABILITY_FILTERS.map((f) => f.id)).toEqual(['all', 'enabled', 'disabled', 'error', 'updates']);
  });

  test('counts tally every state plus updates, with all = total', () => {
    expect(capabilityFilterCounts(ENTRIES)).toEqual({ all: 4, enabled: 2, disabled: 1, error: 1, updates: 1 });
  });

  test('each chip keeps only its own state; error is never folded away', () => {
    expect(filterCapabilityItems(ENTRIES, '', 'enabled').map((e) => e.name)).toEqual(['read_file', 'old-plugin']);
    expect(filterCapabilityItems(ENTRIES, '', 'disabled').map((e) => e.name)).toEqual(['write_file']);
    expect(filterCapabilityItems(ENTRIES, '', 'error').map((e) => e.name)).toEqual(['broken-skill']);
    expect(filterCapabilityItems(ENTRIES, '', 'updates').map((e) => e.name)).toEqual(['old-plugin']);
    expect(filterCapabilityItems(ENTRIES, '', 'all')).toHaveLength(4);
  });

  test('search narrows by name fragment, case-insensitively, and combines with the chip', () => {
    expect(filterCapabilityItems(ENTRIES, 'FILE', 'all').map((e) => e.name)).toEqual(['read_file', 'write_file']);
    expect(filterCapabilityItems(ENTRIES, 'file', 'disabled').map((e) => e.name)).toEqual(['write_file']);
    expect(filterCapabilityItems(ENTRIES, 'zzz', 'all')).toEqual([]);
  });

  test('with no update data anywhere, the Updates chip honestly matches nothing', () => {
    const noUpdates = ENTRIES.map((e) => ({ ...e, hasUpdate: false }));
    expect(capabilityFilterCounts(noUpdates).updates).toBe(0);
    expect(filterCapabilityItems(noUpdates, '', 'updates')).toEqual([]);
  });
});
