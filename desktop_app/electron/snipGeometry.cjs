'use strict';

/**
 * Epic 8, E8-4.1 / finding N04 — snip capture geometry.
 *
 * The overlay selection lives in window-client coordinates: CSS
 * pixels (DIP) relative to the top-left of the window's content
 * area. A screenshot lives in the physical pixels of one display.
 * These pure helpers own the transform between the two spaces —
 * window offset, display choice, display offset, and the display's
 * scale factor — so the maths can be pinned by unit tests instead
 * of being discovered on the owner's multi-monitor Windows desk.
 *
 * Shapes mirror Electron's:
 *   rect                 { x, y, width, height }  (DIP, window-client)
 *   windowContentBounds  { x, y, width, height }  (DIP, screen space;
 *                        BrowserWindow.getContentBounds())
 *   display              { id, bounds: {x,y,width,height},
 *                          size: {width,height}, scaleFactor }
 *                        (bounds/size are DIP; size === bounds size)
 */

function isValidRect(rect) {
  return Boolean(rect)
    && Number.isFinite(rect.x) && Number.isFinite(rect.y)
    && Number.isFinite(rect.width) && Number.isFinite(rect.height)
    && rect.width > 0 && rect.height > 0;
}

function pointInBounds(x, y, bounds) {
  return x >= bounds.x && x < bounds.x + bounds.width
    && y >= bounds.y && y < bounds.y + bounds.height;
}

/**
 * Work out which display to capture and exactly which physical
 * pixels of it. `displays` is ordered with the primary display
 * first — it is the fallback when no display contains the
 * selection. Returns null when the input cannot name a real region
 * (the caller must then fail honestly, never invent an image).
 */
function computeCaptureGeometry({ rect, windowContentBounds, displays } = {}) {
  if (!isValidRect(rect) || !windowContentBounds || !Array.isArray(displays) || displays.length === 0) {
    return null;
  }

  // Window-client DIP -> absolute screen DIP.
  const abs = {
    x: windowContentBounds.x + rect.x,
    y: windowContentBounds.y + rect.y,
    width: rect.width,
    height: rect.height,
  };
  const centreX = abs.x + abs.width / 2;
  const centreY = abs.y + abs.height / 2;

  // The display the selection is on: the one holding its centre,
  // else the one holding the window, else the primary (displays[0]).
  let displayIndex = displays.findIndex((d) => d && d.bounds && pointInBounds(centreX, centreY, d.bounds));
  if (displayIndex === -1) {
    const winCentreX = windowContentBounds.x + windowContentBounds.width / 2;
    const winCentreY = windowContentBounds.y + windowContentBounds.height / 2;
    displayIndex = displays.findIndex((d) => d && d.bounds && pointInBounds(winCentreX, winCentreY, d.bounds));
  }
  if (displayIndex === -1) displayIndex = 0;
  const display = displays[displayIndex];
  if (!display || !display.bounds) return null;

  const scaleFactor = Number.isFinite(display.scaleFactor) && display.scaleFactor > 0
    ? display.scaleFactor
    : 1;
  const dipWidth = (display.size && display.size.width) || display.bounds.width;
  const dipHeight = (display.size && display.size.height) || display.bounds.height;
  const thumbnailSize = {
    width: Math.round(dipWidth * scaleFactor),
    height: Math.round(dipHeight * scaleFactor),
  };
  if (thumbnailSize.width <= 0 || thumbnailSize.height <= 0) return null;

  // Display-local DIP -> physical pixels, clamped to the display:
  // a window straddling two displays captures only the part of the
  // selection that lies on the chosen display.
  const rawX = Math.round((abs.x - display.bounds.x) * scaleFactor);
  const rawY = Math.round((abs.y - display.bounds.y) * scaleFactor);
  const rawRight = Math.round((abs.x + abs.width - display.bounds.x) * scaleFactor);
  const rawBottom = Math.round((abs.y + abs.height - display.bounds.y) * scaleFactor);
  const crop = {
    x: Math.max(0, rawX),
    y: Math.max(0, rawY),
    width: Math.min(thumbnailSize.width, rawRight) - Math.max(0, rawX),
    height: Math.min(thumbnailSize.height, rawBottom) - Math.max(0, rawY),
  };
  if (crop.width <= 0 || crop.height <= 0) return null;

  return {
    displayId: display.id,
    displayIndex,
    thumbnailSize,
    crop,
  };
}

/**
 * Pick the desktopCapturer source for a computed geometry: the
 * source whose display_id matches wins; the display's index among
 * all displays is the next best signal; a lone source is used as
 * is. Returns null when nothing can be identified — capturing the
 * wrong display silently would be worse than failing.
 */
function selectScreenSource(sources, geometry) {
  if (!Array.isArray(sources) || sources.length === 0 || !geometry) return null;
  const byId = sources.find(
    (s) => s && s.display_id != null && String(s.display_id) === String(geometry.displayId),
  );
  if (byId) return byId;
  if (Number.isInteger(geometry.displayIndex) && sources[geometry.displayIndex]) {
    return sources[geometry.displayIndex];
  }
  if (sources.length === 1) return sources[0];
  return null;
}

module.exports = { computeCaptureGeometry, selectScreenSource };
