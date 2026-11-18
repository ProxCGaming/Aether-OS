import fs from 'fs';
import path from 'path';

// Full Furnish Wave 1 · S-2 — the token scales approved in
// Penpot Batch 1 land in index.css with identical values.
// CSS has no runtime test surface in this repo (see
// Wave3CssDedup.test.js), so the definitions are pinned by
// direct reads: every Batch 1 value must appear exactly once,
// and the two colours corrected in Penpot (FF1-13) must hold
// their code values — muted #94948F, danger #C92A2A.

const indexCss = fs.readFileSync(path.join(__dirname, '..', 'index.css'), 'utf8');

const varValue = (name) => {
  const m = indexCss.match(new RegExp(`--${name}:\\s*([^;]+);`));
  return m ? m[1].trim() : null;
};
const varCount = (name) => (indexCss.match(new RegExp(`--${name}:`, 'g')) || []).length;

describe('S-2 — type scale (Batch 1: size/weight pairs)', () => {
  it.each([
    ['text-display', "600 28px var(--font-sans)"],
    ['text-title', "500 15px var(--font-sans)"],
    ['text-body', "400 13px var(--font-sans)"],
    ['text-caption', "400 11px var(--font-sans)"],
    ['text-mono', "400 12px var(--font-mono)"],
  ])('--%s is defined once with the Batch 1 value', (name, value) => {
    expect(varCount(name)).toBe(1);
    expect(varValue(name)).toBe(value);
  });
});

describe('S-2 — elevation scale', () => {
  it.each([
    ['elevation-resting', 'none'],
    ['elevation-raised', '0 2px 8px rgba(0, 0, 0, 0.35)'],
    ['elevation-floating', '0 12px 32px rgba(0, 0, 0, 0.5)'],
  ])('--%s is defined once with the Batch 1 value', (name, value) => {
    expect(varCount(name)).toBe(1);
    expect(varValue(name)).toBe(value);
  });
});

describe('S-2 — z-order scale (palette above modal, by design)', () => {
  it.each([
    ['z-sidebar', '10'],
    ['z-panel', '40'],
    ['z-overlay', '200'],
    ['z-modal', '1000'],
    ['z-palette', '1100'],
    ['z-top', '10000'],
  ])('--%s is defined once with the Batch 1 value', (name, value) => {
    expect(varCount(name)).toBe(1);
    expect(varValue(name)).toBe(value);
  });

  it('palette sits above modal', () => {
    expect(Number(varValue('z-palette'))).toBeGreaterThan(Number(varValue('z-modal')));
  });
});

describe('S-2 — motion easing tokenised (FF1-15)', () => {
  it('--motion-easing holds the shell cubic-bezier, defined once', () => {
    expect(varCount('motion-easing')).toBe(1);
    expect(varValue('motion-easing')).toBe('cubic-bezier(0.23, 1, 0.32, 1)');
  });

  it('the pre-existing motion durations are untouched', () => {
    expect(varValue('motion-fast')).toBe('120ms');
    expect(varValue('motion-base')).toBe('180ms');
  });
});

describe('FF1-13 — the corrected colours hold their values', () => {
  it.each([
    ['text-muted', '#94948F'],
    ['danger', '#C92A2A'],
  ])('--%s is %s', (name, value) => {
    expect(varValue(name)).toBe(value);
  });

  it('the stale Penpot values never reappear', () => {
    expect(indexCss).not.toContain('#6E6E6A');
    expect(indexCss).not.toContain('#E5484D');
  });
});
