'use strict';

/**
 * S3-M3 — IPC trust boundary for the Electron main process.
 *
 * Every ipcMain handler in main.cjs is a privileged action waiting
 * for a renderer to ask: relaying bytes onto the engine WebSocket,
 * opening native dialogs, registering global shortcuts, opening
 * arbitrary paths in the OS shell, capturing the screen. Before
 * this module, event.sender was only ever used as a window lookup
 * (BrowserWindow.fromWebContents) — never validated — and renderer
 * arguments flowed into those actions verbatim. Any frame that
 * could reach the preload bridge (a main frame navigated to
 * attacker content, a compromised subframe) inherited all of it.
 *
 * Sender rule: the app creates exactly two windows, and both load
 * exactly one page —
 *   dev:  http://localhost:5173            (vite; floating window
 *         adds ?floating=true&session=…)
 *   prod: dist/index.html via loadFile     (floating window adds
 *         the same query through loadFile's query option)
 * A sender is trusted only if the URL of the frame that invoked
 * (event.senderFrame, falling back to the sender WebContents)
 * names that page: in dev, the exact dev origin at '/' or
 * '/index.html'; in prod, a file:// URL whose path is exactly the
 * app index file. Query/hash never affect the decision, so the
 * floating window passes; every other origin, port, host and file
 * is refused. The <webview> preview guests (M01) load arbitrary
 * URLs and hold no preload bridge, and would fail this check even
 * if they somehow reached it.
 *
 * Argument validators are per-handler and deliberately no stricter
 * than the real renderer protocol (call sites noted per function):
 * they refuse shapes no legitimate caller sends, and pass
 * legitimate values through unchanged (or, for showOpenDialog,
 * as a sanitised copy holding only whitelisted keys).
 *
 * Pure logic with no Electron import, so it is unit-testable in
 * Jest like snipGeometry.cjs / notify.cjs / windowState.cjs.
 * Validators return { ok: true, value } | { ok: false, error }.
 */

const path = require('path');
const { fileURLToPath } = require('url');

const DEV_ORIGIN = 'http://localhost:5173';

// ------------------------------------------------------------------
// Sender validation
// ------------------------------------------------------------------

function getSenderUrl(event) {
  if (!event || typeof event !== 'object') return '';
  const frame = event.senderFrame;
  if (frame && typeof frame.url === 'string' && frame.url) return frame.url;
  const sender = event.sender;
  if (sender) {
    if (typeof sender.getURL === 'function') {
      try {
        const url = sender.getURL();
        if (typeof url === 'string' && url) return url;
      } catch { /* fall through to the property */ }
    }
    if (typeof sender.url === 'string' && sender.url) return sender.url;
  }
  return '';
}

function isTrustedSenderUrl(urlString, { isDev = false, appIndexPath = '', devOrigin = DEV_ORIGIN } = {}) {
  if (typeof urlString !== 'string' || !urlString) return false;
  let parsed;
  try { parsed = new URL(urlString); } catch { return false; }

  if (isDev) {
    let dev;
    try { dev = new URL(devOrigin); } catch { return false; }
    if (parsed.protocol !== dev.protocol) return false;
    if (parsed.hostname !== dev.hostname) return false;
    if (parsed.port !== dev.port) return false;
    // The vite server serves the app shell at the root only; the
    // floating window is the same page plus a query string.
    return parsed.pathname === '' || parsed.pathname === '/' || parsed.pathname === '/index.html';
  }

  if (parsed.protocol !== 'file:') return false;
  if (!appIndexPath) return false;
  let senderPath;
  try { senderPath = fileURLToPath(parsed); } catch { return false; }
  return path.normalize(senderPath) === path.normalize(appIndexPath);
}

function isTrustedSender(event, options) {
  return isTrustedSenderUrl(getSenderUrl(event), options);
}

// ------------------------------------------------------------------
// Argument validation
// ------------------------------------------------------------------

/**
 * engine-send: the relay accepts a string (sent as-is) or any
 * JSON-serialisable value (stringified by main before sending).
 * The renderer (SocketContext) in practice sends JSON strings.
 * Refused: no message at all, primitives the relay would have
 * mangled, and unserialisable (circular) objects.
 */
function validateEngineMessage(msg) {
  if (typeof msg === 'string') {
    if (msg.length === 0) return { ok: false, error: 'Engine message must not be empty.' };
    return { ok: true, value: msg };
  }
  if (msg && typeof msg === 'object') {
    try {
      const wire = JSON.stringify(msg);
      if (typeof wire === 'string' && wire.length > 0) return { ok: true, value: wire };
    } catch { /* circular — refused below */ }
  }
  return { ok: false, error: 'Engine message must be a string or a JSON-serialisable object.' };
}

/**
 * open-path: a single non-empty path string. Keeps the handler's
 * existing error wording for the missing-path case.
 */
function validateOpenPathArg(filePath) {
  if (typeof filePath !== 'string' || filePath.trim().length === 0) {
    return { ok: false, error: 'No file path was provided.' };
  }
  if (filePath.includes('\0')) return { ok: false, error: 'Invalid file path.' };
  return { ok: true, value: filePath };
}

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
  // Keys Electron accepts as a whole accelerator on their own:
  // function keys and media/volume keys. A bare letter or a lone
  // modifier is never a legitimate global shortcut.
  const lower = token.toLowerCase();
  return /^f([1-9]|1[0-9]|2[0-4])$/.test(lower)
    || lower.startsWith('media') || lower.startsWith('volume');
}

