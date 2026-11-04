/**
 * S3-M3 — Electron IPC sender + argument validation.
 *
 * Before this fix, every ipcMain handler in main.cjs trusted the
 * renderer completely: event.sender was only used as a window
 * lookup (BrowserWindow.fromWebContents), never validated, and
 * renderer arguments flowed verbatim into privileged actions
 * (engine WebSocket relay, dialog.showOpenDialog,
 * globalShortcut.register, shell.openPath). Any frame that could
 * reach the preload bridge — a navigated-away main frame, a
 * compromised subframe — could drive all of it.
 *
 * The fix lives in electron/ipcValidation.cjs (pure helpers, like
 * snipGeometry.cjs / notify.cjs) wired into main.cjs through ONE
 * shared guard. main.cjs cannot be required in Jest (single-
 * instance lock, windows, socket on load — see shortcutsQuit.test
 * .js), so the wiring is pinned by source-contract tests at the
 * bottom, the same pattern as preload-no-token.test.js.
 */
const fs = require('fs');
const path = require('path');
const { pathToFileURL } = require('url');

const {
  DEV_ORIGIN,
  getSenderUrl,
  isTrustedSenderUrl,
  isTrustedSender,
  validateEngineMessage,
  validateOpenPathArg,
  validateAccelerator,
  validateShowOpenDialogOptions,
  validateSessionId,
} = require('../ipcValidation.cjs');

// The app's own pages, exactly as main.cjs loads them:
//   main window     dev  http://localhost:5173
//                   prod loadFile(dist/index.html)
//   floating window dev  http://localhost:5173?floating=true&session=…
//                   prod loadFile(dist/index.html, {query:{floating,session}})
const APP_INDEX = path.join(__dirname, '..', '..', 'dist', 'index.html');
const APP_FILE_URL = pathToFileURL(APP_INDEX).href;
const PROD = { isDev: false, appIndexPath: APP_INDEX };
const DEV = { isDev: true, appIndexPath: APP_INDEX };

const prodEvent = (url) => ({ senderFrame: { url }, sender: { getURL: () => url } });
const FOREIGN_URLS = [
  'https://evil.example.com/phish',
  'http://localhost:9999/attacker',
  'data:text/html,<script>alert(1)</script>',
  'about:blank',
  '',
];

describe('S3-M3 sender validation: production (file://) build', () => {
  it('accepts the app page the main window loads', () => {
    expect(isTrustedSenderUrl(APP_FILE_URL, PROD)).toBe(true);
    expect(isTrustedSender(prodEvent(APP_FILE_URL), PROD)).toBe(true);
  });

  it('accepts the floating window: same file, query string attached', () => {
    const floating = `${APP_FILE_URL}?floating=true&session=abc123`;
    expect(isTrustedSenderUrl(floating, PROD)).toBe(true);
    expect(isTrustedSender(prodEvent(floating), PROD)).toBe(true);
  });

  it('refuses foreign URLs', () => {
    for (const url of FOREIGN_URLS) {
      expect(isTrustedSenderUrl(url, PROD)).toBe(false);
      expect(isTrustedSender(prodEvent(url), PROD)).toBe(false);
    }
  });

  it('refuses other local files, including lookalikes of the app page', () => {
    expect(isTrustedSenderUrl(pathToFileURL('/tmp/evil.html').href, PROD)).toBe(false);
    expect(isTrustedSenderUrl(pathToFileURL(path.join(__dirname, '..', '..', 'dist', 'other.html')).href, PROD)).toBe(false);
    expect(isTrustedSenderUrl(`${APP_FILE_URL}.evil`, PROD)).toBe(false);
    expect(isTrustedSenderUrl(pathToFileURL(path.join(__dirname, '..', '..', 'index.html')).href, PROD)).toBe(false);
  });

  it('refuses the dev-server URL in a production build', () => {
    expect(isTrustedSenderUrl('http://localhost:5173/', PROD)).toBe(false);
    expect(isTrustedSenderUrl('http://localhost:5173', PROD)).toBe(false);
  });

  it('refuses a sender with no frame and no URL', () => {
    expect(isTrustedSender({}, PROD)).toBe(false);
    expect(isTrustedSender({ senderFrame: null, sender: null }, PROD)).toBe(false);
    expect(isTrustedSender(null, PROD)).toBe(false);
    expect(isTrustedSender(undefined, PROD)).toBe(false);
  });
});

