// Wave 6 glass refraction map — the displacement map behind
// #aether-glass-refraction (App.jsx), generated once at module load.
//
// The original filter displaced the backdrop with feTurbulence
// fractal noise: the noise field covers the WHOLE surface, so
// backdrop content was warped everywhere — and at a glass panel's
// edges, where the displaced sampling crosses high-contrast content
// behind the panel (text especially), the edge fringe broke up
// into a glitchy white glow that vanished and reappeared along the
// border (owner report, 13 Nov 2026). Real refraction bends light
// at the rim of the glass and leaves the middle optically flat.
//
// This map encodes exactly that, from the signed distance field of
// a rounded rectangle: displacement is ZERO everywhere further
// than BAND px (map space) from the edge, and ramps smoothly
// (cosine falloff) to full strength at the rim, pointing along the
// SDF gradient. feDisplacementMap reads R as the x deviation and
// G as the y deviation, 0.5 = no shift; the filter's scale turns
// the rim's ±0.5 deviation into the visible bend. Because the map
// stretches to each element's box, the flat interior / bending rim
// proportions hold at every panel size, and text behind the middle
// of a panel is never touched.

export const GLASS_MAP_SIZE = 256;
const GLASS_MAP_RADIUS = 30; // corner radius, map px
// Rim band width, map px (64/256 = the outer 25% of the panel).
// Tuned up from 38 at the owner's word ("more dramatic", 13 Nov
// 2026), together with the filter scale in App.jsx (32 → 52, i.e.
// max rim shift ~16px → ~26px). The interior beyond the band is
// still exactly zero — dramatic at the rim, flat in the middle.
const GLASS_MAP_BAND = 64;

// 1x1 neutral PNG (every channel 0.5-ish grey): a valid feImage
// target that displaces nothing, used if canvas is unavailable
// (tests, exotic webviews) so the filter never goes invalid.
const NEUTRAL_MAP_URL =
  'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8Xw8AAwMCAO+ip1sAAAAASUVORK5CYII=';

function roundedRectSdf(x, y, size, radius) {
  const half = size / 2;
  const qx = Math.abs(x - half) - (half - radius);
  const qy = Math.abs(y - half) - (half - radius);
  const ax = Math.max(qx, 0);
  const ay = Math.max(qy, 0);
  return Math.hypot(ax, ay) + Math.min(Math.max(qx, qy), 0) - radius;
}

// Displacement deviation at a map pixel, in feDisplacementMap
// channel units: [0, 0] = no shift, ±0.5 = full scale shift.
// Exported for tests — the flat interior is the contract.
export function glassDisplacementAt(x, y, size = GLASS_MAP_SIZE) {
  const distInside = -roundedRectSdf(x, y, size, GLASS_MAP_RADIUS);
  if (distInside <= 0 || distInside >= GLASS_MAP_BAND) return [0, 0];
  const t = distInside / GLASS_MAP_BAND;
  const strength = (Math.cos(Math.PI * t) + 1) / 2; // 1 at rim → 0 at band edge
  // SDF gradient (outward normal), central differences.
  const e = 1.5;
  const gx =
    roundedRectSdf(x + e, y, size, GLASS_MAP_RADIUS) -
    roundedRectSdf(x - e, y, size, GLASS_MAP_RADIUS);
  const gy =
    roundedRectSdf(x, y + e, size, GLASS_MAP_RADIUS) -
    roundedRectSdf(x, y - e, size, GLASS_MAP_RADIUS);
  const len = Math.hypot(gx, gy);
  if (len === 0) return [0, 0];
  // Sample shifted INWARD (opposite the outward normal): the rim
  // then reads as the backdrop being pushed outward — the convex
  // thick-edge look — with maximum bend at the rim itself.
  return [(-gx / len) * strength * 0.5, (-gy / len) * strength * 0.5];
}

function buildGlassDisplacementMapUrl() {
  try {
    const size = GLASS_MAP_SIZE;
    const canvas = document.createElement('canvas');
    canvas.width = size;
    canvas.height = size;
    const ctx = canvas.getContext('2d');
    if (!ctx) return NEUTRAL_MAP_URL;
    const image = ctx.createImageData(size, size);
    const data = image.data;
    for (let y = 0; y < size; y += 1) {
      for (let x = 0; x < size; x += 1) {
        const [dx, dy] = glassDisplacementAt(x + 0.5, y + 0.5, size);
        const i = (y * size + x) * 4;
        data[i] = Math.round(255 * (0.5 + dx));
        data[i + 1] = Math.round(255 * (0.5 + dy));
        data[i + 2] = 128;
        data[i + 3] = 255;
      }
    }
    ctx.putImageData(image, 0, 0);
    return canvas.toDataURL('image/png');
  } catch {
    return NEUTRAL_MAP_URL;
  }
}

// Computed once — every glass surface shares the one filter, so
// the map is built a single time per app load (~65k pixels).
export const GLASS_DISPLACEMENT_MAP_URL = buildGlassDisplacementMapUrl();
