/**
 * N04 — snip capture geometry: the selection the user drags in the
 * overlay lives in window-client (DIP) coordinates; the screenshot
 * lives in the target display's physical pixels. These pure helpers
 * own the transform so it can be pinned without a real screen.
 */
const { computeCaptureGeometry, selectScreenSource } = require('../snipGeometry.cjs');

const display = (overrides = {}) => ({
  id: 1,
  bounds: { x: 0, y: 0, width: 1920, height: 1080 },
  size: { width: 1920, height: 1080 },
  scaleFactor: 1,
  ...overrides,
});

describe('computeCaptureGeometry', () => {
  it('single display at 100%: client rect passes through, thumbnail is the display size', () => {
    const geo = computeCaptureGeometry({
      rect: { x: 100, y: 100, width: 200, height: 150 },
      windowContentBounds: { x: 0, y: 0, width: 1920, height: 1040 },
      displays: [display()],
    });
    expect(geo).not.toBeNull();
    expect(geo.displayId).toBe(1);
    expect(geo.thumbnailSize).toEqual({ width: 1920, height: 1080 });
    expect(geo.crop).toEqual({ x: 100, y: 100, width: 200, height: 150 });
  });

  it('accounts for the window position inside the display', () => {
    const geo = computeCaptureGeometry({
      rect: { x: 10, y: 20, width: 100, height: 50 },
      windowContentBounds: { x: 400, y: 300, width: 1200, height: 800 },
      displays: [display()],
    });
    expect(geo.crop).toEqual({ x: 410, y: 320, width: 100, height: 50 });
  });

  it('second display with an x offset: captures from that display, in display-local pixels', () => {
    const primary = display({ id: 1 });
    const second = display({ id: 2, bounds: { x: 1920, y: 0, width: 1920, height: 1080 } });
    const geo = computeCaptureGeometry({
      rect: { x: 50, y: 60, width: 300, height: 200 },
      windowContentBounds: { x: 1920, y: 0, width: 1600, height: 1000 },
      displays: [primary, second],
    });
    expect(geo.displayId).toBe(2);
    expect(geo.displayIndex).toBe(1);
    expect(geo.thumbnailSize).toEqual({ width: 1920, height: 1080 });
    expect(geo.crop).toEqual({ x: 50, y: 60, width: 300, height: 200 });
  });

  it('150% scaling: crop and thumbnail are in physical pixels', () => {
    const geo = computeCaptureGeometry({
      rect: { x: 100, y: 100, width: 200, height: 150 },
      windowContentBounds: { x: 0, y: 0, width: 1920, height: 1040 },
      displays: [display({ scaleFactor: 1.5 })],
    });
    expect(geo.thumbnailSize).toEqual({ width: 2880, height: 1620 });
    expect(geo.crop).toEqual({ x: 150, y: 150, width: 300, height: 225 });
  });

  it('mixed DPI: second display at 175% with an offset', () => {
    const primary = display({ id: 1 });
    const second = display({
      id: 2,
      bounds: { x: 1920, y: 0, width: 1920, height: 1080 },
      scaleFactor: 1.75,
    });
    const geo = computeCaptureGeometry({
      rect: { x: 40, y: 50, width: 100, height: 80 },
      windowContentBounds: { x: 2000, y: 100, width: 1200, height: 800 },
      displays: [primary, second],
    });
    expect(geo.displayId).toBe(2);
    expect(geo.thumbnailSize).toEqual({ width: 3360, height: 1890 });
    expect(geo.crop).toEqual({ x: 210, y: 263, width: 175, height: 140 });
  });

  it('clamps a selection that straddles two displays to the chosen display', () => {
    const left = display({ id: 1, bounds: { x: 0, y: 0, width: 1000, height: 800 }, size: { width: 1000, height: 800 } });
    const right = display({ id: 2, bounds: { x: 1000, y: 0, width: 1000, height: 800 }, size: { width: 1000, height: 800 } });
    // Window straddles the boundary; the selection centre lands on the
    // right display, so only the part on that display is captured.
    const geo = computeCaptureGeometry({
      rect: { x: 50, y: 10, width: 100, height: 50 },
      windowContentBounds: { x: 900, y: 0, width: 200, height: 800 },
      displays: [left, right],
    });
    expect(geo.displayId).toBe(2);
    expect(geo.crop).toEqual({ x: 0, y: 10, width: 50, height: 50 });
  });

  it('returns null for unusable input instead of inventing a region', () => {
    const win = { x: 0, y: 0, width: 800, height: 600 };
    expect(computeCaptureGeometry({ rect: { x: 0, y: 0, width: 0, height: 10 }, windowContentBounds: win, displays: [display()] })).toBeNull();
    expect(computeCaptureGeometry({ rect: { x: 0, y: 0, width: 10, height: 10 }, windowContentBounds: win, displays: [] })).toBeNull();
    expect(computeCaptureGeometry({ rect: null, windowContentBounds: win, displays: [display()] })).toBeNull();
    expect(computeCaptureGeometry({ rect: { x: 0, y: 0, width: 10, height: 10 }, windowContentBounds: null, displays: [display()] })).toBeNull();
  });
});

describe('selectScreenSource', () => {
  it('prefers the source whose display_id matches the chosen display', () => {
    const sources = [{ display_id: '2' }, { display_id: '1' }];
    expect(selectScreenSource(sources, { displayId: 1, displayIndex: 0 })).toBe(sources[1]);
  });

  it('falls back to the display index when no display_id matches', () => {
    const sources = [{ display_id: '99' }, { display_id: '98' }];
    expect(selectScreenSource(sources, { displayId: 5, displayIndex: 1 })).toBe(sources[1]);
  });

  it('uses the only source when there is exactly one', () => {
    const sources = [{}];
    expect(selectScreenSource(sources, { displayId: 7, displayIndex: 3 })).toBe(sources[0]);
  });

  it('returns null when nothing can be identified', () => {
    expect(selectScreenSource([], { displayId: 1, displayIndex: 0 })).toBeNull();
    expect(selectScreenSource([{ display_id: '9' }, { display_id: '8' }], { displayId: 1, displayIndex: 4 })).toBeNull();
    expect(selectScreenSource(null, { displayId: 1, displayIndex: 0 })).toBeNull();
  });
});
