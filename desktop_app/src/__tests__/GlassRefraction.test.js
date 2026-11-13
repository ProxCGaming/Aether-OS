/**
 * Glass refraction pins (13 Nov 2026, owner report): the original
 * #aether-glass-refraction filter displaced the backdrop with
 * feTurbulence fractal noise — the whole surface warped, and at
 * panel edges, wherever text sat behind the glass, the bright
 * fringe broke into a glitchy white glow. The replacement map
 * (glassRefraction.js) is built from a rounded-rect SDF: zero
 * displacement in the interior, a smooth bend only at the rim.
 * These tests pin that contract so the noise cannot creep back.
 */
import fs from 'fs';
import path from 'path';

import {
  GLASS_DISPLACEMENT_MAP_URL,
  GLASS_MAP_SIZE,
  glassDisplacementAt,
} from '../glassRefraction';

describe('glass displacement map math', () => {
  const c = GLASS_MAP_SIZE / 2;

  it('is exactly zero at the centre and across the deep interior', () => {
    expect(glassDisplacementAt(c, c)).toEqual([0, 0]);
    expect(glassDisplacementAt(c - 40, c + 30)).toEqual([0, 0]);
    expect(glassDisplacementAt(80, 80)).toEqual([0, 0]);
  });

  it('bends at the rim, pointing inward (sample shift)', () => {
    const [dxLeft] = glassDisplacementAt(0.5, c);
    expect(dxLeft).toBeGreaterThan(0.4); // near full ±0.5 at the rim
    const [, dyTop] = glassDisplacementAt(c, 0.5);
    expect(dyTop).toBeGreaterThan(0.4);
    const [dxRight] = glassDisplacementAt(GLASS_MAP_SIZE - 0.5, c);
    expect(dxRight).toBeLessThan(-0.4);
  });

  it('never exceeds the ±0.5 channel range feDisplacementMap expects', () => {
    for (let y = 0; y < GLASS_MAP_SIZE; y += 7) {
      for (let x = 0; x < GLASS_MAP_SIZE; x += 7) {
        const [dx, dy] = glassDisplacementAt(x + 0.5, y + 0.5);
        expect(Math.abs(dx)).toBeLessThanOrEqual(0.5);
        expect(Math.abs(dy)).toBeLessThanOrEqual(0.5);
      }
    }
  });

  it('exposes a usable map URL (generated, or the neutral fallback)', () => {
    expect(GLASS_DISPLACEMENT_MAP_URL.startsWith('data:image/png')).toBe(true);
  });
});

describe('App glass filter wiring', () => {
  const appSource = fs.readFileSync(path.join(__dirname, '..', 'App.jsx'), 'utf8');

  it('uses the SDF displacement map, not turbulence noise', () => {
    expect(appSource).toContain('<feImage');
    expect(appSource).toContain('GLASS_DISPLACEMENT_MAP_URL');
    expect(appSource).toContain('<feDisplacementMap');
    // The element must be gone (the word may live on in the comment
    // explaining why it was removed).
    expect(appSource).not.toContain('<feTurbulence');
  });
});