/**
 * register-snip-shortcut: a plausible Electron accelerator —
 * '+'-joined modifiers ending in one real key, e.g. the Settings
 * value 'CommandOrControl+Shift+S'. The renderer sends the
 * trimmed contents of a free-text input, so anything that is not
 * shaped like an accelerator is refused instead of being handed
 * to globalShortcut.register.
 */
function validateAccelerator(shortcut) {
  if (typeof shortcut !== 'string') return { ok: false, error: 'Shortcut must be a string.' };
  const value = shortcut.trim();
  if (value.length === 0 || value.length > 100) {
    return { ok: false, error: 'Shortcut must be a non-empty accelerator string.' };
  }
  const tokens = value.split('+').map((t) => t.trim());
  if (tokens.some((t) => t.length === 0)) {
    return { ok: false, error: 'Shortcut has an empty segment.' };
  }
  const key = tokens[tokens.length - 1];
  const modifiers = tokens.slice(0, -1);
  if (!isAcceleratorKey(key) || ACCELERATOR_MODIFIERS.has(key.toLowerCase())) {
    return { ok: false, error: 'Shortcut must end in a real key.' };
  }
  if (modifiers.some((m) => !ACCELERATOR_MODIFIERS.has(m.toLowerCase()))) {
    return { ok: false, error: 'Shortcut contains an unknown modifier.' };
  }
  if (modifiers.length === 0 && !isModifierlessKey(key)) {
    return { ok: false, error: 'Shortcut needs at least one modifier.' };
  }
  return { ok: true, value };
}

const DIALOG_OPTION_KEYS = new Set(['title', 'defaultPath', 'buttonLabel', 'filters', 'message', 'properties']);
const DIALOG_PROPERTIES = new Set([
  'openFile', 'openDirectory', 'multiSelections', 'showHiddenFiles',
  'createDirectory', 'treatPackageAsDirectory', 'dontAddToRecent',
  'showOverwriteConfirmation', 'promptToCreate',
]);

function isPlainObject(value) {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function validateDialogFilters(filters) {
  if (!Array.isArray(filters)) return false;
  return filters.every((f) => isPlainObject(f)
    && Object.keys(f).every((k) => k === 'name' || k === 'extensions')
    && typeof f.name === 'string' && f.name.length > 0
    && Array.isArray(f.extensions) && f.extensions.length > 0
    && f.extensions.every((e) => typeof e === 'string' && /^[A-Za-z0-9*]+$/.test(e)));
}

/**
 * show-open-dialog: the renderer legitimately sends only
 * { title, properties } (Projects.jsx, Onboarding.jsx) or nothing
 * (the handler default). The whitelist additionally admits the
 * other plain dialog fields (defaultPath, buttonLabel, filters,
 * message) in their correct types; unknown keys, mistyped values
 * and unknown `properties` entries are refused. The returned
 * value is a sanitised copy — only whitelisted keys survive.
 */
function validateShowOpenDialogOptions(options) {
  if (options === undefined || options === null) return { ok: true, value: undefined };
  if (!isPlainObject(options)) return { ok: false, error: 'Dialog options must be an object.' };
  for (const key of Object.keys(options)) {
    if (!DIALOG_OPTION_KEYS.has(key)) return { ok: false, error: `Unknown dialog option: ${key}` };
  }
  const clean = {};
  for (const key of ['title', 'defaultPath', 'buttonLabel', 'message']) {
    if (options[key] !== undefined) {
      if (typeof options[key] !== 'string') return { ok: false, error: `Dialog option ${key} must be a string.` };
      clean[key] = options[key];
    }
  }
  if (options.properties !== undefined) {
    if (!Array.isArray(options.properties) || options.properties.length === 0
        || !options.properties.every((p) => typeof p === 'string' && DIALOG_PROPERTIES.has(p))) {
      return { ok: false, error: 'Dialog option properties must be a non-empty array of known dialog properties.' };
    }
    clean.properties = [...options.properties];
  }
  if (options.filters !== undefined) {
    if (!validateDialogFilters(options.filters)) {
      return { ok: false, error: 'Dialog option filters must be { name, extensions[] } entries.' };
    }
    clean.filters = options.filters.map((f) => ({ name: f.name, extensions: [...f.extensions] }));
  }
  return { ok: true, value: clean };
}

/**
 * open-floating-chat: the session id is interpolated into the
 * floating window's dev URL, so it must not be able to break out
 * of the query string. Engine session ids are UUID-hex style
 * strings; null/undefined/'' legitimately mean "no session".
 */
function validateSessionId(sessionId) {
  if (sessionId === undefined || sessionId === null || sessionId === '') {
    return { ok: true, value: '' };
  }
  if (typeof sessionId !== 'string' || sessionId.length > 128
      || !/^[A-Za-z0-9._-]+$/.test(sessionId)) {
    return { ok: false, error: 'Session id must be a short id string.' };
  }
  return { ok: true, value: sessionId };
}

module.exports = {
  DEV_ORIGIN,
  getSenderUrl,
  isTrustedSenderUrl,
  isTrustedSender,
  validateEngineMessage,
  validateOpenPathArg,
  validateAccelerator,
  validateShowOpenDialogOptions,
  validateSessionId,
};