describe('S3-M3 sender validation: dev (vite) build', () => {
  it('accepts the dev origin the windows load', () => {
    expect(DEV_ORIGIN).toBe('http://localhost:5173');
    expect(isTrustedSenderUrl('http://localhost:5173', DEV)).toBe(true);
    expect(isTrustedSenderUrl('http://localhost:5173/', DEV)).toBe(true);
    expect(isTrustedSenderUrl('http://localhost:5173/index.html', DEV)).toBe(true);
  });

  it('accepts the floating window dev URL (query string attached)', () => {
    expect(isTrustedSenderUrl('http://localhost:5173?floating=true&session=abc123', DEV)).toBe(true);
    expect(isTrustedSender(prodEvent('http://localhost:5173?floating=true&session=abc123'), DEV)).toBe(true);
  });

  it('refuses lookalike origins: other ports, 127.0.0.1, https, suffix hosts', () => {
    expect(isTrustedSenderUrl('http://localhost:5174/', DEV)).toBe(false);
    expect(isTrustedSenderUrl('http://127.0.0.1:5173/', DEV)).toBe(false);
    expect(isTrustedSenderUrl('https://localhost:5173/', DEV)).toBe(false);
    expect(isTrustedSenderUrl('http://localhost:5173.evil.example.com/', DEV)).toBe(false);
    expect(isTrustedSenderUrl('http://localhost.evil.example.com:5173/', DEV)).toBe(false);
  });

  it('refuses the production file URL and foreign URLs in dev', () => {
    expect(isTrustedSenderUrl(APP_FILE_URL, DEV)).toBe(false);
    for (const url of FOREIGN_URLS) {
      expect(isTrustedSenderUrl(url, DEV)).toBe(false);
    }
  });
});

describe('S3-M3 getSenderUrl extraction', () => {
  it('prefers senderFrame.url (the frame that actually invoked)', () => {
    const ev = {
      senderFrame: { url: 'https://evil.example.com/' },
      sender: { getURL: () => APP_FILE_URL },
    };
    // The top-level sender URL must not launder a foreign subframe.
    expect(getSenderUrl(ev)).toBe('https://evil.example.com/');
    expect(isTrustedSender(ev, PROD)).toBe(false);
  });

  it('falls back to sender.getURL() when there is no senderFrame', () => {
    expect(getSenderUrl({ sender: { getURL: () => APP_FILE_URL } })).toBe(APP_FILE_URL);
  });

  it('falls back to sender.url, then to empty string', () => {
    expect(getSenderUrl({ sender: { url: APP_FILE_URL } })).toBe(APP_FILE_URL);
    expect(getSenderUrl({ sender: {} })).toBe('');
    expect(getSenderUrl({})).toBe('');
  });
});

describe('S3-M3 validateEngineMessage (engine-send relay)', () => {
  it('accepts the JSON strings the renderer actually sends', () => {
    const wire = JSON.stringify({ type: 'TASK_SUBMIT_REQUEST', schema_version: 1, payload: { prompt: 'hi' } });
    expect(validateEngineMessage(wire)).toEqual({ ok: true, value: wire });
    expect(validateEngineMessage('raw-packet').ok).toBe(true);
  });

  it('accepts a serialisable object and normalises it to the wire string', () => {
    const msg = { type: 'PING', n: 1 };
    expect(validateEngineMessage(msg)).toEqual({ ok: true, value: JSON.stringify(msg) });
    expect(validateEngineMessage([1, 2]).ok).toBe(true);
  });

  it('refuses non-message shapes', () => {
    for (const bad of [undefined, null, 42, true, '', () => {}]) {
      expect(validateEngineMessage(bad).ok).toBe(false);
    }
  });

  it('refuses circular (unserialisable) objects', () => {
    const circular = { a: 1 };
    circular.self = circular;
    expect(validateEngineMessage(circular).ok).toBe(false);
  });
});

