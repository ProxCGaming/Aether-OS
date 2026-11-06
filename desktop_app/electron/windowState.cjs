/**
 * Window geometry persistence (Epic 6, E6-01).
 *
 * Pure store logic with injected fs/path so it is unit-testable
 * outside Electron. Bounds are validated on load: anything that is
 * not a finite, sensibly-sized rectangle falls back to the defaults
 * — a corrupt state file must never produce a zero-size window.
 * VISIBILITY (a saved position whose monitor is gone) is a separate
 * check that needs the live display list: ensureVisibleOnDisplays
 * below, applied by main.cjs at window creation (D4-02).
 */

const DEFAULT_BOUNDS = { width: 1280, height: 800 };
const MIN_WIDTH = 480;
const MIN_HEIGHT = 360;
const MAX_DIMENSION = 10000;

function isFiniteNumber(v) {
  return typeof v === 'number' && Number.isFinite(v);
}

function validateBounds(raw) {
  if (!raw || typeof raw !== 'object') return null;
  const { width, height, x, y } = raw;
  if (!isFiniteNumber(width) || !isFiniteNumber(height)) return null;
  if (width < MIN_WIDTH || height < MIN_HEIGHT) return null;
  if (width > MAX_DIMENSION || height > MAX_DIMENSION) return null;
  const out = { width: Math.round(width), height: Math.round(height) };
  // Position is optional (first run centres via the OS default);
  // a bogus position is dropped, never trusted.
  if (isFiniteNumber(x) && isFiniteNumber(y) && Math.abs(x) < MAX_DIMENSION && Math.abs(y) < MAX_DIMENSION) {
    out.x = Math.round(x);
    out.y = Math.round(y);
  }
  return out;
}

// How much of the window must intersect a display's work area for
// the saved position to count as visible: at least this much of the
// title-bar region (width × height) on screen, or the user cannot
// see or grab the window.
const MIN_VISIBLE_WIDTH = 100;
const MIN_VISIBLE_HEIGHT = 40;

/**
 * D4-02 — validateBounds guarantees sane NUMBERS; this guarantees a
 * VISIBLE window. Bounds saved on a second monitor stay "valid"
 * after that monitor is disconnected, and the window then opens
 * entirely off-screen (tray Show / second-instance focus call
 * show() on the same invisible window, so nothing recovers it).
 *
 * `displays` is a list of displays or bare work areas — each entry
 * may be an Electron Display ({workArea: {x, y, width, height}}) or
 * the work-area rectangle itself; both shapes are normalised here.
 * When the
 * saved rectangle meaningfully intersects at least one work area,
 * the bounds pass through unchanged. When it intersects none —
 * including the sliver case, a few pixels of an edge — the position
 * is dropped and the size kept, so the OS centres a sensibly-sized
 * window on the primary display. With no position or no display
 * information, the bounds pass through untouched (never invent a
 * worse outcome from missing data).
 */
function ensureVisibleOnDisplays(bounds, displays) {
  if (!bounds || bounds.x === undefined || bounds.y === undefined) return bounds;
  if (!Array.isArray(displays) || displays.length === 0) return bounds;
  const visible = displays.some((display) => {
    const area = display && display.workArea ? display.workArea : display;
    if (!area) return false;
    const overlapWidth = Math.min(bounds.x + bounds.width, area.x + area.width)
      - Math.max(bounds.x, area.x);
    const overlapHeight = Math.min(bounds.y + bounds.height, area.y + area.height)
      - Math.max(bounds.y, area.y);
    return overlapWidth >= MIN_VISIBLE_WIDTH && overlapHeight >= MIN_VISIBLE_HEIGHT;
  });
  if (visible) return bounds;
  const rest = { ...bounds };
  delete rest.x;
  delete rest.y;
  return rest;
}

function createWindowStateStore({ filePath, fs }) {
  return {
    load() {
      try {
        const parsed = JSON.parse(fs.readFileSync(filePath, 'utf8'));
        return validateBounds(parsed) || { ...DEFAULT_BOUNDS };
      } catch {
        return { ...DEFAULT_BOUNDS };
      }
    },
    save(bounds) {
      const valid = validateBounds(bounds);
      if (!valid) return false;
      try {
        // D4-08: temp file + rename, the repo's standard for state
        // files (the token writer, the engine config saves). A
        // crash or power loss mid-write leaves the PREVIOUS state
        // intact instead of a truncated file.
        const tmpPath = `${filePath}.tmp-${process.pid}`;
        fs.writeFileSync(tmpPath, JSON.stringify(valid, null, 2), 'utf8');
        fs.renameSync(tmpPath, filePath);
        return true;
      } catch {
        return false;
      }
    },
  };
}

module.exports = {
  createWindowStateStore,
  validateBounds,
  ensureVisibleOnDisplays,
  DEFAULT_BOUNDS,
};
