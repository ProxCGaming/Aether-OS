import { formatBytes } from '../formatBytes';

// Wave 5 (deslop C2): the shared byte formatter carries the
// MediaLibrary semantics. The load-bearing pin is the ≥ 1 TB
// case: WorkspaceIndexingPanel's deleted copy capped its unit
// list at GB with an unclamped index, so a terabyte-scale file
// rendered "1024 undefined" (the D3-24 bug, fixed in only one of
// the three copies at the time). One implementation ends the
// class — these cases pin every unit boundary plus the clamp.

describe('formatBytes — shared byte-size formatting (C2)', () => {
  it('renders missing and zero sizes as 0 B', () => {
    expect(formatBytes(0)).toBe('0 B');
    expect(formatBytes(null)).toBe('0 B');
    expect(formatBytes(undefined)).toBe('0 B');
  });

  it('formats each unit with one decimal, trimming a trailing .0', () => {
    expect(formatBytes(512)).toBe('512 B');
    expect(formatBytes(1024)).toBe('1 KB');
    expect(formatBytes(1536)).toBe('1.5 KB');
    expect(formatBytes(5 * 1024 * 1024)).toBe('5 MB');
    expect(formatBytes(2.5 * 1024 * 1024 * 1024)).toBe('2.5 GB');
  });

  it('renders terabyte-scale sizes instead of indexing past the unit list', () => {
    expect(formatBytes(1024 ** 4)).toBe('1 TB');
    expect(formatBytes(1024 ** 4 + 512 * 1024 ** 3)).toBe('1.5 TB');
    expect(formatBytes(3 * 1024 ** 5)).toBe('3 PB');
  });

  it('clamps beyond-petabyte sizes to PB rather than overflowing the table', () => {
    expect(formatBytes(1024 ** 6)).toBe('1024 PB');
    expect(formatBytes(5 * 1024 ** 6)).toBe('5120 PB');
  });
});