describe('S3-M3 validateOpenPathArg (open-path)', () => {
  it('accepts real paths unchanged', () => {
    expect(validateOpenPathArg('/home/user/report.pdf')).toEqual({ ok: true, value: '/home/user/report.pdf' });
    expect(validateOpenPathArg('C:\\Users\\arindam\\file.txt').ok).toBe(true);
  });

  it('refuses non-string, empty and whitespace-only paths', () => {
    for (const bad of [undefined, null, 42, {}, [], '', '   ']) {
      expect(validateOpenPathArg(bad).ok).toBe(false);
    }
  });

  it('refuses paths containing a NUL byte', () => {
    expect(validateOpenPathArg('/tmp/a\0b').ok).toBe(false);
  });
});

describe('S3-M3 validateAccelerator (register-snip-shortcut)', () => {
  it('accepts the accelerators the app and Settings legitimately use', () => {
    for (const good of [
      'CommandOrControl+Shift+S',
      'CommandOrControl+S',
      'Ctrl+S',
      'Cmd+S',
      'Alt+Space',
      'Ctrl+Shift+P',
      'CommandOrControl+Alt+K',
    ]) {
      expect(validateAccelerator(good).ok).toBe(true);
    }
  });

  it('refuses garbage accelerators', () => {
    for (const bad of [
      undefined, null, 42, {}, '', '   ',
      '!!!', 'Ctrl+', '+S', 'Ctrl++S', 'Shift', 'Ctrl+Shift',
      'Ctrl+FooBarBaz', 'not a shortcut', 'Ctrl + ; rm -rf /',
      'javascript:alert(1)',
    ]) {
      expect(validateAccelerator(bad).ok).toBe(false);
    }
  });
});

describe('S3-M3 validateShowOpenDialogOptions (show-open-dialog)', () => {
  it('accepts "no options" (the handler default applies)', () => {
    expect(validateShowOpenDialogOptions(undefined).ok).toBe(true);
    expect(validateShowOpenDialogOptions(null).ok).toBe(true);
  });

  it('accepts exactly what the renderer call sites send', () => {
    // Projects.jsx
    expect(validateShowOpenDialogOptions({
      properties: ['openDirectory'],
      title: 'Select Workspace Folder',
    })).toEqual({
      ok: true,
      value: { properties: ['openDirectory'], title: 'Select Workspace Folder' },
    });
    // Onboarding.jsx
    expect(validateShowOpenDialogOptions({
      properties: ['openDirectory', 'createDirectory'],
      title: 'Choose your workspace folder',
    }).ok).toBe(true);
    // An empty object is Electron's own default dialog — harmless.
    expect(validateShowOpenDialogOptions({}).ok).toBe(true);
  });

  it('accepts well-formed filters / defaultPath / buttonLabel', () => {
    expect(validateShowOpenDialogOptions({
      properties: ['openFile'],
      filters: [{ name: 'Images', extensions: ['png', 'jpg'] }],
      defaultPath: '/home/user',
      buttonLabel: 'Pick',
    }).ok).toBe(true);
  });

  it('refuses malformed options', () => {
    for (const bad of [
      'openDirectory',
      42,
      ['openDirectory'],
      { properties: 'openDirectory' },              // not an array
      { properties: [] },                            // empty selection mode
      { properties: ['openDirectory', 'execute'] },  // unknown property
      { properties: [42] },
      { title: 42 },
      { title: 'x', evilKey: true },                 // unknown key
      { filters: [{ name: 'x' }] },                  // missing extensions
      { filters: [{ name: 'x', extensions: 'png' }] },
      { filters: 'png' },
      { defaultPath: {} },
    ]) {
      expect(validateShowOpenDialogOptions(bad).ok).toBe(false);
    }
  });
});

