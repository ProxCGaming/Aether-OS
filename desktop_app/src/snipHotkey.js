/**
 * Open Point 13 (DECIDED by the owner, 1 Nov 2026) — the snip
 * hotkey is user-changeable in Settings, with default Ctrl+S on
 * Windows/Linux and Cmd+S on macOS.
 *
 * Renderer mirror of electron/snipShortcut.cjs. The main process
 * is authoritative (it validates again at the IPC boundary and
 * owns registration); this side resolves what Settings displays
 * and what App applies at startup, under the same rules: the
 * platform default when there is no preference, the stored
 * preference when there is one, the pre-decision shipped default
 * ('CommandOrControl+Shift+S') treated as unset, and an invalid
 * stored value falling back to the default — flagged, never
 * silent.
 */

export const LEGACY_DEFAULT_SNIP_HOTKEY = 'CommandOrControl+Shift+S';

export function detectSnipPlatform() {
  if (typeof navigator === 'undefined') return 'other';
  const hint = `${navigator.userAgent || ''} ${navigator.platform || ''}`;
  return /Mac|iPhone|iPad|iPod/i.test(hint) ? 'darwin' : 'other';
}

export function defaultSnipHotkey(platform = detectSnipPlatform()) {
  return platform === 'darwin' ? 'Command+S' : 'Control+S';
}

// Mirror of the accelerator rules in electron/ipcValidation.cjs
// (validateAccelerator). Kept deliberately small; the main
// process re-validates and has the final say.
const ACCELERATOR_MODIFIERS = new Set([
  'command', 'cmd', 'control', 'ctrl', 'commandorcontrol', 'cmdorctrl',
  'alt', 'option', 'altgr', 'shift', 'super', 'meta',
]);
const ACCELERATOR_NAMED_KEYS = new Set([
  'space', 'tab', 'enter', 'return', 'escape', 'esc', 'backspace',
  'delete', 'insert', 'home', 'end', 'pageup', 'pagedown',
  'up', 'down', 'left', 'right', 'plus', 'capslock', 'numlock',
  'scrolllock', 'printscreen',
  'volumeup', 'volumedown', 'volumemute',
  'medianexttrack', 'mediaprevioustrack', 'mediastop', 'mediaplaypause',
]);

function isAcceleratorKey(token) {
  const lower = token.toLowerCase();
  if (/^[a-z0-9]$/.test(lower)) return true;
  if (/^f([1-9]|1[0-9]|2[0-4])$/.test(lower)) return true;
  return ACCELERATOR_NAMED_KEYS.has(lower);
}

function isModifierlessKey(token) {
  const lower = token.toLowerCase();
  return /^f([1-9]|1[0-9]|2[0-4])$/.test(lower)
    || lower.startsWith('media') || lower.startsWith('volume');
}

export function isValidAccelerator(value) {
  if (typeof value !== 'string') return false;
  const trimmed = value.trim();
  if (trimmed.length === 0 || trimmed.length > 100) return false;
  const tokens = trimmed.split('+').map((t) => t.trim());
  if (tokens.some((t) => t.length === 0)) return false;
  const key = tokens[tokens.length - 1];
  const modifiers = tokens.slice(0, -1);
  if (!isAcceleratorKey(key) || ACCELERATOR_MODIFIERS.has(key.toLowerCase())) return false;
  if (modifiers.some((m) => !ACCELERATOR_MODIFIERS.has(m.toLowerCase()))) return false;
  if (modifiers.length === 0 && !isModifierlessKey(key)) return false;
  return true;
}

export function resolveSnipHotkey(stored, platform = detectSnipPlatform()) {
  const fallback = defaultSnipHotkey(platform);
  const asDefault = (flags) => ({
    accelerator: fallback,
    source: 'default',
    storedInvalid: false,
    migratedLegacyDefault: false,
    ...flags,
  });
  if (stored === undefined || stored === null || stored === '') return asDefault({});
  if (typeof stored !== 'string') return asDefault({ storedInvalid: true });
  const value = stored.trim();
  if (value === '') return asDefault({});
  if (value === LEGACY_DEFAULT_SNIP_HOTKEY) return asDefault({ migratedLegacyDefault: true });
  if (!isValidAccelerator(value)) return asDefault({ storedInvalid: true });
  return {
    accelerator: value,
    source: 'stored',
    storedInvalid: false,
    migratedLegacyDefault: false,
  };
}
