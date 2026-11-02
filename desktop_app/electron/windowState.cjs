/**
 * Window geometry persistence (Epic 6, E6-01).
 *
 * Pure store logic with injected fs/path so it is unit-testable
 * outside Electron. Bounds are validated on load: anything that is
 * not a finite, sensibly-sized rectangle falls back to the defaults
 * — a corrupt state file must never produce an off-screen or
 * zero-size window.
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
        fs.writeFileSync(filePath, JSON.stringify(valid, null, 2), 'utf8');
        return true;
      } catch {
        return false;
      }
    },
  };
}

module.exports = { createWindowStateStore, validateBounds, DEFAULT_BOUNDS };