describe('S3-M3 validateSessionId (open-floating-chat)', () => {
  it('accepts engine session ids and "no session"', () => {
    expect(validateSessionId('a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4').ok).toBe(true);
    expect(validateSessionId('sess-1_2.3').ok).toBe(true);
    expect(validateSessionId(null)).toEqual({ ok: true, value: '' });
    expect(validateSessionId(undefined)).toEqual({ ok: true, value: '' });
    expect(validateSessionId('')).toEqual({ ok: true, value: '' });
  });

  it('refuses ids that could break out of the dev URL query', () => {
    for (const bad of ['a&admin=true', 'a#frag', 'a/b', 'a b', 'a?x=1', 42, {}, 'x'.repeat(129)]) {
      expect(validateSessionId(bad).ok).toBe(false);
    }
  });
});

// ------------------------------------------------------------------
// Source contract: main.cjs wiring (pattern: shortcutsQuit.test.js)
// ------------------------------------------------------------------
const mainSource = fs.readFileSync(path.join(__dirname, '..', '..', 'main.cjs'), 'utf8');

const EXPECTED_CHANNELS = [
  'close-window', 'minimize-window', 'select-directory', 'maximize-window',
  'open-logs-folder', 'get-ws-status', 'engine-send', 'open-floating-chat',
  'close-floating-chat', 'get-memory-usage', 'show-open-dialog',
  'trigger-snip', 'get-snip-shortcut', 'register-snip-shortcut',
  'capture-screen', 'open-path',
  // S3-M7 channels (merged in after this suite was written) — they
  // sit behind the same sender guard, like every other channel.
  'set-floating-transfer-state', 'get-floating-transfer-state',
  'clear-floating-transfer-state',
  // Wave 1 slice 4 startup-gate channels — same sender guard; the
  // 'engine-state' broadcast is a webContents.send, not a handler,
  // so it is not a registered channel here.
  'get-engine-state', 'retry-engine-start', 'open-engine-log',
];

function registrationSlices(source) {
  const re = /ipcMain\.(?:handle|on)\('([^']+)'/g;
  const found = [];
  let m;
  while ((m = re.exec(source)) !== null) {
    found.push({ channel: m[1], start: m.index });
  }
  return found.map((entry, i) => ({
    channel: entry.channel,
    body: source.slice(entry.start, i + 1 < found.length ? found[i + 1].start : source.length),
  }));
}

describe('S3-M3 main.cjs wiring contract', () => {
  it('main.cjs loads the shared ipcValidation helper', () => {
    expect(mainSource).toContain("require('./electron/ipcValidation.cjs')");
    expect(mainSource).toContain('isTrustedSender');
  });

  it('registers exactly the expected IPC channels', () => {
    const channels = registrationSlices(mainSource).map((s) => s.channel);
    expect(channels.slice().sort()).toEqual(EXPECTED_CHANNELS.slice().sort());
  });

  it('every registered handler passes through the shared sender guard', () => {
    for (const { channel, body } of registrationSlices(mainSource)) {
      expect({ channel, guarded: body.includes('isTrustedIpcSender(event') })
        .toEqual({ channel, guarded: true });
    }
  });

  it('the guard is defined once and logs refusals', () => {
    expect(mainSource.match(/function isTrustedIpcSender/g)).toHaveLength(1);
    expect(mainSource).toContain('untrusted sender');
  });

  it('the privileged handlers validate their arguments at the boundary', () => {
    const byChannel = Object.fromEntries(registrationSlices(mainSource).map((s) => [s.channel, s.body]));
    expect(byChannel['engine-send']).toContain('validateEngineMessage');
    expect(byChannel['open-path']).toContain('validateOpenPathArg');
    expect(byChannel['register-snip-shortcut']).toContain('validateAccelerator');
    expect(byChannel['show-open-dialog']).toContain('validateShowOpenDialogOptions');
    expect(byChannel['open-floating-chat']).toContain('validateSessionId');
  });

  it('capture-screen keeps its existing geometry validation', () => {
    const byChannel = Object.fromEntries(registrationSlices(mainSource).map((s) => [s.channel, s.body]));
    expect(byChannel['capture-screen']).toContain('computeCaptureGeometry');
  });
});
